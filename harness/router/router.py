"""Roteador do harness do Oba Pocket (docs/protocol.md).

Chamado pela IoT Rule <p>_router com o que a placa publica em
<p>/<dev>/{state,evt,transcript,reply}; a regra junta ao payload o nome da
placa (dev) e o canal (ch). O roteador:
  - abre e fecha as sessões pelo REC do state e cria os eventos dele
    (rec.on, rec.off, oba.changed, transcript.final, reply);
  - aplica os gatilhos do Oba ativo (agent.triggers no JSON dele, lido do
    registro no S3) e chama o agente do catálogo (AGENTS), se o Oba da placa
    bate com o do registro (versão e sha256 do state);
  - confere as ações que o agente devolve antes de publicar em <p>/<dev>/cmd e
    <p>/<dev>/ui/<canal>.

Tabela (pk/sk, TTL em "expires"):
  dev#<dev>        / device          estado da placa e sessão atual
  dev#<dev>        / t#<oba>#<n>     gatilho n do Oba: eventos pendentes, trava, última rodada
  dev#<dev>        / read#<id>       read pedido pelo agente, esperando a resposta
  s#<dev>#<sessão> / e#<ts>#<rnd>    eventos da sessão ("-" = fora do REC)
  s#<dev>#<sessão> / r#<id>          balões e regras (spoken | armed | fired | disarmed | rejected)
  mem#<dev>#<oba>  / memory          memória do Oba nessa placa, entre sessões
"""

import base64
import hashlib
import json
import math
import os
import re
import secrets
import time
import urllib.request
from urllib.parse import urlparse

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

from icons import icon_png, norm, resolve_icon

PREFIX = os.environ.get("PREFIX", "obapocket")
BUCKET = os.environ["BUCKET"]
AGENTS = json.loads(os.environ.get("AGENTS") or "{}")  # nome -> {"type": "agentcore", "arn"} | {"type": "http", "url"}
URL_HOSTS = tuple(h.strip().lower() for h in os.environ.get("URL_HOSTS", "").split(",") if h.strip())

ROUNDS = 3               # rodadas seguidas de um gatilho numa invocação
LOCK_S = 150             # trava de uma rodada (maior que o timeout do agente)
AGENT_TIMEOUT_S = 140
ROUND_MIN_LEFT_MS = 125_000  # só começa uma rodada com tempo para ela terminar
WAIT_MAX_MS = 10_000     # espera o cooldown dentro da invocação até isso
SPEC_CACHE_S = 60
HISTORY_MAX = 1000       # eventos de uma sessão que o agente vê
IDLE_HISTORY = 50        # fora do REC, só os últimos
MEMORY_MAX = 8192
READ_DEPTH = 2           # read -> resposta -> read -> resposta, no máximo
READ_WAIT_S = 600
KEEP_S = 7 * 24 * 3600
NO_SESSION = "-"

MAX_ARMED = 6            # a placa guarda 8: sobra espaço para as regras de evento
MAX_ACTIONS = 12
ARM_TTL_S, ARM_TTL_MAX_S = 900, 86400
MAX_WORDS, MAX_DO = 8, 4
TEXT_MAX = 280
URL_MAX = 300
CARD_TEXT_MAX = 600
CMD_MAX = 14_000         # o buffer MQTT da placa é de 16 KB
UI_MAX = 64_000
SHEET_MAX = 48 * 1024    # folha idle que vai em base64 no ui/oba
OUTLINE_MAX = 256        # pontos do rig (OBA_OUTLINE_MAX em src/oba.h)
UI_OBA_FORMAT = 3        # mudou o que vai no ui/oba: sai de novo para todas as placas
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")  # agente http sem TLS só na própria máquina

# O que o agente pode pedir (docs/protocol.md). reset e oba.* ficam com o roteador e a placa.
AGENT_TYPES = {"speak", "arm", "disarm", "react", "look", "vibrate", "leds", "play", "read", "rec", "state", "ui"}
RULE_DO = {"speak", "react", "look", "vibrate", "leds", "play"}
RULE_EVENTS = {"touch.tap", "button.a", "button.c", "sound.loud", "imu.shake", "imu.tap"}
DEVICE_EVENTS = RULE_EVENTS | {"wake", "rule.fired", "bubble.done"}
REACT = {"idle", "happy", "scared", "shy", "dizzy", "sleepy", "glance", "turn"}
LED_FX = {"off", "solid", "breathe", "rainbow", "strobe", "chase"}
READ_KEYS = {"battery", "imu", "mic", "mood", "rec", "time", "oba", "wifi"}
CARD_KEYS = ("kind", "title", "body", "service", "url", "source")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,24}")
NAME_RE = re.compile(r"[a-z0-9_-]{1,32}")
COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
SOUND_RE = re.compile(r"[a-z0-9_-]{1,31}")
PATH_RE = re.compile(r"(?![./])(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,64}(?<!/)")  # como em "files"

table = boto3.resource("dynamodb").Table(os.environ["TABLE"])
iot = boto3.client("iot-data", endpoint_url=f"https://{os.environ['IOT_ENDPOINT']}")
s3 = boto3.client("s3")
agentcore = boto3.client("bedrock-agentcore",
                         config=Config(read_timeout=AGENT_TIMEOUT_S, retries={"max_attempts": 1}))


def now_ms() -> int:
    return int(time.time() * 1000)


def log(*a):
    print(*a, flush=True)


def expires() -> int:
    return int(time.time()) + KEEP_S


def conditional_failed(e: ClientError) -> bool:
    return e.response["Error"]["Code"] == "ConditionalCheckFailedException"


def clamp(v, lo, hi, default=None):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(v):
        return default
    return max(lo, min(hi, v))


# ------------------------------------------------------------------ mensagens

def publish(dev: str, ch: str, obj, retain: bool = False):
    payload = b"" if obj is None else json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    iot.publish(topic=f"{PREFIX}/{dev}/{ch}", qos=0, retain=retain, payload=payload)


def send(dev: str, type_: str, **kw) -> bool:
    msg = {"v": 1, "type": type_, "ts": now_ms(), **kw}
    if len(json.dumps(msg, ensure_ascii=False)) > CMD_MAX:
        # Grande demais para a placa: os balões com ícone viram texto
        for b in [msg.get("bubble")] + [d.get("bubble") for d in msg.get("do", [])]:
            if b and b.get("kind") == "image":
                b.pop("png", None)
                b["kind"] = "text"
        if len(json.dumps(msg, ensure_ascii=False)) > CMD_MAX:
            log("comando grande demais, descartado:", type_, msg.get("id"))
            return False
    publish(dev, "cmd", msg)
    return True


def ui(dev: str, channel: str, data: dict, retain: bool = False):
    publish(dev, f"ui/{channel}", {**data, "v": 1, "type": channel, "ts": now_ms()}, retain)


def valid_url(url) -> str | None:
    if not isinstance(url, str) or len(url) > URL_MAX:
        return None
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
    except ValueError:  # IPv6 malformado ("https://[x"): sem isso, a rodada inteira cai
        return None
    if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in URL_HOSTS):
        return None
    return url


def clean_urls(obj):
    """Tira de qualquer lugar as url que não são de um domínio permitido."""
    if isinstance(obj, dict):
        return {k: (valid_url(v) if k == "url" else clean_urls(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_urls(v) for v in obj]
    return obj


# ------------------------------------------------------------------ registro de Obas

_specs: dict[str, tuple[float, dict | None, set[str]]] = {}  # id -> (lido em, JSON, sha256 que valem)


def load_spec(oba_id, fresh: bool = False) -> dict | None:
    """O JSON do Oba no registro (S3), guardado por um minuto (fresh: relê agora)."""
    if not isinstance(oba_id, str) or not re.fullmatch(r"[a-z0-9_-]{1,31}", oba_id):
        return None
    hit = _specs.get(oba_id)
    if hit and not fresh and time.time() - hit[0] < SPEC_CACHE_S:
        return hit[1]
    shas = set()
    try:
        raw = s3.get_object(Bucket=BUCKET, Key=f"obas/{oba_id}/oba.json")["Body"].read()
        spec = json.loads(raw)
        # A placa manda o sha dos bytes do cartão; no embutido, o do JSON minificado
        mini = json.dumps(spec, separators=(",", ":"), ensure_ascii=False).encode()
        shas = {hashlib.sha256(raw).hexdigest(), hashlib.sha256(mini).hexdigest()}
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchKey", "AccessDenied"):
            raise
        log("Oba fora do registro:", oba_id)
        spec = None
    _specs[oba_id] = (time.time(), spec, shas)
    return spec


def active_of(state) -> dict:
    return state.get("active") if isinstance(state, dict) and isinstance(state.get("active"), dict) else {}


def spec_version(spec: dict) -> str:
    """A versão como a placa manda: sem "version" no JSON, "0"."""
    v = spec.get("version")
    return v if isinstance(v, str) else "0"


def same_as_board(spec: dict | None, active: dict) -> bool:
    sha = active.get("sha256")
    if not isinstance(sha, str):
        return True  # firmware antigo, sem sha256: não confere
    hit = _specs.get((spec or {}).get("id"))
    return (spec is not None and hit is not None and hit[1] is spec
            and active.get("version") == spec_version(spec) and sha.lower() in hit[2])


def board_spec(spec: dict | None, active: dict) -> tuple[dict | None, bool]:
    """Confere o Oba ativo da placa (state.active: versão e sha256) com o do registro.
    Não bateu com o do cache: relê do S3 uma vez. Devolve o JSON que vale e se bateu."""
    if same_as_board(spec, active):
        return spec, True
    fresh = load_spec((spec or {}).get("id") or active.get("id"), fresh=True)
    return fresh, same_as_board(fresh, active)


def mismatch(spec: dict | None, active: dict) -> str:
    reg = f"{spec.get('id')} {spec.get('version')}" if spec else "sem registro"
    return (f"placa {active.get('id')} {active.get('version')} sha {str(active.get('sha256'))[:12]}, "
            f"registro {reg}")


def triggers(spec: dict | None) -> list[dict]:
    out = []
    for t in ((spec or {}).get("agent") or {}).get("triggers") or []:
        if not isinstance(t, dict) or not isinstance(t.get("on"), str):
            continue
        run = t.get("run") if isinstance(t.get("run"), str) and NAME_RE.fullmatch(t["run"]) else "think"
        out.append({"on": t["on"], "batch": int(clamp(t.get("batch"), 1, 50, 1)),
                    "debounce_ms": int(clamp(t.get("debounce_ms"), 0, 10_000, 0)),
                    "cooldown_ms": int(clamp(t.get("cooldown_ms"), 0, 86_400_000, 0)), "run": run})
    return out[:16]


# ------------------------------------------------------------------ placa e sessão

def dev_key(dev: str, sk: str = "device") -> dict:
    return {"pk": f"dev#{dev}", "sk": sk}


def session_pk(dev: str, sid: str | None) -> str:
    return f"s#{dev}#{sid or NO_SESSION}"


def get_device(dev: str) -> dict:
    return table.get_item(Key=dev_key(dev), ConsistentRead=True).get("Item") or {}


def current_session(item: dict) -> str:
    return item.get("session") if item.get("rec") and item.get("session") else NO_SESSION


def start_session(dev: str) -> str | None:
    """O REC ligou: sessão nova. None se outra invocação já abriu."""
    sid = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3)
    try:
        old = table.update_item(
            Key=dev_key(dev), UpdateExpression="SET rec = :t, #s = :s, started = :now REMOVE retained",
            ConditionExpression="attribute_not_exists(rec) OR rec = :f",
            ExpressionAttributeNames={"#s": "session"},
            ExpressionAttributeValues={":t": True, ":f": False, ":s": sid, ":now": now_ms()},
            ReturnValues="ALL_OLD",
        ).get("Attributes", {})
    except ClientError as e:
        if conditional_failed(e):
            return None
        raise
    log("sessão nova", dev, sid)
    send(dev, "reset", session=sid)
    # Os canais retidos da sessão anterior somem das telas
    for ch in set(old.get("retained") or ()) | {"summary"}:
        publish(dev, f"ui/{ch}", None, retain=True)
    return sid


def end_session(dev: str) -> str | None:
    """O REC desligou. Devolve a sessão que acabou (None se já estava desligado)."""
    try:
        old = table.update_item(
            Key=dev_key(dev), UpdateExpression="SET rec = :f REMOVE #s", ConditionExpression="rec = :t",
            ExpressionAttributeNames={"#s": "session"}, ExpressionAttributeValues={":t": True, ":f": False},
            ReturnValues="ALL_OLD",
        )["Attributes"]
    except ClientError as e:
        if conditional_failed(e):
            return None
        raise
    log("sessão encerrada", dev, old.get("session"))
    send(dev, "reset", session=None)
    return old.get("session")


def open_session(dev: str, oba: str | None, ctx) -> str:
    sid = start_session(dev)
    if not sid:
        return current_session(get_device(dev))
    ev = {"type": "rec.on", "ts": now_ms()}
    store_event(dev, sid, ev)
    dispatch(dev, oba, ev, sid, ctx)
    return sid


def store_event(dev: str, sid: str, ev: dict):
    table.put_item(Item={"pk": session_pk(dev, sid), "sk": f"e#{ev['ts']:013d}#{secrets.token_hex(3)}",
                         "type": ev["type"], "ev": json.dumps(ev, ensure_ascii=False), "expires": expires()})


def query_all(pk: str, prefix: str, limit: int | None = None, newest_first: bool = False) -> list[dict]:
    items, kw = [], {}
    while True:
        r = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(prefix),
                        ConsistentRead=True, ScanIndexForward=not newest_first, **kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r or (limit and len(items) >= limit):
            return items[:limit] if limit else items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def history(dev: str, sid: str) -> list[dict]:
    limit = IDLE_HISTORY if sid == NO_SESSION else HISTORY_MAX
    items = query_all(session_pk(dev, sid), "e#", limit, newest_first=True)
    return [json.loads(it["ev"]) for it in reversed(items)]


def rules(dev: str, sid: str) -> list[dict]:
    items = query_all(session_pk(dev, sid), "r#")
    items.sort(key=lambda it: int(it.get("created", 0)))
    return items


def save_rule(dev: str, sid: str, rid: str, status: str, card: dict | None, rule: dict | None = None):
    item = {"pk": session_pk(dev, sid), "sk": f"r#{rid}", "status": status, "created": now_ms(),
            "card": json.dumps(card or {}, ensure_ascii=False), "expires": expires()}
    if rule:
        item.update(on=rule["on"], words=rule.get("words", []), until=now_ms() + rule["ttl_s"] * 1000)
    table.put_item(Item=item)


def set_rule(dev: str, sid: str, rid: str, status: str):
    try:
        table.update_item(Key={"pk": session_pk(dev, sid), "sk": f"r#{rid}"}, UpdateExpression="SET #s = :s",
                          ConditionExpression="attribute_exists(pk)", ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":s": status})
    except ClientError as e:
        if not conditional_failed(e):
            raise


def get_memory(dev: str, oba: str) -> dict:
    it = table.get_item(Key={"pk": f"mem#{dev}#{oba}", "sk": "memory"}).get("Item")
    return json.loads(it["memory"]) if it else {}


def save_memory(dev: str, oba: str, memory: dict):
    raw = json.dumps(memory, ensure_ascii=False)
    if len(raw.encode()) > MEMORY_MAX:
        log(f"memória do {oba} com {len(raw)} bytes: passou de {MEMORY_MAX}, não guardei")
        return
    table.put_item(Item={"pk": f"mem#{dev}#{oba}", "sk": "memory", "memory": raw, "updated": now_ms()})


# ------------------------------------------------------------------ gatilhos

def bump(dev: str, oba: str, n: int, sid: str) -> int:
    """Mais um evento para o gatilho. Numa sessão nova, a conta recomeça."""
    key, mark = dev_key(dev, f"t#{oba}#{n}"), now_ms()
    names, values = {"#s": "session"}, {":one": 1, ":m": mark, ":s": sid}
    try:
        table.update_item(Key=key, UpdateExpression="ADD pending :one SET lastEvent = :m",
                          ConditionExpression="#s = :s", ExpressionAttributeNames=names,
                          ExpressionAttributeValues=values)
    except ClientError as e:
        if not conditional_failed(e):
            raise
        table.update_item(Key=key, UpdateExpression="SET pending = :one, lastEvent = :m, #s = :s",
                          ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    return mark


def acquire(dev: str, oba: str, n: int, trig: dict, sid: str) -> int | None:
    """Trava o gatilho para uma rodada. Devolve quantos eventos estavam pendentes."""
    now = now_ms()
    try:
        old = table.update_item(
            Key=dev_key(dev, f"t#{oba}#{n}"),
            UpdateExpression="SET runningUntil = :lock, pending = :zero",
            ConditionExpression="#s = :s AND pending >= :batch "
                                "AND (attribute_not_exists(runningUntil) OR runningUntil < :now) "
                                "AND (attribute_not_exists(lastRun) OR lastRun <= :cool)",
            ExpressionAttributeNames={"#s": "session"},
            ExpressionAttributeValues={":lock": now + LOCK_S * 1000, ":zero": 0, ":s": sid, ":now": now,
                                       ":batch": trig["batch"], ":cool": now - trig["cooldown_ms"]},
            ReturnValues="ALL_OLD",
        )["Attributes"]
    except ClientError as e:
        if conditional_failed(e):
            return None
        raise
    return int(old["pending"])


def release(dev: str, oba: str, n: int):
    table.update_item(Key=dev_key(dev, f"t#{oba}#{n}"), UpdateExpression="SET runningUntil = :z, lastRun = :now",
                      ExpressionAttributeValues={":z": 0, ":now": now_ms()})


def cooldown_wait(dev: str, oba: str, n: int, trig: dict, sid: str) -> int | None:
    """Quanto esperar para tentar de novo, ou None se não vale esperar."""
    it = table.get_item(Key=dev_key(dev, f"t#{oba}#{n}"), ConsistentRead=True).get("Item") or {}
    now = now_ms()
    if it.get("session") != sid or int(it.get("pending", 0)) < trig["batch"]:
        return None
    if int(it.get("runningUntil", 0)) >= now:
        return None  # tem uma rodada em andamento: ela trata os pendentes depois
    wait = int(it.get("lastRun", 0)) + trig["cooldown_ms"] - now
    return max(wait, 0) + 50 if wait <= WAIT_MAX_MS else None


def dispatch(dev: str, oba: str | None, ev: dict, sid: str, ctx):
    """Um evento chegou: acorda o agente pelos gatilhos do Oba."""
    spec = load_spec(oba)
    matching = [(n, t) for n, t in enumerate(triggers(spec)) if t["on"] == ev["type"]]
    if not matching:
        return
    marks = {n: bump(dev, oba, n, sid) for n, _ in matching}
    for n, trig in matching:
        run_trigger(dev, spec, n, trig, sid, marks[n], ctx)


def run_trigger(dev: str, spec: dict, n: int, trig: dict, sid: str, mark: int, ctx):
    oba = spec["id"]
    if trig["debounce_ms"]:
        time.sleep(trig["debounce_ms"] / 1000)
        it = table.get_item(Key=dev_key(dev, f"t#{oba}#{n}"), ConsistentRead=True).get("Item") or {}
        if int(it.get("lastEvent", 0)) != mark:
            return  # chegou outro evento depois: a invocação dele trata
    rounds = 0
    for _ in range(ROUNDS * 2 + 1):
        if rounds >= ROUNDS or ctx.get_remaining_time_in_millis() < ROUND_MIN_LEFT_MS:
            return
        pending = acquire(dev, oba, n, trig, sid)
        if pending is None:
            wait = cooldown_wait(dev, oba, n, trig, sid)
            if wait is None or ctx.get_remaining_time_in_millis() - wait < ROUND_MIN_LEFT_MS:
                return
            time.sleep(wait / 1000)
            continue
        rounds += 1
        log(f"gatilho {trig['on']} -> {trig['run']} ({pending} eventos)")
        try:
            run(dev, spec, trig, sid, pending=pending)
        finally:
            release(dev, oba, n)


# ------------------------------------------------------------------ agente

def agent_url_ok(url) -> bool:
    """A transcrição vai no corpo: https, ou http só na própria máquina (como no setup.py)."""
    if not isinstance(url, str):
        return False
    try:
        u = urlparse(url)
        host = u.hostname
    except ValueError:
        return False
    return bool(host) and (u.scheme == "https" or (u.scheme == "http" and host in LOCAL_HOSTS))


def invoke(target: dict, dev: str, sid: str, req: dict) -> dict:
    body = json.dumps(req, ensure_ascii=False).encode()
    started = time.time()
    if target.get("type") == "http":
        if not agent_url_ok(target.get("url")):
            raise ValueError("url do agente recusada: precisa ser https (http só em localhost, 127.0.0.1 ou ::1)")
        r = urllib.request.urlopen(urllib.request.Request(
            target["url"], data=body, headers={"Content-Type": "application/json"}), timeout=AGENT_TIMEOUT_S)
        out = json.loads(r.read())
    else:
        session = f"{PREFIX}-{dev}-{'idle' if sid == NO_SESSION else sid}".ljust(40, "0")[:100]
        r = agentcore.invoke_agent_runtime(agentRuntimeArn=target["arn"], runtimeSessionId=session,
                                           payload=body, contentType="application/json", accept="application/json")
        out = json.loads(r["response"].read())
    log(f"agente {req['run']} em {time.time() - started:.1f}s:", json.dumps(out, ensure_ascii=False)[:1500])
    return out if isinstance(out, dict) else {}


def run(dev: str, spec: dict, trig: dict, sid: str, pending: int = 1, events: list | None = None, depth: int = 0):
    """Uma rodada do agente: monta o contexto, chama e publica as ações."""
    item = get_device(dev)
    state = json.loads(item.get("state") or "{}")
    checked, same = board_spec(spec, active_of(state))
    if not same:
        log("o Oba da placa não bate com o registro:", mismatch(checked, active_of(state)))
        return
    spec = checked
    oba, agent = spec["id"], spec.get("agent") or {}
    endpoint = agent.get("endpoint") or "default"
    target = AGENTS.get(endpoint)
    if not target:
        log(f"endpoint {endpoint!r} do {oba} fora do catálogo")
        return
    hist = history(dev, sid)
    if events is None:
        events = [e for e in hist if e.get("type") == trig["on"]][-pending:]
    new_from = next((i for i, e in enumerate(hist) if events and e is events[0]), len(hist))
    all_rules = rules(dev, sid)
    now = now_ms()
    armed = [{"id": it["sk"][2:], "on": it.get("on"), "words": it.get("words", []),
              "title": json.loads(it["card"]).get("title")}
             for it in all_rules if it["status"] == "armed" and int(it.get("until", 0)) > now]
    shown = [{k: json.loads(it["card"]).get(k) for k in ("kind", "title", "url")}
             for it in all_rules if it["status"] in ("spoken", "fired")]
    req = {
        "v": 1, "run": trig["run"],
        "oba": {"id": oba, "name": spec.get("name"), "version": spec.get("version"),
                "persona": agent.get("persona", ""), "model": agent.get("model"),
                "abilities": agent.get("abilities", []), "mcp": agent.get("mcp", []),
                "sounds": list(spec["sounds"]) if isinstance(spec.get("sounds"), dict) else []},
        "device": {"id": dev, "state": state},
        "session": None if sid == NO_SESSION else sid,
        "trigger": trig, "events": events, "history": hist, "new_from": new_from,
        "armed": armed, "shown": shown, "memory": get_memory(dev, oba),
    }
    ui(dev, "think", {"state": "start", "run": trig["run"]})
    try:
        res = invoke(target, dev, sid, req)
    except Exception as e:
        log("agente falhou:", repr(e))
        ui(dev, "think", {"state": "end", "run": trig["run"], "note": "erro"})
        return
    # O REC mudou enquanto o agente pensava: o resultado não vale mais
    if current_session(get_device(dev)) != current_session(item):
        log("o REC mudou enquanto pensava; descartando as ações")
        ui(dev, "think", {"state": "end", "run": trig["run"], "note": "descartado"})
        return
    if isinstance(res.get("memory"), dict):
        save_memory(dev, oba, res["memory"])
    rec = bool(item.get("rec"))
    apply_actions(dev, oba, sid, rec, trig["run"], res.get("actions") or [], all_rules, depth)
    ui(dev, "think", {"state": "end", "run": trig["run"], "note": str(res.get("note") or "")[:200]})


# ------------------------------------------------------------------ ações do agente

def clean_bubble(b) -> dict | None:
    if not isinstance(b, dict):
        return None
    text = str(b.get("text") or "").strip()[:TEXT_MAX]
    kind = b.get("kind") if b.get("kind") in ("text", "image", "qr") else "text"
    url = valid_url(b.get("url"))
    icon = resolve_icon(b.get("icon"), b.get("service"))
    if kind == "qr" and not url:
        kind = "image" if icon else "text"
    if kind == "image" and not icon:
        kind = "qr" if url else "text"
    if kind == "text" and not text:
        return None
    out = {"kind": kind, "text": text}
    if kind == "image":
        out["png"] = icon_png(icon)
        out["icon"] = icon
    elif kind == "qr":
        out["url"] = url
    return out


def clean_card(c) -> dict | None:
    if not isinstance(c, dict):
        return None
    out = {k: str(c[k])[:CARD_TEXT_MAX] for k in CARD_KEYS if isinstance(c.get(k), (str, int, float))}
    if "url" in out:
        out["url"] = valid_url(out["url"])
    return out or None


def clean_words(words) -> list[str]:
    out = []
    for w in words if isinstance(words, list) else []:
        w = norm(str(w))
        if 2 <= len(w) <= 40 and len(w.split()) <= 4 and w not in out:
            out.append(w)
    return out[:MAX_WORDS]


def clean_simple(a: dict) -> dict | None:
    """react, look, vibrate, leds e play, dentro dos limites da placa."""
    t = a.get("type")
    if t == "play":
        if not isinstance(a.get("sound"), str) or not SOUND_RE.fullmatch(a["sound"]):
            return None
        out = {"type": t, "sound": a["sound"]}
        if a.get("volume") is not None:
            out["volume"] = int(clamp(a["volume"], 1, 255, 128))
        return out
    if t == "react":
        return {"type": t, "do": a["do"]} if a.get("do") in REACT else None
    if t == "look":
        out = {"type": t, "x": clamp(a.get("x"), -1, 1, 0), "y": clamp(a.get("y"), -1, 1, 0)}
        if a.get("ms") is not None:
            out["ms"] = int(clamp(a["ms"], 100, 5000, 1500))
        return out
    if t == "vibrate":
        out = {"type": t}
        if a.get("ms") is not None:
            out["ms"] = int(clamp(a["ms"], 1, 2000, 200))
        if a.get("level") is not None:
            out["level"] = int(clamp(a["level"], 1, 255, 200))
        return out
    if t == "leds":
        if a.get("fx") not in LED_FX:
            return None
        out = {"type": t, "fx": a["fx"]}
        for k in ("color", "trail"):
            if isinstance(a.get(k), str) and COLOR_RE.fullmatch(a[k]):
                out[k] = a[k]
        for k, lo, hi in (("min", 0, 1), ("max", 0, 1), ("speed", 0, 1000), ("ms", 1, 10_000),
                          ("hold_ms", 100, 60_000)):
            v = clamp(a.get(k), lo, hi)
            if v is not None:
                out[k] = int(v) if k in ("ms", "hold_ms") else v
        return out
    return None


def clean_rule(a: dict, rec: bool) -> dict | None:
    on = a.get("on") or "speech"
    if on != "speech" and on not in RULE_EVENTS:
        return None
    rule = {"on": on, "ttl_s": int(clamp(a.get("ttl_s"), 60, ARM_TTL_MAX_S, ARM_TTL_S)), "do": []}
    if on == "speech":
        if not rec:
            return None  # a placa só aceita regra de fala com o REC ligado
        rule["words"] = clean_words(a.get("words"))
        if not rule["words"]:
            return None
    for d in (a.get("do") if isinstance(a.get("do"), list) else [])[:MAX_DO]:
        if not isinstance(d, dict) or d.get("type") not in RULE_DO:
            continue
        if d["type"] == "speak":
            b = clean_bubble(d.get("bubble"))
            if b:
                rule["do"].append({"type": "speak", "bubble": b})
        elif c := clean_simple(d):
            rule["do"].append(c)
    return rule if rule["do"] else None


def clean_id(v, prefix: str) -> str:
    return v if isinstance(v, str) and ID_RE.fullmatch(v) else prefix + secrets.token_hex(3)


def publish_ui(dev: str, oba: str, sid: str, a: dict):
    ch = a.get("channel")
    if not isinstance(ch, str) or not NAME_RE.fullmatch(ch) or ch == "oba":  # ui/oba é do roteador
        log("canal de ui recusado:", ch)
        return
    data = clean_urls(a.get("data") if isinstance(a.get("data"), dict) else {})
    msg = {**data, "oba": oba, "session": None if sid == NO_SESSION else sid}
    if len(json.dumps(msg, ensure_ascii=False)) > UI_MAX:
        log("ui grande demais:", ch)
        return
    retain = bool(a.get("retain"))
    ui(dev, ch, msg, retain)
    if retain:
        table.update_item(Key=dev_key(dev), UpdateExpression="ADD retained :c", ExpressionAttributeValues={":c": {ch}})


def apply_actions(dev: str, oba: str, sid: str, rec: bool, run_name: str, actions: list, all_rules: list,
                  depth: int):
    now = now_ms()
    armed = [it["sk"][2:] for it in all_rules if it["status"] == "armed" and int(it.get("until", 0)) > now]
    for a in actions[:MAX_ACTIONS]:
        t = a.get("type") if isinstance(a, dict) else None
        if t not in AGENT_TYPES:
            log("ação recusada:", t)
            continue
        if t == "ui":
            publish_ui(dev, oba, sid, a)
        elif t == "speak":
            b = clean_bubble(a.get("bubble"))
            if not b:
                continue
            rid, card = clean_id(a.get("id"), "s"), clean_card(a.get("card"))
            if send(dev, "speak", id=rid, bubble=b, **({"card": card} if card else {})):
                save_rule(dev, sid, rid, "spoken", card)
                log(f"fala {rid} {b['kind']}: {b['text'][:80]}")
        elif t == "arm":
            rule = clean_rule(a, rec)
            if not rule:
                log("regra recusada:", json.dumps(a, ensure_ascii=False)[:300])
                continue
            rid, card = clean_id(a.get("id"), "c"), clean_card(a.get("card"))
            if rid in armed:
                armed.remove(rid)  # a nova substitui a antiga
            while len(armed) >= MAX_ARMED:  # sai a mais antiga
                old = armed.pop(0)
                send(dev, "disarm", id=old)
                set_rule(dev, sid, old, "disarmed")
            if send(dev, "arm", id=rid, **rule, **({"card": card} if card else {})):
                save_rule(dev, sid, rid, "armed", card, rule)
                armed.append(rid)
                log(f"regra {rid} {rule['on']} {rule.get('words', '')}: {(card or {}).get('title')}")
        elif t == "disarm":
            rid = a.get("id")
            if rid in armed:
                send(dev, "disarm", id=rid)
                set_rule(dev, sid, rid, "disarmed")
                armed.remove(rid)
        elif t == "read":
            if depth >= READ_DEPTH:
                log("read recusado: muitas leituras seguidas")
                continue
            what = [w for w in a.get("what") or [] if w in READ_KEYS] if isinstance(a.get("what"), list) else []
            rid = "r" + secrets.token_hex(3)
            table.put_item(Item={**dev_key(dev, f"read#{rid}"), "oba": oba, "session": sid, "run": run_name,
                                 "depth": depth + 1, "expires": int(time.time()) + READ_WAIT_S})
            send(dev, "read", id=rid, **({"what": what} if what else {}))
        elif t == "rec":
            if a.get("on") is False:
                send(dev, "rec", on=False)
        elif t == "state":
            send(dev, "state")
        elif c := clean_simple(a):
            send(dev, t, **{k: v for k, v in c.items() if k != "type"})


# ------------------------------------------------------------------ o que a placa publica

def event_from(msg: dict, type_: str | None = None) -> dict:
    ev = {k: v for k, v in msg.items() if k not in ("v", "dev", "ch", "oba")}
    ev["type"] = type_ or msg.get("type")
    ts = msg.get("ts")
    ev["ts"] = int(ts) if isinstance(ts, (int, float)) and ts > 1e12 else now_ms()  # 0 antes do NTP
    return ev


def sheet_b64(spec: dict, rel) -> str | None:
    """Uma folha de sprites do registro em base64, se tiver até SHEET_MAX e o sha de "files" bater."""
    files = spec.get("files") if isinstance(spec.get("files"), dict) else {}
    if not isinstance(rel, str) or not PATH_RE.fullmatch(rel) or not isinstance(files.get(rel), str):
        return None
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=f"obas/{spec['id']}/{rel}")
    except ClientError as e:
        log("folha fora do registro:", spec["id"], rel, e.response["Error"]["Code"])
        return None
    try:
        if int(obj.get("ContentLength") or 0) > SHEET_MAX:
            return None
        data = obj["Body"].read(SHEET_MAX + 1)
    finally:
        obj["Body"].close()
    if len(data) > SHEET_MAX or hashlib.sha256(data).hexdigest() != files[rel].lower():
        return None
    return base64.b64encode(data).decode()


def numbers(v, n: int) -> bool:
    return (isinstance(v, list) and len(v) == n
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v))


def look_of(spec: dict) -> dict:
    """O desenho do Oba só com valores que a tela entende (o portal monta o SVG do rig com
    eles), como o look() do portal/api/api.py."""
    lk = spec.get("look") if isinstance(spec.get("look"), dict) else {}
    pal = lk.get("palette") if isinstance(lk.get("palette"), dict) else {}
    out = {"palette": {k: v for k, v in pal.items() if isinstance(v, str) and COLOR_RE.fullmatch(v)}}
    if lk.get("type") == "sprites":
        frames = lk.get("frames") if isinstance(lk.get("frames"), dict) else {}
        idle = frames.get("idle") if isinstance(frames.get("idle"), dict) else {}
        sp = {k: lk[k] for k in ("size", "origin") if numbers(lk.get(k), 2)}
        if numbers([lk.get("scale")], 1):
            sp["scale"] = lk["scale"]
        fps = idle.get("fps")
        sp["fps"] = fps if isinstance(fps, int) and not isinstance(fps, bool) else 6
        if sheet := sheet_b64(spec, idle.get("sheet")):
            sp["sheet"] = sheet
        out["sprites"] = sp
    else:
        eyes = lk.get("eyes") if isinstance(lk.get("eyes"), dict) else {}
        pts = lk.get("outline")
        if isinstance(pts, list) and 3 <= len(pts) <= OUTLINE_MAX and all(numbers(q, 2) for q in pts):
            out["outline"] = pts
        out["eyes"] = {k: v for k, v in eyes.items()
                       if k in ("left", "right", "y", "rx", "ry") and numbers([v], 1)}
    return out


def publish_oba(dev: str, active: dict, spec: dict | None):
    """ui/oba (retido): o Oba ativo para as telas desenharem."""
    spec = spec or {}
    data = {k: active.get(k) for k in ("id", "name", "version")}
    data.update(look_of(spec))
    words = spec.get("wake_words") if isinstance(spec.get("wake_words"), list) else []
    data["wake_words"] = [w for w in words if isinstance(w, str)][:16]
    sounds = spec.get("sounds") if isinstance(spec.get("sounds"), dict) else {}
    data["sounds"] = sorted(s for s in sounds if SOUND_RE.fullmatch(s))  # o portal oferece tocar
    ui(dev, "oba", data, retain=True)


def on_state(dev: str, msg: dict, ctx):
    if msg.get("online") is False:  # última vontade: a placa caiu
        table.update_item(Key=dev_key(dev), UpdateExpression="SET #on = :f, seen = :now",
                          ExpressionAttributeNames={"#on": "online"},
                          ExpressionAttributeValues={":f": False, ":now": now_ms()})
        log("placa offline", dev)
        return
    active = msg.get("active") if isinstance(msg.get("active"), dict) else {}
    oba = active.get("id") or msg.get("oba")
    state = {k: msg.get(k) for k in ("fw", "rec", "battery", "active", "obas", "caps")}
    spec, same = board_spec(load_spec(oba), active)
    # Com sha256, o ui/oba sai de novo quando o Oba muda sem mudar a versão; sem bater com o
    # registro (p.ex. a placa terminou de instalar antes da CLI subir o Oba), sai de novo no
    # próximo state
    tag = f"{UI_OBA_FORMAT}:{oba}@{active.get('version')}"
    if isinstance(active.get("sha256"), str):
        tag += f"#{active['sha256'][:12]}" + ("" if same else "?")
    old = table.update_item(
        Key=dev_key(dev), UpdateExpression="SET #on = :t, #st = :st, oba = :o, uiOba = :tag, seen = :now",
        ExpressionAttributeNames={"#on": "online", "#st": "state"},
        ExpressionAttributeValues={":t": True, ":st": json.dumps(state, ensure_ascii=False), ":o": oba,
                                   ":tag": tag, ":now": now_ms()},
        ReturnValues="ALL_OLD",
    ).get("Attributes", {})
    if old.get("uiOba") != tag:
        if not same:
            log("o Oba da placa não bate com o registro:", mismatch(spec, active))
        publish_oba(dev, active, spec)
    if old.get("oba") and old["oba"] != oba:
        log("Oba trocou:", old["oba"], "->", oba)
        ev, sid = {"type": "oba.changed", "from": old["oba"], "to": oba, "ts": now_ms()}, current_session(old)
        store_event(dev, sid, ev)
        dispatch(dev, oba, ev, sid, ctx)
    if msg.get("rec"):
        if not old.get("rec"):
            open_session(dev, oba, ctx)
    elif sid := end_session(dev):
        ev = {"type": "rec.off", "ts": now_ms()}
        store_event(dev, sid, ev)
        dispatch(dev, oba, ev, sid, ctx)


def on_transcript(dev: str, msg: dict, ctx):
    text = str(msg.get("text") or "").strip()
    if msg.get("partial") is not False or not text:
        return
    item = get_device(dev)
    sid = current_session(item)
    if sid == NO_SESSION:  # a frase chegou antes do state com o REC
        sid = open_session(dev, msg.get("oba"), ctx)
    ev = event_from({**msg, "text": text[:2000]}, "transcript.final")
    ev.pop("partial", None)
    store_event(dev, sid, ev)
    dispatch(dev, msg.get("oba"), ev, sid, ctx)


def on_evt(dev: str, msg: dict, ctx):
    ev = event_from(msg)
    if ev["type"] not in DEVICE_EVENTS:
        return
    sid = current_session(get_device(dev))
    if ev["type"] == "rule.fired" and isinstance(ev.get("id"), str):
        set_rule(dev, sid, ev["id"], "fired")
        log("regra disparou:", ev["id"], ev.get("trigger") or ev.get("on"))
    store_event(dev, sid, ev)
    dispatch(dev, msg.get("oba"), ev, sid, ctx)


def on_reply(dev: str, msg: dict, ctx):
    rid, re_ = msg.get("id"), msg.get("re")
    if not isinstance(rid, str):
        return
    if not msg.get("ok"):
        log("a placa recusou", re_, rid, msg.get("error"))
        if re_ == "arm":
            set_rule(dev, current_session(get_device(dev)), rid, "rejected")
    if re_ != "read":
        return
    key = dev_key(dev, f"read#{rid}")
    try:
        pend = table.delete_item(Key=key, ConditionExpression="attribute_exists(pk)", ReturnValues="ALL_OLD")
    except ClientError as e:
        if conditional_failed(e):
            return  # não foi o agente que pediu (ou já tratou)
        raise
    pend = pend["Attributes"]
    spec = load_spec(pend["oba"])
    if not spec:
        return
    ev = event_from(msg, "reply")
    store_event(dev, pend["session"], ev)
    trig = {"on": "reply", "batch": 1, "debounce_ms": 0, "cooldown_ms": 0, "run": pend["run"]}
    run(dev, spec, trig, pend["session"], events=[ev], depth=int(pend["depth"]))


HANDLERS = {"state": on_state, "evt": on_evt, "transcript": on_transcript, "reply": on_reply}


def handler(event, context):
    dev, ch = event.get("dev"), event.get("ch")
    if not isinstance(dev, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", dev) or ch not in HANDLERS:
        return
    if event.get("v") != 1:
        log("versão do protocolo desconhecida:", event.get("v"))
        return
    HANDLERS[ch](dev, event, context)
