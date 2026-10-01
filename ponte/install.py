#!/usr/bin/env python3
"""Instala a ponte do Oba nesta máquina (docs/ponte.md).

  python3 ponte/install.py build/ponte-<nome>/          copia o pacote para ~/.oba-ponte
  python3 ponte/install.py build/ponte-<nome>/ --check  copia e testa o MQTT com o Oba
  python3 ponte/install.py --check                      só testa o que já está instalado
  python3 ponte/install.py --uninstall                  para a ponte e apaga ~/.oba-ponte

Não roda nada no Claude Code: só mostra os dois comandos `claude plugin` para você rodar.
OBA_PONTE_HOME troca a pasta.
"""
import fcntl
import json
import os
import queue
import shlex
import shutil
import ssl
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(HERE, "plugin", "lib"))
import comum  # noqa: E402
import daemon  # noqa: E402
import mqtt  # noqa: E402

MARKET, PLUGIN = "oba-pocket", "oba-ponte"
CHECK_S = 15                # espera o state da placa listar a fonte
HELD = None                 # a trava da ponte, presa até o fim (hold)
BUSY = None                 # a trava ponte.install: com ela presa, os hooks não sobem daemon


def die(msg):
    print(f"erro: {msg}")
    sys.exit(1)


def stop_daemon(home):
    if comum.request(home, {"ctl": "quit"}):
        print("ponte que estava rodando: parada (o próximo hook sobe de novo)")


def hold(home):
    """Para a ponte e fica com a trava dela até o fim: um hook de outra sessão não sobe
    outra no meio da cópia (com a config velha) nem do --check (com o mesmo client id)."""
    global HELD, BUSY
    if HELD:
        return
    BUSY = open(comum.path(home, comum.INSTALL), "a")
    os.chmod(BUSY.name, 0o600)
    try:
        fcntl.flock(BUSY, fcntl.LOCK_EX | fcntl.LOCK_NB)    # daqui em diante, daemon novo não sobe
    except OSError:
        die("outro install.py está rodando")
    for _ in range(5):
        stop_daemon(home)
        HELD = daemon.lock(home)    # espera 3 s; um daemon que já estava subindo pega antes: para de novo
        if HELD:
            return
    die("a ponte que estava rodando não parou")


def install(pkg, home):
    try:
        cfg = comum.load_config(pkg)
        ctx = ssl.create_default_context(cafile=cfg["ca"])
        ctx.load_cert_chain(cfg["cert"], cfg["key"])
    except ssl.SSLError as e:
        die(f"o certificado, a chave ou a CA do pacote não abrem ({mqtt.why(e)})")
    except (OSError, ValueError) as e:
        die(f"pacote inválido: {mqtt.why(e) if isinstance(e, OSError) else e}")
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.chmod(home, 0o700)
    hold(home)
    with open(comum.path(pkg, comum.CONFIG), encoding="utf-8") as f:
        raw = json.load(f)
    try:
        os.unlink(comum.path(home, comum.NEWEST))   # reinstalar volta a subir o daemon de cada hook
    except FileNotFoundError:
        pass
    for name in [raw[k] for k in comum.FILES] + [comum.CONFIG]:     # a config por último
        dst = comum.path(home, name)
        os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
        with open(comum.path(pkg, name), "rb") as src:
            data = src.read()
        fd = os.open(dst + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(dst + ".tmp", 0o600)
        os.replace(dst + ".tmp", dst)
    print(f"instalado em {home} (fonte {cfg['thing']}, nome {cfg['name']}, Oba {cfg['device']})")
    print("\nAgora, no terminal, instale o plugin no Claude Code:\n")
    print(f"  claude plugin marketplace add {shlex.quote(HERE)}")
    print(f"  claude plugin install {PLUGIN}@{MARKET} --scope user\n")


def check(home):
    try:
        cfg = comum.load_config(home)
    except (OSError, ValueError) as e:
        die(f"sem instalação em {home}: {mqtt.why(e) if isinstance(e, OSError) else e}")
    hold(home)                  # mesmo client id: o IoT Core derrubaria um dos dois
    ext, re_topic, state = comum.topics(cfg)
    got = queue.Queue()
    c = mqtt.Client(cfg["thing"], mqtt.tls_connect(cfg["endpoint"], cfg["port"], cfg["ca"], cfg["cert"], cfg["key"]),
                    subs=[re_topic, state], will=(ext, daemon.WILL, 1, False),
                    on_message=lambda t, p: got.put((t, p)), log=lambda m: print(" ", m))
    print(f"conectando em {cfg['endpoint']}:{cfg['port']} como {cfg['thing']}…")
    c.start()
    try:
        if not c.connected.wait(20):
            die("não conectou (confira o endpoint, o certificado e a policy)")
        end = time.time() + 5
        while c.suback is None and time.time() < end:
            time.sleep(0.05)
        if c.suback and 0x80 in c.suback:     # antes do status de teste: nada fica no Oba
            die(f"o IoT Core recusou a assinatura de {re_topic} ou {state} (confira iot:Subscribe e iot:Receive na policy)")
        msg = {"v": 1, "type": "status", "ts": comum.now_ms(), "mood": "idle",
               "label": f"{cfg['name']} · teste", "ttl_s": 30}
        if not c.publish(ext, comum.dumps(msg), qos=1, timeout=10):
            die(f"o IoT Core não confirmou a publicação em {ext} (policy?)")
        print(f"status publicado em {ext}")
        seen, ok, end = None, False, time.time() + CHECK_S
        while time.time() < end and not seen:
            try:
                _, payload = got.get(timeout=0.5)
                m = json.loads(payload)
            except (queue.Empty, ValueError):
                continue
            if m.get("type") == "state" and m.get("online") is False:
                seen = "o Oba está desligado (o state retido diz online: false)"
            elif m.get("type") == "state" and any(isinstance(e, dict) and e.get("src") == cfg["thing"]
                                                  for e in m.get("ext") or []):
                seen, ok = f"o Oba ({m.get('oba') or cfg['device']}) já mostra esta fonte", True
        print(seen or f"sem resposta do Oba em {CHECK_S} s (ligado? firmware com fontes externas?)")
        c.publish(ext, comum.dumps({"v": 1, "type": "status", "ts": comum.now_ms(), "online": False}),
                  qos=1, timeout=10)
        print("status online: false publicado.")
        print("Tudo certo: a AWS e o Oba responderam." if ok else
              "A AWS aceitou, mas a volta pelo Oba não foi confirmada: ligue o Oba e rode o --check de novo.")
        return 0 if ok else 1
    finally:
        c.stop()


def uninstall(home):
    if os.path.isdir(home):
        hold(home)
        shutil.rmtree(home)
        print(f"apagado: {home}")
    print("\nNo Claude Code:\n")
    print(f"  claude plugin uninstall {PLUGIN}@{MARKET}")
    print(f"  claude plugin marketplace remove {MARKET}\n")
    print("Na AWS, revogue o certificado desta fonte (docs/ponte.md, Desinstalar).")


def main(argv):
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    home = comum.home()
    if flags - {"--check", "--uninstall"} or len(args) > 1 or not (args or flags):
        print(__doc__)
        return 2
    if not home:
        die("OBA_PONTE_HOME tem que ser um caminho absoluto")
    if "--uninstall" in flags:
        uninstall(home)
        return 0
    if args:
        install(os.path.abspath(args[0]), home)
    if "--check" in flags:
        return check(home)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
