"""O que o hook, o daemon, o install.py e o sim.py dividem: a pasta, a configuração e o socket."""
import json
import os
import re
import socket
import sys
import time

VERSION = (1, 0, 1)         # a mesma do plugin.json: um hook mais novo troca o daemon
CONFIG, SOCK, LOCK, LOG = "config.json", "ponte.sock", "ponte.lock", "ponte.log"
INSTALL = "ponte.install"   # trava do install.py: enquanto ela está presa, nenhum daemon sobe
NEWEST = "ponte.lib"        # a versão e a pasta da lib mais nova que já rodou aqui
FIELDS = ("endpoint", "prefix", "device", "thing", "name", "cert", "key", "ca")
FILES = ("cert", "key", "ca")
TOPIC_PART = re.compile(r"[A-Za-z0-9_.:-]{1,128}")    # prefix, device e thing viram pedaços de tópico
HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
NAME_MAX = 20
LINE_MAX = 32 * 1024 * 1024     # um Write grande vem inteiro no tool_input


def home():
    """A pasta da ponte, ou None se o OBA_PONTE_HOME não for um caminho absoluto (o hook,
    o daemon e o install.py rodam em pastas diferentes)."""
    h = os.path.expanduser(os.environ.get("OBA_PONTE_HOME") or "~/.oba-ponte")
    return h if os.path.isabs(h) else None


def path(h, name):
    return os.path.join(h, name)


def newest(h):
    """(versão, pasta da lib) do daemon mais novo que já rodou nesta pasta, ou None."""
    try:
        with open(path(h, NEWEST), encoding="utf-8") as f:
            d = json.load(f)
        ver, lib = tuple(d["ver"]), d["lib"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (ver and all(isinstance(x, int) for x in ver) and isinstance(lib, str)
            and os.path.isabs(lib) and os.path.isfile(os.path.join(lib, "daemon.py"))):
        return None
    return ver, lib


def note_newest(h, ver, lib):
    """Guarda a lib, se for mais nova que a guardada: o hook de um plugin velho sobe o
    daemon dela, e o velho e o novo não se revezam (docs/ponte.md, Atualizar)."""
    cur = newest(h)
    if cur and cur[0] >= tuple(ver):
        return
    tmp = path(h, NEWEST + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(dumps({"ver": list(ver), "lib": lib}))
    os.replace(tmp, path(h, NEWEST))


def load_config(h):
    """Lê e confere o config.json da pasta. Os arquivos ficam com o caminho completo."""
    with open(path(h, CONFIG), encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("o config.json não é um objeto")
    miss = [k for k in FIELDS if not isinstance(cfg.get(k), str) or not cfg[k].strip()]
    if miss:
        raise ValueError("faltam campos no config.json: " + ", ".join(miss))
    for k in ("prefix", "device", "thing"):
        if not TOPIC_PART.fullmatch(cfg[k]):
            raise ValueError(f"{k} inválido: só letras, números, _ . : e -")
    if not HOST.fullmatch(cfg["endpoint"]):
        raise ValueError("endpoint inválido")
    port = cfg.get("port", 8883)
    if port not in (8883, 443):
        raise ValueError("port tem que ser 8883 ou 443")
    out = dict(cfg, port=port, name=cfg["name"].strip()[:NAME_MAX])
    for k in FILES:
        rel = cfg[k]
        if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
            raise ValueError(f"{k}: o caminho tem que ser relativo à pasta da ponte")
        if not os.path.isfile(path(h, rel)):
            raise ValueError(f"não achei o arquivo de {k} ({rel})")
        out[k] = path(h, rel)
    return out


def topics(cfg):
    """(publica, respostas, state)."""
    base = f"{cfg['prefix']}/{cfg['device']}"
    ext = f"{base}/ext/{cfg['thing']}"
    return ext, ext + "/re", base + "/state"


def now_ms():
    return int(time.time() * 1000)


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def read_line(c, limit=LINE_MAX):
    buf = bytearray()
    while b"\n" not in buf:
        data = c.recv(65536)
        if not data:
            break
        buf += data
        if len(buf) > limit:
            raise ValueError("mensagem grande demais")
    return bytes(buf).split(b"\n", 1)[0]


def dial(h, timeout=1.0):
    """Conecta no socket do daemon, ou None."""
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout)
    try:
        c.connect(path(h, SOCK))
        return c
    except OSError:
        c.close()
        return None


def request(h, msg, timeout=3.0):
    """Manda um comando de controle ({"ctl": "ping" | "quit"}) e devolve a resposta, ou None."""
    c = dial(h, timeout)
    if not c:
        return None
    try:
        c.sendall(dumps(msg).encode() + b"\n")
        line = read_line(c)
        return json.loads(line) if line else None
    except (OSError, ValueError):
        return None
    finally:
        c.close()
