import io
import zipfile

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import ARTER_LON, BIG_POTHOLE, FakeDetector, jpeg_bytes

LAT, LON = 40.190, ARTER_LON + 0.0001


def post_photo(c, lat=LAT):
    return c.post("/api/analyze-road", files={"image": ("yol.jpg", jpeg_bytes(), "image/jpeg")},
                  data={"lat": lat, "lon": LON}).json()["ariza"]["properties"]["id"]


def samples(c, durum=None):
    return c.get("/api/egitim", params={"durum": durum} if durum else {}).json()


def test_class_correction_becomes_a_pending_training_sample(make_pipeline, settings):
    with TestClient(create_app(make_pipeline(detections=[BIG_POTHOLE]))) as c:
        assert len(list(settings.training_dir.iterdir())) == 0
        issue_id = post_photo(c)
        assert len(list(settings.training_dir.iterdir())) == 1  # maskelenmiş, kutusuz kopya
        assert samples(c)["ornekler"] == []  # karar verilmeden örnek yok
        c.post(f"/api/issues/{issue_id}/sinif", json={"sinif": "D20"})
        d = samples(c)
        s = d["ornekler"][0]
        assert s["tur"] == "sinif_duzeltme" and s["durum"] == "bekliyor"
        assert s["baslik"] == "Hasar türü düzeltildi: Çukur → Timsah sırtı çatlak"
        assert d["ozet"]["durum"]["bekliyor"] == 1
        img = c.get(s["onizleme_url"])
        assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"


def test_false_detection_and_repair_create_samples_and_field_validation(make_pipeline):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    with TestClient(create_app(pl)) as c:
        a, b, k = post_photo(c), post_photo(c, LAT + 0.01), post_photo(c, LAT + 0.02)
        c.patch(f"/api/issues/{a}", json={"durum": "yanlis_tespit"})
        pl.detector = FakeDetector([])
        c.post(f"/api/issues/{b}/onarim", files={"image": ("o.jpg", jpeg_bytes(), "image/jpeg")})
        c.post(f"/api/issues/{k}/sinif", json={"sinif": "D00"})
        by_kind = {s["tur"]: s for s in samples(c)["ornekler"]}
        assert set(by_kind) == {"yanlis_tespit", "dogrulandi", "sinif_duzeltme"}
        assert by_kind["yanlis_tespit"]["etiket_sayisi"] == 0  # hasarsız (arka plan) örneği
        v = samples(c)["ozet"]["saha_dogrulamasi"]
        assert v == {**v, "kayit": 3, "model_dogru": 1, "sinif_yanlis": 1, "yanlis_alarm": 1}
        assert v["tespit_kesinligi"] == round(2 / 3, 3) and v["sinif_dogrulugu"] == 0.5
        # Yeniden açılan kayıtta bekleyen “yanlış tespit” kararı geri alınır
        c.patch(f"/api/issues/{a}", json={"durum": "acik"})
        assert "yanlis_tespit" not in {s["tur"] for s in samples(c)["ornekler"]}


def test_review_and_package_contains_only_approved_yolo_labels(make_pipeline):
    with TestClient(create_app(make_pipeline(detections=[BIG_POTHOLE]))) as c:
        a, b = post_photo(c), post_photo(c, LAT + 0.01)
        c.post(f"/api/issues/{a}/sinif", json={"sinif": "D00"})
        c.patch(f"/api/issues/{b}", json={"durum": "yanlis_tespit"})
        assert c.get("/api/egitim-paketi").status_code == 422  # onaylı örnek yok
        ids = {s["tur"]: s["id"] for s in samples(c)["ornekler"]}
        assert c.post(f"/api/egitim/{ids['sinif_duzeltme']}", json={"durum": "onaylandi"}).json()["durum"] == "onaylandi"
        c.post(f"/api/egitim/{ids['yanlis_tespit']}", json={"durum": "haric"})
        assert c.post(f"/api/egitim/{ids['yanlis_tespit']}", json={"durum": "x"}).status_code == 422
        assert [s["id"] for s in samples(c, "onaylandi")["ornekler"]] == [ids["sinif_duzeltme"]]

        r = c.get("/api/egitim-paketi")
        assert r.headers["content-type"] == "application/zip" and "saha_egitim_" in r.headers["content-disposition"]
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = sorted(z.namelist())
        assert names == ["images/saha_%06d.jpg" % ids["sinif_duzeltme"], "labels/saha_%06d.txt" % ids["sinif_duzeltme"],
                         "rapor.json"]
        cls, cx, cy, w, h = z.read(names[1]).decode().split()
        assert cls == "0"  # D00 = sınıf 0
        # Kutu (200,300)-(456,420), görüntü 640x480
        assert abs(float(cx) - 328 / 640) < 1e-4 and abs(float(w) - 256 / 640) < 1e-4 and abs(float(h) - 120 / 480) < 1e-4
        assert '"ornek_sayisi": 1' in z.read("rapor.json").decode()


def test_field_validation_counts_each_kind_correctly(make_pipeline):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    with TestClient(create_app(pl)) as c:
        fixes = [post_photo(c, LAT + 0.01 * i) for i in range(2)]
        alarms = [post_photo(c, LAT + 0.05 + 0.01 * i) for i in range(3)]
        for i in fixes:
            c.post(f"/api/issues/{i}/sinif", json={"sinif": "D00"})
        for i in alarms:
            c.patch(f"/api/issues/{i}", json={"durum": "yanlis_tespit"})
        v = samples(c)["ozet"]["saha_dogrulamasi"]
        assert (v["kayit"], v["model_dogru"], v["sinif_yanlis"], v["yanlis_alarm"]) == (5, 0, 2, 3)
        assert v["tespit_kesinligi"] == 0.4 and v["sinif_dogrulugu"] == 0.0


def test_correction_after_repair_keeps_corrected_label(make_pipeline):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    with TestClient(create_app(pl)) as c:
        a = post_photo(c)
        c.post(f"/api/issues/{a}/sinif", json={"sinif": "D10"})
        pl.detector = FakeDetector([])
        c.post(f"/api/issues/{a}/onarim", files={"image": ("o.jpg", jpeg_bytes(), "image/jpeg")})
        s = samples(c)["ornekler"]
        assert len(s) == 1 and s[0]["tur"] == "sinif_duzeltme"  # onarım, düzeltmeyi ezmez
