import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.labeling import bounding_box, polygon_area, read_shapes
from app.main import create_app
from scripts.evaluate_bursa import build_yolo_dataset, collect_reviewed, image_level

# Yamuk biçimli bir çukur
TRAPEZOID = {"sinif": "D40", "noktalar": [[0.45, 0.55], [0.55, 0.55], [0.60, 0.65], [0.40, 0.65]]}


@pytest.fixture
def label_dir(tmp_path):
    d = tmp_path / "etiket"
    (d / "_model").mkdir(parents=True)
    for name in ("a", "b", "c"):
        Image.new("RGB", (64, 48), "gray").save(d / f"{name}.jpg")
    # Modelin ön-etiketi eski kutu biçiminde (sınıf x y w h)
    (d / "a.txt").write_text("0 0.300000 0.700000 0.100000 0.200000")
    (d / "_model" / "a.txt").write_text("0 0.300000 0.700000 0.100000 0.200000")
    return d


@pytest.fixture
def client(make_pipeline, settings, label_dir):
    settings.label_dir = label_dir
    with TestClient(create_app(make_pipeline())) as c:
        yield c


def test_box_lines_are_read_as_four_corner_polygons(client):
    s = client.get("/api/etiket").json()
    assert [i["ad"] for i in s["gorseller"]] == ["a.jpg", "b.jpg", "c.jpg"]
    assert s["ozet"]["bekleyen"] == 3
    shape = client.get("/api/etiket/a.jpg").json()["sekiller"][0]
    assert shape["sinif"] == "D00"
    assert shape["noktalar"] == [[0.25, 0.6], [0.35, 0.6], [0.35, 0.8], [0.25, 0.8]]
    assert client.get("/api/etiket/a.jpg/gorsel").status_code == 200
    assert "Etiket Düzeltme" in client.get("/etiket").text


def test_save_polygon_in_yolo_segmentation_format_and_reset(client, label_dir):
    r = client.put("/api/etiket/a.jpg", json={"durum": "tamam", "sekiller": [TRAPEZOID]})
    assert r.status_code == 200
    assert r.json()["sekiller"] == [TRAPEZOID]
    assert (label_dir / "a.txt").read_text() == "3 0.450000 0.550000 0.550000 0.550000 0.600000 0.650000 0.400000 0.650000"
    assert client.get("/api/etiket").json()["ozet"]["tamam"] == 1

    r = client.post("/api/etiket/a.jpg/sifirla").json()
    assert r["sekiller"][0]["sinif"] == "D00" and r["durum"] is None


def test_no_damage_and_out_of_scope_clear_shapes(client, label_dir):
    client.put("/api/etiket/a.jpg", json={"durum": "hasarsiz", "sekiller": [TRAPEZOID]})
    assert (label_dir / "a.txt").read_text() == ""
    client.put("/api/etiket/b.jpg", json={"durum": "kapsam_disi"})
    ozet = client.get("/api/etiket").json()["ozet"]
    assert ozet == {"tamam": 0, "hasarsiz": 1, "kapsam_disi": 1, "bekleyen": 1}


@pytest.mark.parametrize("shape", [
    {"sinif": "D99", "noktalar": TRAPEZOID["noktalar"]},           # bilinmeyen sınıf
    {"sinif": "D40", "noktalar": [[0.1, 0.1], [0.2, 0.2]]},        # 2 köşe
    {"sinif": "D40", "noktalar": [[0.1, 0.1], [1.5, 0.2], [0.3, 0.4]]},  # aralık dışı
    {"sinif": "D40", "noktalar": [[0.1, 0.1], [0.2, 0.2], [0.3, 0.3]]},  # alanı sıfır (doğru)
])
def test_invalid_shapes_rejected(client, shape):
    assert client.put("/api/etiket/a.jpg", json={"durum": "tamam", "sekiller": [shape]}).status_code == 422


def test_validation_and_path_safety(client):
    assert client.put("/api/etiket/a.jpg", json={"durum": "tamam", "sekiller": []}).status_code == 422
    assert client.put("/api/etiket/a.jpg", json={"durum": "bilinmiyor"}).status_code == 422
    assert client.get("/api/etiket/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/api/etiket/_durum.json").status_code == 404


def test_geometry_helpers(tmp_path):
    assert polygon_area(TRAPEZOID["noktalar"]) == pytest.approx(0.015)
    assert bounding_box(TRAPEZOID["noktalar"]) == pytest.approx({"x": 0.5, "y": 0.6, "w": 0.2, "h": 0.1})
    p = tmp_path / "l.txt"
    p.write_text("2 0.1 0.1 0.3 0.1 0.2 0.4\n\n1 0.5 0.5 0.2 0.2")
    shapes = read_shapes(p)
    assert [s["sinif"] for s in shapes] == ["D20", "D10"] and len(shapes[0]["noktalar"]) == 3


def test_evaluation_inputs(client, label_dir, tmp_path):
    client.put("/api/etiket/a.jpg", json={"durum": "tamam", "sekiller": [TRAPEZOID]})
    client.put("/api/etiket/b.jpg", json={"durum": "hasarsiz"})
    client.put("/api/etiket/c.jpg", json={"durum": "kapsam_disi"})

    reviewed, skipped = collect_reviewed(label_dir)
    assert [r["image"].name for r in reviewed] == ["a.jpg", "b.jpg"]
    assert skipped == {"kapsam_disi": 1, "incelenmemis": 0}
    assert reviewed[0]["boxes"][0] == pytest.approx({"x": 0.5, "y": 0.6, "w": 0.2, "h": 0.1})

    yaml = build_yolo_dataset(reviewed, tmp_path / "veri")
    assert "3: D40" in yaml.read_text()
    assert (tmp_path / "veri" / "labels" / "test" / "a.txt").read_text().startswith("3 0.450000 0.550000")
    assert (tmp_path / "veri" / "labels" / "test" / "b.txt").read_text() == ""

    near = {"x": 0.52, "y": 0.6, "w": 0.2, "h": 0.1}
    m = image_level(reviewed, {"a.jpg": [near], "b.jpg": [near]})
    assert m["yakalama_orani"] == 1.0 and m["yanlis_alarm_orani"] == 1.0
    m = image_level(reviewed, {"a.jpg": [{"x": 0.1, "y": 0.1, "w": 0.05, "h": 0.05}]})
    assert m["yakalama_orani"] == 0.0 and m["yanlis_alarm_orani"] == 0.0
    assert json.dumps(m)
