"""Cliente MQTT 3.1.1 mínimo, só com a biblioteca padrão (docs/ponte.md).

Faz só o que a ponte usa: CONNECT com última vontade e sessão limpa, SUBSCRIBE QoS 1,
PUBLISH QoS 0 e 1 (com PUBACK nos dois sentidos), PINGREQ, DISCONNECT e reconexão com
backoff. A conexão vem de uma função (connect), para os testes rodarem sem TLS: na
ponte ela é tls_connect, com o certificado da fonte, na porta 8883 ou na 443 (ALPN).

Um thread só lê e escreve no socket (o ssl do Python não gosta de dois); publish()
só põe o pacote na fila.
"""
import collections
import random
import socket
import ssl
import struct
import threading
import time

CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK = 1, 2, 3, 4, 8, 9
PINGREQ, PINGRESP, DISCONNECT = 12, 13, 14
PINGREQ_PACKET = bytes([PINGREQ << 4, 0])
PINGRESP_PACKET = bytes([PINGRESP << 4, 0])
DISCONNECT_PACKET = bytes([DISCONNECT << 4, 0])
ALPN = "x-amzn-mqtt-ca"     # MQTT com certificado na porta 443 do AWS IoT Core
IO_TIMEOUT = 10             # conectar, handshake, CONNACK e escrita
POLL_S = 0.1                # quanto o thread espera por bytes antes de olhar a fila
BACKOFF_MAX = 60


# ------------------------------------------------------------------ codec

def varint(n):
    """Tamanho restante do cabeçalho fixo (1 a 4 bytes)."""
    if not 0 <= n <= 268435455:
        raise ValueError("pacote grande demais")
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def mqtt_str(s):
    b = s.encode() if isinstance(s, str) else bytes(s)
    return struct.pack("!H", len(b)) + b


def packet(kind, flags, body):
    return bytes([kind << 4 | flags]) + varint(len(body)) + body


def connect_packet(client_id, keepalive=60, will=None, clean=True, username=None, password=None):
    """will = (tópico, payload, qos, retain)."""
    flags = 0x02 if clean else 0
    payload = mqtt_str(client_id)
    if will:
        topic, msg, qos, retain = will
        flags |= 0x04 | qos << 3 | (0x20 if retain else 0)
        payload += mqtt_str(topic) + mqtt_str(msg)
    if username is not None:
        flags |= 0x80
        payload += mqtt_str(username)
    if password is not None:
        flags |= 0x40
        payload += mqtt_str(password)
    return packet(CONNECT, 0, mqtt_str("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive) + payload)


def publish_packet(topic, payload, qos=0, pid=0, retain=False, dup=False):
    if isinstance(payload, str):
        payload = payload.encode()
    body = mqtt_str(topic) + (struct.pack("!H", pid) if qos else b"") + payload
    return packet(PUBLISH, (8 if dup else 0) | qos << 1 | (1 if retain else 0), body)


def subscribe_packet(pid, topics):
    """topics = [(filtro, qos)]."""
    body = struct.pack("!H", pid) + b"".join(mqtt_str(t) + bytes([q]) for t, q in topics)
    return packet(SUBSCRIBE, 2, body)


def puback_packet(pid):
    return packet(PUBACK, 0, struct.pack("!H", pid))


def parse_publish(flags, body):
    """-> (tópico, payload, qos, pid, retain)."""
    n = struct.unpack("!H", body[:2])[0]
    topic = body[2:2 + n].decode()
    qos, pos, pid = (flags >> 1) & 3, 2 + n, 0
    if qos:
        pid = struct.unpack("!H", body[pos:pos + 2])[0]
        pos += 2
    return topic, bytes(body[pos:]), qos, pid, bool(flags & 1)


class Reader:
    """Junta os bytes que chegam e devolve os pacotes completos: (tipo, flags, corpo)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 2:
            size, mult, i = 0, 1, 1
            while True:
                if i >= len(self.buf):
                    return out          # tamanho ainda incompleto
                b = self.buf[i]
                size += (b & 0x7F) * mult
                mult *= 128
                i += 1
                if not b & 0x80:
                    break
                if i > 4:
                    raise ValueError("tamanho de pacote inválido")
            if len(self.buf) < i + size:
                return out
            first = self.buf[0]
            out.append((first >> 4, first & 0x0F, bytes(self.buf[i:i + size])))
            del self.buf[:i + size]
        return out


# ------------------------------------------------------------------ conexão

def tls_connect(host, port, ca, cert, key, timeout=IO_TIMEOUT):
    """Função que abre o TLS com o certificado de cliente (a porta 443 usa ALPN)."""
    def connect():
        ctx = ssl.create_default_context(cafile=ca)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        if port == 443:
            ctx.set_alpn_protocols([ALPN])
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            return ctx.wrap_socket(raw, server_hostname=host)
        except BaseException:
            raw.close()
            raise
    return connect


def why(e):
    """Motivo curto de um erro, sem nomes de arquivo."""
    if isinstance(e, ssl.SSLError):
        return f"TLS: {e.reason or e.__class__.__name__}"
    if isinstance(e, OSError) and e.strerror:
        return f"{e.__class__.__name__}: {e.strerror}"
    return f"{e.__class__.__name__}: {e}"


class Client:
    def __init__(self, client_id, connect, subs=(), will=None, keepalive=60,
                 on_message=None, on_connect=None, log=None):
        self.client_id = client_id
        self.subs = list(subs)
        self.will = will
        self.keepalive = keepalive
        self.on_message = on_message
        self.on_connect = on_connect
        self.log = log or (lambda *a: None)
        self.connected = threading.Event()
        self.suback = None          # códigos do último SUBACK (0x80 = recusado)
        self._connect = connect
        self._out = collections.deque()
        self._lock = threading.Lock()
        self._acks = {}             # pid -> Event de quem espera o PUBACK
        self._pid = 0
        self._stop = threading.Event()
        self._thread = None
        self._sock = None
        self._up_since = self._last_tx = self._last_rx = 0.0
        self._ping_out = False      # PINGREQ sem resposta ainda
        self.down_at = time.time()  # desde quando está desconectado (a subida conta)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="mqtt", daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        """Manda o que estiver na fila, o DISCONNECT (sem última vontade) e fecha."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def publish(self, topic, payload, qos=0, timeout=None):
        """Põe na fila. Com timeout (e QoS 1), espera o PUBACK. Desconectado: False."""
        if not self.connected.is_set():
            return False
        ev = None
        with self._lock:
            pid = self._next_pid() if qos else 0
            if qos and timeout:
                ev = self._acks[pid] = threading.Event()
                ev.ok = False
            self._out.append(publish_packet(topic, payload, qos, pid))
        if ev is None:
            return True
        ev.wait(timeout)
        with self._lock:
            self._acks.pop(pid, None)
        return ev.ok

    def _next_pid(self):
        self._pid = self._pid % 65535 + 1
        return self._pid

    def _run(self):
        delay = 1
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as e:
                if not self._stop.is_set():
                    self.log(f"mqtt: {why(e)}")
            finally:
                self._drop()
            if self._stop.is_set():
                break
            if self._up_since and time.time() - self._up_since > 30:
                delay = 1           # a conexão durou: recomeça o backoff
            self._stop.wait(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, BACKOFF_MAX)

    def _session(self):
        self._up_since = 0.0
        s = self._sock = self._connect()
        s.settimeout(IO_TIMEOUT)
        s.sendall(connect_packet(self.client_id, self.keepalive, self.will))
        rd, pks = Reader(), []
        while not pks:
            data = s.recv(4096)
            if not data:
                raise ConnectionError("o broker fechou antes do CONNACK")
            pks = rd.feed(data)
        kind, _, body = pks[0]
        if kind != CONNACK or len(body) < 2:
            raise ConnectionError("resposta inesperada ao CONNECT")
        if body[1]:
            raise ConnectionError(f"CONNECT recusado (código {body[1]})")
        with self._lock:
            self._out.clear()
            if self.subs:
                self._out.append(subscribe_packet(self._next_pid(), [(t, 1) for t in self.subs]))
        self._up_since = self._last_tx = self._last_rx = time.time()
        self._ping_out = False
        self.connected.set()
        self.log("mqtt conectado")
        if self.on_connect:
            self._call(self.on_connect)
        for pk in pks[1:]:
            self._handle(pk)
        while not self._stop.is_set():
            self._flush(s)
            now = time.time()
            if now - self._last_rx > self.keepalive * 1.5:
                raise ConnectionError("o broker parou de responder")
            # O ping também quando nada chega: só QoS 0 saindo não traz resposta nenhuma.
            if not self._ping_out and (now - self._last_tx >= self.keepalive * 0.75
                                       or now - self._last_rx >= self.keepalive * 0.75):
                self._ping_out = True
                with self._lock:
                    self._out.append(PINGREQ_PACKET)
                continue
            s.settimeout(POLL_S)
            try:
                data = s.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                continue
            if not data:
                raise ConnectionError("o broker fechou a conexão")
            self._last_rx, self._ping_out = time.time(), False
            for pk in rd.feed(data):
                self._handle(pk)
        with self._lock:
            self._out.append(DISCONNECT_PACKET)
        self._flush(s)

    def _flush(self, s):
        with self._lock:
            if not self._out:
                return
            data = b"".join(self._out)
            self._out.clear()
        s.settimeout(IO_TIMEOUT)
        s.sendall(data)
        self._last_tx = time.time()

    def _handle(self, pk):
        kind, flags, body = pk
        if kind == PUBLISH:
            topic, payload, qos, pid, _ = parse_publish(flags, body)
            if qos == 1:
                with self._lock:
                    self._out.append(puback_packet(pid))
            if qos < 2 and self.on_message:
                self._call(self.on_message, topic, payload)
        elif kind == PUBACK and len(body) >= 2:
            with self._lock:
                ev = self._acks.pop(struct.unpack("!H", body[:2])[0], None)
            if ev:
                ev.ok = True
                ev.set()
        elif kind == SUBACK:
            self.suback = list(body[2:])
            if 0x80 in self.suback:
                self.log("mqtt: assinatura recusada (confira a policy)")

    def _call(self, fn, *a):
        try:
            fn(*a)
        except Exception as e:
            self.log(f"mqtt: erro no callback: {why(e)}")

    def _drop(self):
        was = self.connected.is_set()
        self.connected.clear()
        s, self._sock = self._sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass
        with self._lock:
            self._out.clear()
            acks, self._acks = self._acks, {}
        for ev in acks.values():
            ev.set()
        if was:
            self.down_at = time.time()
            if not self._stop.is_set():
                self.log("mqtt caiu")
