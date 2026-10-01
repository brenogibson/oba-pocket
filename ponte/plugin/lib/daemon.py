#!/usr/bin/env python3
"""A ponte entre o Claude Code e um Oba (docs/ponte.md, docs/protocol.md#fontes-externas-ext).

Um processo por máquina, subido pelo hook.py na primeira vez. Ele:
  - recebe os eventos dos hooks pelo socket Unix ~/.oba-ponte/ponte.sock (uma linha JSON
    por conexão; no PermissionRequest a conexão fica aberta até a decisão);
  - junta as sessões do Claude Code desta máquina num status (idle, busy ou alert) e
    publica em <p>/<dev>/ext/<thing>;
  - transforma os pedidos de permissão em ask, espera o reply da placa em ext/<thing>/re
    e manda o ask.cancel quando o pedido foi respondido em outro lugar;
  - acompanha o state da placa (retido) para republicar o que ela perdeu;
  - sai sozinho depois de 30 min sem sessões e sem eventos.

O log (~/.oba-ponte/ponte.log) só leva tipos de evento, ids e erros: nunca o tool_input,
o transcript, comandos ou caminhos.
"""
import fcntl
import json
import os
import secrets
import select
import signal
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)
import comum  # noqa: E402
import mqtt  # noqa: E402
import resumo  # noqa: E402

TTL_S = 180                 # ttl_s do status; sem mudança, republica a cada TTL_S / 3
ASK_TTL_S = 280             # o hook espera 290 s
LABEL_EVERY_S = 1.5         # só o rótulo mudou: no máximo um status nesse intervalo
BUSY_IDLE_S = 15 * 60       # busy ou alert sem evento vira idle
STALE_S = 12 * 3600         # sessão sem evento nenhum some (o Claude Code morreu sem SessionEnd)
EXIT_S = 30 * 60            # sem sessões e sem eventos: o daemon sai
ENDED_S = 60                # depois do SessionEnd, os hooks assíncronos atrasados não trazem a sessão de volta
RESYNC_S = 5                # no máximo uma republicação por state da placa nesse intervalo
TAIL_S = 1.0                # olha o transcript com pedido pendente
TAIL_BACK = 512 * 1024      # no começo do pedido, relê esse final do transcript
POST_KEEP_S = 30            # lembra os PostToolUse recentes (chegaram antes do PermissionRequest)
TICK_S = 0.5
KEEPALIVE = 60
CONNECT_WAIT_S = 5          # pedido logo depois da subida (ou de uma queda): espera o MQTT conectar
NO_ASK = {"AskUserQuestion", "ExitPlanMode"}     # v1: só alert, a pergunta fica no terminal
LOGGED = {"SessionStart", "SessionEnd", "PermissionRequest", "Stop", "StopFailure", "PreCompact"}
RANK = {"idle": 0, "busy": 1, "alert": 2}
WILL = comum.dumps({"v": 1, "type": "status", "online": False})


def same_input(a, b):
    """Mesmo tool_input. Aceita que um dos lados tenha campos a mais (padrões preenchidos)."""
    if a == b:
        return True
    if isinstance(a, dict) and isinstance(b, dict) and a and b:
        small, big = (a, b) if len(a) <= len(b) else (b, a)
        return all(k in big and big[k] == v for k, v in small.items())
    return False


class Session:
    def __init__(self, sid, now):
        self.id = sid
        self.mood = "idle"
        self.tool = None
        self.cwd = ""
        self.transcript = None
        self.last = now         # último evento (relógio do daemon)
        self.t = 0.0            # hora do hook do último evento aplicado (os assíncronos chegam fora de ordem)
        self.tail = None


class Ask:
    def __init__(self, aid, sid, tool, inp, waiter, msg, deadline, agent=None):
        self.id, self.sid, self.tool, self.input = aid, sid, tool, inp
        self.waiter, self.msg, self.deadline = waiter, msg, deadline
        self.agent = agent      # agent_id de um agente em segundo plano; None = a conversa principal
        self.uses = set()       # tool_use do transcript que são este pedido


class Waiter:
    """O hook que espera a decisão de um PermissionRequest."""

    def __init__(self):
        self.ev = threading.Event()
        self.decision = None

    def done(self, decision=None):
        if not self.ev.is_set():
            self.decision = decision
            self.ev.set()


class Tail:
    """Lê só as linhas novas de um transcript JSONL."""

    def __init__(self, path):
        self.path = path
        self.pos = self._size()
        self.rest = b""

    def _size(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def back(self, n=TAIL_BACK):
        """O final do arquivo até onde já foi lido (sem a primeira linha, que pode estar pela metade)."""
        start = max(0, self.pos - n)
        try:
            with open(self.path, "rb") as f:
                f.seek(start)
                data = f.read(self.pos - start)
        except OSError:
            return []
        lines = data.split(b"\n")
        if start:
            lines = lines[1:]
        return lines[:-1] if not data.endswith(b"\n") else lines

    def new(self):
        size = self._size()
        if size < self.pos:             # o arquivo foi trocado
            self.pos, self.rest = 0, b""
        if size == self.pos:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
        except OSError:
            return []
        self.pos += len(data)
        lines = (self.rest + data).split(b"\n")
        self.rest = lines.pop()
        return lines


def tool_blocks(lines):
    """-> ("use", id, nome, input) e ("result", tool_use_id) das linhas do transcript."""
    for line in lines:
        if b"tool_" not in line:
            continue
        try:
            e = json.loads(line)
            content = (e.get("message") or {}).get("content")
        except (ValueError, AttributeError):
            continue
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id"):
                yield "use", b["id"], b.get("name"), b.get("input")
            elif b.get("type") == "tool_result" and b.get("tool_use_id"):
                yield "result", b["tool_use_id"]


class Ponte:
    """A lógica, sem rede: o daemon chama event(), message(), connected() e tick()."""

    def __init__(self, cfg, publish, clock=time.time, log=comum.log):
        self.name = cfg["name"]
        self.thing = cfg["thing"]
        self.publish = publish      # (msg, qos) -> bool
        self.clock = clock
        self.log = log
        self.lock = threading.RLock()
        self.sessions = {}
        self.asks = {}
        self.cancels = {}           # id -> validade: reenviados se a conexão cair
        self.ended = {}             # sessão -> (hora do hook do SessionEnd, relógio do daemon)
        self.posts = []             # (hora do hook, sessão, ferramenta, input) dos PostToolUse recentes
        self.shown = None           # (mood, label) publicado; None = offline
        self.shown_at = 0.0
        self.board = None           # último state da placa (online: false = desligada)
        self.resync_due = False
        self.resync_at = -RESYNC_S
        self.tail_at = 0.0
        self.last_event = clock()
        self.closing = False        # saindo: os hooks que chegam não publicam mais nada

    # -------------------------------------------------------------- saída

    def send(self, type_, qos=1, ts=None, **fields):
        return self.publish(dict({"v": 1, "type": type_, "ts": ts or comum.now_ms()}, **fields), qos)

    def react(self, do):
        self.send("react", qos=0, do=do)

    def view(self):
        """(mood, label) da sessão mais urgente, ou None sem sessões."""
        if not self.sessions:
            return None
        waiting = {a.sid for a in self.asks.values()}
        mood = lambda s: "alert" if s.id in waiting else s.mood
        top = max(self.sessions.values(), key=lambda s: (RANK[mood(s)], s.last))
        m = mood(top)
        return m, resumo.label(self.name, top.cwd, top.tool if m != "idle" else None)

    def sync(self, force=False):
        now, v = self.clock(), self.view()
        if v is None:
            if self.shown is not None and self.send("status", online=False):
                self.shown = None
            return
        old = self.shown
        due = (force or old is None or v[0] != old[0] or now - self.shown_at >= TTL_S / 3
               or (v != old and now - self.shown_at >= LABEL_EVERY_S))
        if due and self.send("status", mood=v[0], label=v[1], ttl_s=TTL_S):
            self.shown, self.shown_at = v, now

    def send_ask(self, a):
        """Sempre com o ts e o ttl_s originais: a placa desconta o atraso."""
        return a.deadline - self.clock() >= 5 and self.send("ask", **a.msg)

    def cancel(self, a, why):
        self.asks.pop(a.id, None)
        a.waiter.done(None)
        self.cancels[a.id] = a.deadline
        self.send("ask.cancel", id=a.id)
        self.log(f"ask {a.id} cancelado ({why})")

    # -------------------------------------------------------------- hooks

    def event(self, msg, waiter=None):
        pre = self.prepare(msg) if waiter else None     # fora da trava: uma entrada enorme não para as outras sessões
        with self.lock:
            if self.closing:
                if waiter:
                    waiter.done(None)
                return
            try:
                self._event(msg, waiter, pre)
            finally:
                if waiter and not any(a.waiter is waiter for a in self.asks.values()):
                    waiter.done(None)
                self.sync()

    def prepare(self, msg):
        """O resumo de um PermissionRequest que vai para a placa: (title, body, danger, label), ou None."""
        ev = msg.get("ev") if isinstance(msg.get("ev"), dict) else {}
        tool = ev.get("tool_name") if isinstance(ev.get("tool_name"), str) else None
        if ev.get("hook_event_name") != "PermissionRequest" or tool in NO_ASK:
            return None
        cwd = ev.get("cwd") if isinstance(ev.get("cwd"), str) else ""
        inp = ev.get("tool_input")
        title, body, partial = resumo.summarize(tool, inp, cwd)
        return title, body, partial or resumo.danger(tool, inp, cwd), resumo.label(self.name, cwd)

    def _event(self, msg, waiter, pre=None):
        ev = msg.get("ev") if isinstance(msg.get("ev"), dict) else {}
        name = ev.get("hook_event_name")
        sid = str(ev.get("session_id") or "?")
        agent = ev.get("agent_id") if isinstance(ev.get("agent_id"), str) and ev["agent_id"] else None
        now = self.clock()
        t = msg.get("t") if isinstance(msg.get("t"), (int, float)) else now
        self.last_event = now
        if name in LOGGED:
            self.log(f"{name} {sid[:8]}")
        if name == "SessionEnd":
            self.cancel_session(sid, "fim da sessão")
            self.ended[sid] = (t, now)
            if self.sessions.pop(sid, None):
                self.log(f"sessão {sid[:8]} saiu")
            return
        if sid in self.ended:
            # Os assíncronos (até o SessionStart) chegam fora de ordem: só um SessionStart
            # de depois do fim (um --resume) traz a sessão de volta.
            if name != "SessionStart" or t <= self.ended[sid][0]:
                return
            del self.ended[sid]
        s = self.sessions.get(sid)
        if not s:
            s = self.sessions[sid] = Session(sid, now)
        s.last = now
        if isinstance(ev.get("cwd"), str):
            s.cwd = ev["cwd"]
        if isinstance(ev.get("transcript_path"), str):
            s.transcript = ev["transcript_path"]
        fresh = t >= s.t
        if fresh:
            s.t = t
        tool = ev.get("tool_name") if isinstance(ev.get("tool_name"), str) else None
        inp = ev.get("tool_input")

        def mood(m, tool=None):
            if fresh:
                s.mood, s.tool = m, tool

        if name == "SessionStart":
            if ev.get("source") != "compact":
                mood("idle")
        elif name == "UserPromptSubmit":
            self.cancel_session(sid, "novo prompt", main=True)
            mood("busy")
        elif name == "PreToolUse":
            mood("busy", tool)
        elif name in ("PostToolUse", "PostToolUseFailure"):
            self.posts.append((t, sid, tool, inp))
            for a in [a for a in self.asks.values() if a.sid == sid and a.tool == tool and same_input(a.input, inp)]:
                self.cancel(a, "a ferramenta rodou")
            mood("busy", tool)
        elif name == "PermissionRequest":
            s.mood, s.tool, s.t = "alert", tool, max(s.t, t)
            if waiter and pre:
                self.new_ask(s, tool, inp, t, waiter, pre, agent)
        elif name == "Notification":
            kind = ev.get("notification_type")
            if kind in ("permission_prompt", "elicitation_dialog"):
                mood("alert", s.tool)
            elif kind == "idle_prompt":
                mood("idle")
        elif name == "Stop":
            self.cancel_session(sid, "fim da resposta", main=True)
            self.react("happy")
            mood("idle")
        elif name == "StopFailure":
            self.cancel_session(sid, "erro", main=True)
            self.react("scared")
            mood("idle")
        elif name == "PreCompact":
            self.react("dizzy")

    def new_ask(self, s, tool, inp, t, waiter, pre, agent=None):
        now = self.clock()
        if any(p[0] > t and p[1] == s.id and p[2] == tool and same_input(p[3], inp) for p in self.posts):
            return          # a ferramenta já rodou: respondido no terminal antes de o pedido chegar
        if self.board and self.board.get("online") is False:
            # Nos agentes em segundo plano e no claude -p, o Claude Code espera o hook antes
            # do diálogo: sem placa, o hook sai já.
            self.log("placa desligada: o pedido fica no terminal")
            return
        title, body, danger, label = pre
        aid = secrets.token_urlsafe(8)          # 11 caracteres de A-Z a-z 0-9 _ -
        msg = {"ts": comum.now_ms(), "id": aid, "tool": resumo.cut(tool or "?", resumo.TOOL_MAX),
               "title": title, "body": body, "danger": danger, "label": label, "ttl_s": ASK_TTL_S}
        a = self.asks[aid] = Ask(aid, s.id, tool, inp, waiter, msg, now + ASK_TTL_S, agent)
        if s.transcript:
            if not s.tail or s.tail.path != s.transcript:
                s.tail = Tail(s.transcript)
            for b in tool_blocks(s.tail.back()):
                if b[0] == "use" and b[2] == tool and same_input(b[3], inp):
                    a.uses.add(b[1])
                elif b[0] == "result":
                    a.uses.discard(b[1])        # um igual que já tinha terminado
        if not self.send_ask(a):
            del self.asks[aid]              # sem MQTT: o hook sai já (o Oba nunca viu o pedido)
            self.log(f"ask {aid} não saiu (sem conexão)")
            return
        self.log(f"ask {aid} enviado{' (danger)' if msg['danger'] else ''}")

    def cancel_session(self, sid, why, main=False):
        """main: só os pedidos da conversa principal. O prompt novo e o fim da resposta não
        mexem nos pedidos dos agentes em segundo plano, que continuam rodando."""
        for a in [a for a in self.asks.values() if a.sid == sid and not (main and a.agent)]:
            self.cancel(a, why)

    def hook_gone(self, waiter):
        with self.lock:
            for a in [a for a in self.asks.values() if a.waiter is waiter]:
                self.cancel(a, "o hook saiu")
            self.sync()

    # -------------------------------------------------------------- placa

    def connected(self):
        """A conexão MQTT (re)abriu: manda tudo de novo."""
        with self.lock:
            if self.closing:
                return
            now = self.clock()
            if self.sessions:
                self.sync(force=True)
            for a in list(self.asks.values()):
                self.send_ask(a)
            for aid, until in list(self.cancels.items()):
                if until > now:
                    self.send("ask.cancel", id=aid)

    def message(self, topic, payload):
        try:
            m = json.loads(payload)
        except ValueError:
            return
        if not isinstance(m, dict):
            return
        with self.lock:
            if self.closing:
                return
            if topic.endswith("/state"):
                self.on_state(m)
            elif m.get("type") == "reply" and m.get("re") == "ask":
                self.on_reply(m)
            self.sync()

    def on_reply(self, m):
        a = self.asks.pop(m.get("id"), None) if isinstance(m.get("id"), str) else None
        if not a:
            return
        choice = m.get("choice")
        s = self.sessions.get(a.sid)
        err = m.get("error") if isinstance(m.get("error"), str) else ""
        self.log(f"ask {a.id} {choice}" + (f" ({err[:20]})" if err else ""))
        if choice in ("allow", "deny"):
            a.waiter.done(choice)
            self.react("happy" if choice == "allow" else "shy")
            if s:
                s.mood = "busy"         # o Claude continua
        else:
            # skip: o botão Terminal, ou a placa recusou ("fila cheia", "fontes demais",
            # "falta o title") ou deixou vencer ("venceu"). O terminal decide, e o id não
            # volta para a placa: ele já saiu de self.asks.
            a.waiter.done(None)
            if s:
                s.mood = "alert"        # o diálogo continua aberto no terminal

    def on_state(self, m):
        if m.get("type") != "state":
            return
        self.board = m
        if m.get("online") is False:        # a última vontade da placa: ela saiu
            for a in list(self.asks.values()):
                self.cancel(a, "placa desligada")
            return
        self.resync_due = True
        self.resync()

    def resync(self):
        now = self.clock()
        if not self.resync_due or now - self.resync_at < RESYNC_S:
            return
        self.resync_due, self.resync_at = False, now
        ext = self.board.get("ext") if isinstance(self.board.get("ext"), list) else []
        mine = next((e for e in ext if isinstance(e, dict) and e.get("src") == self.thing), None)
        if mine is None and self.sessions:
            self.log("a placa não conhece esta fonte: status de novo")
            self.sync(force=True)
        have = mine.get("asks") if mine and isinstance(mine.get("asks"), int) else 0
        if have and any(until > now for until in self.cancels.values()):
            # Ela pode ter perdido um ask.cancel e contar um pedido velho no lugar de um
            # novo: os cancels de novo (ela ignora os que não conhece). Sem o velho, o
            # próximo state mostra a falta e o novo vai de novo.
            for aid, until in list(self.cancels.items()):
                if until > now:
                    self.send("ask.cancel", id=aid)
        if have < len(self.asks):
            self.log(f"a placa tem {have} de {len(self.asks)} pedidos: de novo")
            for a in list(self.asks.values()):
                self.send_ask(a)

    # -------------------------------------------------------------- relógio

    def tick(self):
        with self.lock:
            now = self.clock()
            for a in [a for a in self.asks.values() if a.deadline <= now]:
                self.cancel(a, "venceu")
            if now - self.tail_at >= TAIL_S:
                self.tail_at = now
                self.follow()
            for sid, s in list(self.sessions.items()):
                if now - s.last >= STALE_S:
                    self.cancel_session(sid, "sessão parada")
                    del self.sessions[sid]
                elif s.mood in ("busy", "alert") and now - s.last >= BUSY_IDLE_S:
                    s.mood, s.tool = "idle", None   # alert também: o terminal pode ter fechado com o diálogo aberto
            self.cancels = {k: v for k, v in self.cancels.items() if v > now}
            self.ended = {k: v for k, v in self.ended.items() if now - v[1] < ENDED_S}
            self.posts = [p for p in self.posts if p[0] > now - POST_KEEP_S]
            self.resync()
            self.sync()

    def follow(self):
        """(c) o transcript ganhou o tool_use do pedido seguido do tool_result dele."""
        for s in self.sessions.values():
            mine = [a for a in self.asks.values() if a.sid == s.id]
            if not mine or not s.tail:
                s.tail = s.tail if mine else None
                continue
            for b in tool_blocks(s.tail.new()):
                for a in mine:
                    if b[0] == "use" and b[2] == a.tool and same_input(b[3], a.input):
                        a.uses.add(b[1])
                    elif b[0] == "result" and b[1] in a.uses and a.id in self.asks:
                        self.cancel(a, "respondido no terminal")

    def idle_exit(self):
        with self.lock:
            return not self.sessions and not self.asks and self.clock() - self.last_event >= EXIT_S

    def try_close(self):
        """Sem pedidos abertos, fecha: daqui em diante os hooks não publicam nada, e o
        terminal decide. Conferir e fechar juntos, senão um pedido chega no meio. -> fechou"""
        with self.lock:
            if not self.asks:
                self.closing = True
            return self.closing

    def shutdown(self):
        with self.lock:
            self.closing = True
            for a in list(self.asks.values()):
                self.cancel(a, "a ponte saiu")
            self.sessions.clear()
            self.sync()

    def status(self):
        with self.lock:
            return {"sessions": len(self.sessions), "asks": len(self.asks),
                    "mood": self.shown[0] if self.shown else None}


class Daemon:
    """O processo: socket Unix, MQTT e o relógio em volta da Ponte."""

    def __init__(self, home, cfg, connect=None, log=comum.log):
        self.home, self.cfg, self.log = home, cfg, log
        self.ext, re_topic, state = comum.topics(cfg)
        self.mqtt = mqtt.Client(
            cfg["thing"],
            connect or mqtt.tls_connect(cfg["endpoint"], cfg["port"], cfg["ca"], cfg["cert"], cfg["key"]),
            subs=[re_topic, state], will=(self.ext, WILL, 1, False), keepalive=KEEPALIVE,
            on_message=lambda t, p: self.ponte.message(t, p),
            on_connect=lambda: self.ponte.connected(), log=log)
        self.ponte = Ponte(cfg, self._publish, log=log)
        self._stop = threading.Event()
        self.ready = threading.Event()
        self.newer = False          # um hook de um plugin mais novo: sai assim que puder

    def _publish(self, msg, qos):
        return self.mqtt.publish(self.ext, comum.dumps(msg), qos)

    def stop(self, *_):
        self._stop.set()

    def serve(self):
        sock = comum.path(self.home, comum.SOCK)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(sock)
        except FileNotFoundError:
            pass
        srv.bind(sock)
        os.chmod(sock, 0o600)
        ino = os.stat(sock).st_ino
        srv.listen(16)
        srv.settimeout(0.5)
        self.mqtt.start()
        threading.Thread(target=self._accept, args=(srv,), name="accept", daemon=True).start()
        self.log("ponte no ar")
        self.ready.set()
        try:
            while not self._stop.wait(TICK_S):
                self.ponte.tick()
                if self.ponte.idle_exit():
                    self.log("30 min sem sessões: saindo")
                    break
                if self.newer and self.ponte.try_close():
                    self.log("plugin mais novo: saindo (o próximo hook sobe o novo)")
                    break
        finally:
            srv.close()
            try:
                if os.stat(sock).st_ino == ino:
                    os.unlink(sock)
            except OSError:
                pass
            self.ponte.shutdown()
            self.mqtt.stop()
            self.log("ponte parada")

    def _accept(self, srv):
        while not self._stop.is_set():
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._conn, args=(c,), name="hook", daemon=True).start()

    def _conn(self, c):
        try:
            c.settimeout(10)
            line = comum.read_line(c)
            if not line:
                return
            msg = json.loads(line)
            if not isinstance(msg, dict):
                return
            ctl = msg.get("ctl")
            if ctl:
                out = dict(self.ponte.status(), ok=True, mqtt=self.mqtt.connected.is_set())
                c.sendall(comum.dumps(out).encode() + b"\n")
                if ctl == "quit":
                    self.stop()
                return
            v = msg.get("ver")
            if isinstance(v, list) and v and all(type(x) is int for x in v) and tuple(v) > comum.VERSION:
                if not self.newer and isinstance(msg.get("lib"), str):
                    try:
                        comum.note_newest(self.home, v, msg["lib"])    # os hooks velhos sobem o novo
                    except OSError as e:
                        self.log(f"{comum.NEWEST}: {mqtt.why(e)}")
                self.newer = True
            if not msg.get("wait"):
                self.ponte.event(msg)
                return
            if not self.mqtt.connected.is_set():
                # Logo depois da subida (ou de uma queda) o MQTT ainda está conectando, e o
                # pedido nem sairia. Sem rede de verdade, passa daqui em poucos segundos.
                self.mqtt.connected.wait(max(0.0, self.mqtt.down_at + CONNECT_WAIT_S - time.time()))
            w = Waiter()
            self.ponte.event(msg, w)
            while not w.ev.wait(0.5):
                if self._stop.is_set():
                    w.done(None)
                elif closed(c):
                    self.ponte.hook_gone(w)     # (e) o processo do hook morreu
                    return
            c.sendall(comum.dumps({"decision": w.decision}).encode() + b"\n")
        except Exception as e:
            self.log(f"hook: {mqtt.why(e)}")
        finally:
            c.close()


def closed(c):
    try:
        r, _, _ = select.select([c], [], [], 0)
        return bool(r) and c.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def lock(home, wait_s=3.0):
    """Trava de uma ponte por pasta. Espera um pouco: a anterior pode estar saindo."""
    f = open(comum.path(home, comum.LOCK), "a")
    os.chmod(f.name, 0o600)
    end = time.time() + wait_s
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.time() >= end:
                f.close()
                return None
            time.sleep(0.1)


def installing(home):
    """O install.py está mexendo na pasta (a trava ponte.install está presa)."""
    try:
        with open(comum.path(home, comum.INSTALL), "a") as f:
            os.chmod(f.name, 0o600)
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    return False


def main():
    home = comum.home()
    if not home or not os.path.isfile(comum.path(home, comum.CONFIG)):
        return 0                    # OBA_PONTE_HOME relativo, ou nada instalado (não recria a pasta)
    if installing(home):
        return 0
    os.chmod(home, 0o700)
    held = lock(home)
    if not held:
        return 0                    # já tem uma ponte rodando
    try:
        cfg = comum.load_config(home)
    except Exception as e:
        comum.log(f"config: {mqtt.why(e) if isinstance(e, OSError) else e}")
        return 1
    try:
        comum.note_newest(home, comum.VERSION, HERE)
    except OSError as e:
        comum.log(f"{comum.NEWEST}: {mqtt.why(e)}")
    d = Daemon(home, cfg)
    signal.signal(signal.SIGTERM, d.stop)
    signal.signal(signal.SIGINT, d.stop)
    d.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
