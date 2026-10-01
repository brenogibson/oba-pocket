import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin", "lib"))
import mqtt  # noqa: E402
from broker import Broker, matches, parse_connect  # noqa: E402


class Codec(unittest.TestCase):
    def test_varint(self):
        for n, b in [(0, "00"), (127, "7f"), (128, "8001"), (16383, "ff7f"), (16384, "808001"),
                     (2097151, "ffff7f"), (268435455, "ffffff7f")]:
            self.assertEqual(mqtt.varint(n).hex(), b)
        with self.assertRaises(ValueError):
            mqtt.varint(268435456)

    def test_connect(self):
        self.assertEqual(mqtt.connect_packet("a", 60).hex(), "100d00044d5154540402003c000161")
        p = mqtt.connect_packet("fonte", 60, will=("p/d/ext/fonte", b"{}", 1, False))
        kind, flags, body = mqtt.Reader().feed(p)[0]
        self.assertEqual((kind, flags), (mqtt.CONNECT, 0))
        self.assertEqual(body[7], 0x0E)                 # clean + will + will QoS 1, sem retain
        info = parse_connect(body)
        self.assertEqual(info["client_id"], "fonte")
        self.assertEqual(info["will"], {"topic": "p/d/ext/fonte", "payload": b"{}", "qos": 1, "retain": False})

    def test_publish(self):
        self.assertEqual(mqtt.publish_packet("a/b", b"hi").hex(), "30070003612f626869")
        self.assertEqual(mqtt.publish_packet("a/b", "hi", qos=1, pid=1).hex(), "32090003612f6200016869")
        kind, flags, body = mqtt.Reader().feed(mqtt.publish_packet("a/b", b"x", qos=1, pid=7, retain=True))[0]
        self.assertEqual(mqtt.parse_publish(flags, body), ("a/b", b"x", 1, 7, True))

    def test_subscribe_puback_ping(self):
        self.assertEqual(mqtt.subscribe_packet(1, [("a/b", 1)]).hex(), "82080001000361" "2f6201")
        self.assertEqual(mqtt.puback_packet(258).hex(), "40020102")
        self.assertEqual(mqtt.PINGREQ_PACKET.hex(), "c000")
        self.assertEqual(mqtt.DISCONNECT_PACKET.hex(), "e000")

    def test_reader_pieces(self):
        data = mqtt.publish_packet("t", b"x" * 300, qos=1, pid=2) + mqtt.PINGRESP_PACKET
        rd, got = mqtt.Reader(), []
        for i in range(len(data)):                      # um byte de cada vez
            got += rd.feed(data[i:i + 1])
        self.assertEqual([k for k, _, _ in got], [mqtt.PUBLISH, mqtt.PINGRESP])
        self.assertEqual(mqtt.parse_publish(got[0][1], got[0][2])[1], b"x" * 300)

    def test_matches(self):
        self.assertTrue(matches("p/d/ext/+", "p/d/ext/mac"))
        self.assertFalse(matches("p/d/ext/+", "p/d/ext/mac/re"))
        self.assertTrue(matches("p/#", "p/d/state"))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.b = Broker()
        self.got = []
        self.ups = threading.Event()

    def tearDown(self):
        self.b.close()

    def client(self, **kw):
        c = mqtt.Client("fonte", self.b.connect, subs=["p/d/ext/fonte/re", "p/d/state"],
                        will=("p/d/ext/fonte", b'{"online":false}', 1, False),
                        on_message=lambda t, p: self.got.append((t, p)), on_connect=self.ups.set, **kw)
        c.start()
        self.assertTrue(c.connected.wait(5))
        return c

    def test_connect_subscribe_retained_publish(self):
        self.b.inject("p/d/state", {"type": "state", "online": True}, retain=True)
        c = self.client()
        subs = self.b.wait_event("subscribe")[0]
        self.assertEqual(subs, [("p/d/ext/fonte/re", 1), ("p/d/state", 1)])
        self.assertTrue(c.publish("p/d/ext/fonte", json.dumps({"x": 1}), qos=1, timeout=5))
        self.assertTrue(c.publish("p/d/ext/fonte", b"q0"))
        self.b.wait_msg("p/d/ext/fonte", lambda m: m == {"x": 1})
        end = time.time() + 5
        while not self.got and time.time() < end:
            time.sleep(0.02)
        self.assertEqual(self.got[0][0], "p/d/state")     # o retido chega pelo nome exato
        self.b.inject("p/d/ext/fonte/re", {"type": "reply"})
        while len(self.got) < 2 and time.time() < end:
            time.sleep(0.02)
        self.assertEqual(json.loads(self.got[1][1]), {"type": "reply"})
        c.stop()
        self.b.wait_event("disconnect")
        time.sleep(0.1)
        self.assertNotIn({"online": False}, self.b.messages("p/d/ext/fonte"))   # sem vontade no DISCONNECT

    def test_will_and_reconnect(self):
        c = self.client()
        self.b.wait_event("subscribe")
        self.ups.clear()
        self.b.drop()
        self.b.wait_msg("p/d/ext/fonte", lambda m: m == {"online": False})
        self.assertTrue(self.ups.wait(5))               # reconectou sozinho
        self.assertEqual(len(self.b.wait_event("subscribe", count=2)), 2)
        c.stop()

    def test_refused(self):
        self.b.connack_rc = 5
        logs = []
        c = mqtt.Client("fonte", self.b.connect, log=logs.append)
        c.start()
        end = time.time() + 5
        while not any("recusado" in m for m in logs) and time.time() < end:
            time.sleep(0.02)
        self.assertFalse(c.connected.is_set())
        self.assertFalse(c.publish("x", b"y"))
        c.stop()
        self.assertTrue(any("código 5" in m for m in logs))

    def test_ping(self):
        c = self.client(keepalive=1)
        self.b.wait_event("ping", timeout=4)
        self.assertTrue(c.connected.is_set())
        c.stop()

    def test_ping_with_qos0_only(self):
        logs = []
        c = self.client(keepalive=2, log=logs.append)
        self.b.wait_event("subscribe")
        end = time.time() + 5
        while time.time() < end:            # sempre saindo algo, sem nada chegando
            c.publish("p/d/ext/fonte", b"q0")
            time.sleep(0.5)
        kinds = [k for k, _ in self.b.events]
        self.assertIn("ping", kinds)
        self.assertEqual(kinds.count("connect"), 1)     # não caiu nem reconectou
        self.assertTrue(c.connected.is_set())
        self.assertFalse([m for m in logs if "caiu" in m or "parou" in m])
        c.stop()


if __name__ == "__main__":
    unittest.main()
