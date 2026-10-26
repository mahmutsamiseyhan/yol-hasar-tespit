"""Saha aracı videosunu işler: aynı hasarı karelerde takip eder, her hasarı API'ye BİR kez gönderir.

Araç kamerası saniyede 30 kare çeker; bir çukur 1-2 saniye boyunca onlarca karede görünür.
GPS'e dayalı birleştirmeden önce ByteTrack ile kare düzeyinde takip yapılır:
her takip kimliği (track id) için en iyi kare seçilir, konumu GPS kaydından
zamana göre enterpolasyonla bulunur ve tek bir bildirim olarak gönderilir.

GPS kaydı biçimleri:
  - CSV: t_saniye,enlem,boylam   (t: videonun başından itibaren saniye)
  - GPX: telefon/araç takip uygulamalarının standart çıktısı (--video-start ile hizalanır)

Kullanım:
    python scripts/process_video.py --video tur.mp4 --gps tur.gpx --video-start 2026-10-26T09:15:00Z \
        --model models/best.pt --api http://localhost:8000
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import sys
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_CLASS_WEIGHTS  # noqa: E402


@dataclass
class GpsTrack:
    times: list[float]  # saniye (video başına göre)
    lats: list[float]
    lons: list[float]

    def at(self, t: float) -> tuple[float, float]:
        """Doğrusal enterpolasyon; kayıt dışındaki zamanlar uçtaki noktaya sabitlenir."""
        if t <= self.times[0]:
            return self.lats[0], self.lons[0]
        if t >= self.times[-1]:
            return self.lats[-1], self.lons[-1]
        i = bisect.bisect_right(self.times, t)
        t0, t1 = self.times[i - 1], self.times[i]
        a = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
        return (self.lats[i - 1] + a * (self.lats[i] - self.lats[i - 1]),
                self.lons[i - 1] + a * (self.lons[i] - self.lons[i - 1]))


def load_gps_csv(path: Path) -> GpsTrack:
    with open(path, newline="", encoding="utf-8") as f:
        rows = sorted((float(r["t_saniye"]), float(r["enlem"]), float(r["boylam"])) for r in csv.DictReader(f))
    return GpsTrack(*map(list, zip(*rows)))


def load_gpx(path: Path, video_start: datetime) -> GpsTrack:
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    pts = []
    for p in ET.parse(path).getroot().iterfind(".//g:trkpt", ns):
        ts = datetime.fromisoformat(p.findtext("g:time", namespaces=ns).replace("Z", "+00:00"))
        pts.append(((ts - video_start).total_seconds(), float(p.get("lat")), float(p.get("lon"))))
    pts.sort()
    return GpsTrack(*map(list, zip(*pts)))


@dataclass
class TrackState:
    frames: int = 0
    best_score: float = -1.0
    best_frame_idx: int = -1
    best_image: bytes | None = None
    classes: dict = field(default_factory=dict)


class TrackAggregator:
    """Her takip kimliği için en iyi kareyi (sınıf ağırlığı × alan × güven) tutar."""

    def __init__(self, min_frames: int = 3, class_weights: dict | None = None):
        self.min_frames = min_frames
        self.class_weights = class_weights or DEFAULT_CLASS_WEIGHTS
        self.tracks: dict[int, TrackState] = {}

    def update(self, track_id: int, cls: str, conf: float, box, frame_idx: int, encode_frame) -> None:
        st = self.tracks.setdefault(track_id, TrackState())
        st.frames += 1
        st.classes[cls] = st.classes.get(cls, 0) + 1
        x1, y1, x2, y2 = box
        score = self.class_weights.get(cls, 0.3) * (x2 - x1) * (y2 - y1) * conf
        if score > st.best_score:
            st.best_score, st.best_frame_idx = score, frame_idx
            st.best_image = encode_frame()  # sadece gerektiğinde JPEG'e çevir

    def results(self) -> list[tuple[int, TrackState]]:
        # Kısa ömürlü (titreyen) takipler yanlış pozitif kabul edilir
        return [(tid, st) for tid, st in self.tracks.items() if st.frames >= self.min_frames]


def post_report(api: str, image: bytes, lat: float, lon: float) -> dict:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("lat", f"{lat:.7f}"), ("lon", f"{lon:.7f}"), ("source", "saha_araci")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="kare.jpg"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n".encode() + image + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"{api.rstrip('/')}/api/analyze-road", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", type=Path, required=True)
    ap.add_argument("--gps", type=Path, required=True, help="CSV (t_saniye,enlem,boylam) veya GPX")
    ap.add_argument("--video-start", help="GPX için videonun başlangıç zamanı (ISO 8601)")
    ap.add_argument("--model", type=Path, default=Path("models/best.pt"))
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--min-frames", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="API'ye göndermeden sonuçları yazdır")
    args = ap.parse_args()

    import cv2
    from ultralytics import YOLO

    if args.gps.suffix.lower() == ".gpx":
        if not args.video_start:
            raise SystemExit("GPX için --video-start gerekli")
        gps = load_gpx(args.gps, datetime.fromisoformat(args.video_start.replace("Z", "+00:00")))
    else:
        gps = load_gps_csv(args.gps)

    fps = cv2.VideoCapture(str(args.video)).get(cv2.CAP_PROP_FPS) or 30.0
    model = YOLO(str(args.model))
    agg = TrackAggregator(min_frames=args.min_frames)

    for frame_idx, result in enumerate(
        model.track(source=str(args.video), stream=True, persist=True, tracker="bytetrack.yaml",
                    conf=args.conf, verbose=False)
    ):
        boxes = result.boxes
        if boxes.id is None:
            continue
        frame = result.orig_img
        encode = lambda f=frame: cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()  # noqa: E731
        for tid, cls_id, conf, xyxy in zip(boxes.id.tolist(), boxes.cls.tolist(), boxes.conf.tolist(),
                                           boxes.xyxy.tolist()):
            agg.update(int(tid), result.names[int(cls_id)], float(conf), xyxy, frame_idx, encode)

    tracks = agg.results()
    print(f"{len(agg.tracks)} takip, {len(tracks)} tanesi en az {args.min_frames} karede görüldü")
    for tid, st in tracks:
        lat, lon = gps.at(st.best_frame_idx / fps)
        cls = max(st.classes, key=st.classes.get)
        line = f"takip {tid}: {cls}, {st.frames} kare, t={st.best_frame_idx / fps:.1f}s, ({lat:.6f}, {lon:.6f})"
        if args.dry_run:
            print(line)
            continue
        res = post_report(args.api, st.best_image, lat, lon)
        ariza = res.get("ariza") or {}
        props = ariza.get("properties", {})
        status = "birleştirildi" if res.get("mevcut_kayitla_birlestirildi") else "yeni kayıt"
        print(f"{line} -> {props.get('is_emri_no', 'hasar yok')} ({status}, öncelik {props.get('oncelik_puani')})")


if __name__ == "__main__":
    main()
