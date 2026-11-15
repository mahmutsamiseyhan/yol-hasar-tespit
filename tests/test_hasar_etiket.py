import json
import zipfile

import pytest

from scripts import etiket_paketle
from scripts.hasar_adaylari import near_test


def test_candidates_near_the_bursa_test_set_are_skipped():
    tests = [(40.2119, 28.9350)]
    assert near_test(40.2120, 28.9351, tests)  # ~15 m
    assert not near_test(40.2200, 28.9350, tests)  # ~900 m


def test_package_keeps_reviewed_images_only(tmp_path):
    labels = tmp_path / "etiket"
    labels.mkdir()
    for name in ("hasarli", "saglam", "disarida", "bakilmadi"):
        (labels / f"{name}.jpg").write_bytes(b"jpg")
    # Çokgen etiket kutuya çevrilir
    (labels / "hasarli.txt").write_text("3 0.1 0.2 0.3 0.2 0.3 0.6 0.1 0.6", encoding="utf-8")
    (labels / "saglam.txt").write_text("1 0.5 0.5 0.2 0.2", encoding="utf-8")  # modelin yanlış kutusu
    (labels / "_durum.json").write_text(json.dumps({
        "hasarli.jpg": {"durum": "tamam"}, "saglam.jpg": {"durum": "hasarsiz"},
        "disarida.jpg": {"durum": "kapsam_disi"}}), encoding="utf-8")

    report = etiket_paketle.build(labels, tmp_path / "paket.zip")
    assert report["egitime_giren"] == 2 and report["kutular"] == {"D40": 1}
    assert report["kararlar"] == {"tamam": 1, "hasarsiz": 1, "kapsam_disi": 1, "incelenmedi": 1}
    with zipfile.ZipFile(tmp_path / "paket.zip") as z:
        names = set(z.namelist())
        assert {"images/hasarli.jpg", "images/saglam.jpg", "labels/hasarli.txt", "labels/saglam.txt"} <= names
        assert "images/disarida.jpg" not in names and "images/bakilmadi.jpg" not in names
        assert z.read("labels/hasarli.txt").decode() == "3 0.200000 0.400000 0.200000 0.400000"
        assert z.read("labels/saglam.txt").decode() == ""  # sağlam yol: boş etiket


def test_bursa_test_set_cannot_be_packaged(tmp_path):
    with pytest.raises(SystemExit):
        etiket_paketle.build(etiket_paketle.BURSA_TEST, tmp_path / "x.zip")
