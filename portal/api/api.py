"""API do portal: Lambda com Function URL (AWS_IAM) que só a distribuição do CloudFront
chama, pelo OAC.

Toda rota exige o access token do user pool do portal no header x-oba-token: o OAC
usa o Authorization para assinar o pedido à Lambda. Não há cookie, então não há CSRF.

Os Obas enviados em zip passam pela mesma validação da CLI (tools/oba.py validate) antes
de ir para o registro; a instalação na placa fica com o instalador (installer.py).

O histórico lê e apaga, na tabela do roteador, só os resumos (hist#<placa>) e as sessões
(s#<placa>#<sessão>) desta placa; a role da Lambda só deixa essas pk (setup.py).
"""

import base64
import io
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import time
import urllib.request
import uuid
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import jwt
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import oba  # noqa: E402  (tools/oba.py: validação)
import oba_remote  # noqa: E402  (tools/oba_remote.py: registro S3)

REGION = os.environ["AWS_REGION"]
POOL = os.environ["USER_POOL"]
CLIENT = os.environ["CLIENT_ID"]
IDENTITY_POOL = os.environ["IDENTITY_POOL"]
VIEWER_POLICY = os.environ["VIEWER_POLICY"]
DEVICE = os.environ["DEVICE"]
CMD_TOPIC = f"{os.environ['PREFIX']}/{DEVICE}/cmd"
SUMMARY_TOPIC = f"{os.environ['PREFIX']}/{DEVICE}/ui/summary"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
BUCKET = os.environ["BUCKET"]          # registro dos Obas
INSTALLER = os.environ["INSTALLER"]
BODY_MAX = 64 * 1024
ZIP_MAX = 4 * 1024 * 1024              # em base64 no evento, tem que caber nos 6 MB da Lambda
ZIP_ENTRIES_MAX = 256
LIST_MAX = 60                          # Obas por resposta (cada um leva a folha idle)
SHEET_MAX = 48 * 1024                  # como no ui/oba (harness/router/router.py)
COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS") or 30)   # o roteador guarda os resumos por isso
HIST_LIST_MAX = 200
# id de um resumo: <ts>_<sessão> (sk <ts>#<sessão> do roteador; "-" = fora do REC)
HIST_ID = r"[0-9]{13}_(?:[0-9]{8}-[0-9]{6}-[0-9a-f]{6}|-)"   # [0-9]: o \d aceita dígito Unicode
WORK_S = 20                            # depois disso, o apagar devolve "more" e o portal pede de novo

KEYS = {k["kid"]: jwt.PyJWK(k) for k in json.loads(os.environ.get("JWKS") or '{"keys": []}')["keys"]}
keys_fetched = 0.0

# GetId é público: quem valida o ID token é o próprio Cognito, e o identity ID nunca vem do cliente
identity = boto3.client("cognito-identity", config=Config(signature_version=UNSIGNED))
iot = boto3.client("iot")
iot_data = boto3.client("iot-data", endpoint_url=f"https://{os.environ['IOT_ENDPOINT']}")
s3 = boto3.client("s3")
lam = boto3.client("lambda")
dynamo = boto3.resource("dynamodb")
# Sem TABLE (o código novo sobe antes do env novo no setup.py), só o histórico fica fora
table = dynamo.Table(os.environ["TABLE"]) if os.environ.get("TABLE") else None
NAME_RE = re.compile(r"[a-z0-9_-]{1,31}")  # id de Oba e nome de som (schema/oba.schema.json)
ACCOUNT = None                             # dono do bucket de registro, do ARN da própria Lambda
STARTED = 0.0                              # começo do pedido (time.monotonic)


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.extra = status, extra


def signing_key(token: str):
    global keys_fetched
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        raise HttpError(401, "token inválido")
    if kid not in KEYS and time.time() - keys_fetched > 300:  # chave nova do Cognito (raro)
        keys_fetched = time.time()
        with urllib.request.urlopen(f"{ISSUER}/.well-known/jwks.json", timeout=5) as r:
            KEYS.update({k["kid"]: jwt.PyJWK(k) for k in json.load(r)["keys"]})
    if kid not in KEYS:
        raise HttpError(401, "token inválido")
    return KEYS[kid]


def verify(token: str, use: str) -> dict:
    """Access token: client_id é o do portal. ID token: aud é o do portal."""
    try:
        claims = jwt.decode(token, signing_key(token), algorithms=["RS256"], issuer=ISSUER,
                            audience=CLIENT if use == "id" else None, leeway=30,
                            options={"require": ["exp", "iat", "sub", "token_use"], "verify_aud": use == "id"})
    except jwt.PyJWTError:
        raise HttpError(401, "token inválido ou vencido")
    if claims["token_use"] != use or (use == "access" and claims.get("client_id") != CLIENT):
        raise HttpError(401, "token de outro app")
    return claims


def body_json(event: dict) -> dict:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raise HttpError(415, "mande JSON")
    if len(raw) > BODY_MAX:
        raise HttpError(413, "pedido grande demais")
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raise HttpError(400, "JSON inválido")
    if not isinstance(data, dict):
        raise HttpError(400, "JSON inválido")
    return data


# ---------- rotas ----------

def me(user: dict, event: dict) -> dict:
    return {"user": user.get("username")}


def iot_access(user: dict, event: dict) -> dict:
    """Anexa a policy de leitura do IoT à identidade do usuário (sem efeito se já estiver)."""
    token = str(body_json(event).get("id_token") or "")
    if verify(token, "id")["sub"] != user["sub"]:
        raise HttpError(403, "o ID token é de outro usuário")
    ident = identity.get_id(IdentityPoolId=IDENTITY_POOL,
                            Logins={f"cognito-idp.{REGION}.amazonaws.com/{POOL}": token})["IdentityId"]
    iot.attach_policy(policyName=VIEWER_POLICY, target=ident)
    return {"identity": ident}


def name(data: dict, key: str) -> str:
    v = data.get(key)
    if not isinstance(v, str) or not NAME_RE.fullmatch(v):
        raise HttpError(400, f"{key} inválido")
    return v


# Só o que a tela da placa oferece. Ligar o REC nunca: só pelo botão na placa
COMMANDS = {
    "oba.activate": lambda d: {"target": name(d, "target")},
    "oba.remove": lambda d: {"target": name(d, "target")},
    "play": lambda d: {"sound": name(d, "sound")},
    "rec": lambda d: {"on": False},
    "state": lambda d: {},
}


def device_install(user: dict, event: dict) -> dict:
    """Instala um Oba do registro na placa: o instalador conta o andamento em ui/install (cid)."""
    data = body_json(event)
    oid = name(data, "id")
    if registered(oid) is None:
        raise HttpError(404, f"{oid} não está no registro")
    cid = "i" + secrets.token_hex(4)
    job = {"cid": cid, "id": oid, "activate": data.get("activate") is True, "user": user.get("username"),
           "ts": int(time.time() * 1000)}
    lam.invoke(FunctionName=INSTALLER, InvocationType="Event", Payload=json.dumps(job).encode())
    return {"cid": cid}


def device_cmd(user: dict, event: dict) -> dict:
    data = body_json(event)
    kind = data.get("type")
    if kind not in COMMANDS:
        raise HttpError(400, "comando não permitido")
    if kind == "rec" and data.get("on") is not False:
        raise HttpError(400, "o REC só liga pelo botão na placa")
    cid = "p" + secrets.token_hex(4)
    msg = {"v": 1, "type": kind, "ts": int(time.time() * 1000), "id": cid, **COMMANDS[kind](data)}
    iot_data.publish(topic=CMD_TOPIC, qos=1, payload=json.dumps(msg).encode())
    return {"id": cid}


# ---------- registro de Obas (S3) ----------

def text(v, n: int) -> str:
    return v[:n] if isinstance(v, str) else ""


def read_key(rel: str, limit: int) -> bytes | None:
    """obas/<rel> do registro, ou None se passar de limit bytes."""
    obj = s3.get_object(Bucket=BUCKET, Key=f"obas/{rel}", ExpectedBucketOwner=ACCOUNT)
    try:
        if int(obj.get("ContentLength") or 0) > limit:
            return None
        data = obj["Body"].read(limit + 1)
    finally:
        obj["Body"].close()
    return data if len(data) <= limit else None


def registered(oid: str) -> str | None:
    """A versão do Oba no registro ("" se o oba.json não der para ler); None: não está lá."""
    r = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"obas/{oid}/", MaxKeys=1, ExpectedBucketOwner=ACCOUNT)
    if not r.get("KeyCount"):
        return None
    try:
        return text(oba.loads(read_key(f"{oid}/oba.json", oba.JSON_MAX) or b"").get("version"), 20)
    except (ClientError, ValueError, AttributeError):
        return ""


def numbers(v, n: int) -> bool:
    return (isinstance(v, list) and len(v) == n
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v))


def look(oid: str, spec: dict) -> dict:
    """O desenho do Oba como o roteador manda no ui/oba, só com valores que a tela entende
    (o SVG do rig é montado com eles)."""
    lk = spec.get("look") if isinstance(spec.get("look"), dict) else {}
    pal = lk.get("palette") if isinstance(lk.get("palette"), dict) else {}
    out = {"palette": {k: v for k, v in pal.items() if isinstance(v, str) and COLOR_RE.fullmatch(v)}}
    if lk.get("type") == "sprites":
        frames = lk.get("frames") if isinstance(lk.get("frames"), dict) else {}
        idle = frames.get("idle") if isinstance(frames.get("idle"), dict) else {}
        sp = {k: lk[k] for k in ("size", "origin") if numbers(lk.get(k), 2)}
        fps = idle.get("fps")
        sp["fps"] = fps if isinstance(fps, int) and not isinstance(fps, bool) else 6
        files = spec.get("files") if isinstance(spec.get("files"), dict) else {}
        rel, sha = idle.get("sheet"), None
        if oba.valid_path(rel) and isinstance(files.get(rel), str):
            sha = files[rel].lower()
        data = read_key(f"{oid}/{rel}", SHEET_MAX) if sha else None
        if data and oba_remote.sha256(data) == sha:
            sp["sheet"] = base64.b64encode(data).decode()
        out["sprites"] = sp
    else:
        eyes = lk.get("eyes") if isinstance(lk.get("eyes"), dict) else {}
        pts = lk.get("outline")
        if isinstance(pts, list) and 3 <= len(pts) <= oba.OUTLINE_MAX and all(numbers(q, 2) for q in pts):
            out["outline"] = pts
        out["eyes"] = {k: v for k, v in eyes.items()
                       if k in ("left", "right", "y", "rx", "ry") and numbers([v], 1)}
    return out


def card(oid: str, sizes: dict) -> dict:
    """Um Oba do registro para a aba Obas. shas: os que valem no active do state."""
    if not NAME_RE.fullmatch(oid) or "oba.json" not in sizes:
        return {"id": oid, "broken": "sem oba.json"}
    try:
        raw = read_key(f"{oid}/oba.json", oba.JSON_MAX)
        spec = oba.loads(raw) if raw else None
    except ClientError:
        return {"id": oid, "broken": "não deu para ler o oba.json"}
    except ValueError:
        spec = None
    if not isinstance(spec, dict) or spec.get("id") != oid:
        return {"id": oid, "broken": "oba.json inválido"}
    files = spec.get("files") if isinstance(spec.get("files"), dict) else {}
    lists = {k: [x[:40] for x in spec.get(k) or [] if isinstance(x, str)][:16] if isinstance(spec.get(k), list) else []
             for k in ("requires", "wake_words")}
    sounds = spec.get("sounds") if isinstance(spec.get("sounds"), dict) else {}
    try:
        drawing = look(oid, spec)
    except ClientError:
        drawing = {}
    return {"id": oid, "name": text(spec.get("name"), 40) or oid, "version": text(spec.get("version"), 20),
            "description": text(spec.get("description"), 400), "author": text(spec.get("author"), 80),
            **lists, "sounds": sorted(k[:31] for k in sounds)[:32],
            "shas": [oba_remote.sha256(raw), oba_remote.minified_sha(spec)],
            "size": len(raw) + sum(sizes.get(rel, 0) for rel in files), "count": len(files) + 1,
            "missing": sorted(rel for rel in files if rel not in sizes)[:5], **drawing}


def list_obas(user: dict, event: dict) -> dict:
    groups = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="obas/", ExpectedBucketOwner=ACCOUNT):
        for o in page.get("Contents", []):
            oid, _, rel = o["Key"][len("obas/"):].partition("/")
            if rel:
                groups.setdefault(oid, {})[rel] = o["Size"]
    ids = sorted(groups)

    def one(oid: str) -> dict:
        try:
            return card(oid, groups[oid])
        except Exception as e:  # noqa: BLE001  um Oba ruim no registro não derruba a lista
            print("card:", oid, type(e).__name__, str(e)[:200])
            return {"id": oid, "broken": "não deu para ler o oba.json"}

    with ThreadPoolExecutor(8) as pool:
        obas = list(pool.map(one, ids[:LIST_MAX]))
    return {"obas": obas, "truncated": max(0, len(ids) - LIST_MAX)}


def remove_oba(user: dict, event: dict, oid: str) -> dict:
    """Tira do registro (o bucket tem versionamento: volta em até 30 dias). A placa fica como está."""
    n = oba_remote.registry_remove(s3, BUCKET, oid, ACCOUNT)
    if not n:
        raise HttpError(404, f"{oid} não está no registro")
    return {"id": oid, "deleted": n}


# ---------- histórico de resumos (DynamoDB do roteador) ----------

HIST_PK = f"hist#{DEVICE}"


def need_table():
    if table is None:
        raise HttpError(503, "o histórico ainda não está pronto: rode o setup.py de novo")


def names(*attrs) -> dict:
    """#<nome> para cada atributo da expressão (vários são palavras reservadas do DynamoDB)."""
    return {f"#{a}": a for a in attrs}


def hist_key(hid: str) -> tuple[str, str]:
    """sk e sessão de um id que a rota já conferiu (HIST_ID)."""
    ts, sid = hid.split("_", 1)
    return f"{ts}#{sid}", sid


def hist_item(it: dict) -> dict:
    return {"id": it["sk"].replace("#", "_", 1), "ts": int(it["ts"]), "title": it.get("title") or "",
            "lines": int(it.get("lines") or 0), "oba": it.get("oba") or "", "expires": int(it["expires"]) * 1000}


def hist_query(attrs: tuple, limit: int | None = None) -> list[dict]:
    """Os resumos do mais novo para o mais antigo, só com attrs; para em limit."""
    items, kw = [], {}
    while True:
        r = table.query(KeyConditionExpression="#pk = :pk", ExpressionAttributeValues={":pk": HIST_PK},
                        ProjectionExpression=", ".join(f"#{a}" for a in attrs),
                        ExpressionAttributeNames=names("pk", *attrs), ScanIndexForward=False,
                        ConsistentRead=True, **kw)
        items += r["Items"]
        if (limit and len(items) >= limit) or "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def list_history(user: dict, event: dict) -> dict:
    """Sem o corpo, até HIST_LIST_MAX. O TTL apaga com atraso: o vencido já não aparece."""
    need_table()
    now = time.time()
    items = [hist_item(it) for it in hist_query(("sk", "ts", "title", "lines", "oba", "expires"), HIST_LIST_MAX + 1)
             if int(it.get("expires", 0)) > now]
    return {"items": items[:HIST_LIST_MAX], "truncated": len(items) > HIST_LIST_MAX, "days": HISTORY_DAYS}


def get_history(user: dict, event: dict, hid: str) -> dict:
    need_table()
    sk, _ = hist_key(hid)
    it = table.get_item(Key={"pk": HIST_PK, "sk": sk}).get("Item")
    if not it or int(it.get("expires", 0)) <= time.time():
        raise HttpError(404, "esse resumo não está mais no histórico")
    try:
        body = json.loads(it["summary"])
    except ValueError:
        body = None
    return {**(body if isinstance(body, dict) else {}), "id": hid, "expires": int(it["expires"]) * 1000}


def recording() -> str | None:
    """A sessão que está gravando agora (REC ligado), ou None. A role só deixa ler rec e session."""
    it = table.get_item(Key={"pk": f"dev#{DEVICE}", "sk": "device"}, ProjectionExpression="#rec, #session",
                        ExpressionAttributeNames=names("rec", "session")).get("Item") or {}
    return it.get("session") if it.get("rec") and it.get("session") else None


def out_of_time() -> bool:
    return time.monotonic() - STARTED > WORK_S


def batch_delete(keys: list[dict]) -> int:
    """BatchWriteItem em lotes de 25; o que a tabela não processou vai de novo, com espera.
    Confere o tempo entre os lotes (com espera, uma página passaria do timeout): devolve
    quantos apagou."""
    for i in range(0, len(keys), 25):
        if i and out_of_time():
            return i
        req = {table.name: [{"DeleteRequest": {"Key": k}} for k in keys[i:i + 25]]}
        for attempt in range(8):
            req = dynamo.batch_write_item(RequestItems=req).get("UnprocessedItems") or {}
            if not req.get(table.name):
                break
            time.sleep(min(0.05 * 2 ** attempt, 1))
        else:
            raise HttpError(503, "a tabela está ocupada: tente de novo")
    return len(keys)


def drop_session(sid: str) -> tuple[int, bool]:
    """Apaga os eventos e as regras da sessão. Devolve quantos e se acabou."""
    n, kw = 0, {}
    while not out_of_time():
        r = table.query(KeyConditionExpression="#pk = :pk", ExpressionAttributeValues={":pk": f"s#{DEVICE}#{sid}"},
                        ProjectionExpression="#pk, #sk", ExpressionAttributeNames=names("pk", "sk"),
                        ConsistentRead=True, Limit=500, **kw)
        got = batch_delete([{"pk": it["pk"], "sk": it["sk"]} for it in r["Items"]])
        n += got
        if got < len(r["Items"]):     # o tempo acabou no meio da página: a Query seguinte começa do resto
            return n, False
        if "LastEvaluatedKey" not in r:
            return n, True
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return n, False


def drop_one(sk: str, sid: str) -> tuple[int, bool]:
    """A sessão e depois o resumo: se o tempo acabar no meio, o resumo fica na lista para
    apagar de novo. Fora do REC ("-") só o resumo: os eventos soltos não são da conversa."""
    n, done = (0, True) if sid == "-" else drop_session(sid)
    if done:
        table.delete_item(Key={"pk": HIST_PK, "sk": sk})
    return n, done


def drop_retained(gone: list[tuple[str, int]]):
    """Tira o ui/summary retido se ele é de um resumo apagado (mesma sessão ou, fora do REC,
    mesmo ts): senão o "Último resumo" da aba Reunião ainda mostraria a conversa."""
    try:
        raw = iot_data.get_retained_message(topic=SUMMARY_TOPIC).get("payload") or b""
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return
        raise
    try:
        cur = json.loads(raw)
    except ValueError:
        return
    if isinstance(cur, dict) and any(cur.get("session") == sid if sid != "-" else cur.get("ts") == ts
                                     for sid, ts in gone):
        iot_data.publish(topic=SUMMARY_TOPIC, qos=1, retain=True, payload=b"")


def delete_history(user: dict, event: dict, hid: str) -> dict:
    """Apaga o resumo e a sessão dele. A sessão que está gravando, não."""
    need_table()
    sk, sid = hist_key(hid)
    if not table.get_item(Key={"pk": HIST_PK, "sk": sk}, ProjectionExpression="#sk",
                          ExpressionAttributeNames=names("sk")).get("Item"):
        raise HttpError(404, "esse resumo não está mais no histórico")
    if sid != "-" and sid == recording():
        raise HttpError(409, "essa sessão ainda está gravando: desligue o REC e apague depois")
    n, done = drop_one(sk, sid)
    if done:
        drop_retained([(sid, int(sk[:13]))])
    return {"id": hid, "events": n, "more": not done}


def clear_history(user: dict, event: dict) -> dict:
    """Apaga todos, menos o da sessão que está gravando. "more": o tempo acabou, peça de novo."""
    need_table()
    live, gone, n, kept, more = recording(), [], 0, 0, False
    for it in hist_query(("sk",)):
        sid = it["sk"].split("#", 1)[1]
        if sid == live:
            kept += 1
            continue
        if out_of_time():
            more = True
            break
        events, done = drop_one(it["sk"], sid)
        n += events
        if not done:
            more = True
            break
        gone.append((sid, int(it["sk"][:13])))
    if gone:
        drop_retained(gone)
    return {"deleted": len(gone), "events": n, "kept": kept, "more": more}


# ---------- envio de um Oba em zip ----------

def unsafe(name: str) -> bool:
    return (not name or "\\" in name or name.startswith("/") or re.match(r"[A-Za-z]:", name) is not None
            or ".." in name.split("/"))


def skipped(name: str) -> bool:
    """Lixo que o Finder e o Windows põem no zip."""
    base = name.rstrip("/").rsplit("/", 1)[-1]
    return name.split("/", 1)[0] == "__MACOSX" or base in (".DS_Store", "Thumbs.db") or base.startswith("._")


def zip_entries(zf: zipfile.ZipFile) -> dict:
    infos = zf.infolist()
    if len(infos) > ZIP_ENTRIES_MAX:
        raise HttpError(413, f"o zip tem {len(infos)} entradas; o máximo é {ZIP_ENTRIES_MAX}")
    entries = {}
    for i in infos:
        name = i.filename
        if unsafe(name):
            raise HttpError(400, f"caminho perigoso no zip: {name[:80]}")
        if stat.S_ISLNK(i.external_attr >> 16):
            raise HttpError(400, f"o zip tem um link simbólico: {name[:80]}")
        if i.is_dir() or skipped(name):
            continue
        if i.flag_bits & 0x1:
            raise HttpError(400, "zip com senha não dá")
        if i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise HttpError(400, "compressão que o portal não lê: use a do Finder, do Windows ou o zip -r")
        if name in entries:
            raise HttpError(400, f"entrada repetida no zip: {name[:80]}")
        entries[name] = i
    return entries


def unzip(raw: bytes, work: Path) -> tuple[Path, list[str]]:
    """Extrai o oba.json (na raiz do zip ou numa pasta só) e os arquivos de "files" para
    work/<id>/. Conta o que descompacta, sem confiar no cabeçalho. Devolve a pasta e o que
    ficou de fora."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        entries = zip_entries(zf)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError, NotImplementedError):
        raise HttpError(400, "o arquivo não é um zip válido")
    tops = [n[:-len("oba.json")] for n in entries if n.endswith("/oba.json") and n.count("/") == 1]
    root = "" if "oba.json" in entries else tops[0] if len(tops) == 1 else None
    if root is None:
        raise HttpError(400, "não achei o oba.json: ponha na raiz do zip ou numa pasta só")
    total = 0

    def read(info: zipfile.ZipInfo, limit: int, what: str) -> bytes:
        nonlocal total
        try:
            with zf.open(info) as f:
                data = f.read(limit + 1)
        except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError, OSError):
            raise HttpError(400, f"zip corrompido em {info.filename[:80]}")
        total += len(data)
        if len(data) > limit:
            raise HttpError(413, f"{what}: passa de {limit // 1024} KB")
        if total > oba.TOTAL_MAX:
            raise HttpError(413, f"descompactado, o Oba passa de {oba.TOTAL_MAX // 1024 // 1024} MB")
        return data

    js = read(entries[root + "oba.json"], oba.JSON_MAX, "oba.json")
    try:
        spec = oba.loads(js)
    except ValueError as e:
        raise HttpError(422, "o oba.json não é JSON válido", problems=[str(e)[:200]])
    oid = spec.get("id") if isinstance(spec, dict) else None
    if not isinstance(oid, str) or not NAME_RE.fullmatch(oid):
        raise HttpError(422, "o oba.json precisa de um id (minúsculas, números, _ e -, até 31)")
    folder = work / oid
    folder.mkdir(parents=True)
    (folder / "oba.json").write_bytes(js)
    files = spec.get("files") if isinstance(spec.get("files"), dict) else {}
    used = {root + "oba.json"}
    for rel in list(files)[:oba.FILES_MAX]:
        info = entries.get(root + rel) if rel != "oba.json" and oba.valid_path(rel) else None
        if info is None:
            continue  # o validador aponta o que falta
        dest = folder / rel
        if not dest.resolve().is_relative_to(folder.resolve()):
            continue
        data = read(info, oba.FILE_MAX, rel)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except OSError:  # arquivo e pasta com o mesmo nome: o validador aponta
            continue
        used.add(info.filename)
    ignored = sorted(n[len(root):] if n.startswith(root) else n for n in entries if n not in used)
    return folder, ignored


def upload_oba(user: dict, event: dict) -> dict:
    if not event.get("isBase64Encoded"):
        raise HttpError(415, "mande o zip do Oba")
    body = event.get("body") or ""
    if len(body) > (ZIP_MAX + 2) // 3 * 4:
        raise HttpError(413, f"o zip passa de {ZIP_MAX // 1024 // 1024} MB: use o tools/oba.py install")
    try:
        raw = base64.b64decode(body, validate=True)
    except ValueError:
        raise HttpError(400, "corpo inválido")
    replace = (event.get("queryStringParameters") or {}).get("replace") == "1"
    work = Path("/tmp") / uuid.uuid4().hex
    try:
        folder, ignored = unzip(raw, work)
        problems = oba.validate(folder)
        if problems:
            problems = [x.replace(f"{work}/", "") for x in problems[:50]]
            raise HttpError(422, f"o Oba tem {len(problems)} problema(s)", problems=problems)
        _, _, spec = oba.load(folder)
        cur = registered(spec["id"])
        if cur is not None and not replace:
            raise HttpError(409, f"{spec['id']} já está no registro", id=spec["id"], version=cur)
        r = oba_remote.registry_sync(s3, BUCKET, folder, spec, owner=ACCOUNT)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"upload": spec["id"], "version": spec.get("version"), "sent": len(r["sent"])}))
    return {"id": spec["id"], "name": spec.get("name"), "version": spec.get("version"), "new": r["new"],
            "sent": len(r["sent"]), "kept": len(r["kept"]), "deleted": len(r["deleted"]), "ignored": ignored[:20]}


ROUTES = [
    ("GET", re.compile(r"/api/me"), me),
    ("POST", re.compile(r"/api/iot-access"), iot_access),
    ("POST", re.compile(r"/api/device/cmd"), device_cmd),
    ("POST", re.compile(r"/api/device/install"), device_install),
    ("GET", re.compile(r"/api/obas"), list_obas),
    ("POST", re.compile(r"/api/obas"), upload_oba),
    ("DELETE", re.compile(r"/api/obas/([a-z0-9_-]{1,31})"), remove_oba),
    ("GET", re.compile(r"/api/history"), list_history),
    ("DELETE", re.compile(r"/api/history"), clear_history),
    ("GET", re.compile(rf"/api/history/({HIST_ID})"), get_history),
    ("DELETE", re.compile(rf"/api/history/({HIST_ID})"), delete_history),
]


def reply(status: int, body: dict) -> dict:
    try:
        out = json.dumps(body, ensure_ascii=False, allow_nan=False)
        out.encode()                     # surrogate solto não vira UTF-8
    except ValueError as e:
        print("reply:", type(e).__name__, str(e)[:200])
        status, out = 500, '{"error": "erro interno"}'
    return {"statusCode": status, "body": out,
            "headers": {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"}}


def route_of(method: str, path: str):
    for m, rx, fn in ROUTES:
        if m == method and (hit := rx.fullmatch(path)):
            return fn, hit.groups()
    raise HttpError(404, "rota não existe")


def handler(event, context):
    global ACCOUNT, STARTED
    ACCOUNT = ACCOUNT or context.invoked_function_arn.split(":")[4]
    STARTED = time.monotonic()
    http = event["requestContext"]["http"]
    method, path = http["method"], event.get("rawPath", "")
    user = None
    try:
        user = verify((event.get("headers") or {}).get("x-oba-token", ""), "access")   # antes: sem token, nem 404
        route, args = route_of(method, path)
        status, out = 200, route(user, event, *args)
    except HttpError as e:
        status, out = e.status, {"error": str(e), **e.extra}
    except ClientError as e:
        print("aws:", e.response["Error"].get("Code"), e.response["Error"].get("Message", "")[:200])
        status, out = 502, {"error": "falha num serviço da AWS"}
    except Exception as e:  # noqa: BLE001  o resto vira 500 com o tipo no log, sem detalhes para fora
        print("erro:", type(e).__name__, str(e)[:200])
        status, out = 500, {"error": "erro interno"}
    print(json.dumps({"method": method, "path": path, "status": status, "user": user and user.get("username")}))
    return reply(status, out)
