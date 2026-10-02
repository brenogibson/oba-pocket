"""Baixa os ícones oficiais dos serviços da AWS para o roteador do harness.

  python3 tools/fetch_icons.py [--url <zip>]

Do pacote de ícones de arquitetura da AWS, só os PNG de 64x64 dos serviços
(Arch_<Nome>_64.png) vão para harness/router/icons/<Nome>.png. Os ícones não
entram no repo: a licença deles é da AWS. O setup.py chama este script quando
a pasta está vazia; se o download falhar, ele segue sem ícones (os balões saem
sem o ícone do serviço).
"""

import argparse
import io
import re
import sys
import urllib.request
import zipfile
import zlib
from http.client import HTTPException
from pathlib import Path

# https://aws.amazon.com/architecture/icons/ (pacote de 31/07/2026)
URL = ("https://d1.awsstatic.com/onedam/marketing-channels/website/public/shared/architecture-icon-release/"
       "Icon-package_07312026.5846e92413caa21490223536cc97f1269e44fa92.zip")
OUT = Path(__file__).resolve().parent.parent / "harness" / "router" / "icons"
NAME = re.compile(r"(?:^|/)64/Arch_([^/\\]+)_64\.png$")  # sem barra: nada sai da pasta icons/


def fetch(url: str = URL, out: Path = OUT) -> int:
    print("baixando", url.split("/")[-1], "...")
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    icons = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:  # lê tudo antes de gravar: zip com erro não deixa metade
            for info in z.infolist():
                m = NAME.search(info.filename)
                if m and "__MACOSX" not in info.filename:
                    icons[m.group(1)] = z.read(info)
    except (zlib.error, EOFError) as e:  # membro corrompido: o zipfile não converte para BadZipFile
        raise zipfile.BadZipFile(e) from e
    out.mkdir(parents=True, exist_ok=True)
    for name, png in icons.items():
        (out / f"{name}.png").write_bytes(png)
    print(f"{len(icons)} ícones em {out}")
    return len(icons)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=URL, help="zip do pacote de ícones (a AWS troca o link a cada versão)")
    args = ap.parse_args()
    try:
        n = fetch(args.url)
    except (OSError, HTTPException, zipfile.BadZipFile, ValueError) as e:  # URLError e HTTPError são OSError
        sys.exit(f"não baixei o pacote ({e}): pegue o link do zip em https://aws.amazon.com/architecture/icons/ "
                 "e rode de novo com --url")
    if not n:
        sys.exit("nenhum ícone Arch_*_64.png no zip: o formato do pacote mudou?")


if __name__ == "__main__":
    main()
