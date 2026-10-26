"""Bursa için konum bağlamı verisini OpenStreetMap'ten (Overpass API) çeker.

Çıktı: data/bursa_context.geojson
  - Ana yollar (motorway/trunk/primary/secondary/tertiary) -> LineString, yol sınıfı + adı
  - Okul, anaokulu, üniversite, hastane, sağlık merkezi     -> Point

Kullanım:
    python scripts/fetch_osm_context.py                     # Bursa merkez (Osmangazi, Nilüfer, Yıldırım)
    python scripts/fetch_osm_context.py --bbox 40.14,28.82,40.28,29.20

Veri © OpenStreetMap katkıcıları, ODbL lisansı.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BBOX = "40.14,28.82,40.28,29.20"  # güney,batı,kuzey,doğu
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
USER_AGENT = "yol-hasar-tespit/0.1 (Bursa road damage PoC)"

QUERY = """
[out:json][timeout:180];
way["highway"~"^(motorway|trunk|primary|secondary|tertiary)(_link)?$"]({bbox});
out geom tags;
nwr["amenity"~"^(school|kindergarten|university|hospital|clinic)$"]({bbox});
out center tags;
"""


def fetch(bbox: str) -> dict:
    body = urllib.parse.urlencode({"data": QUERY.format(bbox=bbox)}).encode()
    last_err = None
    for url in ENDPOINTS:
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=240) as resp:
                return json.loads(resp.read())
        except Exception as e:  # bir sonraki sunucuyu dene
            print(f"  {url} başarısız: {e}", file=sys.stderr)
            last_err = e
    raise SystemExit(f"Overpass verisi alınamadı: {last_err}")


def to_geojson(osm: dict) -> dict:
    features = []
    for el in osm.get("elements", []):
        tags = el.get("tags", {})
        if "highway" in tags and el.get("geometry"):
            coords = [[round(p["lon"], 6), round(p["lat"], 6)] for p in el["geometry"]]
            props = {"highway": tags["highway"], "name": tags.get("name"), "ref": tags.get("ref")}
            features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                             "properties": props})
        elif "amenity" in tags:
            if el["type"] == "node":
                lat, lon = el["lat"], el["lon"]
            elif "center" in el:
                lat, lon = el["center"]["lat"], el["center"]["lon"]
            else:
                continue
            features.append({"type": "Feature",
                             "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                             "properties": {"amenity": tags["amenity"], "name": tags.get("name")}})
    return {
        "type": "FeatureCollection",
        "attribution": "© OpenStreetMap katkıcıları, ODbL",
        "features": features,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", default=DEFAULT_BBOX, help="güney,batı,kuzey,doğu")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "bursa_context.geojson")
    args = ap.parse_args()

    print(f"Overpass sorgusu: {args.bbox}")
    gj = to_geojson(fetch(args.bbox))
    roads = sum(f["geometry"]["type"] == "LineString" for f in gj["features"])
    pois = len(gj["features"]) - roads
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(gj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{roads} yol parçası, {pois} kritik tesis -> {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
