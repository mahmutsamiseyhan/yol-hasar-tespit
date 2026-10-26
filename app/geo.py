"""Coğrafi yardımcılar: mesafe, EXIF GPS okuma, GeoJSON üretimi."""

from __future__ import annotations

import io
import math

from PIL import Image

EARTH_RADIUS_M = 6_371_000.0
GPS_IFD = 0x8825


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Veritabanı ön filtresi için yarıçapı kapsayan enlem/boylam kutusu."""
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    dlon = math.degrees(radius_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


class LocalProjection:
    """Şehir ölçeğinde yeterli eşdikdörtgen izdüşüm: (lat, lon) -> metre cinsinden (x, y)."""

    def __init__(self, lat0: float):
        self.kx = math.cos(math.radians(lat0)) * math.pi * EARTH_RADIUS_M / 180
        self.ky = math.pi * EARTH_RADIUS_M / 180

    def __call__(self, lon: float, lat: float) -> tuple[float, float]:
        return lon * self.kx, lat * self.ky


def _dms_to_deg(dms, ref: str) -> float:
    d, m, s = (float(v) for v in dms)
    deg = d + m / 60 + s / 3600
    return -deg if ref in ("S", "W") else deg


def exif_gps(image_bytes: bytes) -> tuple[float, float] | None:
    """Telefon fotoğrafının EXIF verisinden (enlem, boylam) okur; yoksa None."""
    try:
        exif = Image.open(io.BytesIO(image_bytes)).getexif()
        gps = exif.get_ifd(GPS_IFD)
        if not gps or 2 not in gps or 4 not in gps:
            return None
        lat = _dms_to_deg(gps[2], gps.get(1, "N"))
        lon = _dms_to_deg(gps[4], gps.get(3, "E"))
        if lat == 0 and lon == 0:
            return None
        return lat, lon
    except Exception:
        return None


def feature(lat: float, lon: float, properties: dict) -> dict:
    # GeoJSON (RFC 7946): koordinat sırası [boylam, enlem], WGS84 / EPSG:4326
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]},
        "properties": properties,
    }


def feature_collection(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}
