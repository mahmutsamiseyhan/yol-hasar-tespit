"""Basit istek sınırı: aynı IP'den belirli sürede en fazla N istek (kayan pencere, bellek içi).

İnternete açık demoda tek bir kişinin sunucuyu fotoğrafla boğmasını önler.
Tek süreçli PoC içindir; çok sunuculu kurulumda ters vekil sunucuda (Caddy/Nginx) sınırlama yapılır.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:  # 0: sınırsız
            return True
        now = self._clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def retry_after(self, key: str) -> int:
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return 0
            return max(1, int(self.window_s - (self._clock() - hits[0])) + 1)
