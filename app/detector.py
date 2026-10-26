"""YOLO tabanlı yol hasarı dedektörü (tespit veya segmentasyon modeli)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

MAX_POLYGON_POINTS = 80


@dataclass
class Detection:
    cls: str  # "D40" gibi sınıf kodu
    confidence: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 (piksel)
    # Segmentasyon modelinde hasarın sınırı: [(x, y), ...] piksel. Tespit modelinde None.
    polygon: list[tuple[float, float]] | None = None

    @property
    def outline(self) -> np.ndarray:
        """Hasarın sınırı: çokgen varsa o, yoksa kutunun dört köşesi. (N, 2) piksel."""
        if self.polygon and len(self.polygon) >= 3:
            return np.asarray(self.polygon, dtype=np.float32)
        x1, y1, x2, y2 = self.box
        return np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.float32)

    @property
    def area(self) -> float:
        return polygon_area(self.outline)

    def to_dict(self) -> dict:
        return asdict(self)


def polygon_area(pts: np.ndarray) -> float:
    x, y = pts[:, 0], pts[:, 1]
    return float(0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1))))


def simplify_polygon(pts: np.ndarray, max_points: int = MAX_POLYGON_POINTS) -> list[tuple[float, float]]:
    """Maske kenarındaki yüzlerce noktayı şekli bozmadan seyreltir."""
    contour = pts.reshape(-1, 1, 2).astype(np.float32)
    eps = 0.002 * cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, eps, True)
    while len(approx) > max_points:
        eps *= 1.5
        approx = cv2.approxPolyDP(contour, eps, True)
    if len(approx) < 3:
        approx = contour
    return [(round(float(x), 1), round(float(y), 1)) for x, y in approx.reshape(-1, 2)]


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
        polygons = result.masks.xy if result.masks is not None else [None] * len(result.boxes)
        detections = []
        for xyxy, conf, cls_id, poly in zip(
            result.boxes.xyxy.tolist(), result.boxes.conf.tolist(), result.boxes.cls.tolist(), polygons
        ):
            detections.append(
                Detection(
                    cls=str(names[int(cls_id)]),
                    confidence=round(float(conf), 4),
                    box=tuple(xyxy),
                    polygon=simplify_polygon(poly) if poly is not None and len(poly) >= 3 else None,
                )
            )
        return detections
