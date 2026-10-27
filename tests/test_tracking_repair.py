import csv
import io
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.detector import Detection
from app.main import create_app, tr_sort_key
from app.repository import Repository, normalize_code

from .conftest import ARTER_LON, BIG_POTHOLE, SMALL_CRACK, FakeDetector, jpeg_bytes

LAT, LON = 40.190, ARTER_LON + 0.0001


def post_photo(c, lat=LAT, lon=LON):
    return c.post("/api/analyze-road", files={"image": ("yol.jpg", jpeg_bytes(), "image/jpeg")},
                  data={"lat": lat, "lon": lon}).json()


def test_tracking_code_shows_citizen_the_steps(make_pipeline):
    with TestClient(create_app(make_pipeline(detections=[BIG_POTHOLE]))) as c:
        body = post_photo(c)
        kod, issue_id = body["takip_kodu"], body["ariza"]["properties"]["id"]
        assert body["takip_url"] == f"/takip?kod={kod}"
        t = c.get(f"/api/takip/{kod}").json()
        assert t["is_emri_no"] == body["ariza"]["properties"]["is_emri_no"] and t["durum_adi"] == "Açık"
        assert [s["tamam"] for s in t["adimlar"]] == [True, False, False]
        # Küçük harf ve tiresiz yazılan kod da bulunur
        assert c.get(f"/api/takip/{kod.replace('-', '').lower()}").status_code == 200

        c.patch(f"/api/issues/{issue_id}", json={"durum": "ekip_yonlendirildi"})
        t = c.get(f"/api/takip/{kod}").json()
        assert [s["tamam"] for s in t["adimlar"]] == [True, True, False] and t["adimlar"][1]["tarih"]

        c.patch(f"/api/issues/{issue_id}", json={"durum": "onarildi"})
        t = c.get(f"/api/takip/{kod}").json()
        assert [s["tamam"] for s in t["adimlar"]] == [True, True, True]
        assert t["onarim_goruntu_url"] is None  # fotoğrafsız kapatıldı

        assert c.get("/api/takip/XXXX-XXXX").status_code == 404
        # Takip kodu personel listelerinde de görünmez
        assert all("tracking_code" not in r for r in c.get(f"/api/issues/{issue_id}").json()["bildirimler"])


def test_false_detection_is_explained_to_citizen(make_pipeline):
    with TestClient(create_app(make_pipeline(detections=[BIG_POTHOLE]))) as c:
        body = post_photo(c)
        c.patch(f"/api/issues/{body['ariza']['properties']['id']}", json={"durum": "yanlis_tespit"})
        steps = c.get(f"/api/takip/{body['takip_kodu']}").json()["adimlar"]
        assert [s["ad"] for s in steps] == ["Bildirim alındı", "İncelendi: yol hasarı doğrulanmadı"]


def test_repair_photo_closes_issue_and_checks_for_remaining_damage(make_pipeline, settings):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    with TestClient(create_app(pl)) as c:
        body = post_photo(c)
        issue_id, kod = body["ariza"]["properties"]["id"], body["takip_kodu"]

        pl.detector = FakeDetector([])  # onarılmış yol: model hasar görmüyor
        r = c.post(f"/api/issues/{issue_id}/onarim", files={"image": ("onarim.jpg", jpeg_bytes(), "image/jpeg")},
                   data={"lat": LAT, "lon": LON})
        assert r.status_code == 200, r.text
        res = r.json()
        props = res["ariza"]["properties"]
        assert props["durum"] == "onarildi" and res["kontrol"]["sonuc"] == "temiz"
        assert props["onarim"]["goruntu_url"] and props["onarim"]["tarih"]
        assert c.get(props["onarim"]["goruntu_url"]).status_code == 200
        assert props["goruntu_url"] != props["onarim"]["goruntu_url"]  # önce / sonra ayrı görseller
        # Vatandaş onarım fotoğrafını kendi koduyla görebilir
        t = c.get(f"/api/takip/{kod}").json()
        assert c.get(t["onarim_goruntu_url"]).headers["content-type"] == "image/jpeg"


def test_repair_photo_with_damage_or_wrong_place_warns(make_pipeline):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    with TestClient(create_app(pl)) as c:
        issue_id = post_photo(c)["ariza"]["properties"]["id"]
        r = c.post(f"/api/issues/{issue_id}/onarim", files={"image": ("onarim.jpg", jpeg_bytes(), "image/jpeg")},
                   data={"lat": LAT + 0.002, "lon": LON}).json()  # ~220 m uzakta ve hasar hâlâ görülüyor
        check = r["kontrol"]
        assert check["sonuc"] == "hasar_suphesi" and check["kalan_hasar"][0]["sinif"] == "D40"
        assert check["mesafe_m"] > 200 and any("uzakta" in n for n in check["notlar"])
        assert r["ariza"]["properties"]["durum"] == "onarildi"  # karar mühendiste, kayıt yine kapanır
        assert c.post(f"/api/issues/999/onarim", files={"image": ("o.jpg", jpeg_bytes(), "image/jpeg")}).status_code == 404


def test_retention_also_purges_repair_photos(make_pipeline, settings):
    pl = make_pipeline(detections=[SMALL_CRACK])
    issue_id = pl.analyze(jpeg_bytes(), LAT, LON)["ariza"]["properties"]["id"]
    pl.detector = FakeDetector([])
    repair = pl.record_repair(issue_id, jpeg_bytes(), "ekip")["ariza"]["properties"]["onarim"]["goruntu_url"]
    settings.retention_days = -1
    assert pl.purge_expired_media() == 2
    assert not (settings.media_dir / repair.split("/")[-1]).exists()


def test_filters_districts_and_export(make_pipeline, settings):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    with TestClient(create_app(pl)) as c:
        a = post_photo(c)["ariza"]["properties"]
        pl.detector = FakeDetector([Detection("D00", 0.9, (100, 100, 400, 110))])
        b = post_photo(c, lat=LAT + 0.01)["ariza"]["properties"]
        pl.repo.update_issue(a["id"], district="Nilüfer")
        pl.repo.update_issue(b["id"], district="Osmangazi")
        c.patch(f"/api/issues/{b['id']}", json={"durum": "ekip_yonlendirildi"})

        ids = lambda **q: [f["properties"]["id"] for f in c.get("/api/issues", params=q).json()["features"]]  # noqa: E731
        assert ids(sinif="D40") == [a["id"]] and ids(ilce="Osmangazi") == [b["id"]]
        assert ids(durum="acik,ekip_yonlendirildi") == [a["id"], b["id"]] and ids(durum="onarildi") == []
        assert c.get("/api/issues", params={"sinif": "D99"}).status_code == 422
        assert c.get("/api/ilceler").json()["ilceler"] == ["Nilüfer", "Osmangazi"]

        r = c.get("/api/disa-aktar", params={"bicim": "csv", "ilce": "Nilüfer"})
        assert "attachment" in r.headers["content-disposition"] and r.text.startswith("﻿")
        rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿")), delimiter=";"))
        assert rows[0][0] == "İş emri no" and len(rows) == 2 and rows[1][0] == a["is_emri_no"]
        assert rows[1][rows[0].index("İlçe")] == "Nilüfer" and "," in rows[1][rows[0].index("Enlem")]

        gj = c.get("/api/disa-aktar", params={"bicim": "geojson"})
        assert gj.headers["content-type"].startswith("application/geo+json")
        props = gj.json()["features"][0]["properties"]
        assert "goruntu_url" not in props and props["ilce"]
        assert c.get("/api/disa-aktar", params={"bicim": "xls"}).status_code == 422


def test_csv_export_escapes_formulas():
    from app.export import export_csv

    fc = {"features": [{"geometry": {"coordinates": [29.0, 40.0]}, "properties": {
        "is_emri_no": "=HYPERLINK(1)", "hasar_sinifi": "D40", "hasar_adi": "Çukur", "oncelik_puani": 50,
        "oncelik_seviyesi": "yuksek", "durum": "acik", "bildirim_sayisi": 1, "ilce": None,
        "yol_adi": "-Yol", "yol_sinifi": "local", "olusturma": "2026-10-27T06:00:00+00:00", "onarim": None}}]}
    row = list(csv.reader(io.StringIO(export_csv(fc).lstrip("﻿")), delimiter=";"))[1]
    assert row[0] == "'=HYPERLINK(1)" and row[8] == "'-Yol"
    assert row[12] == "2026-10-27 09:00"  # Türkiye saati


def test_dashboard_stats_daily_counts_and_response_times(make_pipeline):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    a = pl.analyze(jpeg_bytes(), LAT, LON)["ariza"]["properties"]["id"]
    pl.analyze(jpeg_bytes(), LAT, LON)  # aynı noktaya ikinci bildirim
    b = pl.analyze(jpeg_bytes(), LAT + 0.01, LON)["ariza"]["properties"]["id"]
    pl.repo.set_status(a, "ekip_yonlendirildi", "ekip")
    pl.repo.set_status(a, "onarildi", "ekip")
    # Oluşturma zamanlarını geriye al: müdahale süreleri ölçülebilsin
    t0 = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(timespec="seconds")
    pl.repo.update_issue(a, created_at=t0)

    s = pl.repo.dashboard_stats(days=7)
    assert len(s["gunluk"]) == 7 and s["gunluk"][-1]["bildirim"] == 3 and s["gunluk"][-1]["onarilan"] == 1
    assert s["toplam_bildirim"] == 3
    assert s["durum"]["onarildi"] == 1 and s["durum"]["acik"] == 1
    assert s["acik_sinif"] == {"D40": 1}
    assert 9.9 <= s["onarim_suresi"]["ortalama_saat"] <= 10.1 and s["onarim_suresi"]["adet"] == 1
    assert s["ekip_yonlendirme_suresi"]["adet"] == 1
    assert pl.repo.dashboard_stats(damage_class="D00")["toplam_bildirim"] == 0
    assert b


def test_old_database_is_migrated(tmp_path):
    db = tmp_path / "eski.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issues (id INTEGER PRIMARY KEY AUTOINCREMENT, work_order_no TEXT UNIQUE, lat REAL NOT NULL,
            lon REAL NOT NULL, damage_class TEXT NOT NULL, severity REAL NOT NULL, priority INTEGER NOT NULL,
            level TEXT NOT NULL, report_count INTEGER NOT NULL DEFAULT 1, context_json TEXT,
            status TEXT NOT NULL DEFAULT 'acik', image_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, issue_id INTEGER, created_at TEXT NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL, source TEXT NOT NULL, severity REAL NOT NULL,
            detections_json TEXT NOT NULL, privacy_json TEXT, image_path TEXT);
        INSERT INTO issues VALUES (1, 'YH-2026-000001', 40.19, 29.06, 'D40', 0.8, 70, 'kritik', 1, '{}', 'onarildi',
            NULL, '2026-10-20T08:00:00+00:00', '2026-10-21T08:00:00+00:00');
        INSERT INTO reports VALUES (1, 1, '2026-10-20T08:00:00+00:00', 40.19, 29.06, 'vatandas', 0.8, '[]', NULL, NULL);
    """)
    conn.commit()
    conn.close()
    repo = Repository(db)
    issue = repo.get_issue(1)
    assert issue["district"] is None and issue["repaired_at"] == "2026-10-21T08:00:00+00:00"
    assert [e["status"] for e in repo.list_events(1)] == ["acik", "onarildi"]
    code = sqlite3.connect(db).execute("SELECT tracking_code FROM reports").fetchone()[0]
    assert repo.find_by_tracking_code(code)[1]["id"] == 1
    assert Repository(db).list_events(1) == repo.list_events(1)  # ikinci açılışta tekrar eklenmez


def test_helpers():
    assert normalize_code("ab cd-ef gh") == "ABCD-EFGH"
    assert sorted(["Osmangazi", "İnegöl", "İznik", "Gemlik", "Yıldırım", "Çekirge"], key=tr_sort_key) == \
        ["Çekirge", "Gemlik", "İnegöl", "İznik", "Osmangazi", "Yıldırım"]
