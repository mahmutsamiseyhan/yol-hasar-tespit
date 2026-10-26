"""Arıza ve bildirim kayıtları (SQLite).

PoC için SQLite yeterlidir. Üretimde aynı sorgular PostGIS'e taşınır:
`find_nearby_open` -> ST_DWithin(geom::geography, nokta, 15) + GiST indeksi.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .geo import bbox_around, haversine_m

OPEN_STATUSES = ("acik", "ekip_yonlendirildi")
ALL_STATUSES = (*OPEN_STATUSES, "onarildi", "yanlis_tespit")

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order_no TEXT UNIQUE,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    damage_class  TEXT NOT NULL,
    severity      REAL NOT NULL,
    priority      INTEGER NOT NULL,
    level         TEXT NOT NULL,
    report_count  INTEGER NOT NULL DEFAULT 1,
    context_json  TEXT,
    status        TEXT NOT NULL DEFAULT 'acik',
    image_path    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    notified_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_latlon ON issues(lat, lon);
CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);

CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id        INTEGER REFERENCES issues(id),
    created_at      TEXT NOT NULL,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    source          TEXT NOT NULL,
    severity        REAL NOT NULL,
    detections_json TEXT NOT NULL,
    privacy_json    TEXT,
    image_path      TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_issue ON reports(issue_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository:
    def __init__(self, db_path: Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    @property
    def lock(self) -> threading.RLock:
        """Birleştirme kararının (ara + güncelle) atomik olması için dışarıdan da kullanılır."""
        return self._lock

    def find_nearby_open(self, lat: float, lon: float, radius_m: float) -> dict | None:
        lat_min, lat_max, lon_min, lon_max = bbox_around(lat, lon, radius_m)
        placeholders = ",".join("?" * len(OPEN_STATUSES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM issues WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
                f"AND status IN ({placeholders})",
                (lat_min, lat_max, lon_min, lon_max, *OPEN_STATUSES),
            ).fetchall()
        best, best_d = None, radius_m
        for row in rows:
            d = haversine_m(lat, lon, row["lat"], row["lon"])
            if d <= best_d:
                best, best_d = row, d
        return _issue(best) if best else None

    def create_issue(self, *, lat, lon, damage_class, severity, priority, level, context, image_path) -> dict:
        ts = now_iso()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO issues (lat, lon, damage_class, severity, priority, level, context_json, "
                "image_path, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (lat, lon, damage_class, severity, priority, level, json.dumps(context, ensure_ascii=False),
                 image_path, ts, ts),
            )
            issue_id = cur.lastrowid
            work_order_no = f"YH-{datetime.now().year}-{issue_id:06d}"
            self._conn.execute("UPDATE issues SET work_order_no=? WHERE id=?", (work_order_no, issue_id))
            self._conn.commit()
        return self.get_issue(issue_id)

    def update_issue(self, issue_id: int, **fields) -> dict:
        if "context" in fields:
            fields["context_json"] = json.dumps(fields.pop("context"), ensure_ascii=False)
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE issues SET {cols} WHERE id=?", (*fields.values(), issue_id))
            self._conn.commit()
        return self.get_issue(issue_id)

    def add_report(self, *, issue_id, lat, lon, source, severity, detections, privacy, image_path) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reports (issue_id, created_at, lat, lon, source, severity, detections_json, "
                "privacy_json, image_path) VALUES (?,?,?,?,?,?,?,?,?)",
                (issue_id, now_iso(), lat, lon, source, severity, json.dumps(detections),
                 json.dumps(privacy, ensure_ascii=False), image_path),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_issue(self, issue_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        return _issue(row) if row else None

    def list_issues(self, status: str | None = None, min_priority: int = 0) -> list[dict]:
        sql, args = "SELECT * FROM issues WHERE priority >= ?", [min_priority]
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY priority DESC, report_count DESC, updated_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_issue(r) for r in rows]

    def list_reports(self, issue_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reports WHERE issue_id=? ORDER BY created_at", (issue_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detections"] = json.loads(d.pop("detections_json"))
            d["privacy"] = json.loads(d.pop("privacy_json") or "null")
            out.append(d)
        return out

    def stats(self) -> dict:
        with self._lock:
            by_status = dict(self._conn.execute("SELECT status, COUNT(*) FROM issues GROUP BY status").fetchall())
            by_level = dict(
                self._conn.execute(
                    "SELECT level, COUNT(*) FROM issues WHERE status IN (?,?) GROUP BY level", OPEN_STATUSES
                ).fetchall()
            )
            reports = self._conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        return {"durum": by_status, "acik_oncelik_seviyeleri": by_level, "toplam_bildirim": reports}

    def expired_media(self, retention_days: int) -> list[str]:
        """Kapatılmış ve saklama süresi dolmuş kayıtların görsel yolları (KVKK saklama politikası)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.id, r.image_path FROM reports r JOIN issues i ON i.id = r.issue_id "
                "WHERE i.status IN ('onarildi','yanlis_tespit') AND i.updated_at < ? AND r.image_path IS NOT NULL",
                (cutoff,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self._conn.execute(
                    f"UPDATE reports SET image_path=NULL WHERE id IN ({','.join('?' * len(ids))})", ids
                )
                self._conn.execute(
                    "UPDATE issues SET image_path=NULL "
                    "WHERE status IN ('onarildi','yanlis_tespit') AND updated_at < ?",
                    (cutoff,),
                )
                self._conn.commit()
        return [r["image_path"] for r in rows]


def _issue(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["context"] = json.loads(d.pop("context_json") or "{}")
    return d
