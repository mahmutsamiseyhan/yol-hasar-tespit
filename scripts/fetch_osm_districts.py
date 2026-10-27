"""Bursa ilçe sınırlarını OpenStreetMap'ten (Overpass API) çeker; paneldeki ilçe filtresi ve istatistikleri için.

Çıktı: data/bursa_ilceler.geojson (17 ilçe, sadeleştirilmiş çokgenler; çalışma anında internet gerekmez)

Kullanım:
    python scripts/fetch_osm_districts.py

Veri © OpenStreetMap katkıcıları, ODbL lisansı.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from shapely.geometry import LineString, mapping
from shapely.ops import polygonize, unary_union

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_osm_context import ENDPOINTS, USER_AGENT  # noqa: E402

ENDPOINTS = [*ENDPOINTS, "https://overpass.private.coffee/api/interpreter"]

QUERY = """
[out:json][timeout:180];
area["name"="Bursa"]["admin_level"="4"]["boundary"="administrative"]->.il;
relation(area.il)["boundary"="administrative"]["admin_level"="6"];
out geom;
"""
SIMPLIFY_DEG = 0.0002  # ~20 m: ilçe ataması için yeterli, dosya küçük kalır


def fetch() -> dict:
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode({"data": QUERY}).encode()
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
    for rel in osm.get("elements", []):
        name = rel.get("tags", {}).get("name")
        lines = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
                 for m in rel.get("members", []) if m.get("type") == "way" and m.get("role") == "outer"
                 and len(m.get("geometry") or []) >= 2]
        polys = list(polygonize(unary_union(lines)))
        if not name or not polys:
            print(f"  atlandı: {name or rel.get('id')}", file=sys.stderr)
            continue
        geom = unary_union(polys).simplify(SIMPLIFY_DEG, preserve_topology=True)
        features.append({"type": "Feature", "properties": {"ilce": name},
                         "geometry": json.loads(json.dumps(mapping(geom)), parse_float=lambda v: round(float(v), 5))})
    features.sort(key=lambda f: f["properties"]["ilce"])
    return {"type": "FeatureCollection", "features": features,
            "kaynak": "© OpenStreetMap katkıcıları (ODbL), admin_level=6"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "bursa_ilceler.geojson")
    args = ap.parse_args()
    gj = to_geojson(fetch())
    args.out.write_text(json.dumps(gj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    names = [f["properties"]["ilce"] for f in gj["features"]]
    print(f"{len(names)} ilçe: {', '.join(names)}")
    print(f"Kaydedildi: {args.out} ({args.out.stat().st_size / 1e3:.0f} KB)")


if __name__ == "__main__":
    main()
