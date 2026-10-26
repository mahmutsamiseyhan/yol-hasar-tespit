"""Yerel etiket düzeltme aracı: modelin ön-etiketlerini gözden geçirip düzeltmek için.

Etiketler çokgen olarak saklanır; hasarın şekli köşe köşe işaretlenebilir.
Dosya biçimi YOLO segmentasyon formatıdır (Ultralytics):
    <sınıf> x1 y1 x2 y2 ... xn yn        (koordinatlar 0-1 arası, en az 3 köşe)
Bu biçim hem segmentasyon modeli eğitiminde hem de (çokgenin çevreleyen kutusuna
çevrilerek) nesne tespiti değerlendirmesinde doğrudan kullanılabilir.
Eski kutu satırları (<sınıf> x y w h) okunurken 4 köşeli çokgene çevrilir.

Klasör yapısı (LABEL_DIR):
    <ad>.jpg           görüntü
    <ad>.txt           güncel etiket
    _model/<ad>.txt    modelin ilk tahmini ("modelin kutularına dön" için)
    _durum.json        her görüntünün inceleme durumu

Görüntüler bilgisayardan çıkmaz; hesap veya internet gerekmez.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

CLASSES = ["D00", "D10", "D20", "D40"]
STATUSES = ("tamam", "hasarsiz", "kapsam_disi")
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
MAX_POINTS = 200


class Shape(BaseModel):
    sinif: str
    noktalar: list[list[float]] = Field(description="Köşeler [[x, y], ...], 0-1 arası, en az 3")

    @field_validator("noktalar")
    @classmethod
    def _valid_points(cls, pts):
        if not 3 <= len(pts) <= MAX_POINTS:
            raise ValueError(f"Çokgen 3 ile {MAX_POINTS} arasında köşe içermeli.")
        for p in pts:
            if len(p) != 2 or not all(0 <= v <= 1 for v in p):
                raise ValueError("Her köşe [x, y] biçiminde ve 0-1 arasında olmalı.")
        if polygon_area(pts) <= 0:
            raise ValueError("Çokgenin alanı sıfır olamaz.")
        return pts


class LabelUpdate(BaseModel):
    durum: str
    sekiller: list[Shape] = []


def polygon_area(pts: list[list[float]]) -> float:
    n = len(pts)
    return abs(sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))) / 2


def bounding_box(pts: list[list[float]]) -> dict:
    """Çokgeni çevreleyen kutu (merkez x, y, genişlik, yükseklik)."""
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return {"x": (min(xs) + max(xs)) / 2, "y": (min(ys) + max(ys)) / 2, "w": max(xs) - min(xs), "h": max(ys) - min(ys)}


class LabelStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.root.is_dir()

    def images(self) -> list[str]:
        if not self.available:
            return []
        return sorted(p.name for p in self.root.iterdir() if p.suffix.lower() in IMAGE_EXTS)

    def image_path(self, name: str) -> Path:
        # Sadece klasörde gerçekten bulunan dosya adları kabul edilir (yol enjeksiyonuna karşı)
        if name not in self.images():
            raise HTTPException(404, "Görüntü bulunamadı.")
        return self.root / name

    def read(self, name: str) -> dict:
        path = self.image_path(name)
        return {
            "ad": name,
            "durum": self._states().get(name, {}).get("durum"),
            "sekiller": read_shapes(path.with_suffix(".txt")),
        }

    def write(self, name: str, update: LabelUpdate) -> dict:
        if update.durum not in STATUSES:
            raise HTTPException(422, f"Geçersiz durum. Geçerli değerler: {', '.join(STATUSES)}")
        unknown = {s.sinif for s in update.sekiller} - set(CLASSES)
        if unknown:
            raise HTTPException(422, f"Geçersiz sınıf: {', '.join(sorted(unknown))}")
        if update.durum == "tamam" and not update.sekiller:
            raise HTTPException(422, "Şekil yoksa 'hasarsiz' olarak kaydedin.")
        path = self.image_path(name)
        shapes = update.sekiller if update.durum == "tamam" else []
        with self._lock:
            path.with_suffix(".txt").write_text("\n".join(format_shape(s.sinif, s.noktalar) for s in shapes),
                                                encoding="utf-8")
            states = self._states()
            states[name] = {"durum": update.durum,
                            "guncelleme": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._save_states(states)
        return self.read(name)

    def reset(self, name: str) -> dict:
        path = self.image_path(name)
        model_txt = self.root / "_model" / f"{path.stem}.txt"
        with self._lock:
            path.with_suffix(".txt").write_text(
                model_txt.read_text(encoding="utf-8") if model_txt.exists() else "", encoding="utf-8"
            )
            states = self._states()
            states.pop(name, None)
            self._save_states(states)
        return self.read(name)

    def summary(self) -> dict:
        states = self._states()
        items = []
        for name in self.images():
            items.append({"ad": name, "durum": states.get(name, {}).get("durum"),
                          "sekil_sayisi": len(read_shapes((self.root / name).with_suffix(".txt")))})
        counts = {s: sum(i["durum"] == s for i in items) for s in STATUSES}
        counts["bekleyen"] = sum(i["durum"] is None for i in items)
        return {"gorseller": items, "ozet": counts, "siniflar": CLASSES}

    def _states(self) -> dict:
        p = self.root / "_durum.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _save_states(self, states: dict) -> None:
        p = self.root / "_durum.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(states, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)


def format_shape(sinif: str, pts: list[list[float]]) -> str:
    return f"{CLASSES.index(sinif)} " + " ".join(f"{x:.6f} {y:.6f}" for x, y in pts)


def read_shapes(path: Path) -> list[dict]:
    if not path.exists():
        return []
    shapes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        cls, vals = CLASSES[int(parts[0])], [float(v) for v in parts[1:]]
        if len(vals) == 4:  # kutu: x_merkez y_merkez genişlik yükseklik
            x, y, w, h = vals
            pts = [[x - w / 2, y - h / 2], [x + w / 2, y - h / 2], [x + w / 2, y + h / 2], [x - w / 2, y + h / 2]]
        else:
            pts = [[vals[i], vals[i + 1]] for i in range(0, len(vals) - 1, 2)]
        shapes.append({"sinif": cls, "noktalar": [[round(max(0.0, min(1.0, v)), 6) for v in p] for p in pts]})
    return shapes


def create_router(store: LabelStore) -> APIRouter:
    router = APIRouter(prefix="/api/etiket", tags=["etiket"])

    @router.get("")
    def list_images():
        if not store.available:
            raise HTTPException(404, f"Etiket klasörü yok: {store.root}")
        return store.summary()

    @router.get("/{name}")
    def get_labels(name: str):
        return store.read(name)

    @router.get("/{name}/gorsel", include_in_schema=False)
    def get_image(name: str):
        return FileResponse(store.image_path(name))

    @router.put("/{name}")
    def put_labels(name: str, body: LabelUpdate):
        return store.write(name, body)

    @router.post("/{name}/sifirla")
    def reset_labels(name: str):
        return store.reset(name)

    return router
