import asyncio

from fastapi.testclient import TestClient

from app.events import EventBus, report_event
from app.main import create_app

from .conftest import ARTER_LON, BIG_POTHOLE, jpeg_bytes

LAT, LON = 40.190, ARTER_LON + 0.0001


def post(c, **data):
    return c.post("/api/analyze-road", files={"image": ("yol.jpg", jpeg_bytes(), "image/jpeg")},
                  data={"lat": LAT, "lon": LON, **data})


def test_event_bus_fans_out_and_drops_oldest_when_full():
    async def run():
        bus = EventBus(queue_size=2)
        a, b = bus.subscribe(), bus.subscribe()
        for i in range(3):
            bus.publish({"tip": "bildirim", "n": i})
        assert [a.get_nowait()["n"], a.get_nowait()["n"]] == [1, 2]  # en eski olay atıldı
        assert b.qsize() == 2
        bus.unsubscribe(a)
        assert bus.subscriber_count == 1

    asyncio.run(run())


def test_event_stream_formats_server_sent_events():
    async def run():
        bus = EventBus()
        calls = {"n": 0}

        async def disconnected():
            calls["n"] += 1
            return calls["n"] > 1  # bir olaydan sonra istemci ayrılsın

        gen = bus.stream(disconnected)
        assert await gen.__anext__() == "retry: 3000\n\n"
        bus.publish({"tip": "bildirim", "hasar_adi": "Çukur"})
        msg = await gen.__anext__()
        assert msg.startswith("event: bildirim\ndata: ") and "Çukur" in msg
        await gen.aclose()
        assert bus.subscriber_count == 0

    asyncio.run(run())


def test_report_and_status_change_are_published(make_pipeline):
    app = create_app(make_pipeline(detections=[BIG_POTHOLE]))
    published = []
    with TestClient(app) as c:
        app.state.events.publish = published.append
        body = post(c, source="saha_ekibi", konum_dogrulugu_m=8).json()
        issue_id = body["ariza"]["properties"]["id"]
        c.patch(f"/api/issues/{issue_id}", json={"durum": "ekip_yonlendirildi"})
    ev = published[0]
    assert ev["tip"] == "bildirim" and ev["kaynak"] == "saha_ekibi" and ev["hasar"] is True
    assert ev["is_emri_no"] == body["ariza"]["properties"]["is_emri_no"] and ev["seviye"] == "kritik"
    assert published[1] == {"tip": "durum", "id": issue_id, "durum": "ekip_yonlendirildi",
                            "is_emri_no": ev["is_emri_no"]}


def test_report_event_without_damage():
    ev = report_event({"konum": {"enlem": 40.1, "boylam": 29.0}, "hasar_tespit_edildi": False, "ariza": None},
                      "vatandas")
    assert ev == {"tip": "bildirim", "kaynak": "vatandas", "enlem": 40.1, "boylam": 29.0, "hasar": False}


def test_gps_accuracy_is_reported_and_poor_accuracy_warns(make_pipeline):
    with TestClient(create_app(make_pipeline(detections=[]))) as c:
        good = post(c, konum_dogrulugu_m=6).json()
        bad = post(c, konum_dogrulugu_m=42).json()
    assert good["konum"]["dogruluk_m"] == 6
    assert any("±6 m" in n["metin"] for n in good["degerlendirme"])
    assert any(n["tur"] == "uyari" and "±42 m" in n["metin"] for n in bad["degerlendirme"])


def test_mobile_page_and_qr_code(make_pipeline):
    with TestClient(create_app(make_pipeline())) as c:
        page = c.get("/bildir").text
        assert "Yol Hasarı Bildir" in page and 'capture="environment"' in page and "geolocation" in page
        qr = c.get("/api/qr", params={"metin": "https://ornek.example/bildir"})
        assert qr.status_code == 200 and qr.headers["content-type"].startswith("image/svg+xml")
        assert c.get("/api/qr", params={"metin": "x" * 501}).status_code == 422
        assert "canli_baglanti" in c.get("/health").json()
