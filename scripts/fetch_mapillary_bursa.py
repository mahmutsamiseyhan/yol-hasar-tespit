"""Bursa sokak görüntülerini Mapillary'den indirir (Bursa test seti için).

Amaç: RDD2022'de Türkiye verisi yok. Modeli Bursa yollarında ölçmek için
Mapillary'deki açık lisanslı (CC BY-SA 4.0) sokak görüntülerinden bir test seti
oluşturulur, ardından CVAT/Roboflow'da etiketlenir. Mapillary yüz ve plakaları
yükleme sırasında otomatik bulanıklaştırır.

Gereken: ücretsiz Mapillary hesabı -> https://www.mapillary.com/dashboard/developers
         "Client Token" değerini MAPILLARY_TOKEN ortam değişkenine veya proje kökündeki .env dosyasına koyun.

Kullanım:
    MAPILLARY_TOKEN=MLY|... python scripts/fetch_mapillary_bursa.py --per-cell 15 --out data/bursa_test/mapillary

Çıktı: görüntüler + atif.csv (her görüntünün Mapillary linki, çeken kullanıcı, konum, tarih)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://graph.mapillary.com/images"
FIELDS = "id,thumb_2048_url,computed_geometry,geometry,captured_at,creator,is_pano,camera_type"

# İlçe merkezleri (güney, batı, kuzey, doğu)
DISTRICTS = {
    "osmangazi": (40.170, 29.020, 40.215, 29.090),
    "nilufer": (40.195, 28.930, 40.240, 29.000),
    "yildirim": (40.170, 29.080, 40.210, 29.140),
}


def _around(lat: float, lon: float, d: float = 0.015) -> tuple[float, float, float, float]:
    return (lat - d, lon - d, lat + d, lon + d)


# Hasar görme olasılığı yüksek bölgeler: sanayi (ağır vasıta), çevre ilçeler, kırsal yollar.
# Hasarlı örnek bulmak için aday havuzu; eğitilen model bu havuzdaki adayları sıralar.
CANDIDATE_AREAS = {
    "demirtas_osb": _around(40.2692, 29.0684),
    "bursa_osb": _around(40.2400, 28.9587),
    "panayir": _around(40.2486, 29.0689),
    "hamitler": _around(40.2454, 28.9884),
    "kestel": _around(40.2008, 29.2129),
    "gursu": _around(40.2176, 29.1936),
    "gorukle": _around(40.2319, 28.8395),
    "kayapa": _around(40.1720, 28.8408),
    "inegol": _around(40.0800, 29.5097),
}
DISTRICTS.update(CANDIDATE_AREAS)
CORE_DISTRICTS = ["osmangazi", "nilufer", "yildirim"]
CELL_DEG = 0.03  # API sınırı: bbox alanı < 0.01 derece²


def grid(bbox: tuple[float, float, float, float], step: float = CELL_DEG):
    s, w, n, e = bbox
    lat = s
    while lat < n:
        lon = w
        while lon < e:
            yield (lon, lat, min(lon + step, e), min(lat + step, n))  # API sırası: batı,güney,doğu,kuzey
            lon += step
        lat += step


def query(token: str, cell, limit: int = 500, since: str | None = None) -> list[dict]:
    params = {"access_token": token, "fields": FIELDS, "bbox": ",".join(f"{v:.5f}" for v in cell),
              "limit": limit, "is_pano": "false"}
    if since:
        params["start_captured_at"] = since
    with urllib.request.urlopen(f"{API}?{urllib.parse.urlencode(params)}", timeout=60) as resp:
        return json.loads(resp.read()).get("data", [])


def _token_from_env_file(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "MAPILLARY_TOKEN" and value.strip():
            return value.strip().strip("'\"")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("data/bursa_test/mapillary"))
    ap.add_argument("--per-cell", type=int, default=15, help="Her ızgara hücresinden alınacak görüntü sayısı")
    ap.add_argument("--since", default="2021-01-01T00:00:00Z", help="Bu tarihten sonra çekilenler ('' = tümü)")
    ap.add_argument("--districts", nargs="+", default=CORE_DISTRICTS, choices=list(DISTRICTS))
    ap.add_argument("--candidates", action="store_true", help="Yıpranmış yol aday bölgelerini indir")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    token = os.getenv("MAPILLARY_TOKEN") or _token_from_env_file(Path(__file__).resolve().parent.parent / ".env")
    if not token:
        raise SystemExit("MAPILLARY_TOKEN gerekli: ortam değişkeni olarak verin veya .env dosyasına yazın "
                         "(https://www.mapillary.com/dashboard/developers)")

    rng = random.Random(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    meta_path = args.out / "atif.csv"
    rows, seen = [], set()

    districts = list(CANDIDATE_AREAS) if args.candidates else args.districts
    for district in districts:
        for cell in grid(DISTRICTS[district]):
            try:
                images = [im for im in query(token, cell, since=args.since or None) if im.get("thumb_2048_url")]
            except Exception as e:
                print(f"  {district} {cell}: sorgu hatası ({e})")
                continue
            rng.shuffle(images)
            # Aynı sekanstan art arda kareleri azaltmak için ~birbirine yakın olanları seyrelt
            picked = []
            for im in images:
                geom = (im.get("computed_geometry") or im.get("geometry") or {}).get("coordinates")
                if not geom or im["id"] in seen:
                    continue
                key = (round(geom[0], 3), round(geom[1], 3))  # ~100 m'lik hücre
                if key in seen:
                    continue
                seen.update({im["id"], key})
                picked.append((im, geom))
                if len(picked) >= args.per_cell:
                    break

            for im, (lon, lat) in picked:
                path = args.out / f"bursa_{district}_{im['id']}.jpg"
                if not path.exists():
                    urllib.request.urlretrieve(im["thumb_2048_url"], path)  # URL kısa ömürlü: hemen indir
                    time.sleep(0.1)
                creator = (im.get("creator") or {}).get("username", "")
                rows.append({
                    "dosya": path.name, "ilce": district, "enlem": lat, "boylam": lon,
                    "cekim_tarihi": datetime.fromtimestamp(im["captured_at"] / 1000, timezone.utc).date().isoformat(),
                    "kullanici": creator, "mapillary_url": f"https://www.mapillary.com/app/?pKey={im['id']}",
                    "lisans": "CC BY-SA 4.0",
                })
            print(f"{district} {cell}: {len(picked)} görüntü")

    with meta_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["dosya"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nToplam {len(rows)} görüntü -> {args.out}\nAtıf listesi: {meta_path}")
    print("Sonraki adım: görüntüleri CVAT veya Roboflow'da D00/D10/D20/D40 sınıflarıyla etiketleyin (YOLO formatı).")


if __name__ == "__main__":
    main()
