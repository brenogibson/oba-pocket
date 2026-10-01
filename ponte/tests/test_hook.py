"""hook.py de verdade (subprocesso) contra o Daemon em processo e o broker falso."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin", "lib")
sys.path.insert(0, LIB)
import comum  # noqa: E402
import daemon  # noqa: E402
import hook  # noqa: E402
from broker import Broker  # noqa: E402

HOOK = os.path.join(LIB, "hook.py")
INSTALL = os.path.join(LIB, "..", "..", "install.py")
EXT, RE = "oba/bit/ext/mac", "oba/bit/ext/mac/re"


def make_home(port=8883):
    h = tempfile.mkdtemp(prefix="ponte-")
    cfg = {"endpoint": "localhost", "port": port, "prefix": "oba", "device": "bit", "thing": "mac",
           "name": "mac", "cert": "cert.pem", "key": "key.pem", "ca": "AmazonRootCA1.pem"}
    for f in ("cert.pem", "key.pem", "AmazonRootCA1.pem"):
        with open(os.path.join(h, f), "w") as fh:
            fh.write("falso\n")
    with open(os.path.join(h, "config.json"), "w") as fh:
        json.dump(cfg, fh)
    return h


def run_hook(home, ev, wait=True):
    """wait: -> (código, stdout, stderr). Sem wait: o processo, para finish()."""
    env = dict(os.environ, OBA_PONTE_HOME=home, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.Popen([sys.executable, HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env)
    p.stdin.write(json.dumps(ev).encode())
    p.stdin.close()
    p.stdin = None
    return finish(p) if wait else p


def finish(p):
    out, err = p.communicate(timeout=20)
    return p.returncode, out, err


def event(name, **kw):
    return dict({"hook_event_name": name, "session_id": "s1", "cwd": "/home/ana/proj",
                 "transcript_path": "/nao/existe.jsonl", "permission_mode": "default"}, **kw)


class Quiet(unittest.TestCase):
    def test_no_config(self):
        h = tempfile.mkdtemp(prefix="ponte-")
        self.addCleanup(shutil.rmtree, h)
        for ev in (event("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}), "lixo"):
            self.assertEqual(run_hook(h, ev), (0, b"", b""))
        self.assertEqual(os.listdir(h), [])

    def test_missing_home(self):
        h = os.path.join(tempfile.gettempdir(), "ponte-nao-existe")
        self.assertEqual(run_hook(h, event("SessionStart")), (0, b"", b""))
        self.assertFalse(os.path.exists(h))

    def test_relative_home(self):
        cwd = tempfile.mkdtemp(prefix="ponte-")
        self.addCleanup(shutil.rmtree, cwd)
        env = dict(os.environ, OBA_PONTE_HOME="rel/ponte", PYTHONDONTWRITEBYTECODE="1")
        for script, args in ((HOOK, []), (os.path.join(LIB, "daemon.py"), []),
                             (os.path.join(LIB, "..", "..", "install.py"), ["--check"])):
            r = subprocess.run([sys.executable, script] + args, input=json.dumps(event("SessionStart")).encode(),
                               cwd=cwd, env=env, capture_output=True, timeout=20)
            self.assertEqual(r.returncode, 1 if args else 0, script)
        self.assertEqual(os.listdir(cwd), [])       # nada (nem a chave) dentro da pasta atual
        old = os.environ.get("OBA_PONTE_HOME")
        try:
            os.environ["OBA_PONTE_HOME"] = "~/x"
            self.assertEqual(comum.home(), os.path.expanduser("~/x"))
            os.environ["OBA_PONTE_HOME"] = "x"
            self.assertIsNone(comum.home())
        finally:
            os.environ.pop("OBA_PONTE_HOME")
            if old is not None:
                os.environ["OBA_PONTE_HOME"] = old

    def test_versions_match(self):
        root = os.path.join(LIB, "..", "..")
        with open(os.path.join(root, "plugin", ".claude-plugin", "plugin.json")) as f:
            plugin = json.load(f)["version"]
        with open(os.path.join(root, ".claude-plugin", "marketplace.json")) as f:
            market = json.load(f)["plugins"][0]["version"]
        self.assertEqual((plugin, market), (".".join(map(str, comum.VERSION)),) * 2)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.b = Broker()
        self.home = make_home()
        self.logs = []
        d = self.d = daemon.Daemon(self.home, comum.load_config(self.home), connect=self.b.connect,
                                   log=self.logs.append)
        self.th = threading.Thread(target=d.serve, daemon=True)
        self.th.start()
        self.assertTrue(d.ready.wait(5) and d.mqtt.connected.wait(5))
        self.b.wait_event("subscribe")

    def tearDown(self):
        self.d.stop()
        self.th.join(10)
        self.b.close()
        shutil.rmtree(self.home)

    def ask(self, cmd="ls"):
        """Sobe um PermissionRequest e devolve (processo, ask que chegou no broker)."""
        n = len(self.b.messages(EXT))
        p = run_hook(self.home, event("PermissionRequest", tool_name="Bash", tool_input={"command": cmd},
                                      permission_suggestions=[], prompt_id="p1"), wait=False)
        a = self.b.wait_msg(EXT, lambda m: m.get("type") == "ask", after=n, timeout=10)
        return p, a

    def answer(self, a, choice, **kw):
        self.b.inject(RE, dict({"v": 1, "type": "reply", "ts": comum.now_ms(), "oba": "bit", "re": "ask",
                                "id": a["id"], "choice": choice}, **kw))

    def out(self, p):
        rc, out, err = p if isinstance(p, tuple) else finish(p)
        self.assertEqual((rc, err), (0, b""))
        return out.decode()

    def test_status(self):
        p = run_hook(self.home, event("SessionStart", source="startup"))
        self.assertEqual(self.out(p), "")
        m = self.b.wait_msg(EXT, lambda m: m.get("type") == "status")
        self.assertEqual((m["mood"], m["label"], m["ttl_s"]), ("idle", "mac · proj", 180))
        self.assertEqual(self.b.published[-1][2:4], (1, False))          # QoS 1, nunca retido
        self.assertEqual(comum.request(self.home, {"ctl": "ping"}),
                         {"sessions": 1, "asks": 0, "mood": "idle", "ok": True, "mqtt": True})

    def test_allow(self):
        p, a = self.ask("rm -rf build")
        self.assertTrue(a["danger"])
        self.answer(a, "allow")
        self.assertEqual(self.out(p), json.dumps(hook.ALLOW) + "\n")
        self.assertEqual(json.loads(json.dumps(hook.ALLOW))["hookSpecificOutput"]["decision"], {"behavior": "allow"})

    def test_deny(self):
        p, a = self.ask()
        self.answer(a, "deny")
        out = json.loads(self.out(p))
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
            "behavior": "deny", "message": "Negado no Oba", "interrupt": False}}})

    def test_skip(self):
        for err in (None, "venceu", "fila cheia"):
            p, a = self.ask()
            self.answer(a, "skip", **({"error": err} if err else {}))
            self.assertEqual(self.out(p), "")

    def test_cancel_by_post(self):
        p, a = self.ask()
        run_hook(self.home, event("PostToolUse", tool_name="Bash", tool_input={"command": "ls"},
                                  tool_use_id="toolu_1", tool_response={"stdout": ""}))
        self.b.wait_msg(EXT, lambda m: m.get("type") == "ask.cancel" and m["id"] == a["id"])
        self.assertEqual(self.out(p), "")

    def test_hook_killed(self):
        p, a = self.ask()
        p.kill()
        finish(p)
        self.b.wait_msg(EXT, lambda m: m.get("type") == "ask.cancel" and m["id"] == a["id"])

    def test_newer_plugin_replaces_daemon(self):
        p, a = self.ask()
        lib = tempfile.mkdtemp(prefix="lib-")       # o plugin novo: um daemon.py que só deixa uma marca
        self.addCleanup(shutil.rmtree, lib)
        with open(os.path.join(lib, "daemon.py"), "w") as fh:
            fh.write("import os\nopen(os.path.join(os.environ['OBA_PONTE_HOME'], 'novo'), 'w').close()\n")
        c = comum.dial(self.home)
        newer = [comum.VERSION[0], comum.VERSION[1], comum.VERSION[2] + 1]
        c.sendall(comum.dumps({"t": time.time(), "wait": False, "ver": newer, "lib": lib,
                                 "ev": event("PreToolUse", session_id="s2", tool_name="Read", tool_input={})}).encode() + b"\n")
        c.close()
        time.sleep(1.5)
        self.assertTrue(self.th.is_alive())         # espera o pedido que estava aberto
        self.answer(a, "allow")
        self.out(p)
        self.th.join(5)
        self.assertFalse(self.th.is_alive())
        self.assertIn("plugin mais novo: saindo (o próximo hook sobe o novo)", self.logs)
        self.assertEqual(comum.newest(self.home), (tuple(newer), lib))
        self.assertEqual(os.stat(os.path.join(self.home, comum.NEWEST)).st_mode & 0o777, 0o600)
        # Um hook do plugin velho (uma sessão aberta antes da atualização) sobe o daemon novo
        run_hook(self.home, event("PreToolUse", tool_name="Read", tool_input={}))
        self.assertTrue(os.path.exists(os.path.join(self.home, "novo")))

    def test_logs_are_minimal(self):
        p, a = self.ask("cat segredo.txt")
        self.answer(a, "deny")
        self.out(p)
        text = "\n".join(self.logs)
        self.assertIn(a["id"], text)
        for bad in ("segredo", "/home/ana", "proj", "cat "):
            self.assertNotIn(bad, text)


class Connecting(unittest.TestCase):
    def test_ask_while_mqtt_connects(self):
        # O PermissionRequest que sobe o daemon chega antes do MQTT: espera a conexão
        b = Broker()
        self.addCleanup(b.close)
        home = make_home()
        self.addCleanup(shutil.rmtree, home)

        def slow():
            time.sleep(1.5)
            return b.connect()
        d = daemon.Daemon(home, comum.load_config(home), connect=slow, log=lambda m: None)
        th = threading.Thread(target=d.serve, daemon=True)
        th.start()
        self.addCleanup(th.join, 10)
        self.addCleanup(d.stop)
        self.assertTrue(d.ready.wait(5))
        self.assertFalse(d.mqtt.connected.is_set())
        p = run_hook(home, event("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}), wait=False)
        a = b.wait_msg(EXT, lambda m: m.get("type") == "ask", timeout=10)
        b.inject(RE, {"v": 1, "type": "reply", "ts": comum.now_ms(), "oba": "bit", "re": "ask",
                      "id": a["id"], "choice": "allow"})
        self.assertEqual(finish(p), (0, (json.dumps(hook.ALLOW) + "\n").encode(), b""))


def real_certs(h):
    """Certificado de verdade (o install.py abre o pacote com o ssl), ou False sem openssl."""
    if not shutil.which("openssl"):
        return False
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=teste", "-keyout", os.path.join(h, "key.pem"),
                        "-out", os.path.join(h, "cert.pem")], capture_output=True)
    if r.returncode:
        return False
    shutil.copy(os.path.join(h, "cert.pem"), os.path.join(h, "AmazonRootCA1.pem"))
    return True


class Spawn(unittest.TestCase):
    """O hook sobe o daemon de verdade (o certificado falso falha antes de abrir a rede)."""

    def test_spawn_lock_quit(self):
        h = make_home()
        self.addCleanup(shutil.rmtree, h)
        self.assertEqual(run_hook(h, event("SessionEnd", reason="exit")), (0, b"", b""))
        self.assertFalse(os.path.exists(os.path.join(h, comum.SOCK)))    # o SessionEnd não sobe daemon
        self.assertEqual(run_hook(h, event("SessionStart", source="startup")), (0, b"", b""))
        st = comum.request(h, {"ctl": "ping"})
        self.assertEqual((st["ok"], st["mqtt"], st["sessions"]), (True, False, 1))
        env = dict(os.environ, OBA_PONTE_HOME=h, PYTHONDONTWRITEBYTECODE="1")
        second = subprocess.run([sys.executable, os.path.join(LIB, "daemon.py")], env=env,
                                capture_output=True, timeout=20)
        self.assertEqual(second.returncode, 0)                           # a trava: já tem um rodando
        self.assertTrue(comum.request(h, {"ctl": "quit"})["ok"])
        end = time.time() + 10
        while os.path.exists(os.path.join(h, comum.SOCK)) and time.time() < end:
            time.sleep(0.05)
        self.assertFalse(os.path.exists(os.path.join(h, comum.SOCK)))
        for f in (comum.SOCK, comum.LOCK, comum.LOG):
            p = os.path.join(h, f)
            if os.path.exists(p):
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o600, f)
        with open(os.path.join(h, comum.LOG)) as fh:
            log = fh.read()
        self.assertIn("ponte no ar", log)
        self.assertIn("ponte parada", log)
        self.assertNotIn(h, log)

    def test_install_with_busy_hooks(self):
        # Uma sessão rodando ferramentas: os hooks sobem daemon toda hora, e o install.py,
        # o --uninstall e a pasta apagada têm que aguentar
        pkg, h = make_home(), make_home()
        self.addCleanup(shutil.rmtree, pkg)
        self.addCleanup(shutil.rmtree, h, True)
        if not (real_certs(pkg) and real_certs(h)):
            self.skipTest("sem openssl")
        stop = threading.Event()

        def hooks():
            while not stop.is_set():
                run_hook(h, event("PreToolUse", tool_name="Read", tool_input={}))
                time.sleep(0.1)
        ths = [threading.Thread(target=hooks, daemon=True) for _ in range(3)]
        for t in ths:
            t.start()
        self.addCleanup(lambda: [stop.set()] + [t.join(20) for t in ths])
        end = time.time() + 10
        while not comum.request(h, {"ctl": "ping"}) and time.time() < end:
            time.sleep(0.1)
        env = dict(os.environ, OBA_PONTE_HOME=h, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, INSTALL, pkg], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout)
        with open(os.path.join(pkg, "key.pem")) as a, open(os.path.join(h, "key.pem")) as b:
            self.assertEqual(a.read(), b.read())
        r = subprocess.run([sys.executable, INSTALL, "--uninstall"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout)
        time.sleep(1.5)                     # os hooks continuam: ninguém recria a pasta
        self.assertFalse(os.path.exists(h))
