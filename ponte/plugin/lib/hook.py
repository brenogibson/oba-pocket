#!/usr/bin/env python3
"""Hook do Claude Code para a ponte do Oba (docs/ponte.md).

Chamado por todos os eventos do hooks.json. Lê o evento no stdin e manda uma linha JSON
para o daemon pelo socket ~/.oba-ponte/ponte.sock (OBA_PONTE_HOME troca a pasta),
subindo o daemon se ele não estiver rodando.

No PermissionRequest, espera a decisão da placa por até 290 s e imprime o allow ou o
deny do Claude Code. Sem decisão, sai sem saída: vale o diálogo do terminal.

Qualquer problema (sem configuração, sem daemon, erro) é silencioso e com exit 0:
a ponte nunca atrapalha o Claude Code.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WAIT_S = 290
START_S = 5                 # espera o socket do daemon que acabou de subir
LOG_MAX = 1024 * 1024
# Só o que o daemon usa: nada de tool_response, prompt ou last_assistant_message.
KEEP = ("hook_event_name", "session_id", "agent_id", "cwd", "transcript_path", "tool_name",
        "tool_input", "notification_type", "source", "reason", "trigger")
ALLOW = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
DENY = {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                               "decision": {"behavior": "deny", "message": "Negado no Oba", "interrupt": False}}}


def spawn(home, comum):
    import subprocess
    log = comum.path(home, comum.LOG)
    try:
        if os.path.getsize(log) > LOG_MAX:
            os.replace(log, log + ".1")
    except OSError:
        pass
    lib = HERE
    n = comum.newest(home)
    if n and n[0] > comum.VERSION:
        lib = n[1]          # um plugin mais novo já rodou aqui: o daemon dele (o velho não volta)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        subprocess.Popen([sys.executable, os.path.join(lib, "daemon.py")], cwd=home,
                         stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                         start_new_session=True, close_fds=True)
    finally:
        os.close(fd)


def connect(home, comum, start):
    c = comum.dial(home, 0.5)
    if c or not start:
        return c
    spawn(home, comum)
    end = time.time() + START_S
    while time.time() < end:
        time.sleep(0.05)
        c = comum.dial(home, 0.5)
        if c:
            return c
    return None


def main():
    t = time.time()
    sys.dont_write_bytecode = True      # nada de __pycache__ na pasta do plugin
    sys.path.insert(0, HERE)
    import comum
    home = comum.home()
    if not home or not os.path.isfile(comum.path(home, comum.CONFIG)):
        return
    ev = json.load(sys.stdin)
    if not isinstance(ev, dict):
        return
    name = ev.get("hook_event_name")
    wait = name == "PermissionRequest"
    msg = {"t": t, "wait": wait, "ver": list(comum.VERSION), "lib": HERE,
           "ev": {k: ev[k] for k in KEEP if k in ev}}
    c = connect(home, comum, start=name != "SessionEnd")     # o SessionEnd tem ~1,5 s: não sobe daemon
    if not c:
        return
    try:
        c.sendall(comum.dumps(msg).encode() + b"\n")
        if not wait:
            return
        c.settimeout(max(1.0, t + WAIT_S - time.time()))
        line = comum.read_line(c)
    finally:
        c.close()
    decision = json.loads(line).get("decision") if line else None
    out = ALLOW if decision == "allow" else DENY if decision == "deny" else None
    if out:
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    os._exit(0)
