"""Şiddet (hasarın kendisi) ve öncelik (ekibin önce nereye gideceği) hesapları.

    Şiddet  S = max_i [ w_sınıf(i) × (0.5 + 0.5 × boyut(i)) ] + çoklu hasar katkısı      (0–1)
    Öncelik P = 100 × S × K_konum × B_bildirim                                          (1–100)

Model güven skoru şiddete katılmaz; yalnızca eşik (filtre) olarak kullanılır.

Boyut, hasarın türüne göre ölçülür:
  - Çatlaklar (D00, D10): uzunluk. İnce bir çatlağın alanı küçüktür ama tehlikesi uzunluğundadır.
  - Timsah sırtı ve çukur (D20, D40): alan.
Kalibre araç kamerasında (homografi) metre / m², diğer durumlarda görüntüye oranla hesaplanır.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .detector import Detection, polygon_area

# Bu ölçüye ulaşan hasar "tam boyutlu" kabul edilir (boyut katsayısı = 1)
REF_AREA_M2 = 1.0
REF_AREA_RATIO = 0.04
REF_LENGTH_M = 3.0
REF_LENGTH_RATIO = 0.35  # görüntü köşegenine oranla
LENGTH_CLASSES = {"D00", "D10"}
DEFAULT_CLASS_WEIGHT = 0.3  # modelin bilinmeyen bir sınıf döndürmesi durumunda
MULTI_DAMAGE_STEP = 0.05
MULTI_DAMAGE_CAP = 0.15

PRIORITY_LEVELS = [(70, "kritik"), (45, "yuksek"), (25, "orta"), (0, "dusuk")]


@dataclass
class SizeEstimate:
    factor: float  # 0–1
    method: str  # "kalibrasyon" | "alan_orani" | "uzunluk_orani"
    area_m2: float | None = None
    length_m: float | None = None
    measure: str = "alan"  # "alan" | "uzunluk"


def to_ground(pts: np.ndarray, homography: list) -> np.ndarray:
    """Piksel noktalarını homografi ile zemine (metre) izdüşürür."""
    H = np.asarray(homography, dtype=float)
    homog = np.hstack([pts, np.ones((len(pts), 1))]) @ H.T
    return (homog[:, :2] / homog[:, 2:3]).astype(np.float32)


def ground_area_m2(box: tuple[float, float, float, float], homography: list) -> float:
    """Kutunun dört köşesini homografi ile zemine izdüşürüp alanını (m²) hesaplar."""
    x1, y1, x2, y2 = box
    corners = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=float)
    return polygon_area(to_ground(corners, homography))


def longest_extent(pts: np.ndarray) -> float:
    """Şekli saran en küçük döndürülmüş dikdörtgenin uzun kenarı: eğri çatlaklarda da uzunluğa yakın."""
    (_, _), (w, h), _ = cv2.minAreaRect(pts.reshape(-1, 1, 2).astype(np.float32))
    return float(max(w, h))


def estimate_size(det: Detection, img_w: int, img_h: int, homography: list | None = None) -> SizeEstimate:
    pts = det.outline
    if det.cls in LENGTH_CLASSES:
        if homography is not None:
            length = longest_extent(to_ground(pts, homography))
            return SizeEstimate(min(1.0, length / REF_LENGTH_M), "kalibrasyon", length_m=round(length, 2),
                                measure="uzunluk")
        ratio = longest_extent(pts) / math.hypot(img_w, img_h)
        return SizeEstimate(min(1.0, ratio / REF_LENGTH_RATIO), "uzunluk_orani", measure="uzunluk")
    if homography is not None:
        area = polygon_area(to_ground(pts, homography))
        return SizeEstimate(min(1.0, math.sqrt(area / REF_AREA_M2)), "kalibrasyon", round(area, 3))
    ratio = det.area / float(img_w * img_h)
    # Alan karesel büyüdüğü için karekök alınır: boyut katsayısı doğrusal ölçüye yakın davranır
    return SizeEstimate(min(1.0, math.sqrt(ratio / REF_AREA_RATIO)), "alan_orani")


def detection_severity(det: Detection, size: SizeEstimate, class_weights: dict) -> float:
    w = class_weights.get(det.cls, DEFAULT_CLASS_WEIGHT)
    return w * (0.5 + 0.5 * size.factor)


def image_severity(
    detections: list[Detection], img_w: int, img_h: int, class_weights: dict, homography: list | None = None
) -> dict:
    """Bir görüntüdeki tüm tespitlerden tek bir şiddet değeri üretir."""
    if not detections:
        return {"severity": 0.0, "dominant": None, "items": []}

    items = []
    for det in detections:
        size = estimate_size(det, img_w, img_h, homography)
        items.append(
            {
                "detection": det,
                "size": size,
                "severity": detection_severity(det, size, class_weights),
            }
        )
    items.sort(key=lambda x: x["severity"], reverse=True)
    extra = min(MULTI_DAMAGE_CAP, MULTI_DAMAGE_STEP * (len(items) - 1))
    severity = min(1.0, items[0]["severity"] + extra)
    return {"severity": round(severity, 4), "dominant": items[0], "items": items}


def report_multiplier(report_count: int) -> float:
    """Aynı noktadan gelen her ek bildirim önceliği logaritmik olarak artırır."""
    return 1.0 + 0.25 * math.log(max(1, report_count))


def priority_score(severity: float, context_weight: float, report_count: int) -> dict:
    b = report_multiplier(report_count)
    raw = 100.0 * severity * context_weight * b
    score = int(round(min(100.0, max(1.0, raw)))) if severity > 0 else 0
    return {
        "score": score,
        "level": priority_level(score),
        "breakdown": {
            "siddet": round(severity, 3),
            "konum_katsayisi": round(context_weight, 3),
            "bildirim_carpani": round(b, 3),
            "bildirim_sayisi": report_count,
        },
    }


def priority_level(score: float) -> str:
    for threshold, level in PRIORITY_LEVELS:
        if score >= threshold:
            return level
    return "dusuk"
