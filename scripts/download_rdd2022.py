"""RDD2022'yi resmi figshare kaydından (CC BY 4.0) ülke bazında indirir.

Figshare'deki 13 GB'lık arşiv ülke arşivlerini sıkıştırmadan (stored) içerir.
HTTP range istekleriyle yalnızca seçilen ülkeler çekilir; örneğin varsayılan seçim
10.6 GB'lık Norveç bölümünü atlar ve ~2.6 GB indirir.

Kullanım:
    python scripts/download_rdd2022.py --out /content/rdd_raw
    python scripts/download_rdd2022.py --out /content/rdd_raw --countries Czech India

Kaynak: https://figshare.com/articles/dataset/21431547
Atıf: Arya, D. et al. (2022). RDD2022: A multi-national image dataset for automatic Road Damage Detection.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

FIGSHARE_URL = "https://ndownloader.figshare.com/files/38030910"
ALL_COUNTRIES = ["China_Drone", "China_MotorBike", "Czech", "India", "Japan", "Norway", "United_States"]
# Norveç çok yüksek çözünürlüklü ve büyük; drone görüntüleri ise araç kamerası senaryosuna uymuyor
DEFAULT_COUNTRIES = ["Czech", "India", "Japan", "United_States", "China_MotorBike"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--countries", nargs="+", default=DEFAULT_COUNTRIES, choices=ALL_COUNTRIES)
    ap.add_argument("--keep-zips", action="store_true")
    args = ap.parse_args()

    from remotezip import RemoteZip  # pip install remotezip

    args.out.mkdir(parents=True, exist_ok=True)
    with RemoteZip(FIGSHARE_URL) as outer:
        sizes = {i.filename: i.file_size for i in outer.infolist()}
        for country in args.countries:
            member = f"RDD2022/{country}.zip"
            target = args.out / f"{country}"
            if (target / country / "train").exists() or (target / "train").exists():
                print(f"{country}: zaten var, atlanıyor")
                continue
            print(f"{country}: {sizes[member] / 1e6:.0f} MB indiriliyor...")
            outer.extract(member, args.out)
            inner = args.out / member
            with zipfile.ZipFile(inner) as z:
                z.extractall(target)
            if not args.keep_zips:
                inner.unlink()
            print(f"{country}: açıldı -> {target}")
    print("Tamam.")


if __name__ == "__main__":
    main()
