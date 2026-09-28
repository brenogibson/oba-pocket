"""O run sem habilidade: o Oba responde com a persona e escolhe as ações.

Ele vê o que acordou o agente (events), o que aconteceu antes (history), o
estado da placa e a memória, e devolve comandos do protocolo (docs/protocol.md).
O roteador confere e corta tudo antes de mandar para a placa.
"""

from typing import Literal

from pydantic import BaseModel, Field

from core import ask, dumps, make_agent, oba_name, persona
from sources import sources_prompt

HISTORY_EVENTS = 40

Mood = Literal["idle", "happy", "scared", "shy", "dizzy", "sleepy", "glance", "turn"]
LedFx = Literal["off", "solid", "breathe", "rainbow", "strobe", "chase"]


class Say(BaseModel):
    kind: Literal["text", "image", "qr"] = Field(
        default="text", description="text = só frase; image = ícone de um serviço AWS (icon) + frase; "
        "qr = QR code de url + frase")
    text: str = Field(description="A fala, em pt-BR, até 90 caracteres, sem emoji")
    icon: str | None = Field(default=None, description="Nome oficial de um serviço AWS, para kind image")
    url: str | None = Field(default=None, description="Link de um resultado de ferramenta, para kind qr")


class Step(BaseModel):
    type: Literal["speak", "react", "look", "vibrate", "leds", "play"]
    bubble: Say | None = Field(default=None, description="speak: o balão")
    mood: Mood | None = Field(default=None, description="react: um humor, glance (olhadinha) ou turn (virar)")
    x: float | None = Field(default=None, description="look: -1 (esquerda) a 1 (direita)")
    y: float | None = Field(default=None, description="look: -1 (cima) a 1 (baixo)")
    ms: int | None = Field(default=None, description="look: até 5000; vibrate: até 2000")
    level: int | None = Field(default=None, description="vibrate: força de 1 a 255")
    fx: LedFx | None = Field(default=None, description="leds: efeito")
    color: str | None = Field(default=None, description="leds: cor #RRGGBB")
    hold_ms: int | None = Field(default=None, description="leds: quanto dura, até 60000")
    sound: str | None = Field(default=None, description="play: nome de um som do Oba (SONS DO OBA)")
    volume: int | None = Field(default=None, description="play: volume de 1 a 255 (padrão 128)")


class Action(Step):
    type: Literal["speak", "react", "look", "vibrate", "leds", "play", "arm", "disarm", "read", "rec"]
    id: str | None = Field(default=None, description="disarm: id da regra")
    on: str | None = Field(default=None, description="arm: speech (palavras na conversa, só com REC) ou um evento "
                           "da placa (touch.tap, button.a, button.c, sound.loud, imu.shake, imu.tap)")
    words: list[str] | None = Field(default=None, description="arm speech: palavras que disparam")
    ttl_s: int | None = Field(default=None, description="arm: validade em segundos (padrão 900)")
    then: list[Step] | None = Field(default=None, description="arm: de 1 a 4 passos quando disparar")
    what: list[Literal["battery", "imu", "mic", "mood", "rec", "time", "oba", "wifi"]] | None = Field(
        default=None, description="read: sensores para ler; a resposta te acorda de novo (run igual, on reply)")


class Result(BaseModel):
    actions: list[Action] = Field(default_factory=list, description="0 a 4 ações; nenhuma também vale")
    memory: dict[str, str] | None = Field(
        default=None, description="Se quiser lembrar algo nas próximas vezes, a memória inteira nova (curta)")
    note: str = Field(default="", description="Uma frase curta sobre o que você fez (log)")


SYSTEM = """{persona}

Você é o cérebro do {name}, um Oba: um bichinho que mora numa placa M5Stack Core2 \
(tela de 320x240). Quem roda na placa é o corpo; você roda fora e fala com ele por \
comandos. Você acorda quando acontece algo que o {name} pediu para ouvir e decide \
o que fazer: falar num balão, reagir com um humor, olhar para um lado, vibrar, mudar \
os LEDs, tocar um dos seus sons, armar uma regra (quando X acontecer, faça Y, direto na placa), ler um sensor \
ou desligar a gravação. Nada também é uma resposta válida.

Recursos da placa: {caps}

Regras:
- Aja como o personagem da persona, curto e simpático. O balão é pequeno: até 90 \
caracteres, sem emoji, em português correto com acentos.
- Não repita o que já fez há pouco (veja o histórico).
- Links só de resultados de ferramentas. Ferramentas disponíveis:
{sources}"""


def generic(req: dict) -> dict:
    state = (req.get("device") or {}).get("state") or {}
    caps = ", ".join(state.get("caps") or []) or "(desconhecidos)"
    hist = (req.get("history") or [])[-HISTORY_EVENTS:]
    prompt = (
        f"RODADA: {req.get('run')} (gatilho {dumps(req.get('trigger'))})\n\n"
        + "O QUE TE ACORDOU:\n" + dumps(req.get("events") or []) + "\n\n"
        + "HISTÓRICO RECENTE:\n" + ("\n".join(dumps(e) for e in hist) or "(nada)") + "\n\n"
        + "ESTADO DA PLACA:\n" + dumps(state) + "\n\n"
        + "REGRAS ARMADAS:\n" + dumps(req.get("armed") or []) + "\n\n"
        + "SONS DO OBA:\n" + (", ".join((req.get("oba") or {}).get("sounds") or []) or "(nenhum)") + "\n\n"
        + "MEMÓRIA:\n" + dumps(req.get("memory") or {})
    )
    system = SYSTEM.format(persona=persona(req), name=oba_name(req), caps=caps,
                           sources=sources_prompt((req.get("oba") or {}).get("mcp")))
    out = ask(make_agent(req, system), prompt, Result)
    res = {"actions": [to_protocol(a) for a in out["actions"]], "note": out["note"]}
    if out.get("memory") is not None:
        res["memory"] = out["memory"]
    return res


def to_protocol(a: dict) -> dict:
    """Os campos do schema (mood, then) viram os do protocolo (do)."""
    a = {k: v for k, v in a.items() if v is not None}
    if a["type"] == "react":
        a["do"] = a.pop("mood", None)
    elif a["type"] == "arm":
        a["do"] = [to_protocol(s) for s in a.pop("then", [])]
    elif a["type"] == "rec":
        a = {"type": "rec", "on": False}
    return a
