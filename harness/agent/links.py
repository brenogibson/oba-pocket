"""Confere os links das cartas contra o que as ferramentas devolveram.

O modelo às vezes sugere um blog sem copiar a URL (ou inventa o título). Blog,
doc ou demo sem link verificado não vai para o Oba: o balão diria "olha
esse post" sem QR para mostrar.
"""

import json
import re
import unicodedata

NEEDS_URL = ("blog", "doc", "demo")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _walk(obj, out: dict):
    if isinstance(obj, dict):
        url = obj.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            out.setdefault(url, obj.get("title") or "")
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)
    elif isinstance(obj, str) and obj[:1] in "{[":
        try:
            _walk(json.loads(obj), out)
        except ValueError:
            pass


def found_links(messages: list[dict]) -> dict[str, str]:
    """URL -> título de tudo que as ferramentas devolveram nesta chamada."""
    out: dict[str, str] = {}
    for m in messages:
        for block in m.get("content", []):
            result = block.get("toolResult") if isinstance(block, dict) else None
            if not result:
                continue
            for c in result.get("content", []):
                text = c.get("text") if isinstance(c, dict) else None
                if text:
                    _walk(text, out)
                    for url in re.findall(r"https://[^\s\"'<>)\]]+", text):  # resultado em texto livre
                        out.setdefault(url, "")
                elif isinstance(c, dict) and "json" in c:
                    _walk(c["json"], out)
    return out


def _by_title(title: str, links: dict[str, str]) -> str | None:
    """A URL do resultado cujo título mais parece com o da carta."""
    want = set(_norm(title).split())
    if not want:
        return None
    best, score = None, 0.0
    for url, t in links.items():
        have = set(_norm(t).split())
        if have:
            s = len(want & have) / len(want | have)
            if s > score:
                best, score = url, s
    return best if score >= 0.5 else None


def check_url(url: str | None, title: str, links: dict[str, str]) -> str | None:
    if url and url.rstrip("/") in {u.rstrip("/") for u in links}:
        return url
    return _by_title(title, links)


def fix_cards(cards: list[dict], links: dict[str, str], log) -> list[dict]:
    out = []
    for c in cards:
        url = check_url(c.get("url"), c.get("title", ""), links)
        if url != c.get("url"):
            log.info("link da carta %s: %s -> %s", c.get("kind"), c.get("url"), url)  # sem o título
        c["url"] = url
        if c.get("kind") in NEEDS_URL and not url:
            log.info("carta %s sem link verificado descartada", c.get("kind"))
            continue
        out.append(c)
    return out
