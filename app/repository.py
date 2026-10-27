"""Arıza ve bildirim kayıtları (SQLite).

PoC için SQLite yeterlidir. Üretimde aynı sorgular PostGIS'e taşınır:
`find_nearby_open` -> ST_DWithin(geom::geography, nokta, 15) + GiST indeksi.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import statistics
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .geo import bbox_around, haversine_m

OPEN_STATUSES = ("acik", "ekip_yonlendirildi")
CLOSED_STATUSES = ("onarildi", "yanlis_tespit")
ALL_STATUSES = (*OPEN_STATUSES, *CLOSED_STATUSES)
# Türkiye yaz/kış saati uygulamıyor: günlük istatistikler sabit UTC+3 ile gruplanır
TR_TZ = timezone(timedelta(hours=3))
# Takip kodu alfabesi: karıştırılan karakterler (0/O, 1/I/L) yok
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

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
    updated_at    TEXT NOT NULL
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

-- Durum geçmişi: vatandaş takip ekranındaki adımlar ve denetim izi (kim, ne zaman)
CREATE TABLE IF NOT EXISTS issue_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   INTEGER NOT NULL REFERENCES issues(id),
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL,
    actor      TEXT,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_issue ON issue_events(issue_id);
"""

# Eski veritabanlarına eklenen sütunlar (tablo, sütun, tip)
MIGRATIONS = [
    ("issues", "district", "TEXT"),
    ("issues", "repaired_at", "TEXT"),
    ("issues", "repair_image", "TEXT"),
    ("issues", "repair_check_json", "TEXT"),
    ("reports", "tracking_code", "TEXT"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_tracking_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(code: str) -> str:
    raw = "".join(c for c in code.upper() if c.isalnum())
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else code.strip().upper()


class Repository:
    def __init__(self, db_path: Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        for table, column, kind in MIGRATIONS:
            existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_code ON reports(tracking_code)")
        # Geçmişi olmayan eski kayıtlar: açılış ve son durum, bilinen zamanlarla
        for row in self._conn.execute(
            "SELECT * FROM issues WHERE id NOT IN (SELECT DISTINCT issue_id FROM issue_events)"
        ).fetchall():
            self._event(row["id"], "acik", "sistem", at=row["created_at"])
            if row["status"] != "acik":
                self._event(row["id"], row["status"], None, at=row["updated_at"])
            if row["status"] == "onarildi" and not row["repaired_at"]:
                self._conn.execute("UPDATE issues SET repaired_at=? WHERE id=?", (row["updated_at"], row["id"]))
        for row in self._conn.execute("SELECT id FROM reports WHERE tracking_code IS NULL").fetchall():
            self._conn.execute("UPDATE reports SET tracking_code=? WHERE id=?", (new_tracking_code(), row["id"]))
        self._conn.commit()

    @property
    def lock(self) -> threading.RLock:
        """Birleştirme kararının (ara + güncelle) atomik olması için dışarıdan da kullanılır."""
        return self._lock

    def _event(self, issue_id: int, status: str, actor: str | None, note: str | None = None,
               at: str | None = None) -> None:
        self._conn.execute("INSERT INTO issue_events (issue_id, created_at, status, actor, note) VALUES (?,?,?,?,?)",
                           (issue_id, at or now_iso(), status, actor, note))

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

    def create_issue(self, *, lat, lon, damage_class, severity, priority, level, context, image_path,
                     district: str | None = None) -> dict:
        ts = now_iso()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO issues (lat, lon, damage_class, severity, priority, level, context_json, "
                "image_path, district, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (lat, lon, damage_class, severity, priority, level, json.dumps(context, ensure_ascii=False),
                 image_path, district, ts, ts),
            )
            issue_id = cur.lastrowid
            work_order_no = f"YH-{datetime.now().year}-{issue_id:06d}"
            self._conn.execute("UPDATE issues SET work_order_no=? WHERE id=?", (work_order_no, issue_id))
            self._event(issue_id, "acik", "sistem", at=ts)
            self._conn.commit()
        return self.get_issue(issue_id)

    def update_issue(self, issue_id: int, **fields) -> dict:
        if "context" in fields:
            fields["context_json"] = json.dumps(fields.pop("context"), ensure_ascii=False)
        if "repair_check" in fields:
            fields["repair_check_json"] = json.dumps(fields.pop("repair_check"), ensure_ascii=False)
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE issues SET {cols} WHERE id=?", (*fields.values(), issue_id))
            self._conn.commit()
        return self.get_issue(issue_id)

    def set_status(self, issue_id: int, status: str, actor: str | None, note: str | None = None,
                   **fields) -> dict:
        """Durum değişikliği + geçmiş kaydı. Onarıldı dışındaki durumlarda onarım zamanı temizlenir."""
        with self._lock:
            fields["repaired_at"] = now_iso() if status == "onarildi" else None
            self.update_issue(issue_id, status=status, **fields)
            self._event(issue_id, status, actor, note)
            self._conn.commit()
        return self.get_issue(issue_id)

    def add_report(self, *, issue_id, lat, lon, source, severity, detections, privacy, image_path) -> dict:
        with self._lock:
            code = new_tracking_code()
            cur = self._conn.execute(
                "INSERT INTO reports (issue_id, created_at, lat, lon, source, severity, detections_json, "
                "privacy_json, image_path, tracking_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (issue_id, now_iso(), lat, lon, source, severity, json.dumps(detections),
                 json.dumps(privacy, ensure_ascii=False), image_path, code),
            )
            self._conn.commit()
            return {"id": cur.lastrowid, "tracking_code": code}

    def get_issue(self, issue_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        return _issue(row) if row else None

    def list_issues(self, status: str | None = None, min_priority: int = 0, statuses: list[str] | None = None,
                    damage_class: str | None = None, district: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM issues WHERE priority >= ?", [min_priority]
        statuses = statuses or ([status] if status else None)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        if damage_class:
            sql += " AND damage_class = ?"
            args.append(damage_class)
        if district:
            sql += " AND district = ?"
            args.append(district)
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
            d.pop("tracking_code", None)  # takip kodu yalnızca bildirene verilir
            d["detections"] = json.loads(d.pop("detections_json"))
            d["privacy"] = json.loads(d.pop("privacy_json") or "null")
            out.append(d)
        return out

    def list_events(self, issue_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, status, actor, note FROM issue_events WHERE issue_id=? ORDER BY id", (issue_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def find_by_tracking_code(self, code: str) -> tuple[dict, dict] | None:
        """(bildirim, arıza) ya da None."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM reports WHERE tracking_code=?", (normalize_code(code),)).fetchone()
        if not row:
            return None
        return dict(row), self.get_issue(row["issue_id"])

    def districts(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT district FROM issues WHERE district IS NOT NULL ORDER BY district"
            ).fetchall()
        return [r[0] for r in rows]

    def issues_without_district(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT id, lat, lon FROM issues WHERE district IS NULL").fetchall()
        return [dict(r) for r in rows]

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

    def dashboard_stats(self, days: int = 30, damage_class: str | None = None, district: str | None = None,
                        today: datetime | None = None) -> dict:
        """Panel grafikleri: günlük bildirim/onarım, durum, sınıf ve ilçe dağılımı, müdahale süreleri."""
        issues = self.list_issues(damage_class=damage_class, district=district)
        ids = [i["id"] for i in issues]
        marks = ",".join("?" * len(ids))
        with self._lock:
            reports = self._conn.execute(
                f"SELECT created_at FROM reports WHERE issue_id IN ({marks})", ids).fetchall() if ids else []
            dispatched = dict(self._conn.execute(
                f"SELECT issue_id, MIN(created_at) FROM issue_events WHERE status='ekip_yonlendirildi' "
                f"AND issue_id IN ({marks}) GROUP BY issue_id", ids).fetchall()) if ids else {}

        today = (today or datetime.now(TR_TZ)).astimezone(TR_TZ).date()
        day_list = [today - timedelta(days=n) for n in range(days - 1, -1, -1)]
        local_day = lambda ts: datetime.fromisoformat(ts).astimezone(TR_TZ).date()  # noqa: E731
        per_day = Counter(local_day(r[0]) for r in reports)
        repaired = [i for i in issues if i["status"] == "onarildi" and i.get("repaired_at")]
        repaired_per_day = Counter(local_day(i["repaired_at"]) for i in repaired)

        def hours(start: str, end: str) -> float:
            return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 3600

        def summary(xs: list[float]) -> dict:
            return {"ortalama_saat": round(statistics.fmean(xs), 1) if xs else None,
                    "medyan_saat": round(statistics.median(xs), 1) if xs else None, "adet": len(xs)}

        repair_h = [hours(i["created_at"], i["repaired_at"]) for i in repaired]
        dispatch_h = [hours(i["created_at"], dispatched[i["id"]]) for i in issues if i["id"] in dispatched]
        open_issues = [i for i in issues if i["status"] in OPEN_STATUSES]
        return {
            "gun_sayisi": days,
            "gunluk": [{"tarih": d.isoformat(), "bildirim": per_day.get(d, 0), "onarilan": repaired_per_day.get(d, 0)}
                       for d in day_list],
            "durum": {s: sum(1 for i in issues if i["status"] == s) for s in ALL_STATUSES},
            "acik_sinif": dict(Counter(i["damage_class"] for i in open_issues).most_common()),
            "acik_ilce": dict(Counter(i.get("district") or "Bilinmiyor" for i in open_issues).most_common()),
            "acik_seviye": dict(Counter(i["level"] for i in open_issues)),
            "toplam_bildirim": len(reports),
            "onarim_suresi": summary(repair_h),
            "ekip_yonlendirme_suresi": summary(dispatch_h),
        }

    def expired_media(self, retention_days: int) -> list[str]:
        """Kapatılmış ve saklama süresi dolmuş kayıtların görsel yolları (KVKK saklama politikası)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        closed = "status IN ('onarildi','yanlis_tespit') AND updated_at < ?"
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.id, r.image_path FROM reports r JOIN issues i ON i.id = r.issue_id "
                "WHERE i.status IN ('onarildi','yanlis_tespit') AND i.updated_at < ? AND r.image_path IS NOT NULL",
                (cutoff,),
            ).fetchall()
            repair = self._conn.execute(
                f"SELECT repair_image FROM issues WHERE {closed} AND repair_image IS NOT NULL", (cutoff,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self._conn.execute(
                    f"UPDATE reports SET image_path=NULL WHERE id IN ({','.join('?' * len(ids))})", ids
                )
            if ids or repair:
                self._conn.execute(f"UPDATE issues SET image_path=NULL, repair_image=NULL WHERE {closed}", (cutoff,))
                self._conn.commit()
        return [r["image_path"] for r in rows] + [r[0] for r in repair]


def _issue(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["context"] = json.loads(d.pop("context_json") or "{}")
    d["repair_check"] = json.loads(d.pop("repair_check_json", None) or "null")
    return d
