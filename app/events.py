"""Gerçek zamanlı olay yayını: yeni bildirimler ve durum değişiklikleri bağlı panellere anında iletilir.

Server-Sent Events (SSE) kullanılır: tarayıcıdaki EventSource bağlantısı açık kalır,
sunucu her olayda tek satırlık bir mesaj gönderir. Ek altyapı (Redis, WebSocket sunucusu) gerekmez.
Tek süreçli PoC içindir; birden çok sunucu sürecinde ortak bir mesaj kuyruğu gerekir.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

PING_SECONDS = 15


class EventBus:
    def __init__(self, queue_size: int = 100):
        self._subscribers: set[asyncio.Queue] = set()
        self._queue_size = queue_size

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # yavaş istemci: en eski olayı at, yenisini koy
                q.get_nowait()
                q.put_nowait(event)

    async def stream(self, is_disconnected) -> AsyncIterator[str]:
        q = self.subscribe()
        try:
            yield "retry: 3000\n\n"
            while not await is_disconnected():
                try:
                    event = await asyncio.wait_for(q.get(), timeout=PING_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # proxy ve tünellerin bağlantıyı kapatmaması için
                    continue
                yield f"event: {event.get('tip', 'olay')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            self.unsubscribe(q)


def report_event(result: dict, source: str) -> dict:
    """Analiz sonucundan panellere gidecek kısa olay."""
    konum = result["konum"]
    event = {"tip": "bildirim", "kaynak": source, "enlem": konum["enlem"], "boylam": konum["boylam"],
             "hasar": result["hasar_tespit_edildi"]}
    if result.get("ariza"):
        p = result["ariza"]["properties"]
        event.update({"id": p["id"], "is_emri_no": p["is_emri_no"], "hasar_adi": p["hasar_adi"],
                      "oncelik": p["oncelik_puani"], "seviye": p["oncelik_seviyesi"],
                      "bildirim_sayisi": p["bildirim_sayisi"], "yol": p.get("yol_adi") or p.get("yol_sinifi"),
                      "birlesti": result.get("mevcut_kayitla_birlestirildi", False)})
    return event
