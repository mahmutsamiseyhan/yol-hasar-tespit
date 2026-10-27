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
from .geo import exif_gps, feature, haversine_m
from .privacy import PrivacyMasker
from .repository import Repository
from .severity import image_severity, priority_score


# saha_ekibi: belediye personelinin telefonu; saha_araci: kalibre kameralı araç (homografi kullanılır)
SOURCES = ("vatandas", "saha_ekibi", "saha_araci")
MAX_CANDIDATES = 5
LOW_LIGHT = 70  # ortalama parlaklık (0-255) bunun altındaysa ışık yetersiz sayılır
PREVIEW_MAX_SIDE = 1280
# Onarım fotoğrafı arıza konumundan bu kadar uzaktaysa uyarı verilir (telefon GPS hatası payı dahil)
REPAIR_MAX_DISTANCE_M = 50
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
        settings.training_dir.mkdir(parents=True, exist_ok=True)

    def analyze(self, image_bytes: bytes, lat: float | None = None, lon: float | None = None,
                source: str = "vatandas", accuracy_m: float | None = None) -> dict:
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
        detections = [d for d in found if d.confidence >= self.settings.threshold_for(d.cls)]
        candidates = sorted((d for d in found if d.confidence < self.settings.threshold_for(d.cls)),
                            key=lambda d: d.confidence, reverse=True)
        homography = self.settings.camera_homography if source == "saha_araci" else None
        sev = image_severity(detections, w, h, self.settings.class_weights, homography)

        # Ham görüntü diske hiç yazılmaz: önce maskele, sonra sakla
        masked, mask_report = self.masker.mask(image)
        ctx = self.context.lookup(lat, lon)

        base = {
            "konum": {"enlem": lat, "boylam": lon, "kaynak": location_source,
                      "dogruluk_m": round(accuracy_m) if accuracy_m is not None else None},
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
            "sinif_esikleri": self.settings.class_conf_thresholds or None,
            "aday_esigi": self.settings.candidate_conf,
            "model": Path(str(getattr(self.detector, "model_path", "model"))).name,
            "konum_dogrulugu_m": round(accuracy_m) if accuracy_m is not None else None,
            "birlestirme_yaricapi_m": self.settings.merge_radius_m,
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

        # Eğitim kopyası: maskelenmiş ama kutu çizilmemiş (yönetici düzeltirse eğitim örneği olur)
        clean_name = f"{uuid.uuid4().hex}.jpg"
        cv2.imwrite(str(self.settings.training_dir / clean_name), masked, [cv2.IMWRITE_JPEG_QUALITY, 90])

        dominant: Detection = sev["dominant"]["detection"]
        issue, merged = self._upsert_issue(lat, lon, dominant.cls, sev["severity"], image_name)
        saved = self.repo.add_report(
            issue_id=issue["id"], lat=lat, lon=lon, source=source, severity=sev["severity"],
            detections=[d.to_dict() for d in detections], privacy=mask_report.to_dict(), image_path=image_name,
            clean_image=clean_name, image_w=w, image_h=h,
        )

        report["sure_ms"] = round(1000 * (time.perf_counter() - started))
        return {
            **base,
            "hasar_tespit_edildi": True,
            "bildirim_id": saved["id"],
            # Bildirene özel, tahmin edilemeyen kod: iş emri numarası sıralı olduğu için takipte kullanılmaz
            "takip_kodu": saved["tracking_code"],
            "takip_url": f"/takip?kod={saved['tracking_code']}",
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
                    district=ctx.get("ilce"),
                )
                merged = True
            else:
                ctx = self.context.lookup(lat, lon)
                prio = priority_score(severity, ctx["konum_katsayisi"], 1)
                issue = self.repo.create_issue(
                    lat=lat, lon=lon, damage_class=damage_class, severity=severity, priority=prio["score"],
                    level=prio["level"], context={**ctx, "skor": prio["breakdown"]}, image_path=image_name,
                    district=ctx.get("ilce"),
                )
                merged = False
        return issue, merged

    def record_repair(self, issue_id: int, image_bytes: bytes, actor: str | None, lat: float | None = None,
                      lon: float | None = None) -> dict:
        """Saha ekibinin onarım fotoğrafı: maskelenip saklanır, model hasar kalıp kalmadığını kontrol eder.

        Karar mühendiste kalır: model hâlâ hasar görse de kayıt kapatılır, ama panelde uyarı gösterilir.
        """
        issue = self.repo.get_issue(issue_id)
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise AnalysisError("Görüntü okunamadı. JPEG veya PNG yükleyin.")
        remaining = [d for d in self.detector.detect(image) if d.confidence >= self.settings.threshold_for(d.cls)]
        masked, mask_report = self.masker.mask(image)
        annotated = _annotate(masked, [{"detection": d} for d in remaining])
        image_name = f"{uuid.uuid4().hex}.jpg"
        cv2.imwrite(str(self.settings.media_dir / image_name), annotated, [cv2.IMWRITE_JPEG_QUALITY, 88])

        distance = round(haversine_m(lat, lon, issue["lat"], issue["lon"])) if lat is not None and lon is not None else None
        notes = []
        if remaining:
            top = max(remaining, key=lambda d: d.confidence)
            notes.append(f"Onarım fotoğrafında model hâlâ {CLASS_LABELS_TR.get(top.cls, top.cls)} görüyor "
                         f"(%{round(100 * top.confidence)} güven). Yerinde kontrol önerilir.")
        else:
            notes.append("Onarım fotoğrafında model hasar görmedi.")
        if distance is not None and distance > REPAIR_MAX_DISTANCE_M:
            notes.append(f"Fotoğraf konumu arıza kaydından {distance} m uzakta. Doğru noktanın fotoğraflandığını "
                         "kontrol edin.")
        check = {"sonuc": "hasar_suphesi" if remaining else "temiz",
                 "kalan_hasar": [_candidate_out(d) for d in remaining], "mesafe_m": distance,
                 "maskelenen_bolge": mask_report.to_dict()["maskelenen_bolge"], "notlar": notes}
        issue = self.repo.set_status(issue_id, "onarildi", actor, note=" ".join(notes),
                                     repair_image=image_name, repair_check=check)
        self._feedback_for_status(issue_id, "onarildi", actor)
        return {"ariza": issue_to_feature(issue), "kontrol": check, "onizleme": _data_uri(annotated)}

    def correct_class(self, issue_id: int, new_class: str, actor: str | None) -> dict:
        """Yöneticinin hasar türü düzeltmesi: şiddet sınıf ağırlığı oranında güncellenir, öncelik yeniden hesaplanır."""
        issue = self.repo.get_issue(issue_id)
        old_class = issue["damage_class"]
        if new_class == old_class:
            return issue
        weights = self.settings.class_weights
        severity = min(1.0, issue["severity"] * weights.get(new_class, 1.0) / max(weights.get(old_class, 1.0), 1e-6))
        ctx = issue.get("context", {})
        prio = priority_score(severity, ctx.get("konum_katsayisi", 0.55), issue["report_count"])
        self.repo.update_issue(issue_id, damage_class=new_class, severity=round(severity, 4), priority=prio["score"],
                               level=prio["level"], context={**ctx, "skor": prio["breakdown"]})
        self.repo.log_event(issue_id, "sinif", actor,
                            f"Hasar türü düzeltildi: {CLASS_LABELS_TR.get(old_class, old_class)} → "
                            f"{CLASS_LABELS_TR.get(new_class, new_class)} (öncelik {issue['priority']} → {prio['score']})")
        # Eğitim örneği: modelin bu türdeki kutuları düzeltilmiş türle etiketlenir
        for r in self.repo.reports_for_training(issue_id):
            labels = [{"sinif": new_class if d["cls"] == old_class else d["cls"], "kutu": d["box"]} for d in r["detections"]]
            self.repo.upsert_sample(report_id=r["id"], issue_id=issue_id, kind="sinif_duzeltme", labels=labels,
                                    meta={"eski": old_class, "yeni": new_class}, actor=actor)
        return self.repo.get_issue(issue_id)

    def change_status(self, issue_id: int, status: str, actor: str | None, note: str | None = None,
                      team: str | None = None) -> dict:
        issue = self.repo.set_status(issue_id, status, actor, note, team=team)
        self._feedback_for_status(issue_id, status, actor)
        return issue

    def _feedback_for_status(self, issue_id: int, status: str, actor: str | None) -> None:
        """Saha kararından eğitim örneği: yanlış tespit = hasarsız örnek, onarıldı = modelin tespiti doğrulandı."""
        if status in ("acik", "ekip_yonlendirildi"):  # yeniden açıldı: bekleyen kararlar geri alınır
            self.repo.drop_pending_samples(issue_id, ("yanlis_tespit", "dogrulandi"))
            return
        existing = {s["report_id"]: s for s in self.repo.samples_for_issue(issue_id)}
        fix = next((s["meta"] for s in existing.values() if s["kind"] == "sinif_duzeltme"), None)
        for r in self.repo.reports_for_training(issue_id):
            if status == "yanlis_tespit":
                self.repo.upsert_sample(report_id=r["id"], issue_id=issue_id, kind="yanlis_tespit", labels=[],
                                        meta={"model": sorted({d["cls"] for d in r["detections"]})}, actor=actor)
            elif status == "onarildi" and existing.get(r["id"], {}).get("kind") != "sinif_duzeltme":
                labels = [{"sinif": fix["yeni"] if fix and d["cls"] == fix["eski"] else d["cls"], "kutu": d["box"]}
                          for d in r["detections"]]
                self.repo.upsert_sample(report_id=r["id"], issue_id=issue_id,
                                        kind="sinif_duzeltme" if fix else "dogrulandi", labels=labels,
                                        meta=fix or {}, actor=actor)

    def delete_issue(self, issue_id: int, actor: str | None, reason: str | None) -> int:
        """Kaydı fotoğraflarıyla birlikte kalıcı siler (ör. yanlışlıkla gönderilen kişisel fotoğraf)."""
        removed = self.repo.delete_issue(issue_id, actor, reason)
        for name in removed["media"]:
            (self.settings.media_dir / name).unlink(missing_ok=True)
        for name in removed["training"]:
            (self.settings.training_dir / name).unlink(missing_ok=True)
        return len(removed["media"]) + len(removed["training"])

    def fill_missing_districts(self) -> int:
        """İlçe sınırları sonradan eklendiyse eski kayıtların ilçesini doldurur."""
        districts = self.context.districts
        n = 0
        for row in self.repo.issues_without_district() if districts else []:
            name = districts.lookup(row["lat"], row["lon"])
            if name:
                self.repo.update_issue(row["id"], district=name)
                n += 1
        return n

    def purge_expired_media(self) -> int:
        paths = self.repo.expired_media(self.settings.retention_days)
        for p in paths:
            (self.settings.media_dir / p).unlink(missing_ok=True)
        training = self.repo.expired_training_images(self.settings.retention_days)
        for p in training:
            (self.settings.training_dir / p).unlink(missing_ok=True)
        return len(paths) + len(training)


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
            "ilce": issue.get("district"),
            "ekip": issue.get("team"),
            "olusturma": issue["created_at"],
            "guncelleme": issue["updated_at"],
            "onarim": {
                "tarih": issue.get("repaired_at"),
                "goruntu_url": f"/media/{issue['repair_image']}" if issue.get("repair_image") else None,
                "kontrol": issue.get("repair_check"),
            } if issue["status"] == "onarildi" else None,
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
    threshold = ("sınıf bazlı güven eşiklerini" if report.get("sinif_esikleri")
                 else f"güven eşiğini ({pct(report['guven_esigi'])})")
    if found:
        top = max(detections, key=lambda d: d.confidence)
        notes.append({"tur": "karar", "metin": f"{len(detections)} hasar tespit edildi. En belirgini: "
                      f"{CLASS_LABELS_TR.get(top.cls, top.cls)} ({pct(top.confidence)} güven)."})
    elif candidates:
        c = candidates[0]
        notes.append({"tur": "karar", "metin": f"{threshold[0].upper() + threshold[1:]} geçen hasar yok. Eşiğin altında "
                      f"{len(candidates)} zayıf aday var (en güçlüsü {CLASS_LABELS_TR.get(c.cls, c.cls)}, "
                      f"{pct(c.confidence)}); kayıt açılmadı. Zayıf adaylar çoğunlukla gölge, yama, çakıl veya "
                      "yol çizgisidir. Şüphe varsa hasarı daha yakından fotoğraflayın."})
    else:
        notes.append({"tur": "karar", "metin": f"{threshold[0].upper() + threshold[1:]} geçen hasar yok; model eşik altında "
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

    acc = report.get("konum_dogrulugu_m")
    if acc is not None and acc > report["birlestirme_yaricapi_m"]:
        notes.append({"tur": "uyari", "metin": f"Konum doğruluğu düşük (±{acc} m): {report['birlestirme_yaricapi_m']:.0f} m "
                      "birleştirme yarıçapından büyük, mükerrer kayıt eşleşmesi hatalı olabilir. Açık havada "
                      "birkaç saniye bekleyip tekrar deneyin."})
    elif acc is not None:
        notes.append({"tur": "bilgi", "metin": f"Telefon konumu ±{acc} m doğrulukla alındı."})

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
