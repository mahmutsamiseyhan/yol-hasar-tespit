"""RDD2022 (Pascal VOC) -> YOLO formatı dönüştürücü.

RDD2022'nin resmi test bölümü etiketsiz olduğu için etiketli eğitim verisi
ülke bazında tabakalı olarak train/val/test'e ayrılır (varsayılan 80/10/10, sabit seed).
Böylece test metrikleri modelin hiç görmediği, etiketli görüntülerden ölçülür.

Kullanım:
    python scripts/prepare_rdd2022.py --src /content/rdd_raw --out /content/rdd_yolo

--src altındaki klasör yapısı önemli değildir; tüm *.xml dosyaları bulunur ve
aynı isimli görüntüyle eşleştirilir (RDD2022 dosya adları ülke önekiyle benzersizdir).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

CLASSES = ["D00", "D10", "D20", "D40"]
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}


def parse_voc(xml_path: Path, image_path: Path) -> list[tuple[int, float, float, float, float]]:
    root = ET.parse(xml_path).getroot()
    size = root.find("size")
    w = float(size.findtext("width", 0)) if size is not None else 0
    h = float(size.findtext("height", 0)) if size is not None else 0
    if not w or not h:  # bazı XML'lerde boyut eksik: görüntüden oku
        from PIL import Image

        with Image.open(image_path) as im:
            w, h = im.size

    labels = []
    for obj in root.iter("object"):
        name = (obj.findtext("name") or "").strip()
        if name not in CLASSES:
            continue
        bb = obj.find("bndbox")
        x1, y1, x2, y2 = (float(bb.findtext(k)) for k in ("xmin", "ymin", "xmax", "ymax"))
        x1, x2 = sorted((max(0.0, min(w, x1)), max(0.0, min(w, x2))))
        y1, y2 = sorted((max(0.0, min(h, y1)), max(0.0, min(h, y2))))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        labels.append((CLASSES.index(name), (x1 + x2) / 2 / w, (y1 + y2) / 2 / h, (x2 - x1) / w, (y2 - y1) / h))
    return labels


def country_of(path: Path) -> str:
    # RDD2022 dosya adları "Czech_000123.jpg", "China_MotorBike_000001.jpg" biçimindedir
    return path.stem.rsplit("_", 1)[0]


def place(src: Path, dst: Path):
    try:
        os.link(src, dst)  # aynı disk üzerindeyse kopyalamadan bağla
    except OSError:
        shutil.copy2(src, dst)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--split", default="0.8,0.1,0.1", help="train,val,test oranları")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-background", type=float, default=0.15,
                    help="Etiketsiz (hasarsız) görüntülerin veri setindeki azami oranı")
    args = ap.parse_args()

    images = {p.stem: p for p in args.src.rglob("*") if p.suffix.lower() in IMAGE_EXTS}
    xmls = sorted(args.src.rglob("*.xml"))
    print(f"{len(xmls)} XML, {len(images)} görüntü bulundu")

    samples, missing = [], 0
    for xml in xmls:
        # Standart yapı: <ülke>/train/annotations/xmls/X.xml <-> <ülke>/train/images/X.jpg
        sibling = xml.parent.parent.parent / "images" / f"{xml.stem}.jpg"
        img = sibling if sibling.exists() else images.get(xml.stem)
        if img is None:
            missing += 1
            continue
        samples.append((img, parse_voc(xml, img)))
    if missing:
        print(f"Uyarı: görüntüsü bulunamayan {missing} XML atlandı")

    rng = random.Random(args.seed)
    labeled = [s for s in samples if s[1]]
    background = [s for s in samples if not s[1]]
    max_bg = int(len(labeled) * args.max_background / (1 - args.max_background))
    rng.shuffle(background)
    samples = labeled + background[:max_bg]
    print(f"{len(labeled)} etiketli + {min(len(background), max_bg)} hasarsız görüntü kullanılacak")

    by_country = defaultdict(list)
    for s in samples:
        by_country[country_of(s[0])].append(s)

    fractions = [float(x) for x in args.split.split(",")]
    splits = {"train": [], "val": [], "test": []}
    for country, items in sorted(by_country.items()):
        rng.shuffle(items)
        n_train = int(len(items) * fractions[0])
        n_val = int(len(items) * fractions[1])
        splits["train"] += items[:n_train]
        splits["val"] += items[n_train:n_train + n_val]
        splits["test"] += items[n_train + n_val:]

    if args.out.exists():
        shutil.rmtree(args.out)
    stats = {}
    for split, items in splits.items():
        (args.out / "images" / split).mkdir(parents=True)
        (args.out / "labels" / split).mkdir(parents=True)
        counts = Counter()
        countries = Counter()
        for img, labels in items:
            place(img, args.out / "images" / split / img.name)
            lines = [f"{c} {x:.6f} {y:.6f} {w:.6f} {h:.6f}" for c, x, y, w, h in labels]
            (args.out / "labels" / split / f"{img.stem}.txt").write_text("\n".join(lines))
            counts.update(CLASSES[c] for c, *_ in labels)
            countries[country_of(img)] += 1
        stats[split] = {"goruntu": len(items), "sinif_ornekleri": dict(sorted(counts.items())),
                        "ulkeler": dict(sorted(countries.items()))}

    (args.out / "data.yaml").write_text(
        f"path: {args.out.resolve()}\ntrain: images/train\nval: images/val\ntest: images/test\n"
        f"names:\n" + "".join(f"  {i}: {c}\n" for i, c in enumerate(CLASSES))
    )
    (args.out / "stats.json").write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"Hazır: {args.out / 'data.yaml'}")


if __name__ == "__main__":
    main()
