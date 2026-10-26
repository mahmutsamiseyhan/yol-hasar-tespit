import pytest

from app.geo import bbox_around, exif_gps, feature, haversine_m

from .conftest import ARTER_LON, jpeg_bytes


def test_haversine_known_distance():
    # Enlemde 0.001 derece ~ 111 m
    assert haversine_m(40.19, 29.06, 40.191, 29.06) == pytest.approx(111.2, abs=0.5)
    assert haversine_m(40.19, 29.06, 40.19, 29.06) == 0


def test_bbox_contains_radius():
    lat_min, lat_max, lon_min, lon_max = bbox_around(40.19, 29.06, 15)
    assert haversine_m(40.19, 29.06, lat_max, 29.06) == pytest.approx(15, abs=0.1)
    assert haversine_m(40.19, 29.06, 40.19, lon_max) == pytest.approx(15, abs=0.1)


def test_exif_gps_roundtrip():
    gps = {1: "N", 2: (40.0, 11.0, 30.0), 3: "E", 4: (29.0, 3.0, 36.0)}
    lat, lon = exif_gps(jpeg_bytes(gps=gps))
    assert lat == pytest.approx(40 + 11 / 60 + 30 / 3600)
    assert lon == pytest.approx(29 + 3 / 60 + 36 / 3600)


def test_exif_gps_missing():
    assert exif_gps(jpeg_bytes()) is None
    assert exif_gps(b"not an image") is None


def test_geojson_coordinate_order_is_lon_lat():
    f = feature(40.19, 29.06, {"a": 1})
    assert f["geometry"]["coordinates"] == [29.06, 40.19]


def test_context_matches_main_road_and_school(context):
    near = context.lookup(40.190, ARTER_LON + 0.0001)  # yola ~8 m, okula ~77 m
    assert near["yol_sinifi"] == "primary"
    assert near["yol_adi"] == "Test Bulvarı"
    assert near["yakin_kritik_tesisler"][0]["tur"] == "okul"
    assert near["konum_katsayisi"] == pytest.approx(1.0)  # 0.9 + 0.1 okul bonusu


def test_context_far_from_roads_is_local(context):
    far = context.lookup(40.190, ARTER_LON + 0.01)  # ~850 m doğu
    assert far["yol_sinifi"] == "local"
    assert far["yakin_kritik_tesisler"] == []
    assert far["konum_katsayisi"] == pytest.approx(0.55)
