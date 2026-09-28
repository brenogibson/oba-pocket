"""Obas pelo ar: os comandos do tools/oba.py que falam com a placa (MQTT) e com o registro (S3).

  python tools/oba.py install obas/bit [--activate] [--no-registry]
  python tools/oba.py remove bit [--registry]
  python tools/oba.py activate bit
  python tools/oba.py play yay [--volume 128]
  python tools/oba.py list [--registry]
  python tools/oba.py publish obas/bit

Todos aceitam --device <thing> (padrão: o device do config.json) e --config.
O install manda o Oba em pedaços pelo cmd da placa (oba.install e oba.chunk em
docs/protocol.md), espera o aceite na tela da placa e só então sobe o Oba para o
registro S3, que o roteador usa. O publish só sobe para o registro.

Usa o perfil padrão da AWS CLI (ou AWS_PROFILE), boto3 e paho-mqtt 2.x. O MQTT vai
por WebSocket (443) com uma URL assinada (SigV4). A policy IAM mínima está em
docs/protocol.md.
"""
import base64
import datetime
import hashlib
import hmac
import json
import queue
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent

CHUNK = 8192             # INSTALL_CHUNK_MAX em src/install.h
WINDOW = 4               # pedaços em voo
ACK_S = 5                # sem a placa andar por isso: reenvia a partir do último "next"
STALL_S = 30             # sem a placa andar por isso: desiste (ela também desiste com 30 s)
REPLY_S = 10
DONE_S = 90              # conferência, aceite na tela e troca
ASK_S = 60               # a placa espera o toque em "Instalar" por isso
PLAY_S = 3
CONNECT_S = 15
CMD_MAX = 14_000         # o buffer MQTT da placa é de 16 KB
ID_RE = re.compile(r"[a-z0-9_-]{1,31}")
CONTENT_TYPES = {".json": "application/json", ".png": "image/png", ".wav": "audio/wav",
                 ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8"}

_hidden: list[tuple[str, str]] = []   # textos que nunca aparecem na saída (conta, bucket)


# ------------------------------------------------------------------ utilidades

def hide(text) -> str:
    """Tira da mensagem o nome do bucket, o ID da conta e as credenciais de uma URL."""
    text = str(text)
    for secret, label in _hidden:
        text = text.replace(secret, label)
    return re.sub(r"(X-Amz-[A-Za-z-]+=)[^&\s'\"]+", r"\1…", text)


def fail(msg, code: int = 1):
    print(f"erro: {hide(msg)}", file=sys.stderr)
    sys.exit(code)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def minified_sha(spec: dict) -> str:
    """O sha do JSON minificado, que é o que a placa manda no state para o Oba embutido."""
    return sha256(json.dumps(spec, separators=(",", ":"), ensure_ascii=False).encode())


def content_type(path: str) -> str:
    return CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def now_ms() -> int:
    return int(time.time() * 1000)


def config(a) -> dict:
    path = Path(a.config) if getattr(a, "config", None) else ROOT / "config.json"
    if not path.is_file():
        fail(f"{path.name} não existe: cp config.example.json config.json e ajuste")
    cfg = json.loads(path.read_text())
    dev = getattr(a, "device", None) or cfg.get("device", "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", cfg.get("prefix", "")):
        fail("prefix do config.json fora do formato (veja o setup.py)")
    if not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", dev):
        fail("device: letras, números, _, : e - (é o nome do thing no IoT)")
    return {"prefix": cfg["prefix"], "device": dev, "region": cfg.get("region", "us-east-1")}


def oba_files(folder: Path) -> tuple[dict, list[tuple[str, bytes]]]:
    """O JSON e os arquivos do Oba: os de "files" na ordem do JSON e o oba.json por último."""
    import oba
    folder, path, spec = oba.load(folder)
    files = [(rel, (folder / rel).read_bytes()) for rel in spec.get("files") or {}]
    files.append(("oba.json", path.read_bytes()))
    return spec, files


def checked(target: str) -> tuple[Path, dict, list[tuple[str, bytes]]]:
    """Confere o Oba com o tools/oba.py validate; com problema, mostra e sai."""
    import oba
    folder = Path(target)
    if folder.name == "oba.json":
        folder = folder.parent
    if not (folder / "oba.json").is_file():
        fail(f"{folder}: não achei o oba.json")
    errs = oba.validate(folder)
    if errs:
        print(f"{folder}: {len(errs)} problema(s)", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    spec, files = oba_files(folder)
    return folder, spec, files


# ------------------------------------------------------------------ AWS

class Aws:
    """Sessão da AWS do config.json. O nome do bucket tem o ID da conta: nunca aparece na saída."""

    def __init__(self, cfg: dict):
        import boto3
        self.cfg = cfg
        self.session = boto3.Session(region_name=cfg["region"])
        self._account = self._endpoint = None

    @property
    def account(self) -> str:
        if not self._account:
            self._account = self.session.client("sts").get_caller_identity()["Account"]
            _hidden.append((f"{self.cfg['prefix']}-{self._account}", "<registro S3>"))
            _hidden.append((self._account, "<conta>"))
        return self._account

    @property
    def bucket(self) -> str:
        return f"{self.cfg['prefix']}-{self.account}"

    @property
    def endpoint(self) -> str:
        if not self._endpoint:
            iot = self.session.client("iot")
            self._endpoint = iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
        return self._endpoint

    def s3(self):
        return self.session.client("s3")

    def iot_data(self):
        return self.session.client("iot-data", endpoint_url=f"https://{self.endpoint}")

    def credentials(self):
        creds = self.session.get_credentials()
        if creds is None:
            fail("sem credenciais da AWS: configure o perfil da AWS CLI (ou AWS_PROFILE)")
        return creds.get_frozen_credentials()


def sign_url(host: str, region: str, access_key: str, secret_key: str, token: str | None = None,
             now: datetime.datetime | None = None) -> str:
    """URL wss:// assinada com SigV4 (serviço iotdevicegateway). O token da sessão vai
    depois da assinatura, fora dela, como o IoT Core pede."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz[:8]
    scope = f"{date}/{region}/iotdevicegateway/aws4_request"
    qs = ("X-Amz-Algorithm=AWS4-HMAC-SHA256"
          f"&X-Amz-Credential={quote(access_key + '/' + scope, safe='')}"
          f"&X-Amz-Date={amz}&X-Amz-SignedHeaders=host")
    canonical = f"GET\n/mqtt\n{qs}\nhost:{host}\n\nhost\n{sha256(b'')}"
    to_sign = f"AWS4-HMAC-SHA256\n{amz}\n{scope}\n{sha256(canonical.encode())}"
    key = ("AWS4" + secret_key).encode()
    for part in (date, region, "iotdevicegateway", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    url = f"wss://{host}/mqtt?{qs}&X-Amz-Signature={sig}"
    if token:
        url += "&X-Amz-Security-Token=" + quote(token, safe="")
    return url


class Link:
    """MQTT com a placa: assina <p>/<dev>/reply (e os canais extras) e publica em <p>/<dev>/cmd."""

    def __init__(self, aws: Aws, channels=("reply",)):
        import paho.mqtt.client as mqtt
        from botocore.httpsession import get_cert_path
        cfg = aws.cfg
        self.base = f"{cfg['prefix']}/{cfg['device']}/"
        self.topics = [self.base + ch for ch in channels]
        self.inbox: queue.Queue = queue.Queue()
        self.ready = threading.Event()   # assinou (ou o IoT Core recusou: error)
        self.error = None
        c = aws.credentials()
        url = sign_url(aws.endpoint, cfg["region"], c.access_key, c.secret_key, c.token)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=new_id("oba-cli-"),
                                  transport="websockets", protocol=mqtt.MQTTv311)
        self.client.ws_set_options(path=url[url.index("/mqtt"):])
        self.client.tls_set(ca_certs=get_cert_path(True))
        self.client.on_connect = self._connected
        self.client.on_subscribe = self._subscribed
        self.client.on_message = self._message
        try:
            self.client.connect(aws.endpoint, 443, keepalive=30)
        except mqtt.WebsocketConnectionError:
            fail("o IoT Core recusou a conexão (credenciais vencidas ou policy IAM? veja docs/protocol.md)")
        self.client.loop_start()
        if not self.ready.wait(CONNECT_S) or self.error:
            self.close()
            fail(self.error or "o MQTT não conectou (confira a policy IAM em docs/protocol.md)")

    def _connected(self, client, userdata, flags, reason, props):
        if reason.is_failure:
            self.error = f"o IoT Core recusou a conexão: {reason}"
            self.ready.set()
            return
        client.subscribe([(t, 1) for t in self.topics])  # de novo a cada reconexão

    def _subscribed(self, client, userdata, mid, reasons, props):
        bad = [r for r in reasons if r.is_failure]
        if bad:
            self.error = f"o IoT Core recusou a assinatura (policy IAM?): {bad[0]}"
        self.ready.set()

    def _message(self, client, userdata, msg):
        try:
            m = json.loads(msg.payload)
        except ValueError:
            return
        if isinstance(m, dict):
            m["_ch"] = msg.topic[len(self.base):]
            self.inbox.put(m)

    def send(self, msg: dict, wait: bool = False):
        body = json.dumps({"v": 1, "ts": now_ms(), **msg}, separators=(",", ":"), ensure_ascii=False)
        info = self.client.publish(self.base + "cmd", body, qos=1)
        if wait:
            try:
                info.wait_for_publish(REPLY_S)
            except (RuntimeError, ValueError):
                fail("o MQTT caiu antes de mandar o comando")

    def get(self, timeout: float) -> dict | None:
        try:
            return self.inbox.get(timeout=max(timeout, 0))
        except queue.Empty:
            return None

    def reply(self, cid: str, timeout: float) -> dict | None:
        """A resposta (reply) do comando com esse id, ou None se não chegou a tempo."""
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            m = self.get(left)
            if m and m.get("_ch") == "reply" and m.get("id") == cid:
                return m
        return None

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ------------------------------------------------------------------ instalação em pedaços

def install_header(cid: str, target: str, files: list[tuple[str, bytes]], activate: bool) -> dict:
    return {"type": "oba.install", "id": cid, "target": target, "activate": bool(activate),
            "files": [{"path": p, "size": len(d), "sha256": sha256(d)} for p, d in files]}


def chunk_msg(cid: str, path: str, off: int, data: bytes) -> dict:
    return {"type": "oba.chunk", "id": cid, "path": path, "off": off, "data": base64.b64encode(data).decode()}


class Upload:
    """Os pedaços de uma instalação. Deixa até WINDOW pedaços em voo e segue o "next" dos acks
    da placa. Todo pedaço gera um ack (aceito ou não): um ack parado no "next" de antes (um
    pedaço se perdeu), todos respondidos com a placa ainda atrás, ou a placa sem andar por
    ACK_S: recomeça do "next" dela."""

    def __init__(self, cid: str, files: list[tuple[str, bytes]], now: float, chunk: int = CHUNK,
                 window: int = WINDOW):
        self.cid, self.files, self.chunk, self.window = cid, files, chunk, window
        self.start, self.total = {}, 0
        for p, d in files:
            self.start[p] = self.total
            self.total += len(d)
        self.acked = self.sent = self.flight = self.resends = 0
        self.rewound = -1     # de onde foi o último recomeço por ack parado
        self.moved = now      # última vez que a placa andou ou que recomeçamos (relógio do reenvio)
        self.progress = now   # última vez que a placa andou

    def resume(self, nxt, now: float):
        """Começa do "next" da resposta ao cabeçalho."""
        p = self.pos(nxt)
        if p is not None:
            self.acked = self.sent = p
            self.moved = self.progress = now

    def pos(self, nxt) -> int | None:
        """"next" da placa como posição no total (None = todos os arquivos chegaram)."""
        if nxt is None:
            return self.total
        if not isinstance(nxt, dict) or nxt.get("path") not in self.start:
            return None
        off = nxt.get("off")
        if not isinstance(off, int) or isinstance(off, bool) or not 0 <= off <= len(self.data(nxt["path"])):
            return None
        return self.start[nxt["path"]] + off

    def data(self, path: str) -> bytes:
        return next(d for p, d in self.files if p == path)

    @property
    def done(self) -> bool:
        return self.acked >= self.total

    def ack(self, nxt, now: float):
        p = self.pos(nxt)
        if p is None:
            return
        self.flight = max(0, self.flight - 1)
        if p > self.acked:
            self.acked, self.moved, self.progress = p, now, now
            self.sent = max(self.sent, p)
        elif p == self.acked < self.sent and p != self.rewound:
            # A placa não andou com um pedaço que veio depois do dela: esse se perdeu.
            # Volta uma vez só deste ponto; os outros acks do mesmo lote dizem o mesmo
            self.sent, self.rewound, self.moved = p, p, now
        elif self.flight == 0 and self.sent > self.acked:
            self.sent = self.acked  # tudo respondido e a placa parada: o que passou dela se perdeu

    def cut(self, at: int) -> tuple[str, int, bytes]:
        for p, d in self.files:
            s = self.start[p]
            if s <= at < s + len(d):
                off = at - s
                return p, off, d[off:off + self.chunk]
        raise ValueError(at)

    def pump(self, now: float) -> list[dict]:
        """Os pedaços para mandar agora."""
        if self.done:
            return []
        if now - self.moved > ACK_S:
            self.sent, self.flight, self.moved = self.acked, 0, now
            self.resends += 1
        out = []
        while self.flight < self.window and self.sent < self.total:
            path, off, data = self.cut(self.sent)
            out.append(chunk_msg(self.cid, path, off, data))
            self.sent += len(data)
            self.flight += 1
        return out

    def stalled(self, now: float) -> bool:
        return not self.done and now - self.progress > STALL_S


class Progress:
    def __init__(self, label: str, total: int):
        self.label, self.total, self.step = label, max(total, 1), -1
        self.tty = sys.stdout.isatty()

    def show(self, done: int):
        pct = min(100, done * 100 // self.total)
        step = pct if self.tty else pct // 25
        if step == self.step:
            return
        self.step = step
        text = f"  {self.label}: {pct}% ({done // 1024} de {self.total // 1024} KB)"
        print("\r" + text if self.tty else text, end="" if self.tty else "\n", flush=True)

    def end(self):
        if self.tty and self.step >= 0:
            print()


def send_install(link: Link, spec: dict, files: list[tuple[str, bytes]], activate: bool) -> dict:
    """Manda o Oba para a placa. Devolve o reply final (stage "done"); recusa ou erro: sai."""
    cid, name = new_id("i"), spec.get("name") or spec["id"]
    header = install_header(cid, spec["id"], files, activate)
    if len(json.dumps(header)) > CMD_MAX:
        fail("a lista de arquivos não cabe num comando (caminhos muito longos?)")
    link.send(header)
    r = link.reply(cid, REPLY_S)
    if r is None:
        fail("a placa não respondeu ao oba.install (ela está ligada, conectada e com o firmware 0.3.0?)")
    if not r.get("ok"):
        fail(f"a placa recusou: {r.get('error') or 'sem motivo'}")
    now = time.monotonic()
    up, bar = Upload(cid, files, now), Progress(f"enviando {name}", sum(len(d) for _, d in files))
    up.resume(r.get("next"), now)
    deadline, confirm = None, False
    while True:
        now = time.monotonic()
        for msg in up.pump(now):
            link.send(msg)
        bar.show(up.acked)
        if up.stalled(now):
            bar.end()
            fail("a placa parou de responder aos pedaços")
        if up.done and deadline is None:
            deadline = now + DONE_S
        if deadline is not None and now > deadline:
            bar.end()
            fail("a placa não terminou a instalação a tempo")
        m = link.get(0.2)
        if not m or m.get("_ch") != "reply" or m.get("id") != cid:
            continue
        if m.get("re") == "oba.chunk":
            if m.get("ok"):
                up.ack(m.get("next"), now)
            elif not up.done:  # depois do último byte, pedaço atrasado não é erro
                bar.end()
                fail(f"a placa largou a instalação: {m.get('error') or 'sem motivo'}")
        elif m.get("re") == "oba.install":
            if not m.get("ok"):
                bar.end()
                fail(f"a instalação parou na placa: {m.get('error') or 'sem motivo'}")
            if m.get("stage") in ("confirm", "done"):
                up.acked = up.sent = up.total
            if m.get("stage") == "confirm" and not confirm:
                confirm = True
                bar.show(up.acked)
                bar.end()
                print(f"  confirme na placa: toque em \"Instalar\" (até {ASK_S} s)", flush=True)
            if m.get("stage") == "done":
                bar.end()
                return m


# ------------------------------------------------------------------ registro (S3)

def registry_sync(s3, bucket: str, folder: Path, spec: dict, owner: str | None = None,
                  prune: bool = True) -> dict:
    """Sobe o Oba para o registro: obas/<id>/<caminho> de cada arquivo de "files" e o oba.json
    por último (o roteador nunca vê um JSON antes dos arquivos dele). Pula o que não mudou
    (ETag = md5 do objeto) e, com prune, apaga os arquivos daquele Oba que saíram de "files"."""
    prefix, kw = f"obas/{spec['id']}/", ({"ExpectedBucketOwner": owner} if owner else {})
    etags = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, **kw):
        for o in page.get("Contents", []):
            etags[o["Key"][len(prefix):]] = o["ETag"].strip('"')
    files = [(rel, (folder / rel).read_bytes()) for rel in spec.get("files") or {}]
    files.append(("oba.json", (folder / "oba.json").read_bytes()))
    out = {"sent": [], "kept": [], "deleted": [], "new": "oba.json" not in etags}
    for rel, data in files:
        if etags.get(rel) == hashlib.md5(data, usedforsecurity=False).hexdigest():
            out["kept"].append(rel)
            continue
        s3.put_object(Bucket=bucket, Key=prefix + rel, Body=data, ContentType=content_type(rel), **kw)
        out["sent"].append(rel)
    if prune:
        out["deleted"] = sorted(set(etags) - {rel for rel, _ in files})
        for i in range(0, len(out["deleted"]), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": prefix + k} for k in out["deleted"][i:i + 1000]],
                                                     "Quiet": True}, **kw)
    return out


def registry_remove(s3, bucket: str, oba_id: str, owner: str | None = None) -> int:
    prefix, kw, n = f"obas/{oba_id}/", ({"ExpectedBucketOwner": owner} if owner else {}), 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, **kw):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True}, **kw)
            n += len(keys)
    return n


def registry_list(s3, bucket: str, owner: str | None = None) -> list[dict]:
    """Os Obas do registro: id, nome, versão e os dois sha que valem no state (arquivo e minificado)."""
    kw, out = ({"ExpectedBucketOwner": owner} if owner else {}), []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="obas/", Delimiter="/", **kw):
        for cp in page.get("CommonPrefixes", []):
            oid = cp["Prefix"][len("obas/"):-1]
            try:
                raw = s3.get_object(Bucket=bucket, Key=f"obas/{oid}/oba.json", **kw)["Body"].read()
                spec = json.loads(raw)
            except Exception:  # sem oba.json (sobra) ou JSON quebrado
                out.append({"id": oid, "name": None, "version": None, "shas": set()})
                continue
            out.append({"id": oid, "name": spec.get("name"), "version": spec.get("version"),
                        "shas": {sha256(raw), minified_sha(spec)}})
    return out


def show_sync(r: dict):
    parts = [f"{len(r['sent'])} enviado(s)", f"{len(r['kept'])} sem mudança"]
    if r["deleted"]:
        parts.append(f"{len(r['deleted'])} apagado(s)")
    print(f"  registro S3: {', '.join(parts)}")


# ------------------------------------------------------------------ comandos

def cmd_install(a):
    folder, spec, files = checked(a.folder)
    aws = Aws(config(a))
    total = sum(len(d) for _, d in files)
    print(f"{spec.get('name')} ({spec['id']} {spec.get('version')}): {len(files)} arquivo(s), "
          f"{(total + 1023) // 1024} KB para a placa {aws.cfg['device']}")
    with Link(aws) as link:
        r = send_install(link, spec, files, a.activate)
        print(f"  instalado na placa{' e ativo' if r.get('active') else ''}")
        if a.no_registry:
            print("  registro S3 não mexido (--no-registry): o roteador só acorda o agente desse Oba "
                  "quando o registro bater com a placa (publish)")
            return
        show_sync(registry_sync(aws.s3(), aws.bucket, folder, spec, owner=aws.account))
        # A placa publicou o state antes do registro mudar: pede outro para o roteador conferir de novo
        link.send({"type": "state"}, wait=True)


def simple(a, msg: dict, done: str):
    """Manda um comando com id, espera o reply e mostra o resultado."""
    aws = Aws(config(a))
    with Link(aws) as link:
        cid = new_id("c")
        link.send({**msg, "id": cid}, wait=True)
        r = link.reply(cid, REPLY_S)
    if r is None:
        fail("a placa não respondeu (ela está ligada e conectada?)")
    if not r.get("ok"):
        fail(f"a placa recusou: {r.get('error') or 'sem motivo'}")
    print(done)
    return aws


def cmd_remove(a):
    aws = simple(a, {"type": "oba.remove", "target": a.id}, f"{a.id}: apagado do cartão")
    if a.registry:
        n = registry_remove(aws.s3(), aws.bucket, a.id, owner=aws.account)
        print(f"  registro S3: {n} arquivo(s) apagado(s)")


def cmd_activate(a):
    simple(a, {"type": "oba.activate", "target": a.id}, f"{a.id}: ativo")


def cmd_play(a):
    msg = {"type": "play", "id": new_id("p"), "sound": a.sound}
    if a.volume is not None:
        msg["volume"] = a.volume
    aws = Aws(config(a))
    with Link(aws) as link:
        link.send(msg, wait=True)
        r = link.reply(msg["id"], PLAY_S)  # aceito não responde
    if r is not None and not r.get("ok"):
        fail(f"a placa recusou: {r.get('error') or 'sem motivo'}")
    print(f"{a.sound}: mandado")


def retained_state(aws: Aws) -> dict | None:
    iot = aws.iot_data()
    try:
        r = iot.get_retained_message(topic=f"{aws.cfg['prefix']}/{aws.cfg['device']}/state")
    except iot.exceptions.ResourceNotFoundException:
        return None
    try:
        state = json.loads(r["payload"])
    except ValueError:
        return None
    if isinstance(state, dict):
        state["_age_s"] = max(0, (now_ms() - int(r.get("lastModifiedTime") or now_ms())) // 1000)
        return state
    return None


def age(s: int) -> str:
    return f"{s} s" if s < 90 else f"{s // 60} min" if s < 5400 else f"{s // 3600} h"


def cmd_list(a):
    aws = Aws(config(a))
    state = retained_state(aws)
    dev = aws.cfg["device"]
    active = {}
    if state is None:
        print(f"placa {dev}: ainda não publicou o state")
    elif state.get("online") is False and not state.get("obas"):
        print(f"placa {dev}: offline (state de {age(state['_age_s'])} atrás)")
    else:
        active = state.get("active") if isinstance(state.get("active"), dict) else {}
        bat = state.get("battery") if isinstance(state.get("battery"), dict) else {}
        extra = [f"firmware {state.get('fw') or '?'}", "REC ligado" if state.get("rec") else "REC desligado"]
        if bat.get("pct") is not None:
            extra.append(f"bateria {bat['pct']}%{' carregando' if bat.get('charging') else ''}")
        print(f"placa {dev}: {', '.join(extra)} (state de {age(state['_age_s'])} atrás)")
        for o in state.get("obas") or []:
            if not isinstance(o, dict):
                continue
            mark = "*" if o.get("id") == active.get("id") else " "
            print(f"  {mark} {str(o.get('id')):<16} {str(o.get('version') or ''):<10} {o.get('name') or ''}"
                  f"{' (embutido)' if o.get('builtin') else ''}")
    if not a.registry:
        return
    print("registro S3:")
    items = registry_list(aws.s3(), aws.bucket, owner=aws.account)
    if not items:
        print("  vazio")
    for it in items:
        note = ""
        if it["id"] == active.get("id") and isinstance(active.get("sha256"), str):
            same = active.get("version") == it["version"] and active["sha256"].lower() in it["shas"]
            note = "  (bate com a placa)" if same else "  (NÃO bate com o Oba ativo da placa)"
        print(f"    {it['id']:<16} {str(it['version'] or '?'):<10} {it['name'] or '(sem oba.json)'}{note}")


def cmd_publish(a):
    folder, spec, _ = checked(a.folder)
    aws = Aws(config(a))
    print(f"{spec.get('name')} ({spec['id']} {spec.get('version')}) para o registro S3")
    show_sync(registry_sync(aws.s3(), aws.bucket, folder, spec, owner=aws.account))


def oba_id(v: str) -> str:
    if not ID_RE.fullmatch(v):
        raise ValueError(v)
    return v


def volume(v: str) -> int:
    n = int(v)
    if not 1 <= n <= 255:
        raise ValueError(v)
    return n


def guarded(fn):
    """Erros da AWS e da rede viram uma linha legível, sem a conta nem o bucket."""
    def run(a):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            fn(a)
        except KeyboardInterrupt:
            fail("interrompido", 130)
        except (ClientError, BotoCoreError, OSError) as e:
            fail(e)
    return run


def register(sub):
    """Acrescenta os comandos ao argparse do tools/oba.py."""
    import argparse
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--device", help="thing da placa (padrão: device do config.json)")
    common.add_argument("--config", help="config.json (padrão: o da raiz)")
    p = sub.add_parser("install", parents=[common], help="instala um Oba na placa pelo ar e sobe para o registro")
    p.add_argument("folder")
    p.add_argument("--activate", action="store_true", help="ativa depois de instalar")
    p.add_argument("--no-registry", action="store_true", help="não mexe no registro S3")
    p.set_defaults(fn=guarded(cmd_install))
    p = sub.add_parser("remove", parents=[common], help="apaga um Oba do cartão da placa")
    p.add_argument("id", type=oba_id)
    p.add_argument("--registry", action="store_true", help="apaga também do registro S3")
    p.set_defaults(fn=guarded(cmd_remove))
    p = sub.add_parser("activate", parents=[common], help="ativa um Oba instalado na placa")
    p.add_argument("id", type=oba_id)
    p.set_defaults(fn=guarded(cmd_activate))
    p = sub.add_parser("play", parents=[common], help="toca um som do Oba ativo (só com o REC desligado)")
    p.add_argument("sound", type=oba_id)
    p.add_argument("--volume", type=volume, help="1 a 255 (padrão da placa: 128)")
    p.set_defaults(fn=guarded(cmd_play))
    p = sub.add_parser("list", parents=[common], help="mostra os Obas da placa (do state retido)")
    p.add_argument("--registry", action="store_true", help="mostra também os do registro S3")
    p.set_defaults(fn=guarded(cmd_list))
    p = sub.add_parser("publish", parents=[common], help="sobe um Oba só para o registro S3")
    p.add_argument("folder")
    p.set_defaults(fn=guarded(cmd_publish))
