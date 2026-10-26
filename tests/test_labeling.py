import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from scripts.evaluate_bursa import build_yolo_dataset, collect_reviewed, image_level

BOX = {"sinif": "D40", "x": 0.5, "y": 0.6, "w": 0.2, "h": 0.1}


@pytest.fixture
def label_dir(tmp_path):
    d = tmp_path / "etiket"
    (d / "_model").mkdir(parents=True)
    for name in ("a", "b", "c"):
        Image.new("RGB", (64, 48), "gray").save(d / f"{name}.jpg")
    (d / "a.txt").write_text("0 0.300000 0.700000 0.100000 0.200000")
    (d / "_model" / "a.txt").write_text("0 0.300000 0.700000 0.100000 0.200000")
    return d


@pytest.fixture
def client(make_pipeline, settings, label_dir):
    settings.label_dir = label_dir
    with TestClient(create_app(make_pipeline())) as c:
        yield c


def test_list_and_read(client):
    s = client.get("/api/etiket").json()
    assert [i["ad"] for i in s["gorseller"]] == ["a.jpg", "b.jpg", "c.jpg"]
    assert s["ozet"]["bekleyen"] == 3
    d = client.get("/api/etiket/a.jpg").json()
    assert d["kutular"] == [{"sinif": "D00", "x": 0.3, "y": 0.7, "w": 0.1, "h": 0.2}]
    assert d["durum"] is None
    assert client.get("/api/etiket/a.jpg/gorsel").status_code == 200
    assert "Etiket Düzeltme" in client.get("/etiket").text


def test_save_correction_and_reset(client, label_dir):
    r = client.put("/api/etiket/a.jpg", json={"durum": "tamam", "kutular": [BOX]})
    assert r.status_code == 200 and r.json()["kutular"][0]["sinif"] == "D40"
    assert (label_dir / "a.txt").read_text().startswith("3 0.500000 0.600000")
    assert client.get("/api/etiket").json()["ozet"]["tamam"] == 1

    r = client.post("/api/etiket/a.jpg/sifirla").json()
    assert r["kutular"][0]["sinif"] == "D00" and r["durum"] is None


def test_no_damage_and_out_of_scope_clear_boxes(client, label_dir):
    client.put("/api/etiket/a.jpg", json={"durum": "hasarsiz", "kutular": [BOX]})
    assert (label_dir / "a.txt").read_text() == ""
    client.put("/api/etiket/b.jpg", json={"durum": "kapsam_disi"})
    ozet = client.get("/api/etiket").json()["ozet"]
    assert ozet == {"tamam": 0, "hasarsiz": 1, "kapsam_disi": 1, "bekleyen": 1}


def test_validation_and_path_safety(client):
    assert client.put("/api/etiket/a.jpg", json={"durum": "tamam", "kutular": []}).status_code == 422
    assert client.put("/api/etiket/a.jpg", json={"durum": "bilinmiyor"}).status_code == 422
    bad = {**BOX, "sinif": "D99"}
    assert client.put("/api/etiket/a.jpg", json={"durum": "tamam", "kutular": [bad]}).status_code == 422
    out_of_range = {**BOX, "x": 1.5}
    assert client.put("/api/etiket/a.jpg", json={"durum": "tamam", "kutular": [out_of_range]}).status_code == 422
    assert client.get("/api/etiket/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/api/etiket/_durum.json").status_code == 404


def test_evaluation_inputs(client, label_dir, tmp_path):
    client.put("/api/etiket/a.jpg", json={"durum": "tamam", "kutular": [BOX]})
    client.put("/api/etiket/b.jpg", json={"durum": "hasarsiz"})
    client.put("/api/etiket/c.jpg", json={"durum": "kapsam_disi"})

    reviewed, skipped = collect_reviewed(label_dir)
    assert [r["image"].name for r in reviewed] == ["a.jpg", "b.jpg"]
    assert skipped == {"kapsam_disi": 1, "incelenmemis": 0}

    yaml = build_yolo_dataset(reviewed, tmp_path / "veri")
    assert "3: D40" in yaml.read_text()
    assert (tmp_path / "veri" / "labels" / "test" / "a.txt").read_text().startswith("3 ")
    assert (tmp_path / "veri" / "labels" / "test" / "b.txt").read_text() == ""

    near = {"x": 0.52, "y": 0.6, "w": 0.2, "h": 0.1}
    m = image_level(reviewed, {"a.jpg": [near], "b.jpg": [near]})
    assert m["yakalama_orani"] == 1.0 and m["yanlis_alarm_orani"] == 1.0
    m = image_level(reviewed, {"a.jpg": [{"x": 0.1, "y": 0.1, "w": 0.05, "h": 0.05}]})
    assert m["yakalama_orani"] == 0.0 and m["yanlis_alarm_orani"] == 0.0
    assert json.dumps(m)  # JSON'a yazılabilir olmalı
