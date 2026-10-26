"""KVKK uyumlu maskeleme: kişi, yüz ve plaka bölgeleri diske yazılmadan önce bulanıklaştırılır.

Hasar tespiti ham görüntü üzerinde bellekte yapılır; sadece maskelenmiş görüntü saklanır.
Dedektörler takılıp çıkarılabilir; hangilerinin aktif olduğu her yanıtta raporlanır.
Bulunamayan bir model dosyası sistemi durdurmaz, o dedektör devre dışı kalır.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

log = logging.getLogger(__name__)

Box = tuple[int, int, int, int]

COCO_PERSON = 0
COCO_VEHICLES = {2, 3, 5, 7}  # car, motorcycle, bus, truck
# Plakalar araç kutusunun alt kısmındadır; özel plaka modeli yoksa bu bölge temkinli şekilde maskelenir
PLATE_ZONE_RATIO = 0.4


class RegionDetector(Protocol):
    name: str

    def find(self, image_bgr: np.ndarray) -> list[Box]: ...


class YuNetFaceDetector:
    """OpenCV'nin YuNet yüz dedektörü (opencv_zoo, MIT lisanslı ONNX modeli)."""

    name = "yunet_yuz"

    def __init__(self, model_path: Path, score_threshold: float = 0.6):
        self._detector = cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), score_threshold)

    def find(self, image_bgr: np.ndarray) -> list[Box]:
        h, w = image_bgr.shape[:2]
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(image_bgr)
        if faces is None:
            return []
        return [(int(f[0]), int(f[1]), int(f[0] + f[2]), int(f[1] + f[3])) for f in faces]


class CocoPersonVehicleDetector:
    """COCO ön-eğitimli YOLO: kişileri tamamen, araçların plaka bölgesini maskeler."""

    name = "coco_kisi_arac"

    def __init__(self, model_name: str = "yolo11n.pt", conf: float = 0.3):
        from ultralytics import YOLO

        self._model = YOLO(model_name)
        self.conf = conf

    def find(self, image_bgr: np.ndarray) -> list[Box]:
        result = self._model.predict(image_bgr, conf=self.conf, verbose=False)[0]
        boxes: list[Box] = []
        for (x1, y1, x2, y2), cls_id in zip(result.boxes.xyxy.tolist(), result.boxes.cls.tolist()):
            cls_id = int(cls_id)
            if cls_id == COCO_PERSON:
                boxes.append((int(x1), int(y1), int(x2), int(y2)))
            elif cls_id in COCO_VEHICLES:
                plate_top = y2 - (y2 - y1) * PLATE_ZONE_RATIO
                boxes.append((int(x1), int(plate_top), int(x2), int(y2)))
        return boxes


class YoloRegionDetector:
    """Plaka/yüz için özel eğitilmiş bir YOLO modeli: tüm tespitleri maskeler."""

    name = "ozel_plaka_yuz"

    def __init__(self, model_path: Path, conf: float = 0.25):
        from ultralytics import YOLO

        self._model = YOLO(str(model_path))
        self.conf = conf

    def find(self, image_bgr: np.ndarray) -> list[Box]:
        result = self._model.predict(image_bgr, conf=self.conf, verbose=False)[0]
        return [tuple(int(v) for v in xyxy) for xyxy in result.boxes.xyxy.tolist()]


@dataclass
class MaskReport:
    active_detectors: list[str]
    masked_regions: int

    def to_dict(self) -> dict:
        return {"aktif_dedektorler": self.active_detectors, "maskelenen_bolge": self.masked_regions}


def blur_regions(image_bgr: np.ndarray, boxes: list[Box], pad: float = 0.1) -> np.ndarray:
    out = image_bgr.copy()
    h, w = out.shape[:2]
    for x1, y1, x2, y2 in boxes:
        px, py = int((x2 - x1) * pad), int((y2 - y1) * pad)
        x1, y1 = max(0, x1 - px), max(0, y1 - py)
        x2, y2 = min(w, x2 + px), min(h, y2 + py)
        if x2 <= x1 or y2 <= y1:
            continue
        roi = out[y1:y2, x1:x2]
        # Önce pikselleştir, sonra bulanıklaştır: geri döndürülmesi zor, görsel olarak yumuşak
        small = cv2.resize(roi, (max(1, (x2 - x1) // 12), max(1, (y2 - y1) // 12)), interpolation=cv2.INTER_AREA)
        roi = cv2.resize(small, (x2 - x1, y2 - y1), interpolation=cv2.INTER_NEAREST)
        k = max(3, ((min(x2 - x1, y2 - y1) // 4) | 1))
        out[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (k, k), 0)
    return out


class PrivacyMasker:
    def __init__(self, detectors: list[RegionDetector]):
        self.detectors = detectors

    def mask(self, image_bgr: np.ndarray) -> tuple[np.ndarray, MaskReport]:
        boxes: list[Box] = []
        for det in self.detectors:
            try:
                boxes.extend(det.find(image_bgr))
            except Exception:  # bir dedektörün hatası tüm hattı durdurmamalı
                log.exception("KVKK dedektörü hata verdi: %s", det.name)
        masked = blur_regions(image_bgr, boxes) if boxes else image_bgr.copy()
        return masked, MaskReport([d.name for d in self.detectors], len(boxes))


def build_default_masker(models_dir: Path, privacy_model_path: Path | None = None) -> PrivacyMasker:
    detectors: list[RegionDetector] = []

    yunet = models_dir / "face_detection_yunet_2023mar.onnx"
    if yunet.exists():
        detectors.append(YuNetFaceDetector(yunet))
    else:
        log.warning("YuNet yüz modeli yok (%s); scripts/download_models.py ile indirin.", yunet)

    try:
        coco = models_dir / "yolo11n.pt"
        detectors.append(CocoPersonVehicleDetector(str(coco) if coco.exists() else "yolo11n.pt"))
    except Exception:
        log.warning("COCO kişi/araç dedektörü yüklenemedi.", exc_info=True)

    if privacy_model_path and privacy_model_path.exists():
        detectors.append(YoloRegionDetector(privacy_model_path))

    if not detectors:
        log.error("Hiçbir KVKK dedektörü aktif değil! Görüntüler maskelenmeden saklanacak.")
    return PrivacyMasker(detectors)
