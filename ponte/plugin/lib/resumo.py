"""Resumo, ofuscação e perigo dos pedidos de permissão (docs/ponte.md).

summarize() monta o título e o corpo do ask a partir do tool_input, já com os segredos
trocados (redact) e nos tamanhos do contrato, e diz se o corpo ficou incompleto. danger()
diz se aprovar deve exigir segurar o botão. As listas DANGER_BASH, DANGER_PATHS e REDACT são para crescer.
"""
import json
import os
import re

TITLE_MAX = 60
BODY_MAX = 1500             # bytes em UTF-8
LABEL_MAX = 48
TOOL_MAX = 40
SCAN_MAX = 64 * 1024        # ofusca só o começo de entradas enormes (o corpo sai com 1500 bytes)
INCOMPLETO = "(incompleto: confira no terminal)"   # última linha do corpo que não mostra tudo
LINES = 8                   # linhas de cada lado nos trechos de Edit e Write
EDITS = 3                   # edições mostradas no MultiEdit

TITLES = {
    "Bash": "Rodar comando",
    "Edit": "Editar arquivo",
    "MultiEdit": "Editar arquivo",
    "Write": "Gravar arquivo",
    "NotebookEdit": "Editar notebook",
    "WebFetch": "Abrir página",
    "WebSearch": "Buscar na web",
    "Task": "Chamar agente",
    "Agent": "Chamar agente",
}
PATH_TOOLS = {"Edit": "file_path", "MultiEdit": "file_path", "Write": "file_path",
              "NotebookEdit": "notebook_path"}


# ------------------------------------------------------------------ ofuscação

SECRET = "<segredo>"
# O valor depois de token=, --password etc. Nunca esconde o que roda no shell: entre aspas
# duplas ou sem aspas, $( e crase ficam de fora (aparecem na placa); aspas simples não expandem
_DQ = r"\"(?![^\"]*(?:\$\(|`))[^\"]*\""
_BARE = r"(?![^\s\"',;&|]*(?:\$\(|`))[^\s\"',;&|]+"
_VALUE = rf"({_DQ}|'[^']*'|{_BARE})"
_KEYS = r"(?:secret|password|passwd|token|api[_-]?key)"
# O nome em volta da palavra-chave tem limite e começa no início do nome: sem isso, uma
# linha longa de letras e números (um hex de firmware) leva segundos para passar
_NAME = r"(?<![\w.-])[\w.-]{0,64}?" + _KEYS + r"[\w.-]{0,64}"


def _b64(m):
    """Base64 longo de verdade tem dígito, letra e pedaços compridos (um caminho não)."""
    t = m.group(0)
    if (any(c.isdigit() for c in t) and re.search(r"[A-Za-z]", t)
            and max(len(p) for p in re.split(r"[/+_=-]", t)) >= 16):
        return "<base64>"
    return t


_ACCOUNT = (re.compile(r"(?<!\d)\d{12}(?!\d)"), "<conta>")     # id de conta AWS
REDACT = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "<chave privada>"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "<chave AWS>"),
    (re.compile(r"(?i)(aws_secret_access_key[\"']?\s*[=:]\s*)" + _VALUE), r"\1" + SECRET),
    (re.compile(r"(?i)(authorization[\"']?\s*:\s*[\"']?(?:bearer|basic)\s+)(?![^\s\"',;]*(?:\$\(|`))[^\s\"',;]+"),
     r"\1" + SECRET),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1" + SECRET),
    (re.compile(r"(?i)(" + _NAME + r"[\"']?\s*[=:]\s*)" + _VALUE), r"\1" + SECRET),
    (re.compile(r"(?i)(--?" + _KEYS + rf"[\w-]{{0,64}}\s+)({_DQ}|'[^']*'|(?!-){_BARE})"), r"\1" + SECRET),
    (re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])"), "<hex>"),
    (re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/=_-])"), _b64),
    _ACCOUNT,
]


def redact(text):
    for rx, rep in REDACT:
        text = rx.sub(rep, text)
    return text


# ------------------------------------------------------------------ tamanhos

def cut(s, n):
    """Até n caracteres."""
    return s if len(s) <= n else s[:n - 1] + "…"


def cut_bytes(s, n):
    """Até n bytes em UTF-8, sem quebrar um caractere."""
    b = s.encode("utf-8")
    if len(b) <= n:
        return s
    return b[:n - 3].decode("utf-8", "ignore") + "…"


def compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


# ------------------------------------------------------------------ resumo

def rel(p, cwd):
    """Caminho relativo ao cwd; fora dele, com ~ no lugar da home."""
    if not isinstance(p, str) or not p:
        return "?"
    full = os.path.normpath(os.path.join(cwd or "/", os.path.expanduser(p)))
    if cwd:
        c = os.path.normpath(cwd)
        if full == c or full.startswith(c.rstrip(os.sep) + os.sep):
            return os.path.relpath(full, c)
    h = os.path.expanduser("~")
    if full == h or full.startswith(h + os.sep):
        return "~" + full[len(h):]
    return full


def snippet(text, sign, n=LINES):
    """-> (linhas, cortou)"""
    lines = str(text or "").splitlines() or [""]
    out = [f"{sign} {line}" for line in lines[:n]]
    if len(lines) > n:
        out.append(f"{sign} … (+{len(lines) - n} linhas)")
    return out, len(lines) > n


def _mcp(tool):
    server, _, name = tool[5:].partition("__")
    return f"MCP {server}: {name}" if name else f"MCP {server}"


def summarize(tool, inp, cwd):
    """-> (title, body, partial) já ofuscados e cortados. partial: o corpo não mostra a
    entrada inteira (cortado, ou o comando teve algo além de um id de conta ofuscado).
    Aí o corpo termina com INCOMPLETO, e a ponte pede a segurada para aprovar."""
    tool = str(tool or "?")
    if not isinstance(inp, dict):
        inp = {"input": inp}
    s = lambda k: str(inp.get(k) or "")
    title = TITLES.get(tool) or (_mcp(tool) if tool.startswith("mcp__") else f"Usar {tool}")
    partial = redacted = False
    if tool == "Bash":
        cmd = s("command")[:SCAN_MAX]
        red = redact(cmd)
        partial = red != _ACCOUNT[0].sub(_ACCOUNT[1], cmd) or len(s("command")) > SCAN_MAX
        red = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", red))  # espaço em série não empurra o fim do corte
        body = "$ " + red + ("\n\n" + redact(s("description")[:SCAN_MAX]) if s("description") else "")
        redacted = True
    elif tool in ("Edit", "MultiEdit"):
        edits = inp.get("edits") if tool == "MultiEdit" else [inp]
        edits = [e for e in edits if isinstance(e, dict)] if isinstance(edits, list) else []
        lines = [rel(inp.get("file_path"), cwd)]
        for e in edits[:EDITS]:
            for text, sign in ((e.get("old_string"), "-"), (e.get("new_string"), "+")):
                part, cutted = snippet(text, sign)
                lines += part
                partial = partial or cutted
        if len(edits) > EDITS:
            partial = True
            lines.append(f"(+{len(edits) - EDITS} edições)")
        body = "\n".join(lines)
    elif tool == "Write":
        text = s("content").splitlines()
        partial = len(text) > LINES * 2
        body = "\n".join([rel(inp.get("file_path"), cwd), ""] + text[:LINES * 2]
                         + ([f"… (+{len(text) - LINES * 2} linhas)"] if len(text) > LINES * 2 else []))
    elif tool == "NotebookEdit":
        body = rel(inp.get("notebook_path"), cwd)
    elif tool == "WebFetch":
        body = s("url")
    elif tool == "WebSearch":
        body = s("query")
    elif tool in ("Task", "Agent"):
        kind = s("subagent_type")
        body = s("description") + (f" ({kind})" if kind else "")
    else:
        body = compact(inp)
    title = cut(redact(title[:SCAN_MAX]), TITLE_MAX)
    partial = partial or len(body) > SCAN_MAX
    body = body[:SCAN_MAX] if redacted else redact(body[:SCAN_MAX])
    if partial or len(body.encode("utf-8")) > BODY_MAX:
        body = cut_bytes(body, BODY_MAX - len(INCOMPLETO.encode("utf-8")) - 1) + "\n" + INCOMPLETO
        partial = True
    return title, body, partial


# ------------------------------------------------------------------ perigo

DANGER_BASH = [re.compile(p, re.I) for p in (
    r"\brm\s+(?:-\S+\s+)*(?:-[a-z]*r[a-z]*|--recursive)\b",       # rm -r, -rf, -fr, -R
    r"\bsudo\b",
    r"\bgit\s+push\b[^;&|\n]*\s(?:-f|--force(?:-with-lease)?)\b",
    r"\bgit\s+push\b[^;&|\n]*\s\+[\w/.-]",                         # git push origin +main
    r"\bgit\s+reset\s+(?:\S+\s+)*--hard\b",
    r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b",
    r"\bchmod\s+(?:\S+\s+)*-R\s+(?:\S+\s+)*0?777\b|\bchmod\s+0?777\s+-R\b",
    r"\bmkfs(?:\.\w+)?\b",
    r"\bdd\b[^;&|\n]*\bof=",
    r"\baws\s+[^;&|\n]*\b(?:delete|terminate|remove|put-bucket-policy)",
    r"\baws\s+s3\s+(?:rm|rb)\b",
    r"\bkubectl\s+[^;&|\n]*\bdelete\b",
    r"\bterraform\s+(?:\S+\s+)*destroy\b",
    r"\bdrop\s+(?:table|database)\b",
)]
DANGER_PATHS = [re.compile(p) for p in (
    r"(^|/)\.ssh(/|$)",
    r"(^|/)\.env(\.[^/]*)?$",
    r"^/(private/)?etc(/|$)",
)]


def _inside(p, root):
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def path_danger(p, cwd):
    """Fora do cwd, ou num lugar sensível."""
    if not isinstance(p, str) or not p:
        return True
    full = os.path.normpath(os.path.join(cwd or "/", os.path.expanduser(p)))
    real = os.path.realpath(full)
    if any(rx.search(x) for rx in DANGER_PATHS for x in (full, real)):
        return True
    if not cwd:
        return True
    return not (_inside(full, os.path.normpath(cwd)) and _inside(real, os.path.realpath(cwd)))


def danger(tool, inp, cwd):
    if not isinstance(inp, dict):
        return False
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        return any(rx.search(cmd) for rx in DANGER_BASH)
    if tool in PATH_TOOLS:
        return path_danger(inp.get(PATH_TOOLS[tool]), cwd)
    return False


def label(name, cwd, tool=None):
    """<name> · <pasta> · <ferramenta>, até 48 caracteres."""
    folder = os.path.basename(os.path.normpath(cwd)) if cwd else ""
    parts = [p for p in (name, folder, tool) if p]
    return " · ".join(parts)[:LABEL_MAX]
