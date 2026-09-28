"""Catálogo de MCPs do harness: os Obas escolhem pelo nome (agent.mcp).

Uma URL solta vinda do JSON de um Oba nunca é usada. Para plugar uma fonte
nova, acrescente uma entrada em CATALOG; as URLs vêm do ambiente do runtime
(o setup.py passa as do config.json). Fonte sem URL fica de fora.
"""

import os
from dataclasses import dataclass, field

from strands.tools.mcp import MCPClient


@dataclass
class Source:
    url: str | None
    kinds: list[str]           # tipos de carta que essa fonte ajuda a sugerir
    hint: str                  # como o agente deve usar essa fonte
    tools: list[str] = field(default_factory=list)  # ferramentas liberadas (vazio = todas)
    headers: dict[str, str] = field(default_factory=dict)


CATALOG = {
    "aws-knowledge": Source(
        url=os.environ.get("AWS_KNOWLEDGE_MCP_URL", "https://knowledge-mcp.global.api.aws"),
        kinds=["service", "blog", "doc"],
        hint="Documentação, blogs e novidades oficiais da AWS. Use aws___search_documentation "
        "para achar um blog ou página de doc com link real; o topic 'general' traz blogs e "
        "'current_awareness' traz lançamentos.",
        tools=["aws___search_documentation"],
    ),
    "demos": Source(
        url=os.environ.get("DEMOS_MCP_URL"),
        kinds=["demo"],
        hint="Catálogo de demos prontas que podemos mostrar ao vivo. Quando a conversa combinar "
        "com uma demo, sugira ela (kind 'demo') com o link retornado pela ferramenta.",
        headers={"Authorization": os.environ["DEMOS_MCP_TOKEN"]} if os.environ.get("DEMOS_MCP_TOKEN") else {},
    ),
}


def pick(names) -> dict[str, Source]:
    """As fontes pedidas pelo Oba que existem no catálogo e têm URL."""
    names = names if isinstance(names, list) else []
    return {n: CATALOG[n] for n in names if isinstance(n, str) and n in CATALOG and CATALOG[n].url}


_clients: dict[tuple, list[MCPClient]] = {}


def mcp_clients(names) -> list[MCPClient]:
    """Conexões MCP reaproveitadas entre chamadas da mesma sessão do runtime."""
    sources = pick(names)
    key = tuple(sorted(sources))
    if key not in _clients:
        _clients[key] = [
            MCPClient(
                url=s.url,
                headers=s.headers or None,
                tool_filters={"allowed": s.tools} if s.tools else None,
                application_name="oba-pocket",
                continue_on_error=True,  # uma fonte fora do ar não derruba o agente
            )
            for s in sources.values()
        ]
    return _clients[key]


def sources_prompt(names) -> str:
    lines = [f"- {n} ({', '.join(s.kinds)}): {s.hint}" for n, s in pick(names).items()]
    return "\n".join(lines) or "(nenhuma: não há ferramentas de busca, então não sugira links)"
