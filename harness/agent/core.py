"""O que as habilidades usam: o agente Strands montado a partir do Oba e a
conversa (as frases finais do history) em texto."""

import json
import logging
import os

from strands import Agent
from strands.models import BedrockModel

from sources import mcp_clients

# Modelos permitidos: o Oba escolhe um pelo nome (agent.model); sem ele, ou fora da lista, vale o primeiro
MODELS = [m.strip() for m in os.environ.get("MODELS", "us.anthropic.claude-sonnet-5").split(",") if m.strip()]
REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_TOKENS = 4000
MAX_TRANSCRIPT_CHARS = 12000  # ~1h de reunião cabe; acima disso fica só o final

log = logging.getLogger("oba-agent")


def make_agent(req: dict, system_prompt: str) -> Agent:
    oba = req.get("oba") or {}
    model = oba.get("model") if oba.get("model") in MODELS else MODELS[0]
    return Agent(
        model=BedrockModel(model_id=model, region_name=REGION, max_tokens=MAX_TOKENS),
        system_prompt=system_prompt,
        tools=mcp_clients(oba.get("mcp")),
        callback_handler=None,
    )


def ask(agent: Agent, prompt: str, schema):
    result = agent(prompt, structured_output_model=schema)
    m = result.metrics
    tools = {name: t.call_count for name, t in m.tool_metrics.items()}
    log.info("ciclos=%d ferramentas=%s tokens=%s", m.cycle_count, tools, m.accumulated_usage)
    return result.structured_output.model_dump()


def oba_name(req: dict) -> str:
    return (req.get("oba") or {}).get("name") or "Oba"


def persona(req: dict) -> str:
    return ((req.get("oba") or {}).get("persona") or f"Você é o {oba_name(req)}.").strip()


def transcript(req: dict) -> tuple[list[str], int]:
    """As frases finais do history e o índice da primeira nova."""
    hist = req.get("history") or []
    new_from = req.get("new_from")
    new_from = new_from if isinstance(new_from, int) else len(hist)
    lines, first_new = [], None
    for i, ev in enumerate(hist):
        if ev.get("type") != "transcript.final" or not str(ev.get("text") or "").strip():
            continue
        if first_new is None and i >= new_from:
            first_new = len(lines)
        lines.append(str(ev["text"]).strip())
    return lines, len(lines) if first_new is None else first_new


def render_transcript(lines: list[str], new_from: int | None) -> str:
    out, size = [], 0
    for i in range(len(lines) - 1, -1, -1):
        size += len(lines[i]) + 1
        if size > MAX_TRANSCRIPT_CHARS:
            break
        out.append(lines[i])
        if new_from is not None and i == new_from:
            out.append("--- NOVAS FRASES ---")
    out.reverse()
    return "\n".join(out) or "(ninguém falou nada ainda)"


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
