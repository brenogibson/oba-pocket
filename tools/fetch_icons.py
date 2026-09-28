"""Baixa os ícones oficiais dos serviços da AWS para o roteador do harness.

  python3 tools/fetch_icons.py [--url <zip>]

Do pacote de ícones de arquitetura da AWS, só os PNG de 64x64 dos serviços
(Arch_<Nome>_64.png) vão para harness/router/icons/<Nome>.png. Os ícones não
entram no repo: a licença deles é da AWS. O setup.py chama este script quando
a pasta está vazia.
"""

import argparse
import io
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

# https://aws.amazon.com/architecture/icons/ (pacote de 31/07/2026)
URL = ("https://d1.awsstatic.com/onedam/marketing-channels/website/public/shared/architecture-icon-release/"
       "Icon-package_07312026.5846e92413caa21490223536cc97f1269e44fa92.zip")
OUT = Path(__file__).resolve().parent.parent / "harness" / "router" / "icons"
NAME = re.compile(r"(?:^|/)64/Arch_(.+)_64\.png$")


def fetch(url: str = URL, out: Path = OUT) -> int:
    print("baixando", url.split("/")[-1], "...")
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    out.mkdir(parents=True, exist_ok=True)
    names = set()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            m = NAME.search(info.filename)
            if not m or "__MACOSX" in info.filename:
                continue
            (out / f"{m.group(1)}.png").write_bytes(z.read(info))
            names.add(m.group(1))
    print(f"{len(names)} ícones em {out}")
    return len(names)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=URL, help="zip do pacote de ícones (a AWS troca o link a cada versão)")
    args = ap.parse_args()
    if not fetch(args.url):
        sys.exit("nenhum ícone Arch_*_64.png no zip: o formato do pacote mudou?")


if __name__ == "__main__":
    main()
