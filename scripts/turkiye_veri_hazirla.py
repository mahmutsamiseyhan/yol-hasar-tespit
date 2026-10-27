"""Türkiye şehirlerinden eğitim verisi: Mapillary görüntüleri + mevcut modelle güvenli yarı otomatik etiketleme.

Bursa bilerek dışarıda bırakılır: Bursa test seti modelin hiç görmediği bölgeden kalmalı.

Kurallar (sözde etiket hatalarını sınırlamak için):
  - Model en düşük eşikte (MIN_CONF) bile hiçbir şey görmüyorsa -> hasarsız yol (boş etiket)
  - Tüm tespitler POS_CONF ve üstündeyse                     -> o kutularla hasarlı örnek
  - Arada kalan (emin olunmayan) görüntüler                    -> atılır
  - Gece / karanlık görüntüler                                 -> atılır
Çıktı YOLO formatında bir zip: images/ + labels/ + atif.csv + rapor.json

Kullanım:
    python scripts/turkiye_veri_hazirla.py --model models/best.pt --out data/turkiye_egitim
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from fetch_mapillary_bursa import _token_from_env_file, grid, query  # noqa: E402

CLASSES = ["D00", "D10", "D20", "D40"]
MIN_CONF, POS_CONF = 0.05, 0.60
LOW_LIGHT = 70
MAX_SIDE = 1280
CELL_DEG = 0.0135  # ~4x4 km şehir alanı 3x3 hücreye bölünür
BURSA = (39.95, 28.40, 40.45, 29.80)  # güney, batı, kuzey, doğu: hariç tutulur

# Şehir merkezleri (enlem, boylam); her biri ~4x4 km alan
CITIES = {
    "istanbul_sisli": (41.065, 28.995), "istanbul_kadikoy": (40.990, 29.030), "istanbul_umraniye": (41.020, 29.120),
    "istanbul_bagcilar": (41.040, 28.850), "ankara_kizilay": (39.920, 32.850), "ankara_kecioren": (39.980, 32.860),
    "izmir_konak": (38.420, 27.140), "izmir_bornova": (38.460, 27.220), "antalya": (36.890, 30.700),
    "konya": (37.870, 32.490), "eskisehir": (39.770, 30.520), "kocaeli": (40.770, 29.940),
    "kayseri": (38.720, 35.480), "gaziantep": (37.070, 37.380), "samsun": (41.290, 36.330),
    "trabzon": (41.000, 39.720), "adana": (37.000, 35.320), "mersin": (36.800, 34.630),
    "denizli": (37.780, 29.090), "sakarya": (40.780, 30.400), "tekirdag": (40.980, 27.510),
    "diyarbakir": (37.920, 40.210), "erzurum": (39.900, 41.270), "malatya": (38.350, 38.310),
    "manisa": (38.615, 27.430),
}


def in_bursa(lat: float, lon: float) -> bool:
    s, w, n, e = BURSA
    return s <= lat <= n and w <= lon <= e


def classify(dets: list[tuple[int, float, list[float]]]) -> str:
    """'hasarsiz' | 'hasarli' | 'belirsiz'"""
    if not dets:
        return "hasarsiz"
    if all(conf >= POS_CONF for _, conf, _ in dets):
        return "hasarli"
    return "belirsiz"


def download(token: str, out: Path, per_cell: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    raw = out / "_ham"
    raw.mkdir(parents=True, exist_ok=True)
    rows, seen = [], set()
    for city, (lat, lon) in CITIES.items():
        bbox = (lat - 0.02, lon - 0.02, lat + 0.02, lon + 0.02)
        got, failures = 0, 0
        # Yoğun bölgelerde (ör. İstanbul) Mapillary sorguları sürekli zaman aşımına düşüyor (HTTP 500):
        # küçük hücreler kullanılır, arka arkaya 2 hücre başarısız olursa şehir atlanır
        for cell in grid(bbox, step=CELL_DEG):
            if failures >= 2:
                print(f"  {city}: sunucu yanıt vermiyor, şehir atlandı", flush=True)
                break
            try:
                images = [im for im in query(token, cell, limit=100) if im.get("thumb_2048_url")]
            except Exception:
                failures += 1
                continue
            failures = 0
            rng.shuffle(images)
            picked = 0
            for im in images:
                geom = (im.get("computed_geometry") or im.get("geometry") or {}).get("coordinates")
                if not geom or im["id"] in seen or in_bursa(geom[1], geom[0]):
                    continue
                key = (round(geom[0], 3), round(geom[1], 3))  # ~100 m'de bir görüntü: ardışık kareler elenir
                if key in seen:
                    continue
                seen.update({im["id"], key})
                path = raw / f"tr_{city}_{im['id']}.jpg"
                try:
                    if not path.exists():
                        urllib.request.urlretrieve(im["thumb_2048_url"], path)
                except Exception:
                    continue
                rows.append({"dosya": path.name, "sehir": city, "enlem": geom[1], "boylam": geom[0],
                             "kullanici": (im.get("creator") or {}).get("username", ""),
                             "mapillary_url": f"https://www.mapillary.com/app/?pKey={im['id']}",
                             "lisans": "CC BY-SA 4.0"})
                picked += 1
                got += 1
                if picked >= per_cell:
                    break
            time.sleep(0.2)
        print(f"{city:<20} {got} görüntü", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", type=Path, default=ROOT / "models" / "best.pt")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "turkiye_egitim")
    ap.add_argument("--per-cell", type=int, default=6)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    token = _token_from_env_file(ROOT / ".env")
    if not token:
        raise SystemExit("MAPILLARY_TOKEN gerekli (.env)")
    rows = download(token, args.out, args.per_cell, args.seed)
    print(f"\nToplam {len(rows)} görüntü indirildi. Etiketleniyor...")

    from ultralytics import YOLO

    model = YOLO(str(args.model))
    img_dir, lbl_dir = args.out / "images", args.out / "labels"
    for d in (img_dir, lbl_dir):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    decisions, kept_rows, boxes = Counter(), [], Counter()
    for r in rows:
        src = args.out / "_ham" / r["dosya"]
        img = cv2.imread(str(src))
        if img is None:
            decisions["okunamadi"] += 1
            continue
        if cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 2].mean() < LOW_LIGHT:
            decisions["karanlik"] += 1
            continue
        h, w = img.shape[:2]
        scale = min(1.0, MAX_SIDE / max(h, w))
        if scale < 1:
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        res = model.predict(img, conf=MIN_CONF, verbose=False)  # modelin eğitim boyutu[0]
        dets = [(int(c), float(p), b) for c, p, b in zip(res.boxes.cls.tolist(), res.boxes.conf.tolist(),
                                                          res.boxes.xywhn.tolist())]
        kind = classify(dets)
        decisions[kind] += 1
        if kind == "belirsiz":
            continue
        cv2.imwrite(str(img_dir / r["dosya"]), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        (lbl_dir / f"{Path(r['dosya']).stem}.txt").write_text(
            "\n".join(f"{c} {x:.6f} {y:.6f} {bw:.6f} {bh:.6f}" for c, _, (x, y, bw, bh) in dets))
        boxes.update(CLASSES[c] for c, _, _ in dets)
        kept_rows.append({**r, "etiket": kind})

    with (args.out / "atif.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(kept_rows[0].keys()) if kept_rows else ["dosya"])
        w.writeheader()
        w.writerows(kept_rows)
    report = {"indirilen": len(rows), "kararlar": dict(decisions), "egitime_giren": len(kept_rows),
              "kutular": dict(boxes), "kurallar": {"hasarsiz": f"tespit yok (eşik {MIN_CONF})",
                                                   "hasarli": f"tüm tespitler >= {POS_CONF}",
                                                   "atilan": "arada kalan, karanlık"},
              "sehirler": dict(Counter(r["sehir"] for r in kept_rows))}
    (args.out / "rapor.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    zip_path = args.out.parent / "turkiye_egitim.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for p in sorted(img_dir.iterdir()) + sorted(lbl_dir.iterdir()) + [args.out / "atif.csv", args.out / "rapor.json"]:
            z.write(p, p.relative_to(args.out))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nZip: {zip_path} ({zip_path.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
