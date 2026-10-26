"""Uçtan uca karar destek hattı.

görüntü -> hasar tespiti -> şiddet -> KVKK maskeleme -> konum bağlamı
        -> mükerrer kayıt birleştirme -> öncelik -> (kritikse) iş emri bildirimi
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import cv2
import numpy as np

from .config import CLASS_LABELS_TR, Settings
from .context import RoadContext
from .detector import Detection, Detector
from .geo import exif_gps, feature
from .notifier import build_work_order, send_webhook
from .privacy import PrivacyMasker
from .repository import Repository, now_iso
from .severity import image_severity, priority_score

log = logging.getLogger(__name__)

SOURCES = ("vatandas", "saha_araci")
CLASS_COLORS = {"D40": (0, 0, 230), "D20": (0, 120, 255), "D10": (0, 200, 255), "D00": (0, 220, 180)}


class AnalysisError(ValueError):
    """Kullanıcının düzeltebileceği giriş hataları (HTTP 422)."""


class RoadDamagePipeline:
    def __init__(
        self,
        settings: Settings,
        detector: Detector,
        masker: PrivacyMasker,
        context: RoadContext,
        repo: Repository,
        notify=send_webhook,
    ):
        self.settings = settings
        self.detector = detector
        self.masker = masker
        self.context = context
        self.repo = repo
        self._notify = notify
        settings.media_dir.mkdir(parents=True, exist_ok=True)

    def analyze(self, image_bytes: bytes, lat: float | None = None, lon: float | None = None,
                source: str = "vatandas") -> dict:
        if source not in SOURCES:
            raise AnalysisError(f"Geçersiz kaynak: {source}. Geçerli değerler: {', '.join(SOURCES)}")
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise AnalysisError("Görüntü okunamadı. JPEG veya PNG yükleyin.")

        location_source = "istek"
        if lat is None or lon is None:
            gps = exif_gps(image_bytes)
            if gps is None:
                raise AnalysisError("Konum yok: enlem/boylam gönderin veya GPS bilgisi içeren bir fotoğraf yükleyin.")
            lat, lon = gps
            location_source = "exif"
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise AnalysisError("Geçersiz koordinat.")

        h, w = image.shape[:2]
        detections = [d for d in self.detector.detect(image) if d.confidence >= self.settings.conf_threshold]
        homography = self.settings.camera_homography if source == "saha_araci" else None
        sev = image_severity(detections, w, h, self.settings.class_weights, homography)

        # Ham görüntü diske hiç yazılmaz: önce maskele, sonra sakla
        masked, mask_report = self.masker.mask(image)

        base = {
            "konum": {"enlem": lat, "boylam": lon, "kaynak": location_source},
            "kvkk": mask_report.to_dict(),
            "tespitler": [_detection_out(i) for i in sev["items"]],
        }
        if not detections:
            return {**base, "hasar_tespit_edildi": False, "ariza": None}

        image_name = f"{uuid.uuid4().hex}.jpg"
        cv2.imwrite(str(self.settings.media_dir / image_name), _annotate(masked, sev["items"]),
                    [cv2.IMWRITE_JPEG_QUALITY, 88])

        dominant: Detection = sev["dominant"]["detection"]
        issue, merged = self._upsert_issue(lat, lon, dominant.cls, sev["severity"], image_name)
        report_id = self.repo.add_report(
            issue_id=issue["id"], lat=lat, lon=lon, source=source, severity=sev["severity"],
            detections=[d.to_dict() for d in detections], privacy=mask_report.to_dict(), image_path=image_name,
        )

        return {
            **base,
            "hasar_tespit_edildi": True,
            "bildirim_id": report_id,
            "mevcut_kayitla_birlestirildi": merged,
            "goruntu_url": f"/media/{image_name}",
            "ariza": issue_to_feature(issue),
            "is_emri_tetiklendi": issue.get("_notify", False),
        }

    def _upsert_issue(self, lat, lon, damage_class, severity, image_name) -> tuple[dict, bool]:
        with self.repo.lock:
            existing = self.repo.find_nearby_open(lat, lon, self.settings.merge_radius_m)
            if existing:
                n = existing["report_count"] + 1
                # Her bildirimle konum ortalanır; tekil GPS hatası zamanla sönümlenir
                new_lat = existing["lat"] + (lat - existing["lat"]) / n
                new_lon = existing["lon"] + (lon - existing["lon"]) / n
                stronger = severity >= existing["severity"]
                new_sev = max(severity, existing["severity"])
                ctx = self.context.lookup(new_lat, new_lon)
                prio = priority_score(new_sev, ctx["konum_katsayisi"], n)
                issue = self.repo.update_issue(
                    existing["id"], lat=new_lat, lon=new_lon, report_count=n, severity=new_sev,
                    damage_class=damage_class if stronger else existing["damage_class"],
                    image_path=image_name if stronger else existing["image_path"],
                    priority=prio["score"], level=prio["level"], context={**ctx, "skor": prio["breakdown"]},
                )
                merged = True
            else:
                ctx = self.context.lookup(lat, lon)
                prio = priority_score(severity, ctx["konum_katsayisi"], 1)
                issue = self.repo.create_issue(
                    lat=lat, lon=lon, damage_class=damage_class, severity=severity, priority=prio["score"],
                    level=prio["level"], context={**ctx, "skor": prio["breakdown"]}, image_path=image_name,
                )
                merged = False

            # Eşik ilk kez aşıldığında tek bir iş emri; kilit içinde işaretlenir ki çift gönderim olmasın
            if issue["priority"] >= self.settings.critical_threshold and not issue["notified_at"]:
                issue = self.repo.update_issue(issue["id"], notified_at="beklemede")
                issue["_notify"] = True
        return issue, merged

    def send_work_order(self, issue_id: int) -> bool:
        """Arka planda çalışır. Webhook yoksa iş emri sadece kayıt altına alınır."""
        issue = self.repo.get_issue(issue_id)
        if not self.settings.webhook_url:
            log.info("Webhook tanımlı değil; iş emri %s sadece sistemde.", issue["work_order_no"])
            self.repo.update_issue(issue_id, notified_at="webhook_yok")
            return False
        ok = self._notify(self.settings.webhook_url, build_work_order(issue, issue_to_feature(issue)))
        # Başarısızsa işaret kaldırılır; bir sonraki bildirimde yeniden denenir
        self.repo.update_issue(issue_id, notified_at=now_iso() if ok else None)
        return ok

    def purge_expired_media(self) -> int:
        paths = self.repo.expired_media(self.settings.retention_days)
        for p in paths:
            (self.settings.media_dir / p).unlink(missing_ok=True)
        return len(paths)


def issue_to_feature(issue: dict) -> dict:
    ctx = issue.get("context", {})
    return feature(
        issue["lat"],
        issue["lon"],
        {
            "id": issue["id"],
            "is_emri_no": issue["work_order_no"],
            "hasar_sinifi": issue["damage_class"],
            "hasar_adi": CLASS_LABELS_TR.get(issue["damage_class"], issue["damage_class"]),
            "siddet": issue["severity"],
            "oncelik_puani": issue["priority"],
            "oncelik_seviyesi": issue["level"],
            "bildirim_sayisi": issue["report_count"],
            "durum": issue["status"],
            "yol_sinifi": ctx.get("yol_sinifi"),
            "yol_adi": ctx.get("yol_adi"),
            "yakin_kritik_tesisler": ctx.get("yakin_kritik_tesisler", []),
            "skor_detayi": ctx.get("skor"),
            "goruntu_url": f"/media/{issue['image_path']}" if issue.get("image_path") else None,
            "olusturma": issue["created_at"],
            "guncelleme": issue["updated_at"],
        },
    )


def _detection_out(item: dict) -> dict:
    det: Detection = item["detection"]
    return {
        "sinif": det.cls,
        "ad": CLASS_LABELS_TR.get(det.cls, det.cls),
        "guven": det.confidence,
        "kutu": [round(v, 1) for v in det.box],
        "siddet": round(item["severity"], 3),
        "boyut_katsayisi": round(item["size"].factor, 3),
        "boyut_yontemi": item["size"].method,
        "alan_m2": item["size"].area_m2,
    }


def _annotate(image: np.ndarray, items: list[dict]) -> np.ndarray:
    out = image.copy()
    thickness = max(2, round(min(out.shape[:2]) / 300))
    for item in items:
        det: Detection = item["detection"]
        x1, y1, x2, y2 = (int(v) for v in det.box)
        color = CLASS_COLORS.get(det.cls, (200, 200, 200))
        cv2.rectangle(out, (x1, y1), (x2, y2), color, thickness)
        # OpenCV Türkçe karakter çizemez; etikette sınıf kodu kullanılır, adı arayüzde gösterilir
        label = f"{det.cls} {det.confidence:.0%}"
        scale = thickness / 3
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
        cv2.rectangle(out, (x1, max(0, y1 - th - 8)), (x1 + tw + 6, y1), color, -1)
        cv2.putText(out, label, (x1 + 3, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), thickness)
    return out
