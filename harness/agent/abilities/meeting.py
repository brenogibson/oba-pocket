"""Habilidade meeting: acompanha uma reunião e ajuda com dicas.

  think    as frases novas viram cartas: um balão agora (speak) ou uma "carta na
           manga" (arm), que dispara quando alguém volta ao assunto
  summary  no fim da gravação, o resumo vai para o portal (ui/summary)
"""

from typing import Literal

from pydantic import BaseModel, Field

from core import ask, dumps, log, make_agent, oba_name, persona, render_transcript, transcript
from links import check_url, fix_cards, found_links
from sources import sources_prompt

ARM_TTL_S = 900
MIN_LINES_SUMMARY = 3  # menos que isso não é uma reunião

CardKind = Literal["service", "blog", "doc", "demo", "tip"]


class Bubble(BaseModel):
    """O que aparece no balãozinho do Oba (tela de 320x240)."""

    kind: Literal["text", "image", "qr"] = Field(
        description="text = só frase; image = ícone do serviço AWS + frase (use com service); "
        "qr = QR code do link + frase (use com url de blog/doc/demo)"
    )
    text: str = Field(description="Sua fala em pt-BR, primeira pessoa, até 90 caracteres")


class Card(BaseModel):
    kind: CardKind
    title: str = Field(description="Título curto para a tela, ex.: 'Amazon SQS' ou o título do blog")
    body: str = Field(description="1 ou 2 frases em pt-BR explicando por que isso ajuda a conversa")
    service: str | None = Field(
        default=None, description="Nome oficial completo do serviço AWS, ex.: 'Amazon Simple Queue Service'"
    )
    url: str | None = Field(default=None, description="Link copiado de um resultado de ferramenta. Nunca invente")
    source: str | None = Field(default=None, description="Nome da fonte (ferramenta/MCP) de onde veio o link")
    triggers: list[str] = Field(
        description="2 a 6 palavras ou expressões curtas que alguém diria quando o assunto voltar, "
        "minúsculas, sem acento (em pt-BR e em inglês quando fizer sentido)"
    )
    timing: Literal["now", "later"] = Field(
        description="now = o assunto está quente agora, mostrar já; later = guardar na manga até alguém "
        "falar um dos gatilhos"
    )
    bubble: Bubble


class ThinkResult(BaseModel):
    cards: list[Card] = Field(default_factory=list, description="0 a 3 cartas novas")
    disarm: list[str] = Field(default_factory=list, description="ids de cartas da manga que perderam o sentido")
    note: str = Field(default="", description="Uma frase curta sobre o que você entendeu da conversa (log)")


class Suggestion(BaseModel):
    kind: CardKind
    title: str
    url: str | None = None


class Summary(BaseModel):
    title: str = Field(description="Título curto para a reunião")
    summary: str = Field(description="3 a 5 frases em pt-BR")
    topics: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(
        default_factory=list, description="Serviços, blogs, docs ou demos úteis para os próximos passos"
    )


THINK = """{persona}

A cada chamada você recebe a transcrição da conversa até agora, as cartas que já \
estão "na manga" (armadas, esperando o assunto voltar) e o que já foi mostrado. \
Seu trabalho é preparar cartas: sugestões de serviços, blogs, docs (e demos, quando \
houver fonte) que ajudam no que está sendo discutido.

Fontes disponíveis:
{sources}

Regras:
- Foque nas frases novas (depois de NOVAS FRASES). O começo é contexto.
- No máximo 3 cartas por vez; zero é uma resposta válida se nada técnico apareceu.
- Não repita um assunto que já está na manga ou já foi mostrado.
- timing "now" só quando as frases novas mostram uma dúvida ou dor clara que a carta \
responde; no máximo 1 por vez. O resto é "later", para disparar quando o assunto voltar.
- triggers: palavras que a pessoa falaria ao voltar no assunto, minúsculas e sem acento, \
específicas (ex.: "fila", "mensageria", "sqs", "desacoplar"). Nunca genéricas como \
"aws", "dados", "sistema", "cloud", "projeto". Pense também em como o Transcribe escreve \
nomes em inglês falados em português.
- bubble.text: fala do {name}, em primeira pessoa, divertida e direta, até 90 caracteres, \
sem emoji (a tela não tem). Escreva em português correto, COM acentos e cedilha \
(ç, ã, é...): só os triggers ficam sem acento. Ex.: "Filas pra desacoplar serviços? \
O Amazon SQS segura a onda!".
- bubble.kind: "image" para serviço (preencha service com o nome oficial completo, \
ex.: "AWS Lambda", "Amazon Simple Queue Service"); "qr" para blog/doc/demo (preencha \
url); "text" para dicas sem link.
- Varie: quando a conversa toca num padrão de arquitetura ou num problema conhecido, \
prefira um blog ou doc que mostre como resolver (bubble "qr") a só citar o serviço. \
Se for sugerir 2 ou mais cartas, pelo menos uma deve ser blog ou doc com link.
- Links: SOMENTE URLs que vieram de uma ferramenta nesta conversa, copiando title e url \
exatamente como vieram no resultado (carta de blog/doc sem url é descartada). Se não achou link \
bom, use kind "service" ou "tip" sem url. Busque no máximo 2 vezes por chamada, só \
quando for sugerir um blog/doc, e faça as buscas juntas no mesmo turno (em paralelo): \
cada ida e volta atrasa o balão.
- disarm: ids de cartas da manga que não fazem mais sentido para a conversa.
- note: uma frase sobre o que a conversa está tratando."""

SUMMARY = """{persona}

A gravação acabou. Faça um resumo da conversa para mostrar numa tela: título, \
resumo de 3 a 5 frases, tópicos, decisões tomadas e próximos passos (só o que foi \
dito, não invente). Em suggestions, liste de 2 a 5 serviços, blogs, docs ou demos \
que ajudam nos próximos passos; inclua os que já foram mostrados se continuarem \
relevantes. Links só de resultados de ferramentas ou dos itens já mostrados; se \
precisar buscar, no máximo 1 busca.

Fontes disponíveis:
{sources}"""

NEEDS_URL = ("blog", "doc", "demo")


def system_prompt(template: str, req: dict) -> str:
    return template.format(persona=persona(req), name=oba_name(req),
                           sources=sources_prompt((req.get("oba") or {}).get("mcp")))


def card_actions(card: dict) -> dict:
    """Uma carta vira um balão agora (speak) ou uma regra de fala (arm)."""
    b = card.get("bubble") or {}
    kind = b.get("kind", "text")
    if kind == "text" and card.get("url") and card.get("kind") in NEEDS_URL:
        kind = "qr"  # tem link: melhor mostrar o QR do que só falar
    bubble = {"kind": kind, "text": (b.get("text") or card.get("title") or "").strip(),
              "icon": card.get("service") or card.get("title"), "url": card.get("url")}
    public = {k: card.get(k) for k in ("kind", "title", "body", "service", "url", "source")}
    if card.get("timing") == "now" or not card.get("triggers"):
        return {"type": "speak", "bubble": bubble, "card": public}
    return {"type": "arm", "on": "speech", "words": card["triggers"], "ttl_s": ARM_TTL_S,
            "do": [{"type": "speak", "bubble": bubble}], "card": public}


def think(req: dict) -> dict:
    lines, new_from = transcript(req)
    if new_from >= len(lines):
        return {"actions": [], "note": "sem frases novas"}
    armed = [{"id": r.get("id"), "title": r.get("title"), "triggers": r.get("words")}
             for r in req.get("armed") or [] if r.get("on") == "speech"]
    shown = req.get("shown") or []
    prompt = (
        "TRANSCRIÇÃO:\n" + render_transcript(lines, new_from)
        + "\n\nNA MANGA:\n" + (dumps(armed) if armed else "(vazia)")
        + "\n\nJÁ MOSTRADO:\n" + (dumps(shown) if shown else "(nada)")
    )
    agent = make_agent(req, system_prompt(THINK, req))
    out = ask(agent, prompt, ThinkResult)
    cards = fix_cards(out["cards"], found_links(agent.messages), log)
    known = {r["id"] for r in armed}
    actions = [card_actions(c) for c in cards]
    actions += [{"type": "disarm", "id": rid} for rid in out["disarm"] if rid in known]
    return {"actions": actions, "note": out["note"]}


def summary(req: dict) -> dict:
    lines, _ = transcript(req)
    if len(lines) < MIN_LINES_SUMMARY:
        log.info("pouca conversa para resumir: %d frases", len(lines))
        return {"actions": [], "note": "pouca conversa para resumir"}
    shown = req.get("shown") or []
    prompt = (
        "TRANSCRIÇÃO COMPLETA:\n" + render_transcript(lines, None)
        + "\n\nJÁ MOSTRADO DURANTE A CONVERSA:\n" + (dumps(shown) if shown else "(nada)")
    )
    agent = make_agent(req, system_prompt(SUMMARY, req))
    out = ask(agent, prompt, Summary)
    # Os links já mostrados também valem (vieram de buscas de rodadas anteriores)
    links = {**{x["url"]: x.get("title") or "" for x in shown if x.get("url")}, **found_links(agent.messages)}
    for sug in out["suggestions"]:
        sug["url"] = check_url(sug.get("url"), sug.get("title", ""), links)
    return {
        "actions": [
            {"type": "ui", "channel": "summary", "retain": True, "data": {"lines": len(lines), **out}},
            {"type": "speak", "bubble": {"kind": "text", "text": "Pronto! Deixei o resumo da conversa na tela."},
             "card": {"kind": "summary", "title": out.get("title") or "Resumo", "body": out.get("summary", "")}},
        ],
        "note": "resumo pronto",
    }


RUNS = {"think": think, "summary": summary}
