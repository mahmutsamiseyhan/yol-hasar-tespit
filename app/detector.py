"""YOLO tabanlı yol hasarı dedektörü."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import numpy as np


@dataclass
class Detection:
    cls: str  # "D40" gibi sınıf kodu
    confidence: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 (piksel)

    @property
    def outline(self) -> np.ndarray:
        """Kutunun dört köşesi, (4, 2) piksel."""
        x1, y1, x2, y2 = self.box
        return np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.float32)

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.box
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)

    def to_dict(self) -> dict:
        return asdict(self)


def polygon_area(pts: np.ndarray) -> float:
    x, y = pts[:, 0], pts[:, 1]
    return float(0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1))))


class Detector(Protocol):
    def detect(self, image_bgr: np.ndarray) -> list[Detection]: ...


def box_iou(a: tuple, b: tuple) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def merge_detections(dets: list[Detection], iou: float = 0.5) -> list[Detection]:
    """İki taramadan gelen tespitleri birleştirir: aynı sınıfta örtüşenlerden en güvenlisi kalır."""
    kept: list[Detection] = []
    for d in sorted(dets, key=lambda d: d.confidence, reverse=True):
        if all(k.cls != d.cls or box_iou(k.box, d.box) < iou for k in kept):
            kept.append(d)
    return kept


class YoloDetector:
    """Ultralytics YOLO modelini sarmalar. Model yalnızca ilk kullanımda yüklenir.

    Dikey fotoğraflarda (çoğu vatandaş fotoğrafı) yol karenin alt kısmında küçük kalır.
    Bu durumda alt bölge ayrıca daha yüksek çözünürlükte taranır ve sonuçlar birleştirilir.
    """

    PORTRAIT_RATIO = 1.15  # yükseklik / genişlik bu değeri aşarsa dikey sayılır
    ROAD_REGION_TOP = 0.5  # yol bölgesi: karenin alt yarısı
    ROAD_REGION_IMGSZ = 960

    def __init__(self, model_path: Path, conf: float = 0.35, iou: float = 0.5, imgsz: int | None = 640,
                 road_focus: bool = True):
        self.model_path = Path(model_path)
        self.conf = conf
        self.iou = iou
        self.imgsz = imgsz
        self.road_focus = road_focus
        self._model = None

    def uses_road_focus(self, image_bgr: np.ndarray) -> bool:
        h, w = image_bgr.shape[:2]
        return self.road_focus and h > w * self.PORTRAIT_RATIO

    @property
    def available(self) -> bool:
        return self.model_path.exists()

    def _load(self):
        if self._model is None:
            if not self.available:
                raise FileNotFoundError(
                    f"Model ağırlıkları bulunamadı: {self.model_path}. "
                    "Colab'da eğittiğiniz best.pt dosyasını bu konuma koyun."
                )
            from ultralytics import YOLO

            self._model = YOLO(str(self.model_path))
        return self._model

    def input_size(self) -> int:
        """imgsz verilmemişse modelin eğitildiği görüntü boyutu (ağırlık dosyasında kayıtlı)."""
        if self.imgsz is None:
            self.imgsz = int(self._load().overrides.get("imgsz") or 640)
        return self.imgsz

    def _predict(self, image_bgr: np.ndarray, imgsz: int, y_offset: int = 0) -> list[Detection]:
        result = self._load().predict(image_bgr, conf=self.conf, iou=self.iou, imgsz=imgsz, verbose=False)[0]
        names = result.names
        detections = []
        for (x1, y1, x2, y2), conf, cls_id in zip(
            result.boxes.xyxy.tolist(), result.boxes.conf.tolist(), result.boxes.cls.tolist()
        ):
            detections.append(Detection(cls=str(names[int(cls_id)]), confidence=round(float(conf), 4),
                                        box=(x1, y1 + y_offset, x2, y2 + y_offset)))
        return detections

    def detect(self, image_bgr: np.ndarray) -> list[Detection]:
        detections = self._predict(image_bgr, self.input_size())
        if self.uses_road_focus(image_bgr):
            top = int(image_bgr.shape[0] * self.ROAD_REGION_TOP)
            detections = merge_detections(
                detections + self._predict(image_bgr[top:], self.ROAD_REGION_IMGSZ, y_offset=top), self.iou
            )
        return detections
