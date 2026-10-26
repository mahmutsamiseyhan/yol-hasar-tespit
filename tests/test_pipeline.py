import cv2
import numpy as np
import pytest

from app.pipeline import AnalysisError
from app.privacy import blur_regions

from .conftest import ARTER_LON, BIG_POTHOLE, SMALL_CRACK, jpeg_bytes

LAT, LON = 40.190, ARTER_LON + 0.0001


def test_no_damage_creates_nothing(make_pipeline, settings):
    pl = make_pipeline(detections=[])
    res = pl.analyze(jpeg_bytes(), LAT, LON)
    assert res["hasar_tespit_edildi"] is False
    assert pl.repo.list_issues() == []
    assert list(settings.media_dir.iterdir()) == []


def test_low_confidence_is_filtered(make_pipeline):
    from app.detector import Detection

    pl = make_pipeline(detections=[Detection("D40", 0.1, (0, 0, 300, 300))])
    assert pl.analyze(jpeg_bytes(), LAT, LON)["hasar_tespit_edildi"] is False


def test_new_issue_has_work_order_and_geojson(make_pipeline, settings):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    res = pl.analyze(jpeg_bytes(), LAT, LON)
    f = res["ariza"]
    assert res["mevcut_kayitla_birlestirildi"] is False
    assert f["type"] == "Feature" and f["geometry"]["coordinates"] == [round(LON, 7), LAT]
    p = f["properties"]
    assert p["is_emri_no"].startswith("YH-")
    assert p["hasar_sinifi"] == "D40" and p["hasar_adi"] == "Çukur"
    assert p["yol_sinifi"] == "primary"
    assert (settings.media_dir / p["goruntu_url"].split("/")[-1]).exists()


def test_nearby_reports_merge_and_raise_priority(make_pipeline):
    pl = make_pipeline(detections=[SMALL_CRACK])
    first = pl.analyze(jpeg_bytes(), LAT, LON)["ariza"]["properties"]
    # ~8 m kuzeyde ikinci bildirim -> aynı kayıt
    second = pl.analyze(jpeg_bytes(), LAT + 0.00007, LON)
    p = second["ariza"]["properties"]
    assert second["mevcut_kayitla_birlestirildi"] is True
    assert p["id"] == first["id"]
    assert p["bildirim_sayisi"] == 2
    assert p["oncelik_puani"] > first["oncelik_puani"]
    assert len(pl.repo.list_issues()) == 1
    assert len(pl.repo.list_reports(p["id"])) == 2


def test_far_reports_create_separate_issues(make_pipeline):
    pl = make_pipeline(detections=[SMALL_CRACK])
    pl.analyze(jpeg_bytes(), LAT, LON)
    pl.analyze(jpeg_bytes(), LAT + 0.0003, LON)  # ~33 m
    assert len(pl.repo.list_issues()) == 2


def test_closed_issue_is_not_merged(make_pipeline):
    pl = make_pipeline(detections=[SMALL_CRACK])
    first = pl.analyze(jpeg_bytes(), LAT, LON)["ariza"]["properties"]
    pl.repo.update_issue(first["id"], status="onarildi")
    second = pl.analyze(jpeg_bytes(), LAT, LON)
    assert second["mevcut_kayitla_birlestirildi"] is False


def test_merge_keeps_most_severe_class(make_pipeline):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    pl.analyze(jpeg_bytes(), LAT, LON)
    pl.detector.detections = [SMALL_CRACK]
    p = pl.analyze(jpeg_bytes(), LAT, LON)["ariza"]["properties"]
    assert p["hasar_sinifi"] == "D40"


def test_critical_issue_triggers_single_work_order(make_pipeline, settings):
    sent = []
    settings.webhook_url = "http://n8n.local/webhook/yol"
    pl = make_pipeline(detections=[BIG_POTHOLE], notify=lambda url, payload: sent.append(payload) or True)
    res = pl.analyze(jpeg_bytes(), LAT, LON)  # ana arter + okul yakını + büyük çukur
    assert res["ariza"]["properties"]["oncelik_seviyesi"] == "kritik"
    assert res["is_emri_tetiklendi"] is True
    assert pl.send_work_order(res["ariza"]["properties"]["id"]) is True
    assert sent[0]["is_emri_no"] == res["ariza"]["properties"]["is_emri_no"]
    assert sent[0]["geojson"]["type"] == "Feature"

    again = pl.analyze(jpeg_bytes(), LAT, LON)
    assert again["is_emri_tetiklendi"] is False


def test_failed_webhook_is_retried_on_next_report(make_pipeline, settings):
    settings.webhook_url = "http://n8n.local/webhook/yol"
    pl = make_pipeline(detections=[BIG_POTHOLE], notify=lambda url, payload: False)
    res = pl.analyze(jpeg_bytes(), LAT, LON)
    assert pl.send_work_order(res["ariza"]["properties"]["id"]) is False
    assert pl.analyze(jpeg_bytes(), LAT, LON)["is_emri_tetiklendi"] is True


def test_location_from_exif(make_pipeline):
    pl = make_pipeline(detections=[SMALL_CRACK])
    gps = {1: "N", 2: (40.0, 11.0, 24.0), 3: "E", 4: (29.0, 3.0, 36.0)}
    res = pl.analyze(jpeg_bytes(gps=gps))
    assert res["konum"]["kaynak"] == "exif"
    assert res["konum"]["enlem"] == pytest.approx(40.19)


def test_missing_location_and_bad_input(make_pipeline):
    pl = make_pipeline(detections=[SMALL_CRACK])
    with pytest.raises(AnalysisError, match="Konum"):
        pl.analyze(jpeg_bytes())
    with pytest.raises(AnalysisError, match="okunamadı"):
        pl.analyze(b"garbage", LAT, LON)
    with pytest.raises(AnalysisError, match="kaynak"):
        pl.analyze(jpeg_bytes(), LAT, LON, source="uydu")


def test_stored_image_is_masked(make_pipeline, settings):
    plate = (500, 50, 600, 90)
    pl = make_pipeline(detections=[BIG_POTHOLE], privacy_boxes=[plate])
    res = pl.analyze(jpeg_bytes(), LAT, LON)
    assert res["kvkk"] == {"aktif_dedektorler": ["sahte_plaka"], "maskelenen_bolge": 1}
    stored = cv2.imread(str(settings.media_dir / res["goruntu_url"].split("/")[-1]))
    # Gürültülü test görüntüsünde maskelenen bölgenin varyansı belirgin şekilde düşmeli
    region = stored[55:85, 510:590].astype(float)
    untouched = stored[10:40, 10:90].astype(float)
    assert region.std() < untouched.std() / 3


def test_blur_regions_clips_to_image():
    img = np.full((100, 100, 3), 255, np.uint8)
    out = blur_regions(img, [(-50, -50, 20, 20), (90, 90, 300, 300), (50, 50, 50, 50)])
    assert out.shape == img.shape


def test_retention_purges_closed_issue_images(make_pipeline, settings):
    pl = make_pipeline(detections=[SMALL_CRACK])
    p = pl.analyze(jpeg_bytes(), LAT, LON)["ariza"]["properties"]
    image = settings.media_dir / p["goruntu_url"].split("/")[-1]
    pl.repo.update_issue(p["id"], status="onarildi")
    settings.retention_days = -1  # süresi dolmuş say
    assert pl.purge_expired_media() == 1
    assert not image.exists()
    assert pl.repo.get_issue(p["id"])["image_path"] is None
