from fastapi.testclient import TestClient

from app.auth import hash_password
from app.main import create_app

from .conftest import ARTER_LON, BIG_POTHOLE, FakeDetector, jpeg_bytes

LAT, LON = 40.190, ARTER_LON + 0.0001


def post_photo(c, lat=LAT):
    return c.post("/api/analyze-road", files={"image": ("yol.jpg", jpeg_bytes(), "image/jpeg")},
                  data={"lat": lat, "lon": LON}).json()


def test_team_assignment_is_recorded_and_filterable(make_pipeline, settings):
    settings.teams = ["Asfalt 1", "Asfalt 2"]
    with TestClient(create_app(make_pipeline(detections=[BIG_POTHOLE]))) as c:
        a = post_photo(c)["ariza"]["properties"]["id"]
        b = post_photo(c, lat=LAT + 0.01)["ariza"]["properties"]["id"]
        assert c.get("/api/ayarlar").json()["ekipler"] == ["Asfalt 1", "Asfalt 2"]
        assert c.patch(f"/api/issues/{a}", json={"durum": "ekip_yonlendirildi", "ekip": "Yok"}).status_code == 422
        r = c.patch(f"/api/issues/{a}", json={"durum": "ekip_yonlendirildi", "ekip": "Asfalt 2"}).json()
        assert r["properties"]["ekip"] == "Asfalt 2"
        ids = [f["properties"]["id"] for f in c.get("/api/issues", params={"ekip": "Asfalt 2"}).json()["features"]]
        assert ids == [a] and b not in ids
        events = c.get(f"/api/issues/{a}").json()["gecmis"]
        assert events[-1]["action"] == "durum" and events[-1]["note"] == "Ekip: Asfalt 2"
        csv = c.get("/api/disa-aktar", params={"bicim": "csv", "ekip": "Asfalt 2"}).text
        assert "Ekip" in csv.splitlines()[0] and "Asfalt 2" in csv


def test_note_is_added_to_history(make_pipeline):
    with TestClient(create_app(make_pipeline(detections=[BIG_POTHOLE]))) as c:
        issue_id = post_photo(c)["ariza"]["properties"]["id"]
        r = c.post(f"/api/issues/{issue_id}/not", json={"metin": "  Asfalt ekibi yarın sabah  "})
        assert r.status_code == 200
        last = r.json()["gecmis"][-1]
        assert last == {**last, "action": "not", "note": "Asfalt ekibi yarın sabah", "actor": "yonetici",
                        "status": "acik"}
        assert c.post(f"/api/issues/{issue_id}/not", json={"metin": ""}).status_code == 422
        assert c.post("/api/issues/999/not", json={"metin": "x"}).status_code == 404


def test_class_correction_recalculates_priority(make_pipeline):
    with TestClient(create_app(make_pipeline(detections=[BIG_POTHOLE]))) as c:
        before = post_photo(c)["ariza"]["properties"]
        assert before["hasar_sinifi"] == "D40"
        r = c.post(f"/api/issues/{before['id']}/sinif", json={"sinif": "D00"}).json()
        after = r["ariza"]["properties"]
        assert after["hasar_sinifi"] == "D00" and after["hasar_adi"] == "Boyuna çatlak"
        # Çukur 1.0 → boyuna çatlak 0.4: şiddet ve öncelik orantılı düşer
        assert abs(after["siddet"] - before["siddet"] * 0.4) < 0.01
        assert after["oncelik_puani"] < before["oncelik_puani"]
        assert after["skor_detayi"]["siddet"] == round(after["siddet"], 3)
        assert r["gecmis"][-1]["action"] == "sinif" and "Çukur → Boyuna çatlak" in r["gecmis"][-1]["note"]
        assert c.post(f"/api/issues/{before['id']}/sinif", json={"sinif": "D99"}).status_code == 422


def test_delete_removes_record_photos_and_tracking_but_keeps_audit(make_pipeline, settings):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    with TestClient(create_app(pl)) as c:
        body = post_photo(c)
        issue_id, kod = body["ariza"]["properties"]["id"], body["takip_kodu"]
        pl.detector = FakeDetector([])
        c.post(f"/api/issues/{issue_id}/onarim", files={"image": ("o.jpg", jpeg_bytes(), "image/jpeg")})
        assert len(list(settings.media_dir.iterdir())) == 2  # tespit + onarım fotoğrafı

        r = c.request("DELETE", f"/api/issues/{issue_id}", json={"gerekce": "Kişisel fotoğraf gönderilmiş"})
        assert r.status_code == 200 and r.json()["silinen_gorsel"] == 3  # + eğitim kopyası
        assert list(settings.media_dir.iterdir()) == [] and list(settings.training_dir.iterdir()) == []
        assert c.get(f"/api/issues/{issue_id}").status_code == 404
        assert c.get(f"/api/takip/{kod}").status_code == 404
        assert c.get("/api/issues").json()["features"] == []

        hist = c.get("/api/gecmis").json()["islemler"]
        assert hist[0]["action"] == "silme" and hist[0]["work_order_no"] == body["ariza"]["properties"]["is_emri_no"]
        assert hist[0]["note"] == "Kişisel fotoğraf gönderilmiş" and hist[0]["actor"] == "yonetici"
        assert {h["action"] for h in hist} == {"silme"}  # silinen kaydın geçmişi de silindi, iz audit'te
        assert c.request("DELETE", f"/api/issues/{issue_id}").status_code == 404


def test_history_lists_actions_across_records(make_pipeline):
    with TestClient(create_app(make_pipeline(detections=[BIG_POTHOLE]))) as c:
        a = post_photo(c)["ariza"]["properties"]["id"]
        c.patch(f"/api/issues/{a}", json={"durum": "ekip_yonlendirildi", "ekip": "Ekip 1"})
        c.post(f"/api/issues/{a}/not", json={"metin": "Trafik yoğun, gece çalışılacak"})
        hist = c.get("/api/gecmis", params={"limit": 2}).json()["islemler"]
        assert [h["action"] for h in hist] == ["not", "durum"]
        assert all(h["work_order_no"].startswith("YH-") for h in hist)


def test_admin_actions_require_login(make_pipeline, settings):
    settings.admin_users = {"yonetici": hash_password("gizli-sifre-1", iterations=1000)}
    with TestClient(create_app(make_pipeline(detections=[BIG_POTHOLE]))) as c:
        issue_id = post_photo(c)["ariza"]["properties"]["id"]
        assert c.post(f"/api/issues/{issue_id}/not", json={"metin": "x"}).status_code == 401
        assert c.post(f"/api/issues/{issue_id}/sinif", json={"sinif": "D00"}).status_code == 401
        assert c.request("DELETE", f"/api/issues/{issue_id}").status_code == 401
        assert c.get("/api/gecmis").status_code == 401 and c.get("/api/ayarlar").status_code == 401
