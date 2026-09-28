"""Gera as fontes do balão (VLW suavizada, com os acentos do português) em
src/bubble_fonts.cpp (dados) e .h (declarações). As fontes u8g2 do M5GFX (efont)
não têm ç, ã, õ...

  python3 tools/make_font.py     (precisa de Pillow)

Fonte: Nunito (SIL Open Font License, ver tools/fonts/Nunito-OFL.txt).
"""

import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
TTF = HERE / "fonts" / "Nunito.ttf"
SRC = HERE.parent / "src"
WEIGHT = b"Bold"
SIZES = {"BUBBLE_FONT_BIG": 22, "BUBBLE_FONT_SMALL": 16}
CHARS = [c for c in range(0x20, 0x7F)] + [c for c in range(0xA0, 0x100)] + [
    0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2026]


def vlw(size: int) -> bytes:
    font = ImageFont.truetype(str(TTF), size)
    font.set_variation_by_name(WEIGHT)
    ascent, descent = font.getmetrics()
    metrics, bitmaps = [], []
    for cp in CHARS:
        ch = chr(cp)
        x0, y0, x1, y1 = font.getbbox(ch, anchor="ls")  # relativo à linha de base
        w, h = max(0, x1 - x0), max(0, y1 - y0)
        adv = round(font.getlength(ch))
        if w and h:
            img = Image.new("L", (w, h), 0)
            ImageDraw.Draw(img).text((-x0, -y0), ch, font=font, fill=255, anchor="ls")
            bitmaps.append(img.tobytes())
        else:
            w = h = 0
            bitmaps.append(b"")
        metrics.append(struct.pack(">7i", cp, h, w, adv, -y0 if h else 0, x0 if w else 0, 0))
    head = struct.pack(">6i", len(CHARS), 11, size, 0, ascent, descent)
    return head + b"".join(metrics) + b"".join(bitmaps)


def c_array(name: str, data: bytes) -> str:
    rows = [", ".join(f"0x{b:02x}" for b in data[i:i + 20]) for i in range(0, len(data), 20)]
    return f"const uint8_t {name}[{len(data)}] = {{\n  " + ",\n  ".join(rows) + "\n};\n"


def write(fonts: dict) -> None:
    """fonts = {nome: (px, bytes)}. Os dados ficam só no .cpp, para não haver
    uma cópia na flash por arquivo que usa as fontes."""
    head = "// Gerado por tools/make_font.py a partir da Nunito (SIL OFL 1.1). Não edite.\n"
    h = [head, "// Fontes VLW (suavizadas) com ASCII + Latin-1, para M5GFX loadFont().\n"
         "#pragma once\n#include <stdint.h>\n\n"]
    cpp = [head, '#include "bubble_fonts.h"\n']
    for name, (size, data) in fonts.items():
        h.append(f"extern const uint8_t {name}[{len(data)}];  // {size} px\n")
        cpp.append(f"\n// {size} px, {len(data) // 1024} KB\n" + c_array(name, data))
    (SRC / "bubble_fonts.h").write_text("".join(h))
    (SRC / "bubble_fonts.cpp").write_text("".join(cpp))


if __name__ == "__main__":
    fonts = {name: (size, vlw(size)) for name, size in SIZES.items()}
    for name, (size, data) in fonts.items():
        print(name, size, len(data), "bytes")
    write(fonts)
