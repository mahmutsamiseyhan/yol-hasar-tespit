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

-- Saha geri bildiriminden eğitim örnekleri: yöneticinin düzeltmesi etiket olur, onaylananlar eğitime girer
CREATE TABLE IF NOT EXISTS training_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id   INTEGER NOT NULL UNIQUE REFERENCES reports(id),
    issue_id    INTEGER NOT NULL REFERENCES issues(id),
    kind        TEXT NOT NULL,          -- sinif_duzeltme | yanlis_tespit | dogrulandi
    labels_json TEXT NOT NULL,          -- [{"sinif": "D00", "kutu": [x1, y1, x2, y2]}], boş = hasar yok
    meta_json   TEXT,
    status      TEXT NOT NULL DEFAULT 'bekliyor',   -- bekliyor | onaylandi | haric
    created_by  TEXT,
    reviewed_by TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_status ON training_samples(status);

-- Silinen kayıtların izi: kayıt ve fotoğrafları gider, kimin neyi neden sildiği kalır
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    actor         TEXT,
    action        TEXT NOT NULL,
    work_order_no TEXT,
    details_json  TEXT
);
"""

# Eski veritabanlarına eklenen sütunlar (tablo, sütun, tip)
MIGRATIONS = [
    ("issues", "district", "TEXT"),
    ("issues", "repaired_at", "TEXT"),
    ("issues", "repair_image", "TEXT"),
    ("issues", "repair_check_json", "TEXT"),
    ("reports", "tracking_code", "TEXT"),
    ("issues", "team", "TEXT"),
    ("issue_events", "action", "TEXT"),  # durum | ekip | not | sinif (boş = durum, eski kayıtlar)
    ("reports", "clean_image", "TEXT"),  # maskelenmiş, kutu çizilmemiş kopya (eğitim için; data/egitim/)
    ("reports", "image_w", "INTEGER"),
    ("reports", "image_h", "INTEGER"),
]
SAMPLE_KINDS = ("sinif_duzeltme", "yanlis_tespit", "dogrulandi")
SAMPLE_STATUSES = ("bekliyor", "onaylandi", "haric")


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
               at: str | None = None, action: str = "durum") -> None:
        self._conn.execute(
            "INSERT INTO issue_events (issue_id, created_at, status, actor, note, action) VALUES (?,?,?,?,?,?)",
            (issue_id, at or now_iso(), status, actor, note, action))

    def log_event(self, issue_id: int, action: str, actor: str | None, note: str) -> None:
        """Durumu değiştirmeyen işlemler (not, sınıf düzeltme) için geçmiş kaydı."""
        with self._lock:
            row = self._conn.execute("SELECT status FROM issues WHERE id=?", (issue_id,)).fetchone()
            self._event(issue_id, row["status"], actor, note, action=action)
            self._conn.execute("UPDATE issues SET updated_at=? WHERE id=?", (now_iso(), issue_id))
            self._conn.commit()

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
                   team: str | None = None, **fields) -> dict:
        """Durum değişikliği + geçmiş kaydı. Onarıldı dışındaki durumlarda onarım zamanı temizlenir."""
        with self._lock:
            fields["repaired_at"] = now_iso() if status == "onarildi" else None
            if team:
                fields["team"] = team
                note = f"Ekip: {team}" + (f". {note}" if note else "")
            self.update_issue(issue_id, status=status, **fields)
            self._event(issue_id, status, actor, note)
            self._conn.commit()
        return self.get_issue(issue_id)

    def add_report(self, *, issue_id, lat, lon, source, severity, detections, privacy, image_path,
                   clean_image: str | None = None, image_w: int | None = None, image_h: int | None = None) -> dict:
        with self._lock:
            code = new_tracking_code()
            cur = self._conn.execute(
                "INSERT INTO reports (issue_id, created_at, lat, lon, source, severity, detections_json, "
                "privacy_json, image_path, tracking_code, clean_image, image_w, image_h) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (issue_id, now_iso(), lat, lon, source, severity, json.dumps(detections),
                 json.dumps(privacy, ensure_ascii=False), image_path, code, clean_image, image_w, image_h),
            )
            self._conn.commit()
            return {"id": cur.lastrowid, "tracking_code": code}

    def get_issue(self, issue_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        return _issue(row) if row else None

    def list_issues(self, status: str | None = None, min_priority: int = 0, statuses: list[str] | None = None,
                    damage_class: str | None = None, district: str | None = None,
                    team: str | None = None) -> list[dict]:
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
        if team:
            sql += " AND team = ?"
            args.append(team)
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
            d.pop("clean_image", None)
            d["detections"] = json.loads(d.pop("detections_json"))
            d["privacy"] = json.loads(d.pop("privacy_json") or "null")
            out.append(d)
        return out

    def list_events(self, issue_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, status, actor, note, COALESCE(action, 'durum') AS action FROM issue_events "
                "WHERE issue_id=? ORDER BY id", (issue_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_issue(self, issue_id: int, actor: str | None, reason: str | None) -> list[str]:
        """Kaydı, bildirimlerini ve geçmişini kalıcı siler; silinecek görsel yollarını döner. İz audit_log'da kalır."""
        with self._lock:
            issue = self.get_issue(issue_id)
            images = [r[0] for r in self._conn.execute(
                "SELECT image_path FROM reports WHERE issue_id=? AND image_path IS NOT NULL", (issue_id,))]
            images += [p for p in (issue.get("image_path"), issue.get("repair_image")) if p]
            clean = [r[0] for r in self._conn.execute(
                "SELECT clean_image FROM reports WHERE issue_id=? AND clean_image IS NOT NULL", (issue_id,))]
            report_count = self._conn.execute("SELECT COUNT(*) FROM reports WHERE issue_id=?", (issue_id,)).fetchone()[0]
            details = {"hasar_sinifi": issue["damage_class"], "durum": issue["status"], "ilce": issue.get("district"),
                       "oncelik": issue["priority"], "bildirim_sayisi": report_count, "gerekce": reason}
            self._conn.execute("INSERT INTO audit_log (created_at, actor, action, work_order_no, details_json) "
                               "VALUES (?,?,?,?,?)", (now_iso(), actor, "silme", issue["work_order_no"],
                                                      json.dumps(details, ensure_ascii=False)))
            for table in ("training_samples", "reports", "issue_events"):
                self._conn.execute(f"DELETE FROM {table} WHERE issue_id=?", (issue_id,))
            self._conn.execute("DELETE FROM issues WHERE id=?", (issue_id,))
            self._conn.commit()
        return {"media": sorted(set(images)), "training": sorted(set(clean))}

    # ---------- Eğitim örnekleri (saha geri bildirimi) ----------
    def reports_for_training(self, issue_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, detections_json, clean_image, image_w, image_h FROM reports "
                "WHERE issue_id=? AND clean_image IS NOT NULL", (issue_id,)).fetchall()
        return [{**dict(r), "detections": json.loads(r["detections_json"])} for r in rows]

    def samples_for_issue(self, issue_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM training_samples WHERE issue_id=?", (issue_id,)).fetchall()
        return [_sample(r) for r in rows]

    def upsert_sample(self, *, report_id: int, issue_id: int, kind: str, labels: list, meta: dict | None,
                      actor: str | None) -> None:
        """Etiket değiştiyse değerlendirme baştan başlar (durum: bekliyor)."""
        ts = now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO training_samples (report_id, issue_id, kind, labels_json, meta_json, status, created_by, "
                "created_at, updated_at) VALUES (?,?,?,?,?,'bekliyor',?,?,?) ON CONFLICT(report_id) DO UPDATE SET "
                "kind=excluded.kind, labels_json=excluded.labels_json, meta_json=excluded.meta_json, status='bekliyor', "
                "created_by=excluded.created_by, reviewed_by=NULL, updated_at=excluded.updated_at",
                (report_id, issue_id, kind, json.dumps(labels), json.dumps(meta or {}, ensure_ascii=False), actor,
                 ts, ts))
            self._conn.commit()

    def drop_pending_samples(self, issue_id: int, kinds: tuple[str, ...]) -> int:
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM training_samples WHERE issue_id=? AND status != 'onaylandi' "
                f"AND kind IN ({','.join('?' * len(kinds))})", (issue_id, *kinds))
            self._conn.commit()
        return cur.rowcount

    def list_samples(self, status: str | None = None) -> list[dict]:
        sql = ("SELECT s.*, r.clean_image, r.image_w, r.image_h, r.detections_json, r.created_at AS report_at, "
               "i.work_order_no, i.damage_class, i.status AS issue_status FROM training_samples s "
               "JOIN reports r ON r.id = s.report_id JOIN issues i ON i.id = s.issue_id")
        args = []
        if status:
            sql += " WHERE s.status = ?"
            args.append(status)
        sql += " ORDER BY s.updated_at DESC, s.id DESC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = _sample(r)
            d["detections"] = json.loads(d.pop("detections_json"))
            out.append(d)
        return out

    def get_sample(self, sample_id: int) -> dict | None:
        rows = [s for s in self.list_samples() if s["id"] == sample_id]
        return rows[0] if rows else None

    def review_sample(self, sample_id: int, status: str, reviewer: str | None) -> None:
        with self._lock:
            self._conn.execute("UPDATE training_samples SET status=?, reviewed_by=?, updated_at=? WHERE id=?",
                               (status, reviewer, now_iso(), sample_id))
            self._conn.commit()

    def sample_summary(self) -> dict:
        """Değerlendirme durumları ve saha doğrulaması (kayıt başına, örnek başına değil)."""
        with self._lock:
            by_status = dict(self._conn.execute(
                "SELECT status, COUNT(*) FROM training_samples GROUP BY status").fetchall())
            kinds = dict(self._conn.execute(
                "SELECT kind, COUNT(DISTINCT issue_id) FROM training_samples WHERE status != 'haric' GROUP BY kind"
            ).fetchall())
        correct, wrong_class, false_alarm = (kinds.get("dogrulandi", 0), kinds.get("sinif_duzeltme", 0),
                                             kinds.get("yanlis_tespit", 0))
        total = correct + wrong_class + false_alarm
        return {
            "durum": {s: by_status.get(s, 0) for s in SAMPLE_STATUSES},
            "saha_dogrulamasi": {
                "kayit": total, "model_dogru": correct, "sinif_yanlis": wrong_class, "yanlis_alarm": false_alarm,
                # Hasar gerçekten var mıydı (tür yanlış olsa bile)? Ve tür doğru muydu?
                "tespit_kesinligi": round((correct + wrong_class) / total, 3) if total else None,
                "sinif_dogrulugu": round(correct / (correct + wrong_class), 3) if correct + wrong_class else None,
            },
        }

    def expired_training_images(self, retention_days: int) -> list[str]:
        """Saklama süresi dolan kapalı kayıtların eğitim kopyaları; onaylanmış eğitim örnekleri korunur."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.id, r.clean_image FROM reports r JOIN issues i ON i.id = r.issue_id "
                "WHERE i.status IN ('onarildi','yanlis_tespit') AND i.updated_at < ? AND r.clean_image IS NOT NULL "
                "AND r.id NOT IN (SELECT report_id FROM training_samples WHERE status='onaylandi')", (cutoff,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                marks = ",".join("?" * len(ids))
                self._conn.execute(f"UPDATE reports SET clean_image=NULL WHERE id IN ({marks})", ids)
                self._conn.execute(f"DELETE FROM training_samples WHERE report_id IN ({marks})", ids)
                self._conn.commit()
        return [r["clean_image"] for r in rows]

    def recent_history(self, limit: int = 100) -> list[dict]:
        """Tüm sistemdeki son işlemler: durum, ekip, not, sınıf düzeltme ve silmeler (denetim için)."""
        with self._lock:
            events = self._conn.execute(
                "SELECT e.created_at, e.actor, COALESCE(e.action, 'durum') AS action, e.status, e.note, "
                "i.work_order_no, i.id AS issue_id FROM issue_events e JOIN issues i ON i.id = e.issue_id "
                "ORDER BY e.created_at DESC, e.id DESC LIMIT ?", (limit,)).fetchall()
            deletions = self._conn.execute(
                "SELECT created_at, actor, action, work_order_no, details_json FROM audit_log "
                "ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        rows = [dict(r) for r in events]
        for r in deletions:
            d = json.loads(r["details_json"] or "{}")
            rows.append({"created_at": r["created_at"], "actor": r["actor"], "action": r["action"], "status": None,
                         "note": d.get("gerekce"), "work_order_no": r["work_order_no"], "issue_id": None,
                         "ayrinti": d})
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]

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


def _sample(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["labels"] = json.loads(d.pop("labels_json") or "[]")
    d["meta"] = json.loads(d.pop("meta_json", None) or "{}")
    return d


def _issue(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["context"] = json.loads(d.pop("context_json") or "{}")
    d["repair_check"] = json.loads(d.pop("repair_check_json", None) or "null")
    return d
