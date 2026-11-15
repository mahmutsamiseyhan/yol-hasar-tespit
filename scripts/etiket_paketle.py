"""Etiket aracında (/etiket) incelenen görüntüleri Colab eğitim paketine çevirir.

  tamam       -> çokgenlerin çevreleyen kutularıyla hasarlı örnek
  hasarsiz    -> boş etiket: modelin hasar sandığı ama sağlam olan yol (yanlış alarmı azaltan zor örnek)
  kapsam_disi ve incelenmemiş görüntüler pakete girmez

Bursa test klasörü (data/bursa_test/etiket) bilerek reddedilir: test görüntüsü eğitime girerse ölçüm anlamsızlaşır.

Kullanım:
    python scripts/etiket_paketle.py --labels data/hasar_adaylari --out data/hasar_etiket.zip
Çıktı zip: images/ + labels/ + atif.csv + rapor.json (v3 notebook'u Drive'dan okur)
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.labeling import CLASSES, LabelStore, bounding_box, read_shapes  # noqa: E402

BURSA_TEST = (ROOT / "data" / "bursa_test" / "etiket").resolve()


def build(labels: Path, out: Path) -> dict:
    if labels.resolve() == BURSA_TEST:
        raise SystemExit("Bursa test seti eğitime paketlenemez.")
    store = LabelStore(labels)
    states = store._states()
    decisions, boxes = Counter(), Counter()
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for name in store.images():
            state = states.get(name, {}).get("durum")
            decisions[state or "incelenmedi"] += 1
            if state not in ("tamam", "hasarsiz"):
                continue
            shapes = read_shapes((labels / name).with_suffix(".txt")) if state == "tamam" else []
            lines = []
            for s in shapes:
                b = bounding_box(s["noktalar"])
                lines.append(f"{CLASSES.index(s['sinif'])} {b['x']:.6f} {b['y']:.6f} {b['w']:.6f} {b['h']:.6f}")
                boxes[s["sinif"]] += 1
            z.write(labels / name, f"images/{Path(name).stem}.jpg")
            z.writestr(f"labels/{Path(name).stem}.txt", "\n".join(lines))
        if (labels / "atif.csv").exists():
            z.write(labels / "atif.csv", "atif.csv")
        report = {"kararlar": dict(decisions), "kutular": dict(boxes),
                  "egitime_giren": decisions["tamam"] + decisions["hasarsiz"],
                  "kaynak": str(labels.relative_to(ROOT) if labels.resolve().is_relative_to(ROOT) else labels)}
        z.writestr("rapor.json", json.dumps(report, indent=2, ensure_ascii=False))
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labels", type=Path, default=ROOT / "data" / "hasar_adaylari")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "hasar_etiket.zip")
    args = ap.parse_args()
    report = build(args.labels, args.out)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nZip: {args.out} ({args.out.stat().st_size / 1e6:.0f} MB) → Drive'daki yol-hasar-tespit klasörüne yükleyin.")


if __name__ == "__main__":
    main()
