"""Arıza kayıtlarının dışa aktarımı: CBS için GeoJSON (EPSG:4326), Excel için CSV."""

from __future__ import annotations

import csv
import io

from .repository import TR_TZ

STATUS_TR = {"acik": "Açık", "ekip_yonlendirildi": "Ekip yönlendirildi", "onarildi": "Onarıldı",
             "yanlis_tespit": "Yanlış tespit"}
LEVEL_TR = {"kritik": "Kritik", "yuksek": "Yüksek", "orta": "Orta", "dusuk": "Düşük"}
CSV_COLUMNS = [
    ("is_emri_no", "İş emri no"), ("hasar_sinifi", "Hasar sınıfı"), ("hasar_adi", "Hasar"),
    ("oncelik_puani", "Öncelik puanı"), ("oncelik_seviyesi", "Öncelik"), ("durum", "Durum"),
    ("bildirim_sayisi", "Bildirim sayısı"), ("ilce", "İlçe"), ("yol_adi", "Yol"), ("yol_sinifi", "Yol sınıfı"),
    ("enlem", "Enlem"), ("boylam", "Boylam"), ("olusturma", "Oluşturma"), ("onarim_tarihi", "Onarım tarihi"),
    ("onarim_suresi_saat", "Onarım süresi (saat)"), ("onarim_kontrolu", "Onarım fotoğrafı kontrolü"),
]
# Kayıtlarda yalnızca panelde anlamlı olan alanlar dışarı verilmez (sunucu içi görsel bağlantıları)
INTERNAL_FIELDS = ("goruntu_url", "id")


def _local(ts: str | None) -> str:
    from datetime import datetime

    return datetime.fromisoformat(ts).astimezone(TR_TZ).strftime("%Y-%m-%d %H:%M") if ts else ""


def _safe(value) -> str:
    """Excel formül enjeksiyonunu önler: =, +, -, @ ile başlayan metinler formül olarak çalışmasın."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def export_geojson(fc: dict) -> dict:
    features = []
    for f in fc["features"]:
        props = {k: v for k, v in f["properties"].items() if k not in INTERNAL_FIELDS}
        if props.get("onarim"):
            props["onarim"] = {k: v for k, v in props["onarim"].items() if k != "goruntu_url"}
        features.append({**f, "properties": props})
    return {**fc, "features": features}


def export_csv(fc: dict) -> str:
    from datetime import datetime

    buf = io.StringIO()
    buf.write("﻿")  # Excel'in Türkçe karakterleri doğru açması için UTF-8 BOM
    writer = csv.writer(buf, delimiter=";")  # Türkçe Excel'de liste ayırıcı noktalı virgüldür
    writer.writerow([title for _, title in CSV_COLUMNS])
    for f in fc["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        onarim = p.get("onarim") or {}
        hours = ""
        if onarim.get("tarih"):
            delta = datetime.fromisoformat(onarim["tarih"]) - datetime.fromisoformat(p["olusturma"])
            hours = f"{delta.total_seconds() / 3600:.1f}".replace(".", ",")
        check = (onarim.get("kontrol") or {}).get("sonuc")
        row = {**p, "enlem": f"{lat:.6f}".replace(".", ","), "boylam": f"{lon:.6f}".replace(".", ","),
               "durum": STATUS_TR.get(p["durum"], p["durum"]),
               "oncelik_seviyesi": LEVEL_TR.get(p["oncelik_seviyesi"], p["oncelik_seviyesi"]),
               "olusturma": _local(p["olusturma"]), "onarim_tarihi": _local(onarim.get("tarih")),
               "onarim_suresi_saat": hours,
               "onarim_kontrolu": {"temiz": "Hasar görülmedi", "hasar_suphesi": "Hasar şüphesi"}.get(check, "")}
        writer.writerow([_safe(row.get(key)) for key, _ in CSV_COLUMNS])
    return buf.getvalue()
