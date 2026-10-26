"""RDD2022 kutularını SAM ile çokgene çevirerek YOLO segmentasyon veri seti üretir.

RDD2022'de yalnızca kutu etiketi var. Her kutu SAM2'ye (Segment Anything) "bu kutunun
içindeki nesneyi bul" diye verilir, çıkan maske çokgene çevrilir.

Kalite kuralları (sözde etiket hatalarını sınırlamak için):
  - Maske kutuyla kırpılır: SAM'in kutu dışına taşan kısmı (gölge, yol yüzeyi) atılır
  - Maske kutunun çok küçük bir kısmını kaplıyorsa (MIN_FILL) SAM nesneyi bulamamış sayılır
  - Maske kutunun neredeyse tamamını kaplıyorsa (MAX_FILL) şekil bilgisi yoktur
  Bu iki durumda insanın çizdiği kutu 4 köşeli çokgen olarak korunur.

Etiketler görüntü başına ayrı dosyaya yazılır; oturum koparsa aynı komut kaldığı yerden devam eder.

Kullanım (Colab, GPU):
    python scripts/rdd_to_seg_sam.py --src /content/rdd_yolo --out /content/drive/MyDrive/yol-hasar-tespit/rdd_seg
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np

CLASSES = ["D00", "D10", "D20", "D40"]
MIN_FILL = 0.03
MAX_FILL = 0.92
MAX_POINTS = 80
MIN_PART_RATIO = 0.05  # toplam maske alanının bu kadarından küçük parçalar gürültü sayılır


def read_boxes(path: Path) -> list[tuple[int, float, float, float, float]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 5:
            out.append((int(parts[0]), *map(float, parts[1:])))
    return out


def box_polygon(xc, yc, w, h) -> list[list[float]]:
    x1, y1, x2, y2 = xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def mask_to_polygon(mask: np.ndarray, box_px: tuple[int, int, int, int]) -> tuple[list[list[float]] | None, float]:
    """Kutuyla kırpılmış maskeden piksel koordinatlı tek bir çokgen üretir. (çokgen, doluluk oranı) döner."""
    x1, y1, x2, y2 = box_px
    clipped = np.zeros_like(mask, dtype=np.uint8)
    clipped[y1:y2, x1:x2] = mask[y1:y2, x1:x2]
    box_area = max(1, (x2 - x1) * (y2 - y1))
    fill = float(clipped.sum()) / box_area
    if not MIN_FILL <= fill <= MAX_FILL:
        return None, fill

    contours, _ = cv2.findContours(clipped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    total = sum(cv2.contourArea(c) for c in contours) or 1.0
    parts = [c for c in contours if cv2.contourArea(c) >= MIN_PART_RATIO * total]
    if not parts:
        return None, fill

    simplified = []
    for c in parts:
        eps = 0.002 * cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, eps, True).reshape(-1, 2)
        while len(approx) > MAX_POINTS:
            eps *= 1.5
            approx = cv2.approxPolyDP(c, eps, True).reshape(-1, 2)
        if len(approx) >= 3:
            simplified.append(approx.astype(np.float32))
    if not simplified:
        return None, fill
    if len(simplified) == 1:
        pts = simplified[0]
    else:
        # Parçalı maskeler (kesik kesik çatlaklar) YOLO formatı için tek çokgende birleştirilir
        from ultralytics.data.converter import merge_multi_segment

        pts = np.concatenate(merge_multi_segment([s.reshape(-1).tolist() for s in simplified]), axis=0)
    return pts.tolist(), fill


def format_line(cls: int, pts: list[list[float]]) -> str:
    return f"{cls} " + " ".join(f"{min(1, max(0, x)):.6f} {min(1, max(0, y)):.6f}" for x, y in pts)


def link(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        try:
            dst.symlink_to(src.resolve())
        except OSError:
            shutil.copy2(src, dst)


def convert_split(sam, src: Path, out: Path, split: str, log_path: Path, limit: int | None) -> None:
    img_dir, lbl_dir = src / "images" / split, src / "labels" / split
    (out / "images" / split).mkdir(parents=True, exist_ok=True)
    (out / "labels" / split).mkdir(parents=True, exist_ok=True)
    images = sorted(p for p in img_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if limit:
        images = images[:limit]

    todo = [p for p in images if not (out / "labels" / split / f"{p.stem}.txt").exists()]
    print(f"{split}: {len(images)} görüntü, {len(images) - len(todo)} hazır, {len(todo)} işlenecek")
    with log_path.open("a", encoding="utf-8") as log:
        for n, img_path in enumerate(todo, 1):
            link(img_path, out / "images" / split / img_path.name)
            boxes = read_boxes(lbl_dir / f"{img_path.stem}.txt")
            lines, sources = [], []
            if boxes:
                img = cv2.imread(str(img_path))
                h, w = img.shape[:2]
                px = [(int((xc - bw / 2) * w), int((yc - bh / 2) * h), int((xc + bw / 2) * w), int((yc + bh / 2) * h))
                      for _, xc, yc, bw, bh in boxes]
                px = [(max(0, a), max(0, b), min(w, c), min(h, d)) for a, b, c, d in px]
                masks = []
                try:
                    r = sam(img, bboxes=[list(b) for b in px], verbose=False)[0]
                    if r.masks is not None:
                        masks = [cv2.resize(m.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
                                 for m in r.masks.data.cpu().numpy()]
                except Exception as e:  # tek bir görüntünün hatası tüm dönüşümü durdurmamalı
                    print(f"  SAM hatası, kutular korunuyor: {img_path.name}: {e}")
                for i, (cls, xc, yc, bw, bh) in enumerate(boxes):
                    poly, fill = (mask_to_polygon(masks[i], px[i]) if i < len(masks) else (None, 0.0))
                    if poly is not None:
                        lines.append(format_line(cls, [[x / w, y / h] for x, y in poly]))
                        sources.append(("sam", CLASSES[cls], round(fill, 3)))
                    else:
                        lines.append(format_line(cls, box_polygon(xc, yc, bw, bh)))
                        sources.append(("kutu", CLASSES[cls], round(fill, 3)))
            (out / "labels" / split / f"{img_path.stem}.txt").write_text("\n".join(lines))
            log.write(json.dumps({"split": split, "img": img_path.name, "kaynak": sources}) + "\n")
            if n % 200 == 0:
                log.flush()
                print(f"  {split}: {n}/{len(todo)}")


def summarize(log_path: Path) -> dict:
    by_class = defaultdict(Counter)
    seen = {}
    for line in log_path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        seen[(rec["split"], rec["img"])] = rec["kaynak"]  # yeniden çalıştırmalarda son kayıt geçerli
    for sources in seen.values():
        for src, cls, _ in sources:
            by_class[cls][src] += 1
    return {cls: {"sam": c["sam"], "kutu": c["kutu"],
                  "sam_orani": round(c["sam"] / max(1, c["sam"] + c["kutu"]), 3)} for cls, c in sorted(by_class.items())}


def preview(out: Path, path: Path, n: int = 12, seed: int = 0) -> None:
    """Rastgele örneklerde çokgenleri çizer: dönüşüm kalitesini gözle kontrol etmek için."""
    colors = {0: (0, 220, 180), 1: (0, 200, 255), 2: (0, 120, 255), 3: (0, 0, 230)}
    labels = [p for p in (out / "labels" / "train").glob("*.txt") if p.stat().st_size > 0]
    random.Random(seed).shuffle(labels)
    tiles = []
    for lbl in labels[:n]:
        img = cv2.imread(str(out / "images" / "train" / f"{lbl.stem}.jpg"))
        if img is None:
            continue
        h, w = img.shape[:2]
        over = img.copy()
        for line in lbl.read_text().splitlines():
            vals = line.split()
            cls, pts = int(vals[0]), np.array(vals[1:], dtype=float).reshape(-1, 2) * [w, h]
            cv2.fillPoly(over, [pts.astype(np.int32)], colors[cls])
            cv2.polylines(img, [pts.astype(np.int32)], True, colors[cls], max(2, w // 300))
        img = cv2.addWeighted(over, 0.35, img, 0.65, 0)
        tiles.append(cv2.resize(img, (480, 360)))
    if not tiles:
        return
    while len(tiles) % 4:
        tiles.append(np.full((360, 480, 3), 255, np.uint8))
    cv2.imwrite(str(path), np.vstack([np.hstack(tiles[i:i + 4]) for i in range(0, len(tiles), 4)]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, required=True, help="prepare_rdd2022.py çıktısı (YOLO kutu veri seti)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sam", default="sam2.1_b.pt", help="sam2.1_t.pt daha hızlı, sam2.1_l.pt daha doğru")
    ap.add_argument("--splits", nargs="+", default=["train", "val", "test"])
    ap.add_argument("--limit", type=int, help="Deneme için bölüm başına en fazla bu kadar görüntü")
    args = ap.parse_args()

    from ultralytics import SAM

    sam = SAM(args.sam)
    args.out.mkdir(parents=True, exist_ok=True)
    log_path = args.out / "_kaynak.jsonl"
    for split in args.splits:
        convert_split(sam, args.src, args.out, split, log_path, args.limit)

    (args.out / "data.yaml").write_text(
        f"path: {args.out.resolve()}\ntrain: images/train\nval: images/val\ntest: images/test\n"
        "names:\n" + "".join(f"  {i}: {c}\n" for i, c in enumerate(CLASSES))
    )
    stats = summarize(log_path)
    (args.out / "donusum_istatistik.json").write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    preview(args.out, args.out / "donusum_onizleme.jpg")
    print("\nSınıf bazında SAM ile çokgene çevrilen / kutu olarak kalan:")
    for cls, s in stats.items():
        print(f"  {cls}: SAM {s['sam']}, kutu {s['kutu']} (SAM oranı %{100 * s['sam_orani']:.0f})")
    print(f"Hazır: {args.out / 'data.yaml'}")


if __name__ == "__main__":
    main()
