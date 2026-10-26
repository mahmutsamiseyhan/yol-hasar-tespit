"""FastAPI servisi: yol hasarı analizi, arıza listesi (GeoJSON) ve harita arayüzü."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ROOT_DIR, Settings, get_settings
from .context import RoadContext
from .detector import YoloDetector
from .geo import feature_collection
from .labeling import LabelStore, create_router
from .pipeline import AnalysisError, RoadDamagePipeline, issue_to_feature
from .privacy import build_default_masker
from .repository import ALL_STATUSES, Repository

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def build_pipeline(settings: Settings) -> RoadDamagePipeline:
    return RoadDamagePipeline(
        settings=settings,
        detector=YoloDetector(settings.model_path, settings.conf_threshold, settings.iou_threshold, settings.imgsz),
        masker=build_default_masker(ROOT_DIR / "models", settings.privacy_model_path),
        context=RoadContext.from_file(
            settings.context_path,
            road_weights=settings.road_weights,
            poi_radius_m=settings.poi_radius_m,
            poi_bonus=settings.poi_bonus,
        ),
        repo=Repository(settings.db_path),
    )


class StatusUpdate(BaseModel):
    durum: str


def create_app(pipeline: RoadDamagePipeline | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.pipeline is None:
            app.state.pipeline = build_pipeline(get_settings())
        purged = app.state.pipeline.purge_expired_media()
        if purged:
            log.info("KVKK saklama süresi dolan %d görsel silindi.", purged)
        yield

    app = FastAPI(
        title="Bursa Yol Hasarı Karar Destek Sistemi",
        description="Görüntüden yol hasarı tespiti, önceliklendirme ve CBS uyumlu iş emri üretimi (PoC).",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.pipeline = pipeline

    def p() -> RoadDamagePipeline:
        return app.state.pipeline

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    def health():
        pl = p()
        detector = pl.detector
        return {
            "durum": "ok",
            "model_yuklu": getattr(detector, "available", True),
            "model_turu": "segmentasyon" if "seg" in str(getattr(detector, "model_path", "")) else "tespit",
            "model_yolu": str(getattr(detector, "model_path", "")),
            "kvkk_dedektorleri": [d.name for d in pl.masker.detectors],
            "konum_baglami_yuklu": pl.context.loaded,
            "webhook_tanimli": bool(pl.settings.webhook_url),
        }

    @app.post("/api/analyze-road")
    async def analyze_road(
        background: BackgroundTasks,
        image: UploadFile = File(..., description="Yol fotoğrafı (JPEG/PNG)"),
        lat: float | None = Form(None, description="Enlem (boşsa EXIF'ten okunur)"),
        lon: float | None = Form(None, description="Boylam (boşsa EXIF'ten okunur)"),
        source: str = Form("vatandas", description="vatandas | saha_araci"),
    ):
        data = await image.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Dosya 15 MB sınırını aşıyor.")
        pl = p()
        try:
            result = pl.analyze(data, lat=lat, lon=lon, source=source)
        except AnalysisError as e:
            raise HTTPException(422, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(503, str(e)) from e
        if result.get("is_emri_tetiklendi"):
            background.add_task(pl.send_work_order, result["ariza"]["properties"]["id"])
        return result

    @app.get("/api/issues")
    def list_issues(durum: str | None = None, min_oncelik: int = 0):
        if durum and durum not in ALL_STATUSES:
            raise HTTPException(422, f"Geçersiz durum. Geçerli değerler: {', '.join(ALL_STATUSES)}")
        issues = p().repo.list_issues(status=durum, min_priority=min_oncelik)
        return feature_collection([issue_to_feature(i) for i in issues])

    @app.get("/api/issues/{issue_id}")
    def get_issue(issue_id: int):
        issue = p().repo.get_issue(issue_id)
        if not issue:
            raise HTTPException(404, "Arıza bulunamadı.")
        return {"ariza": issue_to_feature(issue), "bildirimler": p().repo.list_reports(issue_id)}

    @app.patch("/api/issues/{issue_id}")
    def update_status(issue_id: int, body: StatusUpdate):
        """Saha ekibinin geri bildirimi. 'yanlis_tespit' kayıtları modelin yeniden eğitiminde kullanılır."""
        if body.durum not in ALL_STATUSES:
            raise HTTPException(422, f"Geçersiz durum. Geçerli değerler: {', '.join(ALL_STATUSES)}")
        if not p().repo.get_issue(issue_id):
            raise HTTPException(404, "Arıza bulunamadı.")
        return issue_to_feature(p().repo.update_issue(issue_id, status=body.durum))

    @app.get("/api/stats")
    def stats():
        return p().repo.stats()

    @app.get("/etiket", include_in_schema=False)
    def labeling_page():
        return FileResponse(STATIC_DIR / "etiket.html")

    settings = pipeline.settings if pipeline else get_settings()
    app.include_router(create_router(LabelStore(settings.label_dir)))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    media_dir = settings.media_dir
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=media_dir), name="media")
    return app


app = create_app()
