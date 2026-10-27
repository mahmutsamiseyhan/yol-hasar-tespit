"""FastAPI servisi: yol hasarı analizi, arıza listesi (GeoJSON) ve harita arayüzü.

Herkese açık: vatandaş bildirim sayfası (/bildir), takip sayfası (/takip), analiz uç noktası.
Belediye personeline özel (yönetici girişi): iş takip paneli, arıza listesi, durum değişikliği, görseller,
istatistik ve dışa aktarma.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import COOKIE_NAME, Auth
from .config import CLASS_LABELS_TR, ROOT_DIR, Settings, get_settings
from .context import RoadContext
from .detector import YoloDetector
from .events import EventBus, report_event
from .export import STATUS_TR, export_csv, export_geojson
from .geo import feature_collection
from .labeling import LabelStore, create_router
from .ratelimit import RateLimiter
from .pipeline import AnalysisError, RoadDamagePipeline, issue_to_feature
from .privacy import build_default_masker
from .repository import ALL_STATUSES, TR_TZ, Repository

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MEDIA_NAME = re.compile(r"[0-9a-f]{32}\.jpg")
STAFF_SOURCES = ("saha_ekibi", "saha_araci")


def build_pipeline(settings: Settings) -> RoadDamagePipeline:
    return RoadDamagePipeline(
        settings=settings,
        detector=YoloDetector(settings.model_path, settings.candidate_conf, settings.iou_threshold, settings.imgsz),
        masker=build_default_masker(ROOT_DIR / "models", settings.privacy_model_path),
        context=RoadContext.from_file(
            settings.context_path,
            districts_path=settings.districts_path,
            road_weights=settings.road_weights,
            poi_radius_m=settings.poi_radius_m,
            poi_bonus=settings.poi_bonus,
        ),
        repo=Repository(settings.db_path),
    )


class StatusUpdate(BaseModel):
    durum: str
    aciklama: str | None = Field(None, max_length=500)


class Login(BaseModel):
    kullanici: str = Field(max_length=100)
    sifre: str = Field(max_length=200)


def create_app(pipeline: RoadDamagePipeline | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.pipeline is None:
            app.state.pipeline = build_pipeline(get_settings())
        purged = app.state.pipeline.purge_expired_media()
        if purged:
            log.info("KVKK saklama süresi dolan %d görsel silindi.", purged)
        filled = app.state.pipeline.fill_missing_districts()
        if filled:
            log.info("%d eski kaydın ilçesi dolduruldu.", filled)
        yield

    app = FastAPI(
        title="Bursa Yol Hasarı Karar Destek Sistemi",
        description="Görüntüden yol hasarı tespiti, önceliklendirme ve CBS uyumlu iş emri üretimi (PoC).",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.pipeline = pipeline
    app.state.events = EventBus()
    settings = pipeline.settings if pipeline else get_settings()
    auth = Auth(settings.admin_users, settings.secret_key, settings.session_hours)
    app.state.auth = auth
    if not auth.enabled:
        log.warning("Yönetici girişi KAPALI (ADMIN_PASSWORD tanımlı değil): panel herkese açık. "
                    "İnternete açık kurulumda mutlaka şifre tanımlayın.")
    # İnternete açık kurulumda kötüye kullanımı sınırlamak için IP başına dakikalık istek sınırı
    upload_limit = RateLimiter(settings.upload_limit_per_min)
    write_limit = RateLimiter(settings.write_limit_per_min)
    login_limit = RateLimiter(settings.login_limit_per_min)
    track_limit = RateLimiter(30)

    def guard(limiter: RateLimiter, request: Request) -> None:
        ip = request.client.host if request.client else "?"
        if not limiter.allow(ip):
            raise HTTPException(429, "Çok fazla istek. Lütfen biraz bekleyip tekrar deneyin.",
                                headers={"Retry-After": str(limiter.retry_after(ip))})

    def p() -> RoadDamagePipeline:
        return app.state.pipeline

    def current_user(request: Request) -> str | None:
        return auth.user_from(request.cookies.get(COOKIE_NAME))

    def staff(request: Request) -> str:
        """Personel uç noktaları. Giriş kapalıysa (yerel geliştirme) herkes 'yonetici' sayılır."""
        if not auth.enabled:
            return "yonetici"
        user = current_user(request)
        if not user:
            raise HTTPException(401, "Bu işlem için belediye personeli girişi gerekli.")
        if request.method not in ("GET", "HEAD"):
            # Başka bir siteden tetiklenen isteklere karşı (çerez SameSite=Strict, bu ikinci savunma)
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                raise HTTPException(403, "İstek kaynağı doğrulanamadı.")
        return user

    def page_for_staff(request: Request, file: str):
        if auth.enabled and not current_user(request):
            return RedirectResponse(f"/giris?sonra={quote(request.url.path)}", status_code=303)
        return FileResponse(STATIC_DIR / file, headers={"Cache-Control": "no-store"})

    # ---------- Giriş ----------
    @app.get("/", include_in_schema=False)
    def index(request: Request):
        return page_for_staff(request, "index.html")

    @app.get("/giris", include_in_schema=False)
    def login_page(request: Request):
        if not auth.enabled or current_user(request):
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC_DIR / "giris.html")

    @app.post("/api/giris", tags=["oturum"])
    def login(body: Login, request: Request):
        guard(login_limit, request)
        if not auth.enabled:
            return {"kullanici": "yonetici", "giris_gerekli": False}
        user = body.kullanici.strip()
        if not auth.check(user, body.sifre):
            log.warning("Başarısız giriş denemesi: %r (%s)", user[:40], request.client.host if request.client else "?")
            raise HTTPException(401, "Kullanıcı adı veya şifre hatalı.")
        resp = JSONResponse({"kullanici": user, "giris_gerekli": True})
        resp.set_cookie(COOKIE_NAME, auth.issue(user), max_age=auth.ttl_s, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")
        log.info("Giriş: %s", user)
        return resp

    @app.post("/api/cikis", tags=["oturum"])
    def logout():
        resp = JSONResponse({"cikis": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/api/oturum", tags=["oturum"])
    def session(request: Request):
        return {"giris_gerekli": auth.enabled, "kullanici": current_user(request) if auth.enabled else "yonetici"}

    @app.get("/health")
    def health():
        pl = p()
        detector = pl.detector
        return {
            "durum": "ok",
            "model_yuklu": getattr(detector, "available", True),
            "model": Path(str(getattr(detector, "model_path", ""))).name,
            "kvkk_dedektorleri": [d.name for d in pl.masker.detectors],
            "konum_baglami_yuklu": pl.context.loaded,
            "yonetici_girisi": auth.enabled,
            "canli_baglanti": app.state.events.subscriber_count,
            "genel_adres": pl.settings.public_url,
        }

    # ---------- Herkese açık: bildirim ve takip ----------
    @app.post("/api/analyze-road")
    async def analyze_road(
        request: Request,
        image: UploadFile = File(..., description="Yol fotoğrafı (JPEG/PNG)"),
        lat: float | None = Form(None, description="Enlem (boşsa EXIF'ten okunur)"),
        lon: float | None = Form(None, description="Boylam (boşsa EXIF'ten okunur)"),
        source: str = Form("vatandas", description="vatandas | saha_ekibi | saha_araci (son ikisi giriş ister)"),
        konum_dogrulugu_m: float | None = Form(None, description="Telefon GPS doğruluğu (metre)"),
    ):
        guard(upload_limit, request)
        if source in STAFF_SOURCES and auth.enabled and not current_user(request):
            raise HTTPException(403, "Saha ekibi bildirimi için belediye personeli girişi gerekli.")
        data = await image.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Dosya 15 MB sınırını aşıyor.")
        pl = p()
        try:
            # Analiz birkaç saniye sürebilir; olay döngüsünü kilitlememek için ayrı iş parçacığında çalışır
            result = await run_in_threadpool(pl.analyze, data, lat, lon, source, konum_dogrulugu_m)
        except AnalysisError as e:
            raise HTTPException(422, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(503, str(e)) from e
        app.state.events.publish(report_event(result, source))
        return result

    @app.get("/bildir", include_in_schema=False)
    def mobile_report_page():
        return FileResponse(STATIC_DIR / "bildir.html")

    @app.get("/takip", include_in_schema=False)
    def tracking_page():
        return FileResponse(STATIC_DIR / "takip.html")

    def tracked(kod: str, request: Request) -> tuple[dict, dict]:
        guard(track_limit, request)  # kod tahminini zorlaştırır
        found = p().repo.find_by_tracking_code(kod)
        if not found:
            raise HTTPException(404, "Bu takip koduyla bir bildirim bulunamadı. Kodu kontrol edin.")
        return found

    @app.get("/api/takip/{kod}", tags=["takip"])
    def track(kod: str, request: Request):
        """Vatandaşın bildirim takibi. Yalnızca bildirene verilen kodla erişilir; başka bildirimlerin bilgisi dönmez."""
        report, issue = tracked(kod, request)
        events = p().repo.list_events(issue["id"])
        first = lambda s: next((e["created_at"] for e in events if e["status"] == s), None)  # noqa: E731
        status = issue["status"]
        steps = [{"ad": "Bildirim alındı", "tarih": report["created_at"], "tamam": True}]
        if status == "yanlis_tespit":
            steps.append({"ad": "İncelendi: yol hasarı doğrulanmadı", "tarih": first("yanlis_tespit"), "tamam": True})
        else:
            steps += [
                {"ad": "Ekip yönlendirildi", "tarih": first("ekip_yonlendirildi"),
                 "tamam": status in ("ekip_yonlendirildi", "onarildi")},
                {"ad": "Onarıldı", "tarih": issue.get("repaired_at"), "tamam": status == "onarildi"},
            ]
        ctx = issue.get("context", {})
        return {
            "takip_kodu": report["tracking_code"],
            "is_emri_no": issue["work_order_no"],
            "hasar_adi": CLASS_LABELS_TR.get(issue["damage_class"], issue["damage_class"]),
            "durum": status,
            "durum_adi": STATUS_TR.get(status, status),
            "adimlar": steps,
            "bildirim_sayisi": issue["report_count"],
            "yol_adi": ctx.get("yol_adi"),
            "ilce": issue.get("district"),
            "konum": {"enlem": report["lat"], "boylam": report["lon"]},
            "onarim_goruntu_url": f"/api/takip/{report['tracking_code']}/onarim.jpg"
            if status == "onarildi" and issue.get("repair_image") else None,
        }

    @app.get("/api/takip/{kod}/onarim.jpg", include_in_schema=False)
    def tracked_repair_photo(kod: str, request: Request):
        _, issue = tracked(kod, request)
        path = p().settings.media_dir / (issue.get("repair_image") or "")
        if issue["status"] != "onarildi" or not issue.get("repair_image") or not path.exists():
            raise HTTPException(404, "Onarım fotoğrafı yok.")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/qr", include_in_schema=False)
    def qr(metin: str):
        """Mobil bildirim sayfasının adresi için QR kod (SVG)."""
        if len(metin) > 500:
            raise HTTPException(422, "Metin çok uzun.")
        import qrcode
        import qrcode.image.svg

        svg = qrcode.make(metin, image_factory=qrcode.image.svg.SvgPathImage, border=2).to_string(encoding="unicode")
        return Response(svg, media_type="image/svg+xml")

    # ---------- Personele özel ----------
    @app.get("/api/olaylar", include_in_schema=False)
    async def events(request: Request, _: str = Depends(staff)):
        """Gerçek zamanlı olay akışı (Server-Sent Events): panel yeni bildirimleri anında görür."""
        return StreamingResponse(app.state.events.stream(request.is_disconnected), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def filtered(durum: str | None, sinif: str | None, ilce: str | None, min_oncelik: int = 0) -> dict:
        statuses = [s for s in (durum or "").split(",") if s]
        if any(s not in ALL_STATUSES for s in statuses):
            raise HTTPException(422, f"Geçersiz durum. Geçerli değerler: {', '.join(ALL_STATUSES)}")
        if sinif and sinif not in CLASS_LABELS_TR:
            raise HTTPException(422, f"Geçersiz hasar sınıfı. Geçerli değerler: {', '.join(CLASS_LABELS_TR)}")
        issues = p().repo.list_issues(statuses=statuses or None, damage_class=sinif, district=ilce,
                                      min_priority=min_oncelik)
        return feature_collection([issue_to_feature(i) for i in issues])

    @app.get("/api/issues")
    def list_issues(durum: str | None = None, sinif: str | None = None, ilce: str | None = None,
                    min_oncelik: int = 0, _: str = Depends(staff)):
        """Filtreler: durum (virgülle birden çok), sinif (D00/D10/D20/D40), ilce, min_oncelik."""
        return filtered(durum, sinif, ilce, min_oncelik)

    @app.get("/api/issues/{issue_id}")
    def get_issue(issue_id: int, _: str = Depends(staff)):
        issue = p().repo.get_issue(issue_id)
        if not issue:
            raise HTTPException(404, "Arıza bulunamadı.")
        return {"ariza": issue_to_feature(issue), "bildirimler": p().repo.list_reports(issue_id),
                "gecmis": p().repo.list_events(issue_id)}

    def publish_status(feature: dict, user: str) -> None:
        props = feature["properties"]
        app.state.events.publish({"tip": "durum", "id": props["id"], "durum": props["durum"],
                                  "is_emri_no": props["is_emri_no"], "kullanici": user})

    @app.patch("/api/issues/{issue_id}")
    def update_status(issue_id: int, body: StatusUpdate, request: Request, user: str = Depends(staff)):
        """Saha ekibinin geri bildirimi. 'yanlis_tespit' kayıtları modelin yeniden eğitiminde kullanılır."""
        guard(write_limit, request)
        if body.durum not in ALL_STATUSES:
            raise HTTPException(422, f"Geçersiz durum. Geçerli değerler: {', '.join(ALL_STATUSES)}")
        if not p().repo.get_issue(issue_id):
            raise HTTPException(404, "Arıza bulunamadı.")
        feature = issue_to_feature(p().repo.set_status(issue_id, body.durum, user, body.aciklama))
        publish_status(feature, user)
        return feature

    @app.post("/api/issues/{issue_id}/onarim")
    async def record_repair(
        issue_id: int,
        request: Request,
        image: UploadFile = File(..., description="Onarılmış yolun fotoğrafı"),
        lat: float | None = Form(None, description="Fotoğrafın çekildiği konum (varsa)"),
        lon: float | None = Form(None),
        user: str = Depends(staff),
    ):
        """Onarım kanıtı: fotoğraf maskelenip saklanır, model hasar kalıp kalmadığını kontrol eder, kayıt kapanır."""
        guard(write_limit, request)
        if not p().repo.get_issue(issue_id):
            raise HTTPException(404, "Arıza bulunamadı.")
        data = await image.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Dosya 15 MB sınırını aşıyor.")
        try:
            result = await run_in_threadpool(p().record_repair, issue_id, data, user, lat, lon)
        except AnalysisError as e:
            raise HTTPException(422, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(503, str(e)) from e
        publish_status(result["ariza"], user)
        return result

    @app.get("/media/{name}", include_in_schema=False)
    def media(name: str, _: str = Depends(staff)):
        path = p().settings.media_dir / name
        if not MEDIA_NAME.fullmatch(name) or not path.exists():
            raise HTTPException(404, "Görsel bulunamadı.")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/stats")
    def stats(_: str = Depends(staff)):
        return p().repo.stats()

    @app.get("/api/istatistik")
    def dashboard_stats(gun: int = 30, sinif: str | None = None, ilce: str | None = None,
                        _: str = Depends(staff)):
        """Grafikler için: günlük bildirim ve onarım, dağılımlar, ortalama müdahale süresi."""
        return p().repo.dashboard_stats(days=max(7, min(gun, 365)), damage_class=sinif, district=ilce)

    @app.get("/api/ilceler")
    def districts(_: str = Depends(staff)):
        loaded = p().context.districts.names if p().context.districts else []
        return {"ilceler": sorted(set(loaded) | set(p().repo.districts()), key=tr_sort_key)}

    @app.get("/api/disa-aktar")
    def export(bicim: str = "geojson", durum: str | None = None, sinif: str | None = None,
               ilce: str | None = None, _: str = Depends(staff)):
        """Filtrelenmiş kayıtlar: GeoJSON (Netcad, ArcGIS, QGIS katmanı) ya da CSV (Excel)."""
        if bicim not in ("geojson", "csv"):
            raise HTTPException(422, "Biçim geojson ya da csv olmalı.")
        fc = filtered(durum, sinif, ilce)
        name = f"yol_hasar_{datetime.now(TR_TZ).strftime('%Y%m%d_%H%M')}.{bicim}"
        headers = {"Content-Disposition": f'attachment; filename="{name}"'}
        if bicim == "csv":
            return Response(export_csv(fc), media_type="text/csv; charset=utf-8", headers=headers)
        return JSONResponse(export_geojson(fc), media_type="application/geo+json", headers=headers)

    # Etiket aracı geliştirme içindir; internete açık kurulumda kapatılır (ENABLE_LABELING=0)
    if settings.enable_labeling:
        @app.get("/etiket", include_in_schema=False)
        def labeling_page(request: Request):
            return page_for_staff(request, "etiket.html")

        app.include_router(create_router(LabelStore(settings.label_dir)), dependencies=[Depends(staff)])

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    return app


def tr_sort_key(name: str) -> list[int]:
    """Türkçe alfabe sırası (Python'un varsayılan sıralaması Ç, İ, Ö, Ş, Ü'yü sona atar)."""
    order = "AaBbCcÇçDdEeFfGgĞğHhIıİiJjKkLlMmNnOoÖöPpRrSsŞşTtUuÜüVvYyZz"
    return [order.index(c) if c in order else 1000 + ord(c) for c in name]


app = create_app()
