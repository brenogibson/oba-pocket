#!/usr/bin/env python3
"""Desenha o Bit: as folhas de quadros (sprites/*.png) e os sons (sons/*.wav).

  python3 obas/bit/draw.py                        # gera tudo e atualiza "files" no oba.json
  python3 obas/bit/draw.py --preview previa.png   # e salva uma prévia ampliada dos quadros

Não tem nada sorteado: rodar de novo dá os mesmos bytes. Precisa do Pillow.
Cada quadro tem 40×40 pixels (look.size); na tela, com scale 4, fica com 160×160.
"""
import argparse
import hashlib
import json
import math
import struct
import sys
import wave
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tools"))
from oba import dump  # noqa: E402

W, H = 40, 40

# Paleta do desenho (uma letra por cor)
PAL = {
    "K": "#0B0F22",  # contorno
    "O": "#FFA940",  # lataria
    "H": "#FFD27A",  # brilho da lataria
    "S": "#DD7330",  # sombra da lataria
    "M": "#B4BFE3",  # metal (antena, orelhas, braços)
    "m": "#7C88BA",  # metal escuro (pés)
    "V": "#172040",  # visor
    "G": "#34437A",  # reflexo do visor
    "C": "#62F4FF",  # olhos e boca
    "c": "#2A8FA8",  # olhos apagados (dormindo)
    "W": "#FFFFFF",
    "P": "#FF5C8F",  # rosa: coração, bochechas, luz da antena
    "p": "#FFA3C0",  # rosa claro
    "q": "#9A4468",  # luz da antena apagada
    "Y": "#FFE066",  # amarelo: alegria e brilhinhos
    "R": "#FF4A5A",  # vermelho: susto
}
BG = "#1D2547"   # look.palette.bg (só para a prévia)


# ---------------------------------------------------------------- desenho

def mirror(px):
    """Espelha um conjunto de pixels no eixo do meio do quadro."""
    return {(W - 1 - x, y): c for (x, y), c in px.items()}


def rrect(x0, y0, x1, y1, r, color):
    """Retângulo com os cantos arredondados em r pixels."""
    cut = {1: [(0, 0)], 2: [(0, 0), (1, 0), (0, 1)], 3: [(0, 0), (1, 0), (2, 0), (0, 1), (0, 2), (1, 1)]}[r] if r else []
    out = {}
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            dx = min(x - x0, x1 - x)
            dy = min(y - y0, y1 - y)
            if (dx, dy) not in cut:
                out[(x, y)] = color
    return out


def stamp(rows, x0, y0, color=None):
    """Pixels de um desenho em texto ('.' = vazio; letra = cor, ou 'X' = color)."""
    out = {}
    for j, row in enumerate(rows):
        for i, ch in enumerate(row):
            if ch != ".":
                out[(x0 + i, y0 + j)] = color if ch == "X" else ch
    return out


def shade(px, light="H", dark="S", deep=True):
    """Brilho em cima e à esquerda, sombra embaixo e à direita."""
    out = dict(px)
    for (x, y) in px:
        if (x, y - 1) not in px or (x - 1, y) not in px:
            out[(x, y)] = light
        if (x, y + 1) not in px or (x + 1, y) not in px or (deep and (x, y + 2) not in px):
            out[(x, y)] = dark
    return out


def arm(angle, length=6.0, shoulder=(11.0, 32.0)):
    """Bracinho esquerdo: tubo de metal e mão redonda. angle em graus a partir
    de "para baixo", abrindo para fora (90 = na horizontal, 180 = para cima)."""
    a = math.radians(angle)
    sx, sy = shoulder
    ex, ey = sx - math.sin(a) * length, sy + math.cos(a) * length
    vx, vy = ex - sx, ey - sy
    out = {}
    for y in range(H):
        for x in range(W):
            cx, cy = x + 0.5, y + 0.5
            t = max(0.0, min(1.0, ((cx - sx) * vx + (cy - sy) * vy) / (vx * vx + vy * vy)))
            if (cx - sx - t * vx) ** 2 + (cy - sy - t * vy) ** 2 <= 1.0:
                out[(x, y)] = "M"
    hx, hy = int(ex) - 1, int(ey) - 1
    out.update(stamp(["HOO", "OOO", "OOS"], hx, hy))
    return out


class Frame:
    def __init__(self):
        self.px = {}

    def part(self, px, outline=True):
        """Pinta uma peça por cima do que já tem, com contorno próprio."""
        if outline:
            for (x, y) in px:
                for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if n not in px and 0 <= n[0] < W and 0 <= n[1] < H:
                        self.px[n] = "K"
        for p, c in px.items():
            if 0 <= p[0] < W and 0 <= p[1] < H:
                self.px[p] = c

    def image(self):
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for (x, y), c in self.px.items():
            v = PAL[c]
            img.putpixel((x, y), (int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16), 255))
        return img


def pair(left):
    """Olho (ou bochecha) esquerdo e o direito, espelhado."""
    return {**left, **mirror(left)}


# Olho esquerdo em cada expressão (o direito é o espelho). Caixa x 11..15, y 14..19.
EYES = {
    "open": [".XX.", "XWXX", "XXXX", "XXXX", ".XX."],
    "blink": ["....", "....", "....", "XXXX", "...."],
    "happy": [".XX.", "X..X", "X..X"],
    "sleep": ["X..X", ".XX."],
    "shy": ["XXXX", "XXXX", ".XX."],
    "scared": ["X...", ".XX.", "...X", ".XX.", "X..."],
}
EYE_POS = {"open": (12, 15), "blink": (12, 15), "happy": (12, 16), "sleep": (12, 17),
           "shy": (12, 17), "scared": (12, 15)}
SPIRAL = ["XXXXX", "....X", "XXX.X", "X...X", "XXXXX"]

# Boca (inteira, simétrica)
MOUTHS = {
    "smile": (17, 21, ["X....X", ".XXXX."]),
    "grin": (16, 21, ["XXXXXXXX", ".XPPPPX.", "..XXXX.."]),
    "o": (18, 21, [".XX.", "X..X", ".XX."]),
    "flat": (18, 22, ["XXXX"]),
    "tiny": (18, 21, ["X..X", ".XX."]),
    "wavy0": (16, 21, ["X.X.X.X.", ".X.X.X.X"]),
    "wavy1": (16, 21, [".X.X.X.X", "X.X.X.X."]),
}


def rotate(rows, k):
    for _ in range(k % 4):
        rows = ["".join(r[i] for r in reversed(rows)) for i in range(len(rows[0]))]
    return rows


def draw(eyes="open", eye_dx=0, eye_color="C", mouth="smile", cheeks=0, ball="P", glint=True,
         heart=False, tilt=0, arms=(25, 25), arm_len=6.0, layer="mid", dx=0, droop=0, sparkles=()):
    """Um quadro do Bit. As peças vão de trás para a frente. arms: ângulo de cada
    braço (None = escondidos atrás das costas); layer: onde eles ficam (back =
    atrás do corpo, mid = entre o corpo e a cabeça, front = na frente de tudo)."""
    f = Frame()
    hy = droop  # a cabeça (e a antena) descem um pouco no sonolento

    # Antena: haste de metal e a luz na ponta
    stalk = {}
    for y in range(5 + hy, 10 + hy):
        s = round(tilt * (9 + hy - y) / 4)
        stalk[(19 + s, y)] = "M"
        stalk[(20 + s, y)] = "M"
    f.part(stalk)
    if heart:
        f.part(stamp(["XX..XX", "XXXXXX", ".XXXX.", "..XX.."], 17 + tilt, 1 + hy, ball))
    else:
        light = stamp([".XX.", "XXXX", "XXXX", ".XX."], 18 + tilt, 1 + hy, ball)
        if glint:
            light[(19 + tilt, 2 + hy)] = "W"
        f.part(light)

    # Orelhas
    ear = shade(rrect(1, 16 + hy, 4, 22 + hy, 1, "M"), "W", "m", deep=False)
    f.part({**ear, **mirror(ear)})

    limbs = {**arm(arms[0], arm_len), **mirror(arm(arms[1], arm_len))} if arms else {}
    if layer == "back":
        f.part(limbs)

    # Pés e corpo, com o coração no peito
    foot = rrect(12, 36, 17, 38, 1, "m")
    f.part({**foot, **mirror(foot)})
    body = shade(rrect(11, 30, 28, 35, 1, "O"), deep=False)
    body.update(stamp(["XX..XX", "XXXXXX", ".XXXX.", "..XX.."], 17, 31, "P"))
    body[(17, 31)] = "p"
    f.part(body)
    if layer == "mid":
        f.part(limbs)

    # Cabeça e visor
    f.part(shade(rrect(5, 9 + hy, 34, 28 + hy, 3, "O")))
    visor = rrect(9, 13 + hy, 30, 25 + hy, 2, "V")
    for x in range(11, 15):
        visor[(x, 14 + hy)] = "G"
    visor[(11, 15 + hy)] = "G"
    f.part(visor)

    # Rosto, direto no visor
    face = {}
    if eyes.startswith("spiral"):
        k = int(eyes[-1])
        left_eye = stamp(rotate(SPIRAL, k), 11, 15 + hy, eye_color)
        face.update(left_eye)
        face.update(mirror(left_eye))
    else:
        ex, ey = EYE_POS[eyes]
        left_eye = stamp(EYES[eyes], ex, ey + hy, eye_color)
        right_eye = mirror(left_eye)
        # O brilho fica sempre em cima à esquerda (não espelha)
        for (x, y), c in list(right_eye.items()):
            if c == "W":
                right_eye[(x, y)] = eye_color
                right_eye[(x - 2, y)] = "W"
        face.update({(x + eye_dx, y): c for (x, y), c in left_eye.items()})
        face.update({(x + eye_dx, y): c for (x, y), c in right_eye.items()})
    mx, my, rows = MOUTHS[mouth]
    face.update(stamp(rows, mx, my + hy, "C"))
    if cheeks:
        blush = stamp(["PPP"] if cheeks == 1 else ["ppP", "PPP"], 10, 21 + hy - (cheeks == 2))
        face.update(pair(blush))
    f.part(face, outline=False)

    if layer == "front":
        f.part(limbs)

    for sx, sy in sparkles:
        f.part(stamp([".Y.", "YWY", ".Y."], sx, sy), outline=False)

    if dx:
        f.px = {(x + dx, y): c for (x, y), c in f.px.items() if 0 <= x + dx < W}
    return f.image()


# Quadros de cada humor e quantos por segundo (vão para look.frames no oba.json)
def moods():
    idle = []
    for i in range(16):
        eyes = "blink" if i == 6 else "open"
        look = 1 if i in (10, 11) else -1 if i in (12, 13) else 0
        idle.append(draw(eyes=eyes, eye_dx=look, ball="P" if i % 4 < 2 else "p"))

    # Feliz: acena com os dois braços, um de cada vez
    happy = [draw(eyes="happy", mouth="grin", cheeks=1, ball="Y", arms=a, arm_len=7, layer="front", sparkles=s)
             for a, s in [((100, 125), ()), ((125, 100), ((10, 1), (27, 3))),
                          ((100, 125), ()), ((125, 100), ((27, 1), (10, 3)))]]

    # Assustado: segura o rosto com as duas mãos e treme
    scared = [draw(eyes="scared", mouth=m, ball=b, arms=(160 + a, 160 + a), arm_len=8, layer="front", dx=d)
              for m, b, d, a in [("wavy0", "R", -1, 0), ("wavy1", "W", 1, 5), ("wavy0", "R", 1, 0), ("wavy1", "W", -1, 5)]]

    # Tímido: esconde as mãos nas costas e desvia o olhar
    shy = [draw(eyes="shy", eye_dx=-1 if i < 2 else 1, mouth="tiny", cheeks=2, ball="P", heart=True, arms=None)
           for i in range(4)]

    dizzy = [draw(eyes=f"spiral{i}", mouth=f"wavy{i % 2}", ball="YCPC"[i], tilt=(-2, 0, 2, 0)[i],
                  arms=((45, 20), (30, 30), (20, 45), (30, 30))[i]) for i in range(4)]

    sleepy = [draw(eyes="sleep", eye_color="c", mouth="o" if i < 2 else "flat", ball="q", glint=False,
                   arms=(15, 15), droop=0 if i < 2 else 1) for i in range(4)]

    return {"idle": (idle, 4), "happy": (happy, 8), "scared": (scared, 10),
            "shy": (shy, 3), "dizzy": (dizzy, 8), "sleepy": (sleepy, 2)}


def save_sheet(frames, path):
    sheet = Image.new("RGBA", (W * len(frames), H), (0, 0, 0, 0))
    for i, fr in enumerate(frames):
        sheet.paste(fr, (i * W, 0))
    sheet.save(path, "PNG")


def preview(sheets, path, zoom=8):
    """Todos os quadros ampliados, um humor por linha, no fundo da tela."""
    cols = max(len(f) for f, _ in sheets.values())
    pad = 8
    img = Image.new("RGB", (pad + cols * (W * zoom + pad), pad + len(sheets) * (H * zoom + pad + 14)), BG)
    d = ImageDraw.Draw(img)
    for r, (name, (frames, fps)) in enumerate(sheets.items()):
        y = pad + r * (H * zoom + pad + 14)
        d.text((pad, y), f"{name} ({len(frames)} quadros, {fps} fps)", fill="#EAF0FF")
        for i, fr in enumerate(frames):
            big = fr.resize((W * zoom, H * zoom), Image.NEAREST)
            img.paste(big, (pad + i * (W * zoom + pad), y + 14), big)
    img.save(path)


# ---------------------------------------------------------------- sons

RATE = 16000


def tone(dur, f0, f1=None, duty=0.5, tri=0.0, vib=(0.0, 0.0), slide="lin", decay=0.0):
    """Um trecho chiptune: onda quadrada (duty) misturada com triângulo (tri),
    indo de f0 a f1, com vibrato (Hz, fração) e decaimento exponencial."""
    f1 = f0 if f1 is None else f1
    n = int(dur * RATE)
    out, phase = [], 0.0
    for i in range(n):
        p = i / max(1, n - 1)
        f = f0 * (f1 / f0) ** p if slide == "exp" else f0 + (f1 - f0) * p
        f *= 1 + vib[1] * math.sin(2 * math.pi * vib[0] * i / RATE)
        phase = (phase + f / RATE) % 1.0
        sq = 1.0 if phase < duty else -1.0
        tr = 4 * abs(phase - 0.5) - 1
        env = min(1.0, i / (0.004 * RATE), (n - i) / (0.02 * RATE)) * math.exp(-decay * i / RATE)
        out.append(((1 - tri) * sq + tri * tr) * env)
    return out


def rest(dur):
    return [0.0] * int(dur * RATE)


def knock(f, dur=0.06, decay=80.0, bright=0.4, click=0.5, seed=1):
    """Uma batida de madeira (castanhola, wood block): um seno que morre rápido,
    um parcial desafinado (2,76×) que morre antes e um estalo de ruído no ataque.
    O ruído vem de um gerador com semente fixa, então os bytes não mudam."""
    n = int(dur * RATE)
    x, out = seed, []
    for i in range(n):
        t = i / RATE
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        noise = x / 0x3FFFFFFF - 1.0
        body = math.sin(2 * math.pi * f * t) + bright * math.sin(2 * math.pi * 2.76 * f * t) * math.exp(-2 * decay * t)
        tail = min(1.0, (n - i) / (0.005 * RATE))
        out.append((0.7 * body * math.exp(-decay * t) + click * noise * math.exp(-t / 0.0015)) * tail)
    return out


def mix(*parts):
    """Soma trechos que começam em tempos diferentes: (início em s, amostras, ganho)."""
    n = max(int(t0 * RATE) + len(x) for t0, x, _ in parts)
    out = [0.0] * n
    for t0, x, g in parts:
        k = int(t0 * RATE)
        for i, v in enumerate(x):
            out[k + i] += g * v
    return out


SOUNDS = {
    # oi: "bi-bíp" subindo, para quando chamam o nome dele
    "oi": lambda: tone(0.08, 988, duty=0.25) + rest(0.03) + tone(0.17, 1319, 1480, duty=0.25, vib=(12, 0.015)),
    # yay: arpejo de dó maior terminando lá em cima
    "yay": lambda: (tone(0.06, 1047, tri=0.3) + tone(0.06, 1319, tri=0.3) + tone(0.06, 1568, tri=0.3)
                    + tone(0.24, 2093, tri=0.3, vib=(10, 0.02), decay=4)),
    # ai: um estalo agudo e uma sirene caindo
    "ai": lambda: tone(0.04, 1760, duty=0.5) + tone(0.32, 1400, 330, duty=0.5, vib=(24, 0.04), slide="exp"),
    # hmm: wood block, um "toc-toc" subindo um pouco, de quem vai falar algo
    "hmm": lambda: mix((0, knock(1000, decay=75), 1.4), (0.085, knock(1250, decay=80, seed=3), 1.25)),
    # zonzo: um "uóóón" bambo, caindo
    "zonzo": lambda: tone(0.7, 950, 420, duty=0.25, vib=(7, 0.12), slide="exp", decay=1.5),
    # bip: dois bipes curtos, para o teco
    "bip": lambda: tone(0.05, 1568) + rest(0.04) + tone(0.11, 2093, decay=8),
}


def save_wav(samples, path, vol=0.42):
    # Passa-baixa simples para a quadrada não chiar tanto no alto-falante pequeno
    y, pcm = 0.0, bytearray()
    for s in samples:
        y += (s - y) * 0.6
        pcm += struct.pack("<h", max(-32767, min(32767, int(round(y * vol * 32767)))))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(pcm))


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Gera os quadros e os sons do Bit")
    ap.add_argument("--preview", help="PNG com todos os quadros ampliados 8×")
    a = ap.parse_args()

    (HERE / "sprites").mkdir(exist_ok=True)
    (HERE / "sons").mkdir(exist_ok=True)
    made = []
    sheets = moods()
    for name, (frames, _) in sheets.items():
        rel = f"sprites/{name}.png"
        save_sheet(frames, HERE / rel)
        made.append(rel)
    for name, fn in SOUNDS.items():
        rel = f"sons/{name}.wav"
        save_wav(fn(), HERE / rel)
        made.append(rel)

    # Atualiza files (e confere que o JSON só usa o que foi gerado)
    path = HERE / "oba.json"
    oba = json.loads(path.read_text())
    used = [f["sheet"] for f in oba["look"]["frames"].values()] + list(oba.get("sounds", {}).values())
    missing = sorted(set(used) - set(made))
    if missing:
        sys.exit(f"o oba.json usa arquivos que o draw.py não gera: {', '.join(missing)}")
    oba["files"] = {rel: hashlib.sha256((HERE / rel).read_bytes()).hexdigest() for rel in sorted(made)}
    path.write_text(dump(oba))
    for rel in sorted(made):
        print(f"  {rel}: {(HERE / rel).stat().st_size} bytes")
    print(f"{path}: files com {len(made)} arquivos")

    if a.preview:
        preview(sheets, a.preview)
        print(f"prévia em {a.preview}")


if __name__ == "__main__":
    main()
