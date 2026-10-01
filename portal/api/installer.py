"""Instalador do portal: manda um Oba do registro para a placa pelo MQTT, como o
tools/oba.py install, e conta o andamento em <p>/<dev>/ui/install (sem reter).

A API (api.py) invoca de forma assíncrona com {cid, id, activate, ts}. Concorrência 1:
uma instalação por vez, e pedido que esperou demais na fila não começa (a placa
perguntaria na tela sem ninguém olhando o portal).
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import oba_remote  # noqa: E402

REGION = os.environ["AWS_REGION"]
PREFIX, DEVICE = os.environ["PREFIX"], os.environ["DEVICE"]
BUCKET = os.environ["BUCKET"]
IOT_ENDPOINT = os.environ["IOT_ENDPOINT"]
TOPIC = f"{PREFIX}/{DEVICE}/ui/install"
QUEUE_S = 90          # pedido mais velho que isso não começa
EVERY_S = 0.5         # andamento do envio, no máximo 2 por segundo
SPARE_S = 15          # antes do timeout da Lambda: desiste e ainda dá tempo de avisar o portal
CID_RE = re.compile(r"i[0-9a-f]{8}")

s3 = boto3.client("s3")
# O andamento é publicado dentro do laço do envio: com o IoT lento, antes perder um aviso que parar
iot_data = boto3.client("iot-data", endpoint_url=f"https://{IOT_ENDPOINT}",
                        config=Config(connect_timeout=2, read_timeout=3,
                                      retries={"mode": "standard", "total_max_attempts": 2}))


class Report:
    """O andamento em ui/install, com os métodos do Progress da CLI (show, confirm, end)."""

    def __init__(self, cid: str, oba_id: str, deadline: float):
        self.base, self.total, self.last = {"cid": cid, "id": oba_id}, 0, 0.0
        self.sent, self.asked, self.deadline = -1, False, deadline

    def post(self, stage: str, **kw):
        msg = {"v": 1, "ts": int(time.time() * 1000), **self.base, "stage": stage, **kw}
        try:
            iot_data.publish(topic=TOPIC, qos=0, payload=json.dumps(msg, ensure_ascii=False).encode())
        except (ClientError, BotoCoreError) as e:
            print("ui/install:", type(e).__name__, str(e)[:200])

    def show(self, done: int):
        # send_install chama a cada volta do laço, também enquanto espera o toque na placa
        now = time.monotonic()
        if now > self.deadline:  # a placa anda devagar demais: melhor avisar que ser morto pelo timeout
            oba_remote.fail("o envio passou do tempo do instalador (Wi-Fi fraco?); tente de novo")
        if self.asked or done == self.sent or (now - self.last < EVERY_S and done < self.total):
            return
        self.last, self.sent = now, done
        self.post("send", sent=done, total=self.total)

    def confirm(self):
        self.asked = True
        self.post("confirm", sent=self.total, total=self.total, wait_s=oba_remote.ASK_S)

    def end(self):
        pass


def handler(event, context):
    cid, oid = event.get("cid"), event.get("id")
    if not (isinstance(cid, str) and CID_RE.fullmatch(cid) and isinstance(oid, str)
            and oba_remote.ID_RE.fullmatch(oid)):
        print("pedido inválido")
        return
    rep = Report(cid, oid, time.monotonic() + context.get_remaining_time_in_millis() / 1000 - SPARE_S)
    ts = event.get("ts")
    if not isinstance(ts, int) or time.time() - ts / 1000 > QUEUE_S:
        rep.post("error", error="o pedido esperou demais na fila (outra instalação em andamento?)")
        return
    rep.post("start")
    owner = context.invoked_function_arn.split(":")[4]
    result = "done"
    try:
        spec, files = oba_remote.registry_load(s3, BUCKET, oid, owner)
        rep.total = sum(len(d) for _, d in files)
        aws = oba_remote.Aws({"prefix": PREFIX, "device": DEVICE, "region": REGION}, endpoint=IOT_ENDPOINT)
        with oba_remote.Link(aws, client_prefix=f"{PREFIX}-portal-") as link:
            done = oba_remote.send_install(link, spec, files, event.get("activate") is True, cid=cid, progress=rep)
        rep.post("done", active=done.get("active") is True, version=spec.get("version"))
    except oba_remote.ObaError as e:
        result = str(e)
        rep.post("error", error=result)
    except (ClientError, BotoCoreError, OSError) as e:
        result = f"{type(e).__name__}: {str(e)[:200]}"
        rep.post("error", error="falha na AWS ou na rede; tente de novo")
    except Exception as e:  # noqa: BLE001  o portal sempre fica sabendo; o tipo vai para o log
        result = f"{type(e).__name__}: {str(e)[:200]}"
        rep.post("error", error="erro interno do instalador")
    print(json.dumps({"cid": cid, "id": oid, "user": event.get("user"), "result": result}, ensure_ascii=False))
