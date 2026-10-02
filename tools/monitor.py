"""Monitor serial do Oba Pocket que também tira prints da tela e instala Obas.

  python3 tools/monitor.py [pasta] [porta]   (sem porta: acha a placa no USB)

Mostra o log da placa; digite "s" + Enter para salvar um print (PNG) em
pasta/ (padrão: shots/) e "c" para tentar o cartão SD de novo. O firmware
responde a 'S' mandando o frame em base64. "r" liga/desliga a gravação e
"t <frase>" simula que alguém falou a frase (dispara as cartas da manga), mas
esses dois só valem no firmware dev (pio run -e dev -t upload).
"p <nome> <humor> <ms> <olharX> <olharY> <lado> [extra]" desenha um quadro
fixo e salva como pasta/<nome>.png; "h ..." só mostra o CRC32 dele (ver
freezeShot em src/main.cpp).
"u <pasta>" grava o Oba da pasta no cartão da placa (o oba.json e os arquivos
listados em "files") e ativa; "a <id>" ativa um Oba instalado, "l" lista e
"o" abre (ou fecha) a tela de escolha, como o botão do meio.
"y"/"n" respondem à pergunta da instalação pelo ar (como tocar em "Instalar" ou
"Recusar"; com ela aberta, "s" tira o print dela) e "f" mostra o tempo de quadro
(média e máximo desde o último "f").
Abrir a porta reinicia a placa no macOS, por isso fica tudo num processo só.
Precisa de pyserial e Pillow.
"""

import base64
import json
import queue
import sys
import zlib
import threading
import time
from pathlib import Path

import serial
from serial.tools import list_ports
from PIL import Image

# Chips USB-serial do Core2: CP2104 (os mais antigos) e CH9102
USB_IDS = {(0x10C4, 0xEA60), (0x1A86, 0x55D4), (0x1A86, 0x7523)}


def find_port():
    found = [p.device for p in list_ports.comports() if (p.vid, p.pid) in USB_IDS]
    found.sort(key=lambda d: not d.startswith("/dev/cu."))  # no macOS, o cu. e não o tty.
    if not found:
        sys.exit("não achei a placa: ligue o cabo USB ou passe a porta")
    return found[0]


folder = Path(sys.argv[1] if len(sys.argv) > 1 else "shots")
port = sys.argv[2] if len(sys.argv) > 2 else find_port()
folder.mkdir(parents=True, exist_ok=True)
replies = queue.Queue()  # @@GO / @@OK / @@ERR da placa, para o upload


def save(size, rows, name):
    w, h = size
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y, row in rows.items():  # linhas perdidas ficam pretas
        for x in range(w):
            v = (row[2 * x] << 8) | row[2 * x + 1]  # o sprite guarda RGB565 big-endian
            px[x, y] = ((v >> 11) << 3, ((v >> 5) & 0x3F) << 2, (v & 0x1F) << 3)
    out = folder / (f"{name}.png" if name else time.strftime("shot-%H%M%S.png"))
    img.save(out)
    crc = f", crc {zlib.crc32(b''.join(rows[y] for y in range(h))):08x}" if len(rows) == h else ""
    print(f"### print salvo: {out} ({len(rows)}/{h} linhas{crc})", flush=True)


def read_loop(s):
    size, rows, name = None, {}, None
    while True:
        line = s.readline().decode(errors="ignore").rstrip()
        if line.startswith("@@BEGIN"):
            parts = line.split()
            size, rows = tuple(int(v) for v in parts[1:3]), {}
            name = parts[3] if len(parts) > 3 else None
        elif line == "@@END" and size:
            save(size, rows, name)
            size = None
        elif line.startswith(("@@GO", "@@OK", "@@ERR")):
            replies.put(line)
            if not line.startswith("@@GO"):
                print(line, flush=True)
        elif line.startswith("@@") and size:
            try:  # o log da placa às vezes se mistura no meio de uma linha
                idx, data = line[2:].split(":", 1)
                row = base64.b64decode(data)
                if len(row) == size[0] * 2:
                    rows[int(idx)] = row
            except ValueError:
                pass
        elif line:
            print(line, flush=True)


def reply(timeout=5):
    try:
        return replies.get(timeout=timeout)
    except queue.Empty:
        print("### a placa não respondeu", flush=True)
        return "@@ERR"


def send_file(oba_id, rel, data):
    """Manda um arquivo em pedaços de 1 KB, cada um depois do "@@GO" da placa."""
    s.write(f"U{oba_id}/{rel} {len(data)}\n".encode())
    off = 0
    while True:
        r = reply()
        if r == "@@GO" and off < len(data):
            s.write(data[off:off + 1024])
            off += 1024
        else:
            return r.startswith("@@OK")


def upload(target):
    """Grava o Oba da pasta no cartão: os arquivos de "files" e o oba.json por último."""
    src = Path(target)
    try:
        spec = json.loads((src / "oba.json").read_text())
    except (OSError, ValueError) as e:
        print(f"### {src}/oba.json: {e}", flush=True)
        return
    files = [*(spec.get("files") or {}), "oba.json"]
    missing = [rel for rel in files if not (src / rel).is_file()]
    if missing:
        print(f"### faltam arquivos: {', '.join(missing)}", flush=True)
        return
    while not replies.empty():  # respostas velhas
        replies.get()
    for rel in files:
        print(f"### enviando {spec['id']}/{rel}", flush=True)
        if not send_file(spec["id"], rel, (src / rel).read_bytes()):
            print("### upload parou", flush=True)
            return
    s.write(f"A{spec['id']}\n".encode())
    reply()


s = serial.Serial(port, 921600, timeout=1)
threading.Thread(target=read_loop, args=(s,), daemon=True).start()
for cmd in sys.stdin:
    cmd = cmd.strip()
    if cmd.lower() in ("s", "r", "c", "l", "o", "y", "n", "f"):
        s.write(cmd.upper().encode())
    elif cmd.lower().startswith("t "):  # "t alguém falou de fila" simula a fala (firmware dev)
        s.write(b"T" + cmd[2:].encode() + b"\n")
    elif cmd[:2].lower() in ("p ", "h "):  # quadro fixo: "p idle idle 20000 0 0 1"
        s.write(cmd[0].upper().encode() + cmd[2:].encode() + b"\n")
    elif cmd.lower().startswith("u "):  # "u obas/nimbo"
        upload(cmd[2:].strip())
    elif cmd.lower().startswith("a "):
        s.write(b"A" + cmd[2:].strip().encode() + b"\n")
time.sleep(30)  # stdin fechou: espera um print pendente terminar
