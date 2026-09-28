#!/usr/bin/env python3
"""Contorno de um SVG -> pontos do look.outline de um Oba.

Lê o path com id="outline" (ou o primeiro path) e anda por ele em passos de
~--step px, gerando os pontos [x, y]. As coordenadas do SVG são usadas como
estão: desenhe com a origem no centro do Oba (ex.: viewBox="-160 -116 320 240",
que é a tela do Core2 com o Oba no lugar de sempre).

Comandos de path aceitos: M L H V C Q A Z (maiúsculos e minúsculos).

  python tools/svg_to_outline.py obas/nimbo/nimbo.svg              # imprime os pontos
  python tools/svg_to_outline.py obas/nimbo/nimbo.svg --into obas/nimbo/oba.json
"""
import argparse
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from oba import OUTLINE_MAX, dump  # noqa: E402

TOKEN = re.compile(r"[MmLlHhVvCcQqAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def find_path(svg):
    paths = [e for e in ET.parse(svg).iter() if e.tag.split("}")[-1] == "path"]
    for p in paths:
        if p.get("id") == "outline":
            return p.get("d")
    if not paths:
        sys.exit(f"{svg}: nenhum <path>")
    return paths[0].get("d")


def arc_center(p0, rx, ry, phi, large, sweep, p1):
    """Arco do SVG (pontas) -> centro e ângulos (SVG 1.1, apêndice F.6.5)."""
    (x0, y0), (x1, y1) = p0, p1
    c, s = math.cos(phi), math.sin(phi)
    dx, dy = (x0 - x1) / 2, (y0 - y1) / 2
    xp, yp = c * dx + s * dy, -s * dx + c * dy
    rx, ry = abs(rx), abs(ry)
    lam = xp * xp / (rx * rx) + yp * yp / (ry * ry)
    if lam > 1:  # raio pequeno demais: o SVG manda aumentar
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * yp * yp - ry * ry * xp * xp
    den = rx * rx * yp * yp + ry * ry * xp * xp
    k = math.sqrt(max(0.0, num / den)) * (-1 if large == sweep else 1)
    cxp, cyp = k * rx * yp / ry, -k * ry * xp / rx
    cx = c * cxp - s * cyp + (x0 + x1) / 2
    cy = s * cxp + c * cyp + (y0 + y1) / 2

    def angle(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t0 = angle(1, 0, (xp - cxp) / rx, (yp - cyp) / ry)
    dt = angle((xp - cxp) / rx, (yp - cyp) / ry, (-xp - cxp) / rx, (-yp - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    return cx, cy, rx, ry, t0, dt


def segments(d):
    """Cada trecho do path como uma função f(u), u de 0 a 1."""
    toks = TOKEN.findall(d)
    i, cmd = 0, None
    cur = start = (0.0, 0.0)
    out = []

    def nums(n):
        nonlocal i
        v = [float(t) for t in toks[i:i + n]]
        i += n
        return v

    while i < len(toks):
        if re.match(r"[A-Za-z]", toks[i]):
            cmd = toks[i]
            i += 1
        rel = cmd.islower()
        C = cmd.upper()
        ox, oy = cur if rel else (0.0, 0.0)
        if C == "Z":
            a, b = cur, start
            out.append(lambda u, a=a, b=b: (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u))
            cur = start
            continue
        if C == "M":
            x, y = nums(2)
            cur = start = (ox + x, oy + y)
            cmd = "l" if rel else "L"  # pares seguintes são linhas
            continue
        if C in "LHV":
            if C == "L":
                x, y = nums(2)
                p = (ox + x, oy + y)
            elif C == "H":
                (x,) = nums(1)
                p = ((ox if rel else 0) + x, cur[1])
            else:
                (y,) = nums(1)
                p = (cur[0], (oy if rel else 0) + y)
            a = cur
            out.append(lambda u, a=a, b=p: (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u))
            cur = p
        elif C == "C":
            v = nums(6)
            p0, p1, p2, p3 = cur, (ox + v[0], oy + v[1]), (ox + v[2], oy + v[3]), (ox + v[4], oy + v[5])

            def cubic(u, p0=p0, p1=p1, p2=p2, p3=p3):
                w = 1 - u
                return tuple(w**3 * a + 3 * w * w * u * b + 3 * w * u * u * c + u**3 * d
                             for a, b, c, d in zip(p0, p1, p2, p3))
            out.append(cubic)
            cur = p3
        elif C == "Q":
            v = nums(4)
            p0, p1, p2 = cur, (ox + v[0], oy + v[1]), (ox + v[2], oy + v[3])

            def quad(u, p0=p0, p1=p1, p2=p2):
                w = 1 - u
                return tuple(w * w * a + 2 * w * u * b + u * u * c for a, b, c in zip(p0, p1, p2))
            out.append(quad)
            cur = p2
        elif C == "A":
            rx, ry, rot, large, sweep, x, y = nums(7)
            p = (ox + x, oy + y)
            cx, cy, rx, ry, t0, dt = arc_center(cur, rx, ry, math.radians(rot), int(large), int(sweep), p)
            c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))

            def arc(u, cx=cx, cy=cy, rx=rx, ry=ry, t0=t0, dt=dt, c=c, s=s):
                t = t0 + dt * u
                ex, ey = rx * math.cos(t), ry * math.sin(t)
                return (cx + c * ex - s * ey, cy + s * ex + c * ey)
            out.append(arc)
            cur = p
        else:
            sys.exit(f"comando de path não suportado: {cmd}")
    return out


def sample(d, step):
    pts = []
    for f in segments(d):
        # comprimento aproximado do trecho, para decidir quantos pontos
        probe = [f(k / 32) for k in range(33)]
        length = sum(math.dist(a, b) for a, b in zip(probe, probe[1:]))
        n = max(1, round(length / step))
        pts += [f(k / n) for k in range(n)]
    # tira pontos repetidos (fim de um trecho = começo do outro)
    clean = []
    for p in pts:
        if not clean or math.dist(p, clean[-1]) > 0.05:
            clean.append(p)
    if len(clean) > 1 and math.dist(clean[0], clean[-1]) < 0.05:
        clean.pop()
    return [[round(x, 1), round(y, 1)] for x, y in clean]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("svg")
    ap.add_argument("--step", type=float, default=4.0, help="distância entre pontos (px), padrão 4")
    ap.add_argument("--into", help="oba.json onde gravar em look.outline")
    a = ap.parse_args()
    pts = sample(find_path(a.svg), a.step)
    if len(pts) > OUTLINE_MAX:
        sys.exit(f"{len(pts)} pontos: o máximo é {OUTLINE_MAX} (aumente o --step)")
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    print(f"{len(pts)} pontos, x {min(xs)}..{max(xs)}, y {min(ys)}..{max(ys)}", file=sys.stderr)
    if not a.into:
        print(json.dumps(pts))
        return
    path = Path(a.into)
    oba = json.loads(path.read_text())
    oba.setdefault("look", {})["outline"] = pts
    path.write_text(dump(oba))
    print(f"gravado em {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
