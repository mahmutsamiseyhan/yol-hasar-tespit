import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_ROAD_WEIGHTS, Settings  # noqa: E402
from app.context import RoadContext  # noqa: E402
from app.detector import Detection  # noqa: E402
from app.pipeline import RoadDamagePipeline  # noqa: E402
from app.privacy import PrivacyMasker  # noqa: E402
from app.repository import Repository  # noqa: E402

# Test bölgesi: kuzey-güney uzanan bir ana arter ve yanında bir okul
ARTER_LON = 29.0600
CONTEXT_GEOJSON = {
    "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[ARTER_LON, 40.180], [ARTER_LON, 40.200]]},
         "properties": {"highway": "primary", "name": "Test Bulvarı"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [ARTER_LON + 0.001, 40.190]},
         "properties": {"amenity": "school", "name": "Test İlkokulu"}},
    ]
}


class FakeDetector:
    def __init__(self, detections=None):
        self.detections = detections if detections is not None else []

    def detect(self, image_bgr):
        return list(self.detections)


class FakeRegionDetector:
    name = "sahte_plaka"

    def __init__(self, boxes):
        self.boxes = boxes

    def find(self, image_bgr):
        return self.boxes


def jpeg_bytes(w=640, h=480, gps=None) -> bytes:
    rng = np.random.default_rng(0)
    img = Image.fromarray(rng.integers(0, 255, (h, w, 3), dtype=np.uint8))
    buf = io.BytesIO()
    if gps:
        exif = Image.Exif()
        exif.get_ifd(0x8825).update(gps)
        img.save(buf, "JPEG", exif=exif.tobytes())
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.data_dir = tmp_path
    s.db_path = tmp_path / "test.db"
    s.camera_homography = None
    s.class_conf_thresholds = {}  # makinedeki models/esikler.json testleri etkilemesin
    return s


@pytest.fixture
def context():
    return RoadContext(CONTEXT_GEOJSON, road_weights=DEFAULT_ROAD_WEIGHTS, poi_radius_m=150, poi_bonus=0.1)


@pytest.fixture
def make_pipeline(settings, context):
    def _make(detections=None, privacy_boxes=None):
        return RoadDamagePipeline(
            settings=settings,
            detector=FakeDetector(detections),
            masker=PrivacyMasker([FakeRegionDetector(privacy_boxes or [])]),
            context=context,
            repo=Repository(settings.db_path),
        )

    return _make


BIG_POTHOLE = Detection("D40", 0.9, (200, 300, 456, 420))  # görüntünün ~%10'u
SMALL_CRACK = Detection("D00", 0.8, (100, 100, 140, 110))
