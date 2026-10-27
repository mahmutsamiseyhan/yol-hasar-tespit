"""Konum bağlamı: hasar hangi yol sınıfında ve okul/hastane yakınında mı?

Veri scripts/fetch_osm_context.py ile OpenStreetMap'ten bir kez çekilip
data/bursa_context.geojson olarak saklanır; çalışma anında internet gerekmez.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree

from .geo import LocalProjection

log = logging.getLogger(__name__)

ROAD_MATCH_RADIUS_M = 25.0
POI_LABELS_TR = {
    "school": "okul",
    "kindergarten": "anaokulu",
    "university": "üniversite",
    "hospital": "hastane",
    "clinic": "sağlık merkezi",
}


class Districts:
    """Nokta -> ilçe adı. Veri scripts/fetch_osm_districts.py ile bir kez çekilir (data/bursa_ilceler.geojson)."""

    def __init__(self, geojson: dict):
        feats = [f for f in geojson.get("features", []) if f.get("properties", {}).get("ilce")]
        self.names = [f["properties"]["ilce"] for f in feats]
        self._polys = [shape(f["geometry"]) for f in feats]
        self._tree = STRtree(self._polys) if self._polys else None

    @classmethod
    def from_file(cls, path: Path | None) -> "Districts":
        if not path or not Path(path).exists():
            log.warning("İlçe sınırları yok (%s); ilçe filtresi boş kalacak.", path)
            return cls({})
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def lookup(self, lat: float, lon: float) -> str | None:
        if self._tree is None:
            return None
        hits = self._tree.query(Point(lon, lat), predicate="intersects")
        return self.names[int(hits[0])] if len(hits) else None


class RoadContext:
    def __init__(self, geojson: dict, road_weights: dict, poi_radius_m: float, poi_bonus: float,
                 districts: Districts | None = None):
        self.districts = districts
        self.road_weights = road_weights
        self.poi_radius_m = poi_radius_m
        self.poi_bonus = poi_bonus

        feats = geojson.get("features", [])
        lat0 = _mean_lat(feats) or 40.19
        self.proj = LocalProjection(lat0)

        self._roads, self._road_props = [], []
        self._pois, self._poi_props = [], []
        for f in feats:
            geom, props = f["geometry"], f.get("properties", {})
            if geom["type"] == "LineString" and len(geom["coordinates"]) >= 2:
                self._roads.append(LineString([self.proj(*c) for c in geom["coordinates"]]))
                self._road_props.append(props)
            elif geom["type"] == "Point":
                self._pois.append(Point(self.proj(*geom["coordinates"])))
                self._poi_props.append(props)
        self._road_tree = STRtree(self._roads) if self._roads else None
        # Bağlam verisinin kapsadığı alan (enlem/boylam kutusu); dışındaki noktalarda yol sınıfı bilinmez
        lons = [c[0] for f in feats for c in _coords(f["geometry"])]
        lats = [c[1] for f in feats for c in _coords(f["geometry"])]
        self.bounds = (min(lats), min(lons), max(lats), max(lons)) if lats else None
        self._poi_tree = STRtree(self._pois) if self._pois else None

    @classmethod
    def from_file(cls, path: Path, districts_path: Path | None = None, **kwargs) -> "RoadContext":
        kwargs["districts"] = Districts.from_file(districts_path)
        if not Path(path).exists():
            log.warning("Bağlam verisi yok (%s); tüm noktalar 'local' yol kabul edilecek.", path)
            return cls({"features": []}, **kwargs)
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), **kwargs)

    @property
    def loaded(self) -> bool:
        return bool(self._roads)

    def covers(self, lat: float, lon: float) -> bool:
        if self.bounds is None:
            return False
        s, w, n, e = self.bounds
        return s <= lat <= n and w <= lon <= e

    def lookup(self, lat: float, lon: float) -> dict:
        pt = Point(self.proj(lon, lat))

        road_class, road_name = "local", None
        if self._road_tree is not None:
            idx = self._road_tree.query_nearest(pt, max_distance=ROAD_MATCH_RADIUS_M)
            if len(idx):
                props = self._road_props[int(idx[0])]
                road_class = props.get("highway", "local").removesuffix("_link")
                road_name = props.get("name")
        road_weight = self.road_weights.get(road_class, self.road_weights.get("local", 0.55))

        nearby = []
        if self._poi_tree is not None:
            for i in self._poi_tree.query(pt.buffer(self.poi_radius_m)):
                dist = pt.distance(self._pois[int(i)])
                if dist <= self.poi_radius_m:
                    props = self._poi_props[int(i)]
                    nearby.append(
                        {
                            "tur": POI_LABELS_TR.get(props.get("amenity"), props.get("amenity")),
                            "ad": props.get("name"),
                            "mesafe_m": round(dist),
                        }
                    )
        nearby.sort(key=lambda p: p["mesafe_m"])

        weight = min(1.0, road_weight + (self.poi_bonus if nearby else 0.0))
        return {
            "yol_sinifi": road_class,
            "yol_adi": road_name,
            "yol_katsayisi": road_weight,
            "yakin_kritik_tesisler": nearby[:3],
            "konum_katsayisi": round(weight, 3),
            "veri_kapsaminda": self.covers(lat, lon),
            "ilce": self.districts.lookup(lat, lon) if self.districts else None,
        }


def _coords(geom: dict) -> list:
    return [geom["coordinates"]] if geom["type"] == "Point" else geom["coordinates"]


def _mean_lat(features: list[dict]) -> float | None:
    lats = []
    for f in features[:500]:
        coords = f["geometry"]["coordinates"]
        lats.append(coords[1] if f["geometry"]["type"] == "Point" else coords[0][1])
    return sum(lats) / len(lats) if lats else None
