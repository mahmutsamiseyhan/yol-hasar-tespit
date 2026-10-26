"""Yerel etiket düzeltme aracı: modelin ön-etiketlerini gözden geçirip düzeltmek için.

Klasör yapısı (LABEL_DIR):
    <ad>.jpg           görüntü
    <ad>.txt           güncel etiket (YOLO: sınıf x_merkez y_merkez genişlik yükseklik, 0-1)
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
from pydantic import BaseModel, Field

CLASSES = ["D00", "D10", "D20", "D40"]
STATUSES = ("tamam", "hasarsiz", "kapsam_disi")
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}


class Box(BaseModel):
    sinif: str
    x: float = Field(ge=0, le=1, description="Kutu merkezi x (0-1)")
    y: float = Field(ge=0, le=1, description="Kutu merkezi y (0-1)")
    w: float = Field(gt=0, le=1, description="Genişlik (0-1)")
    h: float = Field(gt=0, le=1, description="Yükseklik (0-1)")


class LabelUpdate(BaseModel):
    durum: str
    kutular: list[Box] = []


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
            "kutular": _read_yolo(path.with_suffix(".txt")),
            "model_kutulari": _read_yolo(self.root / "_model" / f"{path.stem}.txt"),
        }

    def write(self, name: str, update: LabelUpdate) -> dict:
        if update.durum not in STATUSES:
            raise HTTPException(422, f"Geçersiz durum. Geçerli değerler: {', '.join(STATUSES)}")
        unknown = {b.sinif for b in update.kutular} - set(CLASSES)
        if unknown:
            raise HTTPException(422, f"Geçersiz sınıf: {', '.join(sorted(unknown))}")
        if update.durum == "tamam" and not update.kutular:
            raise HTTPException(422, "Kutu yoksa 'hasarsiz' olarak kaydedin.")
        path = self.image_path(name)
        boxes = update.kutular if update.durum == "tamam" else []
        lines = [f"{CLASSES.index(b.sinif)} {b.x:.6f} {b.y:.6f} {b.w:.6f} {b.h:.6f}" for b in boxes]
        with self._lock:
            path.with_suffix(".txt").write_text("\n".join(lines), encoding="utf-8")
            states = self._states()
            states[name] = {"durum": update.durum, "guncelleme": datetime.now(timezone.utc).isoformat(timespec="seconds")}
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
            txt = (self.root / name).with_suffix(".txt")
            items.append({"ad": name, "durum": states.get(name, {}).get("durum"),
                          "kutu_sayisi": len(_read_yolo(txt))})
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


def _read_yolo(path: Path) -> list[dict]:
    if not path.exists():
        return []
    boxes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 5:
            c, x, y, w, h = int(parts[0]), *map(float, parts[1:])
            boxes.append({"sinif": CLASSES[c], "x": x, "y": y, "w": w, "h": h})
    return boxes


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
