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


def test_critical_issue_is_flagged_with_work_order(make_pipeline):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    res = pl.analyze(jpeg_bytes(), LAT, LON)  # ana arter + okul yakını + büyük çukur
    p = res["ariza"]["properties"]
    assert p["oncelik_seviyesi"] == "kritik" and res["kritik_oncelik"] is True
    assert p["is_emri_no"].startswith("YH-")

    pl.detector.detections = [SMALL_CRACK]
    side = pl.analyze(jpeg_bytes(), LAT, LON + 0.01)  # ara sokakta küçük çatlak
    assert side["kritik_oncelik"] is False


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



def test_no_damage_report_explains_candidates_location_and_quality(make_pipeline):
    from app.detector import Detection

    weak = Detection("D40", 0.14, (500, 300, 600, 350))  # eşik (0.35) altında
    pl = make_pipeline(detections=[weak])
    res = pl.analyze(jpeg_bytes(), LAT, LON)
    assert res["hasar_tespit_edildi"] is False and res["ariza"] is None
    a = res["analiz"]
    assert a["esik_alti_adaylar"][0]["sinif"] == "D40" and a["esik_alti_adaylar"][0]["guven"] == 0.14
    assert a["konum_baglami"]["yol_sinifi"] == "primary" and a["konum_baglami"]["veri_kapsaminda"] is True
    assert a["goruntu"]["yon"] == "yatay" and a["goruntu"]["isik"] in ("yeterli", "dusuk")
    assert a["guven_esigi"] == 0.35 and isinstance(a["sure_ms"], int)
    assert res["onizleme"].startswith("data:image/jpeg;base64,")
    karar = [n["metin"] for n in res["degerlendirme"] if n["tur"] == "karar"][0]
    assert "zayıf aday" in karar and "%14" in karar


def test_report_warns_when_location_outside_context_data(make_pipeline):
    pl = make_pipeline(detections=[])
    res = pl.analyze(jpeg_bytes(), 39.0, 32.0)  # Ankara civarı: Bursa verisinin dışı
    assert res["analiz"]["konum_baglami"]["veri_kapsaminda"] is False
    assert any("kapsadığı" in n["metin"] for n in res["degerlendirme"] if n["tur"] == "uyari")


def test_damage_report_includes_priority_explanation(make_pipeline):
    pl = make_pipeline(detections=[BIG_POTHOLE])
    res = pl.analyze(jpeg_bytes(), LAT, LON)
    texts = " ".join(n["metin"] for n in res["degerlendirme"])
    assert "Öncelik" in texts and res["ariza"]["properties"]["is_emri_no"] in texts
    assert res["onizleme"].startswith("data:image/jpeg")
