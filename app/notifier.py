"""Kritik arızalar için iş emri bildirimi (n8n / herhangi bir webhook)."""

from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)


def build_work_order(issue: dict, feature: dict) -> dict:
    ctx = issue.get("context", {})
    return {
        "olay": "kritik_yol_hasari",
        "is_emri_no": issue["work_order_no"],
        "birim": "Fen İşleri Dairesi Başkanlığı - Yol Bakım",
        "oncelik_puani": issue["priority"],
        "oncelik_seviyesi": issue["level"],
        "hasar_sinifi": issue["damage_class"],
        "bildirim_sayisi": issue["report_count"],
        "yol": ctx.get("yol_adi") or ctx.get("yol_sinifi"),
        "konum": {"enlem": issue["lat"], "boylam": issue["lon"]},
        "harita_linki": f"https://www.openstreetmap.org/?mlat={issue['lat']}&mlon={issue['lon']}#map=19/{issue['lat']}/{issue['lon']}",
        "geojson": feature,
    }


def send_webhook(url: str, payload: dict, timeout: float = 5.0) -> bool:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
    except Exception:
        log.exception("Webhook gönderilemedi: %s", url)
        return False
    if not ok:
        log.warning("Webhook başarısız yanıt döndü: %s", url)
    return ok
