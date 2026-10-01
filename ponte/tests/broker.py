"""Broker MQTT 3.1.1 falso, em processo, sem TLS (só para os testes).

Imita o que importa do AWS IoT Core: a mensagem retida só chega a quem assina o tópico
exato, a última vontade sai quando a conexão cai sem DISCONNECT, e não sai com ele.
"""
import json
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin", "lib"))
import mqtt  # noqa: E402


def read_str(body, pos):
    n = struct.unpack("!H", body[pos:pos + 2])[0]
    return bytes(body[pos + 2:pos + 2 + n]), pos + 2 + n


def parse_connect(body):
    name, pos = read_str(body, 0)
    level, flags = body[pos], body[pos + 1]
    keepalive = struct.unpack("!H", body[pos + 2:pos + 4])[0]
    pos += 4
    cid, pos = read_str(body, pos)
    out = {"proto": name.decode(), "level": level, "clean": bool(flags & 2), "keepalive": keepalive,
           "client_id": cid.decode(), "will": None}
    if flags & 4:
        topic, pos = read_str(body, pos)
        msg, pos = read_str(body, pos)
        out["will"] = {"topic": topic.decode(), "payload": msg, "qos": (flags >> 3) & 3, "retain": bool(flags & 0x20)}
    return out


def parse_subscribe(body):
    pid = struct.unpack("!H", body[:2])[0]
    pos, subs = 2, []
    while pos < len(body):
        t, pos = read_str(body, pos)
        subs.append((t.decode(), body[pos]))
        pos += 1
    return pid, subs


def matches(filt, topic):
    f, t = filt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)


class Conn:
    def __init__(self, broker, sock):
        self.broker, self.sock = broker, sock
        self.info = None
        self.subs = []
        self.clean_exit = False
        self.lock = threading.Lock()
        self.pid = 0

    def send(self, data):
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass

    def run(self):
        rd = mqtt.Reader()
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                for kind, flags, body in rd.feed(data):
                    self.handle(kind, flags, body)
                    if self.clean_exit:
                        return
        except OSError:
            pass
        finally:
            self.broker.gone(self)

    def handle(self, kind, flags, body):
        b = self.broker
        if kind == mqtt.CONNECT:
            self.info = parse_connect(body)
            b.record("connect", self.info)
            self.send(bytes([0x20, 2, 0, b.connack_rc]))
        elif kind == mqtt.SUBSCRIBE:
            pid, subs = parse_subscribe(body)
            self.subs += [t for t, _ in subs]
            b.record("subscribe", subs)
            self.send(mqtt.packet(mqtt.SUBACK, 0, struct.pack("!H", pid) + bytes(min(q, 1) for _, q in subs)))
            for t, _ in subs:
                if "+" not in t and "#" not in t and t in b.retained:
                    self.deliver(t, b.retained[t], retain=True)
        elif kind == mqtt.PUBLISH:
            topic, payload, qos, pid, retain = mqtt.parse_publish(flags, body)
            if qos == 1:
                self.send(mqtt.puback_packet(pid))
            b.route(topic, payload, qos, retain, self)
        elif kind == mqtt.PINGREQ:
            b.record("ping", None)
            self.send(mqtt.PINGRESP_PACKET)
        elif kind == mqtt.DISCONNECT:
            self.clean_exit = True
            b.record("disconnect", None)

    def deliver(self, topic, payload, retain=False):
        self.pid = self.pid % 65535 + 1
        self.send(mqtt.publish_packet(topic, payload, 1, self.pid, retain=retain))


class Broker:
    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.conns = []
        self.retained = {}
        self.events = []            # (tipo, dados)
        self.published = []         # (tópico, payload bytes, qos, retain, client_id)
        self.cond = threading.Condition()
        self.connack_rc = 0
        self.refuse = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            if self.refuse:
                s.close()
                continue
            c = Conn(self, s)
            with self.cond:
                self.conns.append(c)
            threading.Thread(target=c.run, daemon=True).start()

    def connect(self):
        """A função de conexão para o mqtt.Client (no lugar do TLS)."""
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def record(self, kind, data):
        with self.cond:
            self.events.append((kind, data))
            self.cond.notify_all()

    def route(self, topic, payload, qos, retain, sender=None):
        with self.cond:
            if retain:
                self.retained[topic] = payload
            cid = sender.info["client_id"] if sender and sender.info else None
            self.published.append((topic, payload, qos, retain, cid))
            targets = [c for c in self.conns if any(matches(f, topic) for f in c.subs)]
            self.cond.notify_all()
        for c in targets:
            c.deliver(topic, payload)

    def gone(self, conn):
        with self.cond:
            if conn in self.conns:
                self.conns.remove(conn)
            self.events.append(("gone", conn.clean_exit))
            self.cond.notify_all()
        try:
            conn.sock.close()
        except OSError:
            pass
        will = conn.info and conn.info["will"]
        if will and not conn.clean_exit:
            self.route(will["topic"], will["payload"], will["qos"], will["retain"])

    # ------------------------------------------------------------ para os testes

    def inject(self, topic, obj, retain=False):
        """Publica como se fosse a placa."""
        payload = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.route(topic, payload, 1, retain)

    def drop(self):
        """Derruba as conexões sem DISCONNECT (rede caiu)."""
        with self.cond:
            conns = list(self.conns)
        for c in conns:
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def messages(self, topic=None):
        with self.cond:
            out = []
            for t, p, *_ in self.published:
                if topic is None or t == topic:
                    try:
                        out.append(json.loads(p))
                    except ValueError:
                        out.append(p)
            return out

    def wait_msg(self, topic, pred=lambda m: True, timeout=5.0, after=0):
        """Espera uma mensagem nesse tópico (a partir da posição `after`) e devolve ela."""
        end = time.time() + timeout
        with self.cond:
            while True:
                for m in self.messages(topic)[after:]:
                    if isinstance(m, dict) and pred(m):
                        return m
                left = end - time.time()
                if left <= 0:
                    raise AssertionError(f"não chegou mensagem esperada em {topic}")
                self.cond.wait(left)

    def wait_event(self, kind, timeout=5.0, count=1):
        end = time.time() + timeout
        with self.cond:
            while sum(1 for k, _ in self.events if k == kind) < count:
                left = end - time.time()
                if left <= 0:
                    raise AssertionError(f"não aconteceu: {kind}")
                self.cond.wait(left)
            return [d for k, d in self.events if k == kind]

    def close(self):
        self.srv.close()
        self.drop()
