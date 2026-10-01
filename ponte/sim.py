#!/usr/bin/env python3
"""Simulador da ponte: roda o hook.py de verdade com eventos falsos do Claude Code.

  python3 ponte/sim.py                 uma sessão trabalha, pede permissão e termina
  python3 ponte/sim.py --danger        o pedido é um rm -rf (aprovar exige segurar o botão)
  python3 ponte/sim.py --sessions 3    três sessões ao mesmo tempo (três pedidos na fila)
  python3 ponte/sim.py --keep          não manda o SessionEnd (a fonte fica no Oba)

Usa a ponte instalada (~/.oba-ponte, ou OBA_PONTE_HOME) e publica no Oba de verdade.
Responda na placa: o simulador mostra a decisão que o Claude Code receberia.
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "plugin", "lib", "hook.py")
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(HERE, "plugin", "lib"))
import comum  # noqa: E402
PAUSE = 1.5


def hook(ev, sid, cwd):
    ev = dict(ev, session_id=sid, cwd=cwd, transcript_path=os.path.join(cwd, ".sim-nao-existe.jsonl"),
              permission_mode="default")
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(ev).encode(), capture_output=True,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return p.stdout.decode().strip()


def session(n, danger, keep):
    sid, cwd = str(uuid.uuid4()), os.path.join(os.path.expanduser("~"), f"projeto-{n}")
    say = lambda m: print(f"[sessão {n}] {m}", flush=True)
    edit = {"file_path": os.path.join(cwd, "src", "app.py"), "old_string": "x = 1", "new_string": "x = 2"}
    cmd = ({"command": "rm -rf build/", "description": "Limpa o build"} if danger
           else {"command": "npm test", "description": "Roda os testes"})
    steps = [
        ("começou", {"hook_event_name": "SessionStart", "source": "startup"}),
        ("prompt", {"hook_event_name": "UserPromptSubmit", "prompt": "(simulado)"}),
        ("lendo", {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": edit["file_path"]}}),
        (None, {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": edit["file_path"]}}),
        ("editando", {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": edit}),
        (None, {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": edit}),
        (None, {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": cmd}),
    ]
    for label, ev in steps:
        if label:
            say(label)
        hook(ev, sid, cwd)
        time.sleep(PAUSE if label else 0.3)
    say(f"pede permissão para: {cmd['command']} (responda no Oba)")
    out = hook({"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": cmd,
                "permission_suggestions": [], "prompt_id": sid[:8]}, sid, cwd)
    behavior = json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] if out else None
    say({"allow": "aprovado no Oba", "deny": "negado no Oba"}.get(behavior, "sem decisão: ficaria para o terminal"))
    if behavior == "allow":
        hook({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": cmd}, sid, cwd)
        time.sleep(PAUSE)
    hook({"hook_event_name": "Stop"}, sid, cwd)
    say("terminou a resposta")
    if not keep:
        time.sleep(PAUSE)
        hook({"hook_event_name": "SessionEnd", "reason": "exit"}, sid, cwd)
        say("saiu")


def main(argv):
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    home = comum.home()
    if not home:
        print("OBA_PONTE_HOME tem que ser um caminho absoluto")
        return 1
    if not os.path.isfile(os.path.join(home, "config.json")):
        print(f"a ponte não está instalada em {home} (python3 ponte/install.py <pacote>)")
        return 1
    n = int(argv[argv.index("--sessions") + 1]) if "--sessions" in argv else 1
    runs = [threading.Thread(target=session, args=(i + 1, "--danger" in argv, "--keep" in argv))
            for i in range(max(1, min(n, 4)))]
    for t in runs:
        t.start()
        time.sleep(0.5)
    for t in runs:
        t.join()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
