"""KVKK maskeleme modellerini models/ klasörüne indirir.

  - YuNet yüz dedektörü (OpenCV Zoo, MIT lisansı)
  - YOLO11n COCO (kişi ve araç tespiti, plaka bölgesi maskeleme için)

Hasar modeli (best.pt) Colab'da eğitilir ve elle models/ altına kopyalanır.
"""

from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path

MODELS = Path(__file__).resolve().parent.parent / "models"
YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"


def main():
    MODELS.mkdir(exist_ok=True)

    yunet = MODELS / "face_detection_yunet_2023mar.onnx"
    if not yunet.exists():
        print("YuNet indiriliyor...")
        urllib.request.urlretrieve(YUNET_URL, yunet)
    print(f"✓ {yunet.name} ({yunet.stat().st_size / 1e3:.0f} KB)")

    coco = MODELS / "yolo11n.pt"
    if not coco.exists():
        from ultralytics import YOLO

        YOLO("yolo11n.pt")  # ultralytics ağırlığı çalışma dizinine indirir
        shutil.move("yolo11n.pt", coco)
    print(f"✓ {coco.name} ({coco.stat().st_size / 1e6:.1f} MB)")

    best = MODELS / "best.pt"
    print(f"{'✓' if best.exists() else '✗'} best.pt {'' if best.exists() else '(Colab eğitiminden sonra buraya kopyalayın)'}")


if __name__ == "__main__":
    main()
