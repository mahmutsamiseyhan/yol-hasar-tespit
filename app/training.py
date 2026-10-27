"""Saha geri bildiriminden eğitim verisi: değerlendirme önizlemesi ve YOLO formatında eğitim paketi.

Akış: yönetici hasar türünü düzeltir, “Yanlış tespit” der ya da kaydı onarıldı olarak kapatır → eğitim örneği
oluşur (onay bekliyor) → yönetici değerlendirme ekranında onaylar → onaylananlar paket olarak indirilir →
Colab'da 03 notebook'u Drive'daki saha_egitim*.zip dosyalarını eğitim bölümüne ekler. Colab'ın sürekli
çalışması gerekmez: paket birikince ara sıra bir kez eğitilir.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .config import CLASS_LABELS_TR
from .labeling import CLASSES
from .repository import TR_TZ

PREVIEW_MAX_SIDE = 900
KIND_TR = {"sinif_duzeltme": "Hasar türü düzeltildi", "yanlis_tespit": "Yanlış tespit: hasar yok",
           "dogrulandi": "Doğrulandı"}


def sample_title(s: dict) -> str:
    meta = s.get("meta") or {}
    if s["kind"] == "sinif_duzeltme":
        return f"Hasar türü düzeltildi: {CLASS_LABELS_TR.get(meta.get('eski'), meta.get('eski'))} → " \
               f"{CLASS_LABELS_TR.get(meta.get('yeni'), meta.get('yeni'))}"
    if s["kind"] == "yanlis_tespit":
        return "Yanlış tespit: hasar yok (model " + ", ".join(CLASS_LABELS_TR.get(c, c) for c in meta.get("model", [])) + ")"
    names = sorted({CLASS_LABELS_TR.get(l["sinif"], l["sinif"]) for l in s["labels"]})
    return "Doğrulandı: " + ", ".join(names)


def preview_jpeg(image_path: Path, sample: dict) -> bytes:
    """Kırmızı: modelin ilk tahmini. Yeşil: eğitime girecek etiket (yanlış tespitte yeşil yok = hasarsız örnek)."""
    img = cv2.imread(str(image_path))
    if img is None:
        raise FileNotFoundError(image_path)
    t = max(2, round(min(img.shape[:2]) / 250))
    for d in sample["detections"]:
        x1, y1, x2, y2 = (int(v) for v in d["box"])
        cv2.rectangle(img, (x1, y1), (x2, y2), (40, 40, 220), t)
        cv2.putText(img, f"model {d['cls']}", (x1 + 3, max(14, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, t / 3, (40, 40, 220), t)
    for lab in sample["labels"]:
        x1, y1, x2, y2 = (int(v) for v in lab["kutu"])
        cv2.rectangle(img, (x1 + t, y1 + t), (x2 - t, y2 - t), (60, 170, 40), t)
        cv2.putText(img, lab["sinif"], (x1 + 3, min(img.shape[0] - 6, y2 + 14 * t)), cv2.FONT_HERSHEY_SIMPLEX, t / 3,
                    (60, 170, 40), t)
    h, w = img.shape[:2]
    scale = min(1.0, PREVIEW_MAX_SIDE / max(h, w))
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()


def yolo_lines(sample: dict) -> list[str]:
    w, h = sample["image_w"], sample["image_h"]
    lines = []
    for lab in sample["labels"]:
        x1, y1, x2, y2 = lab["kutu"]
        x1, x2 = np.clip([x1, x2], 0, w)
        y1, y2 = np.clip([y1, y2], 0, h)
        if x2 <= x1 or y2 <= y1 or lab["sinif"] not in CLASSES:
            continue
        lines.append(f"{CLASSES.index(lab['sinif'])} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} "
                     f"{(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}")
    return lines


def build_package(samples: list[dict], training_dir: Path) -> bytes:
    """Onaylı örnekler: images/ + labels/ (YOLO) + rapor.json. Türkiye paketiyle aynı yapı (03 notebook'u okur)."""
    buf = io.BytesIO()
    kinds, boxes, used = Counter(), Counter(), 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for s in samples:
            src = training_dir / (s.get("clean_image") or "")
            if not s.get("clean_image") or not src.exists() or not s.get("image_w"):
                continue
            stem = f"saha_{s['id']:06d}"
            z.write(src, f"images/{stem}.jpg")
            lines = yolo_lines(s)
            z.writestr(f"labels/{stem}.txt", "\n".join(lines))  # boş dosya = hasarsız (arka plan) örneği
            kinds[s["kind"]] += 1
            boxes.update(CLASSES[int(l.split()[0])] for l in lines)
            used += 1
        z.writestr("rapor.json", json.dumps({
            "kaynak": "Bursa saha geri bildirimi (yönetici onaylı)",
            "olusturma": datetime.now(TR_TZ).isoformat(timespec="seconds"),
            "ornek_sayisi": used, "kararlar": dict(kinds), "kutular": dict(boxes),
            "aciklama": {k: v for k, v in KIND_TR.items()},
        }, ensure_ascii=False, indent=2))
    return buf.getvalue()
