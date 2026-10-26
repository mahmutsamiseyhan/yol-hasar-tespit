import json

from app.config import load_class_thresholds
from app.detector import Detection

from .conftest import ARTER_LON, jpeg_bytes

LAT, LON = 40.190, ARTER_LON + 0.0001


def test_loader_reads_notebook_file_and_plain_dict(tmp_path, monkeypatch):
    monkeypatch.delenv("CLASS_CONF_THRESHOLDS", raising=False)
    f = tmp_path / "esikler.json"
    f.write_text(json.dumps({"esikler": {"D00": 0.45, "D40": 0.22}, "kaynak": "RDD2022 doğrulama"}))
    assert load_class_thresholds(f) == {"D00": 0.45, "D40": 0.22}
    f.write_text(json.dumps({"D10": 0.3}))
    assert load_class_thresholds(f) == {"D10": 0.3}
    assert load_class_thresholds(tmp_path / "yok.json") == {}
    monkeypatch.setenv("CLASS_CONF_THRESHOLDS", '{"D20": 0.5}')
    assert load_class_thresholds(f) == {"D20": 0.5}


def test_class_thresholds_decide_what_opens_a_record(make_pipeline, settings):
    settings.class_conf_thresholds = {"D40": 0.20, "D00": 0.60}
    pothole = Detection("D40", 0.25, (200, 300, 300, 380))  # genel eşiğin (0.35) altında ama çukur eşiğinin üstünde
    crack = Detection("D00", 0.50, (100, 100, 120, 300))   # genel eşiğin üstünde ama çatlak eşiğinin altında
    pl = make_pipeline(detections=[pothole, crack])
    res = pl.analyze(jpeg_bytes(), LAT, LON)
    assert [t["sinif"] for t in res["tespitler"]] == ["D40"]
    assert [c["sinif"] for c in res["analiz"]["esik_alti_adaylar"]] == ["D00"]
    assert res["analiz"]["sinif_esikleri"] == {"D40": 0.20, "D00": 0.60}
    assert settings.threshold_for("D10") == settings.conf_threshold
