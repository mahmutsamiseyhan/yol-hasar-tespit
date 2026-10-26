from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import ARTER_LON, BIG_POTHOLE, jpeg_bytes

LAT, LON = 40.190, ARTER_LON + 0.0001


def client_for(pipeline):
    return TestClient(create_app(pipeline))


def test_health(make_pipeline):
    with client_for(make_pipeline()) as c:
        body = c.get("/health").json()
        assert body["durum"] == "ok"
        assert body["kvkk_dedektorleri"] == ["sahte_plaka"]
        assert body["konum_baglami_yuklu"] is True


def test_analyze_list_update_flow(make_pipeline):
    with client_for(make_pipeline(detections=[BIG_POTHOLE])) as c:
        r = c.post(
            "/api/analyze-road",
            files={"image": ("yol.jpg", jpeg_bytes(), "image/jpeg")},
            data={"lat": LAT, "lon": LON, "source": "vatandas"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        issue_id = body["ariza"]["properties"]["id"]
        assert c.get(body["goruntu_url"]).status_code == 200

        fc = c.get("/api/issues").json()
        assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 1

        detail = c.get(f"/api/issues/{issue_id}").json()
        assert len(detail["bildirimler"]) == 1

        r = c.patch(f"/api/issues/{issue_id}", json={"durum": "yanlis_tespit"})
        assert r.json()["properties"]["durum"] == "yanlis_tespit"
        assert c.get("/api/issues", params={"durum": "acik"}).json()["features"] == []
        assert c.get("/api/stats").json()["durum"] == {"yanlis_tespit": 1}


def test_api_errors(make_pipeline):
    with client_for(make_pipeline(detections=[BIG_POTHOLE])) as c:
        r = c.post("/api/analyze-road", files={"image": ("yol.jpg", jpeg_bytes(), "image/jpeg")})
        assert r.status_code == 422 and "Konum" in r.json()["detail"]
        assert c.get("/api/issues/999").status_code == 404
        assert c.patch("/api/issues/1", json={"durum": "bilinmiyor"}).status_code == 422
        assert c.get("/api/issues", params={"durum": "x"}).status_code == 422


def test_missing_model_returns_503(make_pipeline, tmp_path):
    from app.detector import YoloDetector

    pl = make_pipeline()
    pl.detector = YoloDetector(tmp_path / "yok.pt")
    with client_for(pl) as c:
        r = c.post(
            "/api/analyze-road",
            files={"image": ("yol.jpg", jpeg_bytes(), "image/jpeg")},
            data={"lat": LAT, "lon": LON},
        )
        assert r.status_code == 503 and "best.pt" in r.json()["detail"]
        assert c.get("/health").json()["model_yuklu"] is False


def test_dashboard_served(make_pipeline):
    with client_for(make_pipeline()) as c:
        assert "Bursa Yol Hasarı" in c.get("/").text
        assert c.get("/static/vendor/leaflet.js").status_code == 200
