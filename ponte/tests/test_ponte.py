"""A lógica da ponte (daemon.Ponte) com relógio falso e sem rede."""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin", "lib"))
import daemon  # noqa: E402
from daemon import Ponte, Waiter  # noqa: E402

CFG = {"name": "mac", "thing": "mac"}
CWD = "/home/ana/proj"
LS = {"command": "ls"}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.out, self.logs = [], []
        self.p = Ponte(CFG, self.pub, clock=self.clock, log=self.logs.append)
        self.t = 0

    def pub(self, msg, qos):
        self.out.append(msg)
        return True

    def ev(self, name, sid="s1", waiter=None, t=None, **kw):
        self.t += 1
        e = dict({"hook_event_name": name, "session_id": sid, "cwd": CWD}, **kw)
        self.p.event({"t": self.t if t is None else t, "wait": bool(waiter), "ev": e}, waiter)

    def sent(self, kind):
        return [m for m in self.out if m["type"] == kind]

    def status(self):
        return self.sent("status")[-1]

    def reacts(self):
        return [m["do"] for m in self.sent("react")]

    def later(self, s):
        self.clock.t += s
        self.p.tick()

    def ask(self, sid="s1", tool="Bash", inp=LS, **kw):
        """PermissionRequest -> (waiter, ask publicado ou None)."""
        n, w = len(self.sent("ask")), Waiter()
        self.ev("PermissionRequest", sid, w, tool_name=tool, tool_input=inp, **kw)
        asks = self.sent("ask")
        return w, (asks[-1] if len(asks) > n else None)

    def reply(self, aid, choice, **kw):
        m = dict({"v": 1, "type": "reply", "ts": 1, "oba": "bit", "re": "ask", "id": aid, "choice": choice}, **kw)
        self.p.message("p/d/ext/mac/re", json.dumps(m).encode())

    def state(self, ext, online=True):
        self.p.message("p/d/state", json.dumps({"v": 1, "type": "state", "online": online, "ext": ext}).encode())

    def cancels(self):
        return [m["id"] for m in self.sent("ask.cancel")]


class States(Base):
    def test_flow(self):
        self.ev("SessionStart", source="startup")
        self.assertEqual(self.status(), dict(self.status(), mood="idle", label="mac · proj", ttl_s=180))
        self.ev("UserPromptSubmit")
        self.assertEqual(self.status()["mood"], "busy")
        self.ev("PreToolUse", tool_name="Bash", tool_input=LS)
        self.later(2)
        self.assertEqual(self.status()["label"], "mac · proj · Bash")
        self.ev("Notification", notification_type="permission_prompt")
        self.assertEqual(self.status()["mood"], "alert")
        self.ev("PostToolUse", tool_name="Read", tool_input={})
        self.assertEqual(self.status()["mood"], "busy")
        self.ev("PreCompact", trigger="auto")
        self.ev("Stop")
        self.assertEqual(self.status(), dict(self.status(), mood="idle", label="mac · proj"))
        self.ev("StopFailure", error="rate_limit")
        self.assertEqual(self.reacts(), ["dizzy", "happy", "scared"])
        self.ev("Notification", notification_type="idle_prompt")
        self.assertEqual(self.status()["mood"], "idle")
        self.ev("SessionEnd", reason="exit")
        self.assertEqual(self.out[-1], {"v": 1, "type": "status", "ts": self.out[-1]["ts"], "online": False})
        for m in self.out:
            self.assertIsInstance(m["ts"], int)

    def test_many_sessions(self):
        self.ev("SessionStart", "a")
        self.ev("UserPromptSubmit", "b", cwd="/w/outro")
        self.assertEqual(self.status()["mood"], "busy")
        self.assertEqual(self.status()["label"], "mac · outro")
        w, a = self.ask("a")
        self.assertEqual((self.status()["mood"], self.status()["label"]), ("alert", "mac · proj · Bash"))
        self.ev("SessionEnd", "a")
        self.assertEqual(self.cancels(), [a["id"]])
        self.assertEqual(self.status()["mood"], "busy")
        self.ev("SessionEnd", "b")
        self.assertIs(self.status().get("online"), False)

    def test_out_of_order(self):
        self.ev("Stop", t=10)
        self.ev("PreToolUse", t=5, tool_name="Bash", tool_input=LS)     # assíncrono atrasado
        self.assertEqual(self.status()["mood"], "idle")
        self.ev("SessionStart", t=11, source="compact")                 # compactar não vira idle
        self.ev("PreToolUse", t=12, tool_name="Bash", tool_input=LS)
        self.ev("SessionStart", t=13, source="compact")
        self.assertEqual(self.status()["mood"], "busy")

    def test_timers(self):
        self.ev("PreToolUse", tool_name="Bash", tool_input=LS)
        n = len(self.sent("status"))
        self.later(59)
        self.assertEqual(len(self.sent("status")), n)
        self.later(2)                                                   # ttl_s / 3
        self.assertEqual(len(self.sent("status")), n + 1)
        self.later(daemon.BUSY_IDLE_S)
        self.assertEqual(self.status()["mood"], "idle")
        self.assertFalse(self.p.idle_exit())
        self.later(daemon.STALE_S)
        self.assertIs(self.status().get("online"), False)
        self.later(daemon.EXIT_S)
        self.assertTrue(self.p.idle_exit())

    def test_alert_goes_idle(self):
        w, a = self.ask()
        self.p.hook_gone(w)                 # o terminal fechou com o diálogo aberto, sem SessionEnd
        self.assertEqual(self.status()["mood"], "alert")
        self.later(daemon.BUSY_IDLE_S)
        self.assertEqual(self.status()["mood"], "idle")
        self.ev("PreToolUse", "s2", tool_name="Read", tool_input={})
        self.later(2)
        self.assertEqual((self.status()["mood"], self.status()["label"]), ("busy", "mac · proj · Read"))

    def test_late_events_after_end(self):
        self.ev("SessionStart", t=1)
        self.ev("SessionEnd", t=5)
        n = len(self.out)
        self.ev("Stop", t=4)                                            # assíncrono atrasado
        self.ev("PostToolUse", t=6, tool_name="Bash", tool_input=LS)    # saiu antes, rodou depois
        self.ev("SessionStart", t=2)
        self.assertEqual((self.p.sessions, self.out[n:]), ({}, []))
        self.later(daemon.EXIT_S)
        self.assertTrue(self.p.idle_exit())
        self.ev("SessionStart", t=7, source="resume")                   # --resume: a sessão volta
        self.assertEqual(self.status()["mood"], "idle")
        self.ev("SessionEnd", t=8)
        self.later(daemon.ENDED_S)
        self.assertEqual(self.p.ended, {})
        self.ev("Stop", t=3)                                            # passou a janela: volta
        self.assertIn("s1", self.p.sessions)

    def test_label_throttle(self):
        self.ev("PreToolUse", tool_name="Read", tool_input={})
        n = len(self.sent("status"))
        self.ev("PreToolUse", tool_name="Grep", tool_input={})          # só o rótulo mudou
        self.assertEqual(len(self.sent("status")), n)
        self.later(1.6)
        self.assertEqual(self.status()["label"], "mac · proj · Grep")


class Asks(Base):
    def test_fields(self):
        self.ev("SessionStart")
        w, a = self.ask(inp={"command": "rm -rf build", "description": "Limpa"})
        self.assertRegex(a["id"], r"^[A-Za-z0-9_-]{8,12}$")
        self.assertEqual({k: a[k] for k in ("v", "tool", "title", "body", "danger", "label", "ttl_s")},
                         {"v": 1, "tool": "Bash", "title": "Rodar comando", "body": "$ rm -rf build\n\nLimpa",
                          "danger": True, "label": "mac · proj", "ttl_s": 280})
        self.assertFalse(w.ev.is_set())
        self.assertFalse(self.ask(inp=LS)[1]["danger"])
        self.assertEqual(len(self.p.asks), 2)                 # vários ao mesmo tempo
        # o corpo incompleto (aqui, o Write com mais linhas do que cabem): aprovar pede a segurada
        _, big = self.ask(tool="Write", inp={"file_path": CWD + "/x.py", "content": "\n".join("l%d" % i for i in range(30))})
        self.assertTrue(big["danger"])
        self.assertTrue(any(re.search(a["id"], m) for m in self.logs))
        self.assertFalse(any("rm -rf" in m or "/proj" in m or "Limpa" in m for m in self.logs))   # log sem conteúdo

    def test_allow_deny_skip(self):
        w1, a1 = self.ask()
        w2, a2 = self.ask(inp={"command": "pwd"})
        w3, a3 = self.ask(inp={"command": "id"})
        self.reply(a1["id"], "allow")
        self.assertEqual((w1.ev.is_set(), w1.decision), (True, "allow"))
        self.assertEqual(self.status()["mood"], "alert")      # ainda há pedidos
        self.reply(a2["id"], "deny")
        self.assertEqual(w2.decision, "deny")
        self.reply(a3["id"], "skip")
        self.assertEqual((w3.ev.is_set(), w3.decision), (True, None))
        self.assertEqual(self.reacts(), ["happy", "shy"])
        self.assertEqual(self.status()["mood"], "alert")      # skip: o diálogo continua no terminal
        self.assertEqual(self.p.asks, {})
        self.assertEqual(self.cancels(), [])

    def test_allow_goes_busy(self):
        w, a = self.ask()
        self.reply(a["id"], "allow")
        self.assertEqual(self.status()["mood"], "busy")

    def test_board_skips(self):
        """venceu, fila cheia, fontes demais e falta o title: o terminal decide e o id não volta."""
        for err in ("venceu", "fila cheia", "fontes demais", "falta o title"):
            w, a = self.ask()
            self.reply(a["id"], "skip", error=err)
            self.assertEqual((w.ev.is_set(), w.decision), (True, None), err)
            n = len(self.sent("ask"))
            self.p.connected()
            self.later(daemon.RESYNC_S)
            self.state([{"src": "mac", "mood": "alert", "asks": 0}])
            self.assertEqual(len(self.sent("ask")), n, err)
            self.assertNotIn(a["id"], self.cancels())
        self.assertTrue(any("(venceu)" in m for m in self.logs))

    def test_unknown_replies(self):
        w, a = self.ask()
        self.reply("outro", "allow")
        self.reply(a["id"], "allow", re="cmd")
        self.p.message("p/d/ext/mac/re", b"nada")
        self.p.message("p/d/ext/mac/re", b"[1]")
        self.assertFalse(w.ev.is_set())
        self.reply(a["id"], "deny")
        self.reply(a["id"], "allow")                          # repetido: ignorado
        self.assertEqual(w.decision, "deny")

    def test_no_ask_tools(self):
        for tool in ("AskUserQuestion", "ExitPlanMode"):
            w, a = self.ask(tool=tool, inp={"questions": []})
            self.assertIsNone(a)
            self.assertEqual((w.ev.is_set(), w.decision), (True, None))
            self.assertEqual(self.status()["mood"], "alert")

    def test_ttl_and_hook_gone(self):
        w, a = self.ask()
        self.later(daemon.ASK_TTL_S - 10)
        self.assertEqual(self.cancels(), [])
        self.later(10)
        self.assertEqual(self.cancels(), [a["id"]])
        self.assertTrue(w.ev.is_set())
        w, a = self.ask()
        self.p.hook_gone(w)
        self.assertEqual(self.cancels()[-1], a["id"])
        self.assertEqual(self.p.asks, {})

    def test_no_connection(self):
        self.ev("SessionStart")
        self.pub = lambda msg, qos: False
        self.p.publish = self.pub
        w, a = self.ask()
        self.assertTrue(w.ev.is_set())      # o hook sai já: vale o terminal
        self.assertEqual(self.p.asks, {})

    def test_background_agent(self):
        wb, b = self.ask(agent_id="ag1")                    # um agente em segundo plano
        wm, m = self.ask()                                  # a conversa principal
        for name in ("UserPromptSubmit", "Stop", "StopFailure"):
            self.ev(name)
        self.assertEqual(self.cancels(), [m["id"]])         # o do agente continua na placa
        self.assertFalse(wb.ev.is_set())
        self.ev("SessionEnd", reason="exit")
        self.assertEqual(self.cancels(), [m["id"], b["id"]])
        self.assertTrue(wb.ev.is_set())

    def test_try_close(self):
        w, a = self.ask()
        self.assertFalse(self.p.try_close())                # com pedido aberto não fecha
        self.reply(a["id"], "allow")
        self.assertTrue(self.p.try_close())
        n = len(self.out)
        w2, a2 = self.ask(sid="s2")                         # chegou depois de fechar
        self.assertEqual((a2, w2.ev.is_set(), w2.decision), (None, True, None))
        self.ev("PreToolUse", "s3", tool_name="Read")
        self.state([{"src": "mac", "mood": "idle", "asks": 0}])
        self.p.connected()
        self.assertEqual(len(self.out), n)                  # nada publicado
        self.assertNotIn("s2", self.p.sessions)
        self.p.shutdown()
        self.assertEqual(self.out[-1].get("online"), False)

    def test_republish_keeps_ts(self):
        w, a = self.ask()
        self.clock.t += 100
        self.p.connected()
        again = self.sent("ask")[-1]
        self.assertIsNot(again, a)
        self.assertEqual(again, a)                            # mesmo ts e mesmo ttl_s: a placa desconta
        self.clock.t += daemon.ASK_TTL_S - 100 - 4
        self.p.connected()                                    # faltam 4 s: não manda mais
        self.assertEqual(len(self.sent("ask")), 2)


def use(uid, name="Bash", inp=LS):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}]}}


def result(uid):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": uid, "content": "ok"}]}}


class Cancel(Base):
    def test_post_tool_use(self):
        w, a = self.ask(inp={"command": "ls", "description": "Lista"})
        self.ev("PostToolUse", tool_name="Bash", tool_input={"command": "pwd"})       # outro input
        self.ev("PostToolUse", tool_name="Read", tool_input={"command": "ls"})        # outra ferramenta
        self.ev("PostToolUse", "s2", tool_name="Bash", tool_input={"command": "ls"})  # outra sessão
        self.assertEqual(self.cancels(), [])
        self.ev("PostToolUseFailure", tool_name="Bash", tool_input={"command": "ls"})  # subconjunto
        self.assertEqual(self.cancels(), [a["id"]])
        self.assertEqual((w.ev.is_set(), w.decision), (True, None))

    def test_post_before_request(self):
        self.ev("PostToolUse", t=50, tool_name="Bash", tool_input=LS)    # o assíncrono chegou antes
        w, a = self.ask(t=49)
        self.assertIsNone(a)
        self.assertTrue(w.ev.is_set())
        self.later(daemon.POST_KEEP_S + 1)
        self.assertIsNotNone(self.ask(t=200)[1])

    def test_turn_events(self):
        for name in ("Stop", "StopFailure", "UserPromptSubmit", "SessionEnd"):
            w1, a1 = self.ask()
            w2, a2 = self.ask("s2" + name)      # uma sessão que acabou não volta (Ended)
            self.ev(name)
            self.assertIn(a1["id"], self.cancels(), name)
            self.assertNotIn(a2["id"], self.cancels(), name)
            self.assertTrue(w1.ev.is_set())
            self.assertFalse(w2.ev.is_set())
            self.ev("SessionEnd", "s2" + name)

    def test_transcript(self):
        tmp = tempfile.mkdtemp(prefix="ponte-")
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "t.jsonl")

        def add(*objs):
            with open(path, "a") as f:
                f.write("".join(json.dumps(o) + "\n" for o in objs))

        add({"type": "summary"}, use("old"), result("old"), use("u1"))    # um ls antigo já terminou
        w, a = self.ask(transcript_path=path)
        self.later(1)
        add(use("u2", inp={"command": "pwd"}), result("u2"), result("old"))
        self.later(1)
        self.assertEqual(self.cancels(), [])
        add(result("u1"))
        self.later(1)
        self.assertEqual(self.cancels(), [a["id"]])
        w, a = self.ask(transcript_path=path)                            # o use chega depois do pedido
        add(use("u3"))
        self.later(1)
        with open(path, "a") as f:
            f.write(json.dumps(result("u3"))[:20])                       # linha pela metade
        self.later(1)
        self.assertNotIn(a["id"], self.cancels())
        with open(path, "a") as f:
            f.write(json.dumps(result("u3"))[20:] + "\n")
        self.later(1)
        self.assertIn(a["id"], self.cancels())

    def test_resend_cancels_on_connect(self):
        w, a = self.ask()
        self.ev("Stop")
        self.p.connected()
        self.assertEqual(self.cancels(), [a["id"], a["id"]])
        self.later(daemon.ASK_TTL_S + 1)
        self.p.connected()
        self.assertEqual(len(self.cancels()), 2)


class Resync(Base):
    def test_state(self):
        self.ev("SessionStart")
        w, a = self.ask()
        self.ask(inp={"command": "pwd"})
        n_st, n_ask = len(self.sent("status")), len(self.sent("ask"))
        self.state([{"src": "outra", "mood": "busy", "asks": 1}])      # a placa não conhece esta fonte
        self.assertEqual(len(self.sent("status")), n_st + 1)
        self.assertEqual(len(self.sent("ask")), n_ask + 2)
        self.assertEqual(self.sent("ask")[-2]["ts"], a["ts"])
        self.state([])                                                 # menos de 5 s: espera
        self.assertEqual(len(self.sent("status")), n_st + 1)
        self.later(daemon.RESYNC_S)
        self.assertEqual(len(self.sent("status")), n_st + 2)
        self.later(daemon.RESYNC_S)
        self.state([{"src": "mac", "mood": "alert", "asks": 2}])       # a placa está em dia
        self.assertEqual(len(self.sent("ask")), n_ask + 4)
        self.later(daemon.RESYNC_S)
        self.state([{"src": "mac", "mood": "alert", "asks": 1}])       # perdeu um: manda os dois
        self.assertEqual(len(self.sent("ask")), n_ask + 6)
        self.later(daemon.RESYNC_S)
        n = len(self.out)
        self.p.message("p/d/state", b"{")
        self.assertEqual(len(self.out), n)
        self.state([], online=False)                                   # a vontade da placa: os pedidos saem
        self.assertEqual(len(self.cancels()), 2)
        self.assertEqual([m["type"] for m in self.out[n:]], ["ask.cancel", "ask.cancel"])
        self.assertTrue(w.ev.is_set())
        self.assertEqual(w.decision, None)
        _, a = self.ask(inp={"command": "id"})                        # desligada: o hook sai já
        self.assertIsNone(a)
        self.assertEqual(self.p.asks, {})
        self.later(daemon.RESYNC_S)
        self.state([{"src": "mac", "mood": "alert", "asks": 0}])       # voltou
        _, a = self.ask(inp={"command": "id"})
        self.assertIsNotNone(a)

    def test_lost_cancel(self):
        self.ev("SessionStart")
        _, a = self.ask()
        self.ev("PostToolUse", tool_name="Bash", tool_input=LS)         # o ask.cancel se perde
        _, b = self.ask(inp={"command": "pwd"})
        n = len(self.out)
        self.state([{"src": "mac", "mood": "alert", "asks": 1}])       # conta o velho no lugar do novo
        self.assertEqual([(m["type"], m.get("id")) for m in self.out[n:]], [("ask.cancel", a["id"])])
        self.later(daemon.RESYNC_S)
        self.state([{"src": "mac", "mood": "alert", "asks": 0}])       # tirou o velho: falta o novo
        self.assertEqual(self.sent("ask")[-1]["id"], b["id"])
        self.assertEqual(self.cancels(), [a["id"], a["id"]])

    def test_no_sessions(self):
        self.state([])
        self.assertEqual(self.out, [])
