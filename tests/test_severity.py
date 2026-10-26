import pytest

from app.config import DEFAULT_CLASS_WEIGHTS
from app.detector import Detection
from app.severity import (
    estimate_size,
    ground_area_m2,
    image_severity,
    priority_level,
    priority_score,
    report_multiplier,
)

W, H = 640, 480


def test_no_detections_gives_zero():
    sev = image_severity([], W, H, DEFAULT_CLASS_WEIGHTS)
    assert sev["severity"] == 0
    assert priority_score(0, 1.0, 1)["score"] == 0


def test_pothole_outranks_crack_of_same_size():
    box = (100, 100, 300, 250)
    pothole = image_severity([Detection("D40", 0.6, box)], W, H, DEFAULT_CLASS_WEIGHTS)
    crack = image_severity([Detection("D00", 0.95, box)], W, H, DEFAULT_CLASS_WEIGHTS)
    assert pothole["severity"] > crack["severity"]


def test_confidence_does_not_change_severity():
    box = (100, 100, 300, 250)
    low = image_severity([Detection("D40", 0.4, box)], W, H, DEFAULT_CLASS_WEIGHTS)
    high = image_severity([Detection("D40", 0.99, box)], W, H, DEFAULT_CLASS_WEIGHTS)
    assert low["severity"] == high["severity"]


def test_bigger_damage_is_more_severe_but_size_never_zeroes_score():
    tiny = image_severity([Detection("D40", 0.9, (0, 0, 5, 5))], W, H, DEFAULT_CLASS_WEIGHTS)
    big = image_severity([Detection("D40", 0.9, (0, 0, 300, 300))], W, H, DEFAULT_CLASS_WEIGHTS)
    assert big["severity"] > tiny["severity"] >= 0.5


def test_multiple_damages_add_capped_bonus():
    box = (100, 100, 150, 150)
    one = image_severity([Detection("D10", 0.9, box)], W, H, DEFAULT_CLASS_WEIGHTS)["severity"]
    many = image_severity([Detection("D10", 0.9, box)] * 10, W, H, DEFAULT_CLASS_WEIGHTS)["severity"]
    assert many == pytest.approx(one + 0.15, abs=1e-3)


def test_unknown_class_uses_default_weight():
    sev = image_severity([Detection("baska", 0.9, (0, 0, 640, 480))], W, H, DEFAULT_CLASS_WEIGHTS)
    assert sev["severity"] == pytest.approx(0.3)


def test_homography_area():
    # 100 piksel = 1 metre olan düz bir izdüşüm
    H_scale = [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 1]]
    assert ground_area_m2((0, 0, 100, 200), H_scale) == pytest.approx(2.0)
    size = estimate_size(Detection("D40", 0.9, (0, 0, 50, 50)), W, H, H_scale)
    assert size.method == "kalibrasyon" and size.area_m2 == pytest.approx(0.25)
    assert size.factor == pytest.approx(0.5)


def test_report_multiplier_is_logarithmic():
    assert report_multiplier(1) == 1.0
    gain_2 = report_multiplier(2) - report_multiplier(1)
    gain_20 = report_multiplier(20) - report_multiplier(19)
    assert gain_2 > gain_20 > 0


def test_priority_is_clipped_and_leveled():
    assert priority_score(1.0, 1.0, 100)["score"] == 100
    assert priority_score(0.001, 0.55, 1)["score"] == 1
    assert priority_level(85) == "kritik"
    assert priority_level(50) == "yuksek"
    assert priority_level(30) == "orta"
    assert priority_level(5) == "dusuk"


def test_same_pothole_main_road_beats_side_street():
    main = priority_score(0.9, 0.9, 1)["score"]
    side = priority_score(0.9, 0.55, 1)["score"]
    assert main > side


def test_cracks_are_measured_by_length_not_area():
    # Uzun ve ince bir boyuna çatlak kutusu: alanı küçük, uzunluğu büyük
    box = (300, 20, 308, 460)
    crack = estimate_size(Detection("D00", 0.9, box), W, H)
    assert crack.measure == "uzunluk" and crack.method == "uzunluk_orani"
    assert crack.factor > 0.9
    pothole = estimate_size(Detection("D40", 0.9, box), W, H)
    assert pothole.measure == "alan" and pothole.factor < 0.6


def test_crack_length_in_meters_with_homography():
    H_scale = [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 1]]  # 100 piksel = 1 m
    size = estimate_size(Detection("D10", 0.9, (0, 100, 300, 110)), W, H, H_scale)
    assert size.length_m == pytest.approx(3.0, abs=0.01)
    assert size.factor == pytest.approx(1.0)
