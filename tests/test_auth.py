import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE_NAME, Auth, hash_password, parse_users, verify_password
from app.main import create_app

from .conftest import ARTER_LON, BIG_POTHOLE, jpeg_bytes

LAT, LON = 40.190, ARTER_LON + 0.0001
PASSWORD = "test-sifresi-123"


def test_password_hash_and_plain_passwords():
    stored = hash_password(PASSWORD, iterations=1000)
    assert stored.startswith("pbkdf2_sha256$1000$")
    assert verify_password(PASSWORD, stored) and not verify_password("yanlis", stored)
    assert verify_password("duz", "duz") and not verify_password("duz", "baska")


def test_parse_users_supports_list_and_single_password():
    assert parse_users("ali:a1,veli:pbkdf2_sha256$1$s$h, bozuk") == {"ali": "a1", "veli": "pbkdf2_sha256$1$s$h"}
    assert parse_users(None, "yonetici", "x") == {"yonetici": "x"}
    assert parse_users(None) == {}


def test_session_token_is_signed_and_expires():
    auth = Auth({"ali": "x"}, b"gizli", session_hours=1)
    token = auth.issue("ali", now=1000)
    assert auth.user_from(token, now=1000 + 3599) == "ali"
    assert auth.user_from(token, now=1000 + 3601) is None  # süresi doldu
    assert Auth({"ali": "x"}, b"baska-anahtar").user_from(token, now=1000) is None  # imza tutmuyor
    assert Auth({"veli": "x"}, b"gizli").user_from(token, now=1000) is None  # kullanıcı kaldırıldı
    assert auth.user_from("bozuk-deger") is None and auth.user_from(None) is None


@pytest.fixture
def secured(make_pipeline, settings):
    settings.admin_users = {"yonetici": hash_password(PASSWORD, iterations=1000)}

    def _client(detections=None):
        return TestClient(create_app(make_pipeline(detections=detections)))

    return _client


def login(c, password=PASSWORD):
    return c.post("/api/giris", json={"kullanici": "yonetici", "sifre": password})


def post_photo(c, **data):
    return c.post("/api/analyze-road", files={"image": ("yol.jpg", jpeg_bytes(), "image/jpeg")},
                  data={"lat": LAT, "lon": LON, **data})


def test_panel_and_staff_api_require_login(secured):
    with secured([BIG_POTHOLE]) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/giris?sonra=/"
        for url in ("/api/issues", "/api/issues/1", "/api/stats", "/api/istatistik", "/api/disa-aktar",
                    "/api/ilceler", "/api/olaylar", "/media/" + "a" * 32 + ".jpg"):
            assert c.get(url).status_code == 401, url
        assert c.patch("/api/issues/1", json={"durum": "onarildi"}).status_code == 401
        # Herkese açık kalanlar
        assert c.get("/bildir").status_code == 200 and c.get("/takip").status_code == 200
        assert c.get("/giris").status_code == 200 and c.get("/health").json()["yonetici_girisi"] is True
        body = post_photo(c).json()  # vatandaş bildirimi girişsiz çalışır
        assert body["hasar_tespit_edildi"] and body["takip_kodu"]
        assert c.get(body["goruntu_url"]).status_code == 401  # görseller personele özel


def test_login_flow_sets_strict_httponly_cookie(secured):
    with secured([BIG_POTHOLE]) as c:
        assert login(c, "yanlis").status_code == 401
        assert c.get("/api/oturum").json() == {"giris_gerekli": True, "kullanici": None}
        r = login(c)
        assert r.status_code == 200
        cookie = r.headers["set-cookie"]
        assert COOKIE_NAME in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert c.get("/api/oturum").json()["kullanici"] == "yonetici"
        assert c.get("/", follow_redirects=False).status_code == 200
        issue_id = post_photo(c).json()["ariza"]["properties"]["id"]
        assert c.patch(f"/api/issues/{issue_id}", json={"durum": "ekip_yonlendirildi"}).status_code == 200
        gecmis = c.get(f"/api/issues/{issue_id}").json()["gecmis"]
        assert [(e["status"], e["actor"]) for e in gecmis] == [("acik", "sistem"), ("ekip_yonlendirildi", "yonetici")]
        c.post("/api/cikis")
        assert c.get("/api/issues").status_code == 401


def test_staff_source_requires_login(secured):
    with secured([BIG_POTHOLE]) as c:
        assert post_photo(c, source="saha_ekibi").status_code == 403
        assert post_photo(c, source="vatandas").status_code == 200
        login(c)
        assert post_photo(c, source="saha_ekibi").status_code == 200


def test_cross_site_write_is_rejected(secured):
    with secured([BIG_POTHOLE]) as c:
        login(c)
        issue_id = post_photo(c).json()["ariza"]["properties"]["id"]
        r = c.patch(f"/api/issues/{issue_id}", json={"durum": "onarildi"}, headers={"Origin": "https://kotu.example"})
        assert r.status_code == 403
        r = c.patch(f"/api/issues/{issue_id}", json={"durum": "onarildi"}, headers={"Origin": "http://testserver"})
        assert r.status_code == 200


def test_login_attempts_are_rate_limited(secured, settings):
    settings.login_limit_per_min = 3
    with secured() as c:
        codes = [login(c, "yanlis").status_code for _ in range(4)]
        assert codes == [401, 401, 401, 429]
