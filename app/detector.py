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


class YoloDetector:
    """Ultralytics YOLO modelini sarmalar. Model yalnızca ilk kullanımda yüklenir."""

    def __init__(self, model_path: Path, conf: float = 0.35, iou: float = 0.5, imgsz: int = 640):
        self.model_path = Path(model_path)
        self.conf = conf
        self.iou = iou
        self.imgsz = imgsz
        self._model = None

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

    def detect(self, image_bgr: np.ndarray) -> list[Detection]:
        model = self._load()
        result = model.predict(image_bgr, conf=self.conf, iou=self.iou, imgsz=self.imgsz, verbose=False)[0]
        names = result.names
        detections = []
        for xyxy, conf, cls_id in zip(
            result.boxes.xyxy.tolist(), result.boxes.conf.tolist(), result.boxes.cls.tolist()
        ):
            detections.append(
                Detection(cls=str(names[int(cls_id)]), confidence=round(float(conf), 4), box=tuple(xyxy))
            )
        return detections
