"""Hasarlı yol adayları: Türkiye genelinden Mapillary görüntüleri, model taslak kutularıyla etiket aracına.

Model v2 Bursa'da hasarlı yolların yarısına yakınını kaçırıyor; çünkü eğitime Türkiye'den hiç hasarlı örnek
girmedi. Bu betik etiketlenecek aday havuzunu hazırlar:
  1. Şehirlerin merkez ve çevresinden (sanayi, çevre yollar) rastgele hücrelerde Mapillary görüntüsü indirir.
  2. Ana model düşük eşikle bakar; en az bir hasar şüphesi olan görüntüler aday olur (güçlüden zayıfa sıralı).
  3. Modelin kutuları taslak etiket olarak yazılır; /etiket aracında düzeltilir:
       tamam      -> düzeltilmiş kutularla hasarlı örnek
       hasarsiz   -> modelin yanıldığı zor örnek (rögar, şerit, gölge...): yanlış alarmı azaltır
       kapsam_disi-> eğitime girmez

Bursa test seti modelin hiç görmediği yerden kalmalı: test görüntülerinin TEST_BUFFER_M yakınındaki
görüntüler alınmaz. Bursa'nın geri kalanı serbesttir.

Kullanım:
    python scripts/hasar_adaylari.py --out data/hasar_adaylari
    LABEL_DIR=data/hasar_adaylari uvicorn app.main:app   # sonra http://localhost:8000/etiket
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_mapillary_bursa import CANDIDATE_AREAS, DISTRICTS, _token_from_env_file, query  # noqa: E402
from turkiye_veri_hazirla import CITIES  # noqa: E402

CLASSES = ["D00", "D10", "D20", "D40"]
WEIGHTS = {"D40": 1.0, "D20": 0.8, "D10": 0.5, "D00": 0.4}
CELL_DEG = 0.0135      # Mapillary sorgu sınırı içinde küçük hücre (yoğun şehirlerde zaman aşımını önler)
SPREAD_DEG = 0.06      # şehir merkezinden ~6 km: çevre yollar ve sanayi bölgeleri de taransın
LOW_LIGHT = 70
MAX_SIDE = 1600
TEST_BUFFER_M = 150
TEST_SET = ROOT / "data" / "bursa_test" / "secilen_test_seti.csv"


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = math.pi / 180
    a = math.sin((lat2 - lat1) * r / 2) ** 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lon2 - lon1) * r / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def test_points() -> list[tuple[float, float]]:
    if not TEST_SET.exists():
        return []
    with TEST_SET.open(encoding="utf-8") as f:
        return [(float(r["enlem"]), float(r["boylam"])) for r in csv.DictReader(f) if r.get("enlem")]


def near_test(lat: float, lon: float, points) -> bool:
    return any(haversine_m(lat, lon, a, b) < TEST_BUFFER_M for a, b in points)


def areas() -> dict[str, tuple[float, float, float, float]]:
    """(güney, batı, kuzey, doğu). Diğer şehirler geniş alan; Bursa ilçe ve aday bölgeleri olduğu gibi."""
    out = {name: (lat - SPREAD_DEG, lon - SPREAD_DEG, lat + SPREAD_DEG, lon + SPREAD_DEG)
           for name, (lat, lon) in CITIES.items()}
    out.update({f"bursa_{k}": v for k, v in {**DISTRICTS, **CANDIDATE_AREAS}.items()})
    return out


def random_cells(bbox, n: int, rng: random.Random):
    s, w, nn, e = bbox
    for _ in range(n):
        lat = rng.uniform(s, nn - CELL_DEG)
        lon = rng.uniform(w, e - CELL_DEG)
        yield (lon, lat, lon + CELL_DEG, lat + CELL_DEG)  # API sırası: batı, güney, doğu, kuzey


def download(token: str, raw: Path, cells: int, per_cell: int, seed: int, tests) -> list[dict]:
    rng = random.Random(seed)
    raw.mkdir(parents=True, exist_ok=True)
    rows, seen = [], set()
    for area, bbox in areas().items():
        got, failures = 0, 0
        for cell in random_cells(bbox, cells, rng):
            if failures >= 3:
                print(f"  {area}: sunucu yanıt vermiyor, bölge atlandı", flush=True)
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
                if not geom or im["id"] in seen or near_test(geom[1], geom[0], tests):
                    continue
                key = (round(geom[0], 3), round(geom[1], 3))  # ~100 m'de bir görüntü: ardışık kareler elenir
                if key in seen:
                    continue
                seen.update({im["id"], key})
                path = raw / f"{area}_{im['id']}.jpg"
                try:
                    if not path.exists():
                        urllib.request.urlretrieve(im["thumb_2048_url"], path)
                except Exception:
                    continue
                rows.append({"dosya": path.name, "bolge": area, "enlem": geom[1], "boylam": geom[0],
                             "cekim_tarihi": time.strftime("%Y-%m-%d", time.gmtime(im.get("captured_at", 0) / 1000)),
                             "kullanici": (im.get("creator") or {}).get("username", ""),
                             "mapillary_url": f"https://www.mapillary.com/app/?pKey={im['id']}",
                             "lisans": "CC BY-SA 4.0"})
                picked += 1
                got += 1
                if picked >= per_cell:
                    break
            time.sleep(0.2)
        print(f"{area:<24} {got} görüntü", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", type=Path, default=ROOT / "models" / "best.pt")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "hasar_adaylari")
    ap.add_argument("--cells", type=int, default=6, help="Bölge başına rastgele hücre")
    ap.add_argument("--per-cell", type=int, default=5)
    ap.add_argument("--min-conf", type=float, default=0.15, help="Bu güvenin üstünde en az bir kutu varsa aday")
    ap.add_argument("--extra", type=Path, nargs="*", default=[ROOT / "data" / "turkiye_egitim" / "_ham"],
                    help="Daha önce indirilmiş görüntü klasörleri (ör. v2'de 'belirsiz' kalanlar)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    token = _token_from_env_file(ROOT / ".env")
    if not token:
        raise SystemExit("MAPILLARY_TOKEN gerekli (.env)")
    tests = test_points()
    raw = args.out / "_ham"
    rows = download(token, raw, args.cells, args.per_cell, args.seed, tests)
    used = {p.name for p in (ROOT / "data" / "turkiye_egitim" / "images").glob("*.jpg")}  # v2 eğitimine girenler
    for folder in args.extra:
        for p in sorted(folder.glob("*.jpg")) if folder.exists() else []:
            if p.name not in used:
                rows.append({"dosya": p.name, "bolge": "onceki_indirme", "kaynak_klasor": str(folder),
                             "mapillary_url": f"https://www.mapillary.com/app/?pKey={p.stem.rsplit('_', 1)[-1]}",
                             "lisans": "CC BY-SA 4.0"})
    print(f"\n{len(rows)} görüntü modele gösteriliyor...", flush=True)

    from ultralytics import YOLO

    model = YOLO(str(args.model))
    (args.out / "_model").mkdir(parents=True, exist_ok=True)
    decisions, kept = Counter(), []
    for i, r in enumerate(rows, 1):
        src = Path(r.pop("kaynak_klasor", raw)) / r["dosya"]
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
        res = model.predict(img, conf=args.min_conf, verbose=False)[0]
        dets = [(int(c), float(p), b) for c, p, b in zip(res.boxes.cls.tolist(), res.boxes.conf.tolist(),
                                                          res.boxes.xywhn.tolist())]
        if not dets:
            decisions["hasar_suphesi_yok"] += 1
            continue
        decisions["aday"] += 1
        lines = "\n".join(f"{c} {x:.6f} {y:.6f} {bw:.6f} {bh:.6f}" for c, _, (x, y, bw, bh) in dets)
        cv2.imwrite(str(args.out / r["dosya"]), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        (args.out / f"{Path(r['dosya']).stem}.txt").write_text(lines, encoding="utf-8")
        (args.out / "_model" / f"{Path(r['dosya']).stem}.txt").write_text(lines, encoding="utf-8")
        score = max(WEIGHTS[CLASSES[c]] * p for c, p, _ in dets)
        kept.append({**r, "model": " ".join(f"{CLASSES[c]}:{p:.2f}" for c, p, _ in dets), "skor": round(score, 3)})
        if i % 50 == 0:
            print(f"  {i}/{len(rows)} · aday {decisions['aday']}", flush=True)

    kept.sort(key=lambda r: r["skor"], reverse=True)
    fields = sorted({k for r in kept for k in r}, key=lambda k: list(kept[0]).index(k) if k in kept[0] else 99)
    with (args.out / "atif.csv").open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        wr.writerows(kept)
    report = {"gosterilen": len(rows), "kararlar": dict(decisions), "min_guven": args.min_conf,
              "bolgeler": dict(Counter(r["bolge"] for r in kept)),
              "model_siniflari": dict(Counter(m.split(":")[0] for r in kept for m in r["model"].split())),
              "test_tamponu_m": TEST_BUFFER_M}
    (args.out / "rapor.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
