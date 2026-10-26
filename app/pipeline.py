"""Uçtan uca karar destek hattı.

görüntü -> hasar tespiti -> şiddet -> KVKK maskeleme -> konum bağlamı
        -> mükerrer kayıt birleştirme -> öncelik ve iş emri
"""

from __future__ import annotations

import base64
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from .config import CLASS_LABELS_TR, Settings
from .context import RoadContext
from .detector import Detection, Detector
from .geo import exif_gps, feature
from .privacy import PrivacyMasker
from .repository import Repository
from .severity import image_severity, priority_score


SOURCES = ("vatandas", "saha_araci")
MAX_CANDIDATES = 5
LOW_LIGHT = 70  # ortalama parlaklık (0-255) bunun altındaysa ışık yetersiz sayılır
PREVIEW_MAX_SIDE = 1280
ROAD_TR = {"motorway": "Otoyol", "trunk": "Devlet yolu", "primary": "Ana arter", "secondary": "Tali arter",
           "tertiary": "Toplayıcı yol", "local": "Ara sokak / ana yol ağı dışı"}
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
    ):
        self.settings = settings
        self.detector = detector
        self.masker = masker
        self.context = context
        self.repo = repo
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

        started = time.perf_counter()
        h, w = image.shape[:2]
        found = self.detector.detect(image)
        detections = [d for d in found if d.confidence >= self.settings.conf_threshold]
        candidates = sorted((d for d in found if d.confidence < self.settings.conf_threshold),
                            key=lambda d: d.confidence, reverse=True)
        homography = self.settings.camera_homography if source == "saha_araci" else None
        sev = image_severity(detections, w, h, self.settings.class_weights, homography)

        # Ham görüntü diske hiç yazılmaz: önce maskele, sonra sakla
        masked, mask_report = self.masker.mask(image)
        ctx = self.context.lookup(lat, lon)

        base = {
            "konum": {"enlem": lat, "boylam": lon, "kaynak": location_source},
            "kvkk": mask_report.to_dict(),
            "tespitler": [_detection_out(i) for i in sev["items"]],
        }
        report = {
            "konum_baglami": ctx,
            "esik_alti_adaylar": [_candidate_out(d) for d in candidates[:MAX_CANDIDATES]],
            "goruntu": _image_quality(image),
            "analiz_modu": "tam_kare+yol_bolgesi" if getattr(self.detector, "uses_road_focus", lambda _: False)(image)
                           else "tam_kare",
            "guven_esigi": self.settings.conf_threshold,
            "aday_esigi": self.settings.candidate_conf,
            "model": Path(str(getattr(self.detector, "model_path", "model"))).name,
        }
        # Önizleme: kabul edilen tespitler düz, zayıf adaylar kesik çizgiyle. Diske yazılmaz (KVKK).
        preview = _annotate(masked, sev["items"], candidates[:MAX_CANDIDATES])
        if not detections:
            report["sure_ms"] = round(1000 * (time.perf_counter() - started))
            return {**base, "hasar_tespit_edildi": False, "ariza": None, "analiz": report,
                    "onizleme": _data_uri(preview),
                    "degerlendirme": _assessment(False, detections, candidates, report, None)}

        image_name = f"{uuid.uuid4().hex}.jpg"
        cv2.imwrite(str(self.settings.media_dir / image_name), _annotate(masked, sev["items"]),
                    [cv2.IMWRITE_JPEG_QUALITY, 88])

        dominant: Detection = sev["dominant"]["detection"]
        issue, merged = self._upsert_issue(lat, lon, dominant.cls, sev["severity"], image_name)
        report_id = self.repo.add_report(
            issue_id=issue["id"], lat=lat, lon=lon, source=source, severity=sev["severity"],
            detections=[d.to_dict() for d in detections], privacy=mask_report.to_dict(), image_path=image_name,
        )

        report["sure_ms"] = round(1000 * (time.perf_counter() - started))
        return {
            **base,
            "hasar_tespit_edildi": True,
            "bildirim_id": report_id,
            "mevcut_kayitla_birlestirildi": merged,
            "goruntu_url": f"/media/{image_name}",
            "ariza": issue_to_feature(issue),
            "kritik_oncelik": issue["level"] == "kritik",
            "analiz": report,
            "onizleme": _data_uri(preview),
            "degerlendirme": _assessment(True, detections, candidates, report, issue),
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
        return issue, merged

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
        "boyut_olcusu": item["size"].measure,
        "boyut_yontemi": item["size"].method,
        "alan_m2": item["size"].area_m2,
        "uzunluk_m": item["size"].length_m,
    }


def _annotate(image: np.ndarray, items: list[dict], candidates: list[Detection] = ()) -> np.ndarray:
    out = image.copy()
    thickness = max(2, round(min(out.shape[:2]) / 300))
    scale = thickness / 3
    # Zayıf adaylar: gri kesik çizgi, kayıt açılmaz
    for det in candidates:
        x1, y1, x2, y2 = (int(v) for v in det.box)
        _dashed_rect(out, (x1, y1), (x2, y2), (190, 190, 190), max(1, thickness - 1))
        cv2.putText(out, f"{det.cls}? {det.confidence:.0%}", (x1 + 3, max(12, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                    scale * 0.8, (230, 230, 230), max(1, thickness - 1))
    for item in items:
        det: Detection = item["detection"]
        x1, y1, x2, y2 = (int(v) for v in det.box)
        color = CLASS_COLORS.get(det.cls, (200, 200, 200))
        cv2.rectangle(out, (x1, y1), (x2, y2), color, thickness)
        # OpenCV Türkçe karakter çizemez; etikette sınıf kodu kullanılır, adı arayüzde gösterilir
        label = f"{det.cls} {det.confidence:.0%}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
        cv2.rectangle(out, (x1, max(0, y1 - th - 8)), (x1 + tw + 6, y1), color, -1)
        cv2.putText(out, label, (x1 + 3, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), thickness)
    return out


def _dashed_rect(img: np.ndarray, p1: tuple, p2: tuple, color: tuple, thickness: int, dash: int = 14) -> None:
    (x1, y1), (x2, y2) = p1, p2
    for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
        length = int(np.hypot(b[0] - a[0], b[1] - a[1]))
        for start in range(0, length, dash * 2):
            t0, t1 = start / max(1, length), min(1.0, (start + dash) / max(1, length))
            cv2.line(img, (int(a[0] + (b[0] - a[0]) * t0), int(a[1] + (b[1] - a[1]) * t0)),
                     (int(a[0] + (b[0] - a[0]) * t1), int(a[1] + (b[1] - a[1]) * t1)), color, thickness)


def _candidate_out(det: Detection) -> dict:
    return {"sinif": det.cls, "ad": CLASS_LABELS_TR.get(det.cls, det.cls), "guven": det.confidence,
            "kutu": [round(v, 1) for v in det.box]}


def _image_quality(image: np.ndarray) -> dict:
    h, w = image.shape[:2]
    brightness = float(cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[..., 2].mean())
    return {
        "genislik": w,
        "yukseklik": h,
        "yon": "dikey" if h > w * 1.15 else ("yatay" if w > h * 1.15 else "kare"),
        "parlaklik": round(brightness),
        "isik": "dusuk" if brightness < LOW_LIGHT else "yeterli",
    }


def _data_uri(image: np.ndarray) -> str:
    h, w = image.shape[:2]
    scale = min(1.0, PREVIEW_MAX_SIDE / max(h, w))
    if scale < 1:
        image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def _assessment(found: bool, detections: list[Detection], candidates: list[Detection], report: dict,
                issue: dict | None) -> list[dict]:
    """Analizin insan tarafından okunabilir özeti: karar, görüntü kalitesi, konum ve öncelik."""
    notes = []
    pct = lambda c: f"%{round(100 * c)}"  # noqa: E731
    threshold = pct(report["guven_esigi"])
    if found:
        top = max(detections, key=lambda d: d.confidence)
        notes.append({"tur": "karar", "metin": f"{len(detections)} hasar tespit edildi. En belirgini: "
                      f"{CLASS_LABELS_TR.get(top.cls, top.cls)} ({pct(top.confidence)} güven)."})
    elif candidates:
        c = candidates[0]
        notes.append({"tur": "karar", "metin": f"Güven eşiğini ({threshold}) geçen hasar yok. Eşiğin altında "
                      f"{len(candidates)} zayıf aday var (en güçlüsü {CLASS_LABELS_TR.get(c.cls, c.cls)}, "
                      f"{pct(c.confidence)}); kayıt açılmadı. Zayıf adaylar çoğunlukla gölge, yama, çakıl veya "
                      "yol çizgisidir. Şüphe varsa hasarı daha yakından fotoğraflayın."})
    else:
        notes.append({"tur": "karar", "metin": f"Güven eşiğini ({threshold}) geçen hasar yok; model eşik altında "
                      "bile aday görmedi. Yol yüzeyi sağlam görünüyor."})

    img = report["goruntu"]
    if report["analiz_modu"] != "tam_kare":
        notes.append({"tur": "bilgi", "metin": "Fotoğraf dikey çekilmiş: yolun bulunduğu alt yarı ayrıca yüksek "
                      "çözünürlükte tarandı."})
    if img["isik"] == "dusuk":
        notes.append({"tur": "uyari", "metin": "Işık yetersiz: model ağırlıklı olarak gündüz görüntüleriyle "
                      "eğitildi, sonuç güvenilirliği düşebilir."})
    if min(img["genislik"], img["yukseklik"]) < 480:
        notes.append({"tur": "uyari", "metin": "Çözünürlük düşük: küçük çatlaklar kaçırılabilir."})

    ctx = report["konum_baglami"]
    if not ctx.get("veri_kapsaminda", True):
        notes.append({"tur": "uyari", "metin": "Konum, yol ağı verisinin kapsadığı Bursa merkez alanının dışında: "
                      "yol sınıfı bilinmiyor, ara sokak kabul edildi."})
    else:
        road = ROAD_TR.get(ctx["yol_sinifi"], ctx["yol_sinifi"])
        name = f"{ctx['yol_adi']} ({road})" if ctx.get("yol_adi") else road
        notes.append({"tur": "bilgi", "metin": f"Konum: {name}, yol katsayısı {ctx['yol_katsayisi']}."})
    for t in ctx.get("yakin_kritik_tesisler", [])[:2]:
        notes.append({"tur": "bilgi", "metin": f"{t['mesafe_m']} m yakında {t['tur']}"
                      + (f" ({t['ad']})" if t.get("ad") else "") + ": öncelik artırıldı."})
    if not found:
        notes.append({"tur": "bilgi", "metin": f"Bu noktada bir hasar bulunsaydı konum katsayısı "
                      f"{ctx['konum_katsayisi']} ile önceliklendirilirdi."})
    else:
        sk = (issue.get("context") or {}).get("skor", {})
        notes.append({"tur": "karar", "metin": f"Öncelik {issue['priority']} ({issue['level']}): şiddet {sk.get('siddet')}"
                      f" × konum {sk.get('konum_katsayisi')} × bildirim {sk.get('bildirim_carpani')} × 100. "
                      f"İş emri: {issue['work_order_no']}."})
        if issue["level"] == "kritik":
            notes.append({"tur": "uyari", "metin": "Kritik öncelik: iş kuyruğunun en üstüne alındı."})
    return notes
