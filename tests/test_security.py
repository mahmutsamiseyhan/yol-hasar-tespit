from fastapi.testclient import TestClient

from app.main import create_app
from app.ratelimit import RateLimiter

from .conftest import ARTER_LON, jpeg_bytes

LAT, LON = 40.190, ARTER_LON + 0.0001


def test_rate_limiter_sliding_window():
    now = [0.0]
    rl = RateLimiter(limit=2, window_s=60, clock=lambda: now[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")  # başka IP etkilenmez
    assert rl.retry_after("a") == 61
    now[0] = 60.5  # pencere geçti
    assert rl.allow("a")
    assert RateLimiter(limit=0).allow("x")  # 0 = sınırsız


def test_upload_limit_returns_429_with_retry_after(make_pipeline, settings):
    settings.upload_limit_per_min = 2
    with TestClient(create_app(make_pipeline())) as c:
        codes = [c.post("/api/analyze-road", files={"image": ("y.jpg", jpeg_bytes(), "image/jpeg")},
                        data={"lat": LAT, "lon": LON}).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        r = c.post("/api/analyze-road", files={"image": ("y.jpg", jpeg_bytes(), "image/jpeg")},
                   data={"lat": LAT, "lon": LON})
        assert int(r.headers["Retry-After"]) > 0 and "bekleyip" in r.json()["detail"]


def test_labeling_tool_can_be_disabled_for_public_deployments(make_pipeline, settings):
    settings.enable_labeling = False
    with TestClient(create_app(make_pipeline())) as c:
        assert c.get("/etiket").status_code == 404
        assert c.get("/api/etiket").status_code == 404
        assert c.get("/").status_code == 200
