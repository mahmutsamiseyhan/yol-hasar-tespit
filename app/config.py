"""Uygulama ayarları. Tüm değerler ortam değişkenleriyle (.env) ezilebilir."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _default_model() -> Path:
    return Path(os.getenv("MODEL_DIR", ROOT_DIR / "models")) / "best.pt"


def _env_json(name: str, default):
    raw = os.getenv(name)
    return json.loads(raw) if raw else default


# RDD2022 sınıfları. Ağırlıklar Fen İşleri'nin kararına göre değiştirilebilir.
DEFAULT_CLASS_WEIGHTS = {
    "D40": 1.0,  # Çukur
    "D20": 0.8,  # Timsah sırtı çatlak (çukurun habercisi)
    "D10": 0.5,  # Enine çatlak
    "D00": 0.4,  # Boyuna çatlak
}

CLASS_LABELS_TR = {
    "D00": "Boyuna çatlak",
    "D10": "Enine çatlak",
    "D20": "Timsah sırtı çatlak",
    "D40": "Çukur",
}

# OSM yol sınıfı -> konum önem katsayısı
DEFAULT_ROAD_WEIGHTS = {
    "motorway": 1.0,
    "trunk": 1.0,
    "primary": 0.9,
    "secondary": 0.8,
    "tertiary": 0.7,
    "local": 0.55,  # bağlam verisinde bulunmayan ara sokaklar
}


@dataclass
class Settings:
    model_path: Path = field(default_factory=lambda: Path(os.getenv("MODEL_PATH") or _default_model()))
    conf_threshold: float = field(default_factory=lambda: _env_float("CONF_THRESHOLD", 0.35))
    iou_threshold: float = field(default_factory=lambda: _env_float("IOU_THRESHOLD", 0.5))
    imgsz: int = field(default_factory=lambda: int(os.getenv("IMGSZ", 640)))

    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", ROOT_DIR / "data")))
    db_path: Path = field(default_factory=lambda: Path(os.getenv("DB_PATH", ROOT_DIR / "data" / "yol_hasar.db")))
    context_path: Path = field(
        default_factory=lambda: Path(os.getenv("CONTEXT_PATH", ROOT_DIR / "data" / "bursa_context.geojson"))
    )
    # Etiket düzeltme aracının çalıştığı klasör (görüntü + YOLO etiketleri)
    label_dir: Path = field(
        default_factory=lambda: Path(os.getenv("LABEL_DIR", ROOT_DIR / "data" / "bursa_test" / "etiket"))
    )

    # Mükerrer kayıt birleştirme yarıçapı (metre)
    merge_radius_m: float = field(default_factory=lambda: _env_float("MERGE_RADIUS_M", 15.0))

    # KVKK: kapatılan arızaların görselleri bu süre sonunda silinir
    retention_days: int = field(default_factory=lambda: int(os.getenv("RETENTION_DAYS", 90)))
    # Opsiyonel: plaka/yüz için özel eğitilmiş YOLO modeli
    privacy_model_path: Path | None = field(
        default_factory=lambda: Path(p) if (p := os.getenv("PRIVACY_MODEL_PATH")) else None
    )

    class_weights: dict = field(default_factory=lambda: _env_json("CLASS_WEIGHTS", DEFAULT_CLASS_WEIGHTS))
    road_weights: dict = field(default_factory=lambda: _env_json("ROAD_WEIGHTS", DEFAULT_ROAD_WEIGHTS))
    # Okul/hastane yakınlığı bonusu
    poi_radius_m: float = field(default_factory=lambda: _env_float("POI_RADIUS_M", 150.0))
    poi_bonus: float = field(default_factory=lambda: _env_float("POI_BONUS", 0.1))
    # Sabit araç kamerası kalibrasyonu: görüntü pikseli -> zemin (metre) 3x3 homografi matrisi
    camera_homography: list | None = field(default_factory=lambda: _env_json("CAMERA_HOMOGRAPHY", None))

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


def get_settings() -> Settings:
    return Settings()
