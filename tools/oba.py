#!/usr/bin/env python3
"""Ferramentas de Oba (o JSON que descreve cada bichinho do Oba Pocket).

  python3 tools/oba.py validate obas/bit             # schema, arquivos, imagens, sons e limites
  python3 tools/oba.py embed obas/nimbo/oba.json     # gera src/builtin_oba.h (Oba embutido)
  python3 tools/oba.py fmt obas/nimbo/oba.json       # reescreve com a formatação padrão

Pela nuvem (tools/oba_remote.py; placa e região do config.json, --device troca a placa):

  python3 tools/oba.py install obas/bit [--activate] # instala pelo ar; confirme na tela da placa
  python3 tools/oba.py remove bit [--registry]       # tira da placa (e do registro S3)
  python3 tools/oba.py list [--registry]             # Obas da placa (e os do registro)
  python3 tools/oba.py activate bit                  # troca o Oba ativo
  python3 tools/oba.py play yay [--volume 128]       # toca um som do Oba ativo (REC desligado)
  python3 tools/oba.py publish obas/bit              # só manda para o registro S3

Sem a nuvem: copie a pasta do Oba para /obas/<id>/ do cartão, ou use o comando
"u <pasta>" do tools/monitor.py (pela serial, sem tirar o cartão).
"""
import argparse
import hashlib
import json
import re
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "oba.schema.json"
JSON_MAX = 64 * 1024          # OBA_JSON_MAX em src/oba.h
OUTLINE_MAX = 256             # OBA_OUTLINE_MAX em src/oba.h
SPRITES_MAX = 1024 * 1024     # OBA_SPRITES_MAX: folhas decodificadas (RGB565 + máscara)
SOUNDS_MAX = 512 * 1024       # OBA_SOUNDS_MAX: PCM de todos os sons
SOUND_MAX_MS = 10000          # OBA_SOUND_MAX_MS
FRAMES_MAX = 32               # quadros por folha
SPRITE_W_MAX, SPRITE_H_MAX = 240, 200   # tamanho do quadro na tela (size × scale)
FILES_MAX = 64                # INSTALL_FILES_MAX em src/install.h, contando o oba.json
FILE_MAX = 2 * 1024 * 1024    # INSTALL_FILE_MAX
TOTAL_MAX = 6 * 1024 * 1024   # INSTALL_TOTAL_MAX
PATH_CHARS = re.compile(r"[A-Za-z0-9._/-]{1,64}")
PATH_DIRS_MAX = 4             # OBA_FILE_DIRS_MAX: pastas num caminho


def dump(oba):
    """JSON indentado, com os pontos do contorno em linhas de 6."""
    def fmt(o, ind):
        sp = "  " * ind
        if isinstance(o, dict) and o:
            items = [f"{sp}  {json.dumps(k)}: {fmt(v, ind + 1)}" for k, v in o.items()]
            return "{\n" + ",\n".join(items) + "\n" + sp + "}"
        if isinstance(o, list) and o and all(isinstance(p, list) for p in o):
            rows = [", ".join(json.dumps(p) for p in o[i:i + 6]) for i in range(0, len(o), 6)]
            return "[\n" + ",\n".join(f"{sp}  {r}" for r in rows) + "\n" + sp + "]"
        return json.dumps(o, ensure_ascii=False)
    return fmt(oba, 0) + "\n"


def load(target):
    p = Path(target)
    folder = p if p.is_dir() else p.parent
    path = p / "oba.json" if p.is_dir() else p
    return folder, path, json.loads(path.read_text())


def valid_path(rel):
    """Caminho relativo aceito em files, sheet e sounds (o mesmo que a placa aceita)."""
    return (isinstance(rel, str) and PATH_CHARS.fullmatch(rel) is not None and ".." not in rel
            and "//" not in rel and rel[0] not in "/." and not rel.endswith("/")
            and rel.count("/") <= PATH_DIRS_MAX)


PATH_RULE = ("só A-Z a-z 0-9 . _ / -, até 64 caracteres e 4 pastas, sem .. nem //, sem / ou . no começo "
             "e sem / no fim")


# ---------------------------------------------------------------- PNG

class PngError(Exception):
    pass


def _unfilter(raw, h, stride, bpp):
    """Linhas do PNG já sem o filtro (a placa decodifica igual, com o pngle)."""
    prev = bytearray(stride)
    pos = 0
    for _ in range(h):
        ft, row = raw[pos], bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ft == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                row[i] = (row[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif ft != 0:
            raise PngError(f"filtro de linha inválido ({ft})")
        yield row
        prev = row


def png_check(data):
    """(largura, altura, problema ou None). Recusa entrelaçado e alpha que não seja 0 ou 255."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None, None, "não é PNG"
    pos, ihdr, trns, idat = 8, None, None, []
    try:
        while True:
            if pos + 12 > len(data):
                raise PngError("PNG cortado (sem IEND)")
            n, kind = struct.unpack(">I4s", data[pos:pos + 8])
            body = data[pos + 8:pos + 8 + n]
            if len(body) != n or pos + 12 + n > len(data):
                raise PngError("PNG cortado")
            if zlib.crc32(kind + body) != struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0]:
                raise PngError(f"PNG corrompido (CRC do {kind.decode('latin-1')})")
            pos += 12 + n
            if kind == b"IHDR":
                ihdr = struct.unpack(">IIBBBBB", body[:13])
            elif kind == b"tRNS":
                trns = body
            elif kind == b"IDAT":
                idat.append(body)
            elif kind == b"IEND":
                break
        if not ihdr:
            raise PngError("PNG sem IHDR")
    except (PngError, struct.error) as e:
        return None, None, str(e) if isinstance(e, PngError) else "PNG inválido"

    w, h, depth, ctype, _, _, interlace = ihdr
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    ok_depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
    if not channels or depth not in ok_depths[ctype] or not w or not h:
        return w, h, f"PNG com cabeçalho inválido (tipo de cor {ctype}, {depth} bits)"
    if interlace:
        return w, h, "PNG entrelaçado (Adam7); salve sem entrelaçamento"
    stride = (w * channels * depth + 7) // 8
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return w, h, "PNG com dados corrompidos"
    if len(raw) < h * (stride + 1):
        return w, h, "PNG com dados a menos"

    # Só a transparência importa: cor sem alpha (ou com tRNS de uma cor só) é sempre 0 ou 255
    if ctype in (0, 2) or (ctype == 3 and not trns):
        return w, h, None
    bpp = max(1, channels * depth // 8)
    partial = 0
    try:
        for row in _unfilter(raw, h, stride, bpp):
            if ctype == 3:
                if depth == 8:
                    idx = set(row[:w])
                else:
                    per = 8 // depth
                    idx = {(row[x // per] >> (8 - depth * (x % per + 1))) & ((1 << depth) - 1) for x in range(w)}
                partial += sum(1 for i in idx if i < len(trns) and trns[i] not in (0, 255))
            elif depth == 8:
                partial += sum(1 for a in row[channels - 1::channels] if a not in (0, 255))
            else:
                hi, lo = row[2 * channels - 2::2 * channels], row[2 * channels - 1::2 * channels]
                partial += sum(1 for a, b in zip(hi, lo) if not (a == b and a in (0, 255)))
    except PngError as e:
        return w, h, str(e)
    if partial:
        what = "cor(es) da paleta" if ctype == 3 else "pixel(s)"
        return w, h, f"{partial} {what} com alpha parcial; use só 0 (transparente) ou 255"
    return w, h, None


# ---------------------------------------------------------------- WAV

def wav_check(data):
    """(bytes de PCM, problema ou None). Só PCM, 1 canal, 16000 Hz, 16 bits."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return 0, "não é WAV (RIFF/WAVE)"
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        kind, n = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if kind == b"fmt ":
            if n < 16 or body + 16 > len(data):
                return 0, "chunk fmt inválido"
            fmt = struct.unpack("<HHIIHH", data[body:body + 16])
        elif kind == b"data":
            if not fmt:
                return 0, "o chunk fmt precisa vir antes do data"
            tag, ch, rate, _, _, bits = fmt
            if (tag, ch, rate, bits) != (1, 1, 16000, 16):
                return 0, (f"precisa ser PCM, 1 canal, 16000 Hz, 16 bits "
                           f"(é formato {tag}, {ch} canal(is), {rate} Hz, {bits} bits)")
            if body + n > len(data):
                return 0, "o chunk data passa do fim do arquivo"
            if not n or n % 2:
                return 0, "chunk data vazio ou com tamanho ímpar"
            return n, None
        pos = body + n + (n & 1)
    return 0, "WAV sem chunk data"


# ---------------------------------------------------------------- conferência

def _check_files(folder, oba, json_size, errs):
    """Confere files (caminho, existência, sha, limites). Devolve {caminho: bytes} dos que existem."""
    files = oba.get("files") if isinstance(oba.get("files"), dict) else {}
    found, lower, total = {}, {}, json_size
    if len(files) + 1 > FILES_MAX:
        errs.append(f"files: {len(files)} arquivos; com o oba.json, o máximo é {FILES_MAX}")
    for rel, sha in files.items():
        where = f"files.{rel}"
        if rel == "oba.json":
            errs.append(f"{where}: o oba.json não entra em files")
            continue
        if not valid_path(rel):
            errs.append(f"{where}: caminho inválido ({PATH_RULE})")
            continue
        if rel.lower() in lower:
            errs.append(f"{where}: o cartão (FAT) não diferencia de {lower[rel.lower()]}")
        lower[rel.lower()] = rel
        if any(o.startswith(rel + "/") for o in files):
            errs.append(f"{where}: é arquivo e pasta ao mesmo tempo")
        f = folder / rel
        if not f.is_file():
            errs.append(f"{where}: arquivo não existe")
            continue
        data = f.read_bytes()
        found[rel] = data
        total += len(data)
        if not data:
            errs.append(f"{where}: arquivo vazio")
        elif len(data) > FILE_MAX:
            errs.append(f"{where}: {len(data)} bytes; o máximo por arquivo é {FILE_MAX} (2 MB)")
        if isinstance(sha, str) and hashlib.sha256(data).hexdigest() != sha.lower():
            errs.append(f"{where}: sha256 não bate")
    if total > TOTAL_MAX:
        errs.append(f"files: {total} bytes com o oba.json; o máximo é {TOTAL_MAX} (6 MB)")
    return files, found


def _check_sprites(look, files, found, errs):
    size, origin, scale = look.get("size"), look.get("origin"), look.get("scale")
    frames = look.get("frames") if isinstance(look.get("frames"), dict) else {}

    def ok(v, t):
        return isinstance(v, t) and not isinstance(v, bool)

    if not (isinstance(size, list) and len(size) == 2 and all(ok(v, int) and v > 0 for v in size)):
        return  # o schema já reclamou
    w, h = size
    if ok(scale, int):
        if w * scale > SPRITE_W_MAX:
            errs.append(f"look.size: {w} × {scale} = {w * scale} px de largura na tela; o máximo é {SPRITE_W_MAX}")
        if h * scale > SPRITE_H_MAX:
            errs.append(f"look.size: {h} × {scale} = {h * scale} px de altura na tela; o máximo é {SPRITE_H_MAX}")
    if isinstance(origin, list) and len(origin) == 2 and all(ok(v, (int, float)) for v in origin):
        if not 0 <= origin[0] <= w or not 0 <= origin[1] <= h:
            errs.append(f"look.origin: {origin} precisa ficar dentro do quadro (0 a {w}, 0 a {h})")
    mem = {}
    for mood, fr in frames.items():
        sheet = fr.get("sheet") if isinstance(fr, dict) else None
        where = f"look.frames.{mood}.sheet"
        if not isinstance(sheet, str) or sheet in mem:
            continue
        if not valid_path(sheet):
            errs.append(f"{where}: caminho inválido ({PATH_RULE})")
            continue
        if sheet not in files:
            errs.append(f"{where}: {sheet} não está em files")
            continue
        if sheet not in found:
            continue  # já reclamado em files
        pw, ph, problem = png_check(found[sheet])
        if pw and ph:
            mem[sheet] = pw * ph * 3
        if problem:
            errs.append(f"{where}: {sheet}: {problem}")
            continue
        if ph != h:
            errs.append(f"{where}: {sheet} tem {ph} px de altura; precisa ser {h} (look.size)")
        if pw % w:
            errs.append(f"{where}: {sheet} tem {pw} px de largura, que não é múltiplo de {w} (look.size)")
        elif not 1 <= pw // w <= FRAMES_MAX:
            errs.append(f"{where}: {sheet} tem {pw // w} quadros; o máximo é {FRAMES_MAX}")
    total = sum(mem.values())
    if total > SPRITES_MAX:
        errs.append(f"look.frames: as folhas ocupam {total} bytes decodificadas (largura × altura × 3); "
                    f"o máximo é {SPRITES_MAX} (1 MB)")


def _check_sounds(oba, files, found, errs):
    sounds = oba.get("sounds") if isinstance(oba.get("sounds"), dict) else {}
    total = 0
    for name, rel in sounds.items():
        where = f"sounds.{name}"
        if not valid_path(rel):
            errs.append(f"{where}: caminho inválido ({PATH_RULE})")
            continue
        if rel not in files:
            errs.append(f"{where}: {rel} não está em files")
            continue
        if rel not in found:
            continue
        n, problem = wav_check(found[rel])
        if problem:
            errs.append(f"{where}: {rel}: {problem}")
            continue
        if n // 2 * 1000 > SOUND_MAX_MS * 16000:
            errs.append(f"{where}: {rel} tem {n / 32000:.1f} s; o máximo é {SOUND_MAX_MS // 1000} s")
        total += n
    if total > SOUNDS_MAX:
        errs.append(f"sounds: {total} bytes de PCM somando todos os sons; o máximo é {SOUNDS_MAX} (512 KB)")
    requires = oba.get("requires") if isinstance(oba.get("requires"), list) else []
    if sounds and "speaker" not in requires:
        errs.append("requires: o Oba tem sounds, então precisa de \"speaker\"")
    reflexes = oba.get("reflexes") if isinstance(oba.get("reflexes"), list) else []
    for i, r in enumerate(reflexes):
        if isinstance(r, dict) and isinstance(r.get("sound"), str) and r["sound"] not in sounds:
            errs.append(f"reflexes.{i}.sound: {r['sound']!r} não está em sounds")


def validate(target):
    """Lista de problemas (vazia = ok)."""
    try:
        import jsonschema
    except ImportError:
        sys.exit("falta o jsonschema: pip install jsonschema")
    try:
        folder, path, oba = load(target)
    except (OSError, UnicodeDecodeError) as e:
        return [f"não deu para ler o oba.json: {e}"]
    except json.JSONDecodeError as e:
        return [f"oba.json não é JSON válido: {e}"]
    if not isinstance(oba, dict):
        return ["oba.json: precisa ser um objeto"]
    errs = []
    schema = json.loads(SCHEMA.read_text())
    path_def = schema["$defs"]["path"]
    v = jsonschema.Draft202012Validator(schema)
    for e in sorted(v.iter_errors(oba), key=lambda e: list(map(str, e.path))):
        # Caminho ruim, oba.json em files e arquivos demais: as conferências de baixo explicam melhor
        if (e.validator in ("pattern", "maxLength") and e.schema == path_def) or \
                (e.validator == "not" and e.validator_value == {"const": "oba.json"}) or \
                (e.validator == "maxProperties" and list(e.path) == ["files"]):
            continue
        where = ".".join(str(k) for k in e.path) or "(raiz)"
        msg = e.message
        if e.validator in ("maxProperties", "maxItems"):
            msg = f"tem {len(e.instance)} itens; o máximo é {e.validator_value}"
        elif len(msg) > 240:
            msg = msg[:237] + "..."
        errs.append(f"{where}: {msg}")
    raw = path.stat().st_size
    size = len(json.dumps(oba, separators=(",", ":"), ensure_ascii=False).encode())
    if max(raw, size) > JSON_MAX:
        errs.append(f"oba.json tem {raw} bytes ({size} minificado); o máximo é {JSON_MAX}")
    if path.name == "oba.json" and oba.get("id") and folder.name != oba["id"]:
        errs.append(f"a pasta ({folder.name}) precisa ter o nome do id ({oba['id']})")
    files, found = _check_files(folder, oba, raw, errs)
    look = oba.get("look")
    if isinstance(look, dict) and look.get("type") == "sprites":
        _check_sprites(look, files, found, errs)
    _check_sounds(oba, files, found, errs)
    return errs


# ---------------------------------------------------------------- comandos

def cmd_validate(a):
    bad = 0
    for t in a.targets:
        errs = validate(t)
        bad += bool(errs)
        print(f"{t}: {'ok' if not errs else f'{len(errs)} problema(s)'}")
        for e in errs:
            print(f"  - {e}")
    sys.exit(1 if bad else 0)


def cmd_embed(a):
    errs = validate(a.oba)
    if errs:
        sys.exit("não dá para embutir, o Oba tem problemas:\n  - " + "\n  - ".join(errs))
    _, path, oba = load(a.oba)
    if oba["look"].get("type") == "sprites" or oba.get("sounds") or oba.get("files"):
        sys.exit("o Oba embutido precisa ser rig, sem sons nem arquivos (o firmware só leva o JSON)")
    text = json.dumps(oba, separators=(",", ":"), ensure_ascii=False)
    if ")OBA\"" in text:
        sys.exit("o JSON tem )OBA\" e quebraria o raw string")
    rel = path.resolve().relative_to(ROOT)
    out = Path(a.out)
    out.write_text(
        f"// Gerado por tools/oba.py embed a partir de {rel}. Não edite à mão.\n"
        "// É o Oba de quando não há cartão (ou o escolhido não está nele).\n"
        "#pragma once\n\n"
        f'static const char BUILTIN_OBA[] = R"OBA({text})OBA";\n')
    print(f"{out}: {oba['id']} {oba.get('version', '')}, {len(text.encode())} bytes")


def cmd_fmt(a):
    for t in a.targets:
        _, path, oba = load(t)
        path.write_text(dump(oba))
        print(f"{path}: formatado")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate", help="confere um ou mais Obas (pasta ou oba.json)")
    p.add_argument("targets", nargs="+")
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("embed", help="gera o header do Oba embutido no firmware")
    p.add_argument("oba")
    p.add_argument("out", nargs="?", default=str(ROOT / "src" / "builtin_oba.h"))
    p.set_defaults(fn=cmd_embed)
    p = sub.add_parser("fmt", help="reescreve com a formatação padrão")
    p.add_argument("targets", nargs="+")
    p.set_defaults(fn=cmd_fmt)
    # Comandos pela nuvem (install, remove, list, activate, play, publish)
    try:
        import oba_remote
    except ImportError as e:
        if e.name != "oba_remote":
            print(f"aviso: tools/oba_remote.py não carregou ({e}); só validate, embed e fmt", file=sys.stderr)
        oba_remote = None
    if oba_remote:
        oba_remote.register(sub)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
