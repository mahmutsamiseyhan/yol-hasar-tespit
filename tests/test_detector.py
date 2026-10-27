import numpy as np

from app.detector import Detection, YoloDetector, merge_detections


def test_merge_keeps_most_confident_of_overlapping_same_class():
    a = Detection("D40", 0.6, (0, 0, 100, 100))
    b = Detection("D40", 0.4, (5, 5, 105, 105))  # aynı çukur, ikinci taramadan
    c = Detection("D00", 0.5, (5, 5, 105, 105))  # farklı sınıf: korunur
    d = Detection("D40", 0.3, (300, 300, 400, 400))  # başka yer: korunur
    merged = merge_detections([b, a, c, d])
    assert [(m.cls, m.confidence) for m in merged] == [("D40", 0.6), ("D00", 0.5), ("D40", 0.3)]


def test_road_focus_only_for_portrait_images():
    det = YoloDetector("yok.pt")
    assert det.uses_road_focus(np.zeros((2000, 1500, 3), np.uint8)) is True
    assert det.uses_road_focus(np.zeros((1080, 1920, 3), np.uint8)) is False
    assert YoloDetector("yok.pt", road_focus=False).uses_road_focus(np.zeros((2000, 1500, 3), np.uint8)) is False


def test_road_region_detections_are_shifted_back_to_full_frame(monkeypatch):
    det = YoloDetector("yok.pt")
    calls = []

    def fake_predict(image, imgsz, y_offset=0):
        calls.append((image.shape[0], imgsz, y_offset))
        return [Detection("D40", 0.5, (10, 20 + y_offset, 60, 70 + y_offset))] if y_offset else []

    monkeypatch.setattr(det, "_predict", fake_predict)
    out = det.detect(np.zeros((2000, 1500, 3), np.uint8))
    assert calls == [(2000, 640, 0), (1000, 960, 1000)]
    assert out[0].box == (10, 1020, 60, 1070)


def test_input_size_defaults_to_the_size_the_model_was_trained_with(monkeypatch):
    det = YoloDetector("yok.pt", imgsz=None)

    class FakeModel:
        overrides = {"imgsz": 800}

    monkeypatch.setattr(det, "_load", lambda: FakeModel())
    assert det.input_size() == 800
    assert YoloDetector("yok.pt", imgsz=640).input_size() == 640  # açıkça verilen boyut modeli yüklemez
