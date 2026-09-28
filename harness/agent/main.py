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
from core import dumps, log

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
    log.info("%s/%s (%s) em %.1fs: %s", oba.get("id"), req["run"], fn.__name__, time.time() - started,
             dumps(out)[:800])
    return out


if __name__ == "__main__":
    app.run()
