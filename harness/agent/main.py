"""Agente genérico do Oba Pocket no AgentCore Runtime.

Sem estado: o roteador (harness/router) manda a cada chamada o Oba (persona,
modelo, habilidades, MCPs), o que acordou o agente, o histórico da sessão e a
memória, e publica as ações que voltarem. O contrato está em docs/protocol.md.

  {"v": 1, "run": "think", "oba": {...}, "events": [...], "history": [...], ...}
    -> {"actions": [...], "memory"?: {...}, "note": "..."}

Local: python main.py serve /invocations na porta 8080 (AgentCore SDK).
"""

import logging
import time

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from abilities import find
from core import log

logging.basicConfig(level=logging.INFO)
app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(req: dict) -> dict:
    if req.get("v") != 1 or not isinstance(req.get("run"), str):
        return {"actions": [], "note": "requisição fora do contrato v1"}
    oba = req.get("oba") or {}
    fn = find(oba.get("abilities"), req["run"])
    started = time.time()
    out = fn(req)
    # Só os tipos das ações: o texto é da conversa, e o log fica mais que o que o portal apaga
    acts = [a for a in (out.get("actions") or []) if isinstance(a, dict)] if isinstance(out, dict) else []
    log.info("%s/%s (%s) em %.1fs: %d ações %s", oba.get("id"), req["run"], fn.__name__, time.time() - started,
             len(acts), [f"{a.get('type')}/{a['channel']}" if isinstance(a.get("channel"), str) else a.get("type")
                         for a in acts[:12]])
    return out


if __name__ == "__main__":
    app.run()
