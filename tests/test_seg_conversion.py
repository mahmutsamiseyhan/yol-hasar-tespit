import json

import cv2
import numpy as np

from scripts.rdd_to_seg_sam import box_polygon, format_line, mask_to_polygon, summarize

H, W = 200, 300
BOX = (50, 40, 250, 160)  # x1, y1, x2, y2 piksel


def blank():
    return np.zeros((H, W), np.uint8)


def test_object_inside_box_becomes_polygon():
    m = blank()
    cv2.ellipse(m, (150, 100), (60, 30), 0, 0, 360, 1, -1)
    pts, fill = mask_to_polygon(m, BOX)
    assert pts is not None and len(pts) >= 8
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    assert 85 <= min(xs) <= 95 and 205 <= max(xs) <= 215 and 0.2 < fill < 0.4


def test_leak_outside_box_is_clipped():
    m = blank()
    cv2.ellipse(m, (150, 100), (40, 20), 0, 0, 360, 1, -1)
    m[0:30, :] = 1  # kutunun dışına, üstteki yol yüzeyine taşma
    pts, _ = mask_to_polygon(m, BOX)
    assert pts is not None and min(p[1] for p in pts) >= BOX[1]


def test_box_filling_or_empty_masks_fall_back_to_box():
    full = blank()
    full[BOX[1]:BOX[3], BOX[0]:BOX[2]] = 1
    assert mask_to_polygon(full, BOX)[0] is None  # şekil bilgisi yok
    tiny = blank()
    tiny[100:102, 150:152] = 1
    assert mask_to_polygon(tiny, BOX)[0] is None  # SAM nesneyi bulamadı


def test_fragmented_crack_is_merged_into_one_polygon():
    m = blank()
    cv2.line(m, (60, 60), (120, 90), 1, 6)
    cv2.line(m, (140, 100), (240, 150), 1, 6)  # kesik çatlak: iki parça
    pts, _ = mask_to_polygon(m, BOX)
    xs = [p[0] for p in pts]
    assert pts is not None and min(xs) < 70 and max(xs) > 230  # iki parçayı da kapsıyor


def test_line_formatting_and_box_polygon():
    poly = box_polygon(0.5, 0.5, 0.2, 0.4)
    assert poly == [[0.4, 0.3], [0.6, 0.3], [0.6, 0.7], [0.4, 0.7]]
    assert format_line(3, [[0.1, -0.2], [1.5, 0.5], [0.3, 0.3]]) == "3 0.100000 0.000000 1.000000 0.500000 0.300000 0.300000"


def test_summary_uses_latest_record_per_image(tmp_path):
    log = tmp_path / "_kaynak.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in [
        {"split": "train", "img": "a.jpg", "kaynak": [["kutu", "D40", 0.0]]},
        {"split": "train", "img": "a.jpg", "kaynak": [["sam", "D40", 0.4]]},  # yeniden çalıştırma
        {"split": "train", "img": "b.jpg", "kaynak": [["sam", "D00", 0.2], ["kutu", "D00", 0.95]]},
    ]))
    s = summarize(log)
    assert s["D40"] == {"sam": 1, "kutu": 0, "sam_orani": 1.0}
    assert s["D00"] == {"sam": 1, "kutu": 1, "sam_orani": 0.5}
