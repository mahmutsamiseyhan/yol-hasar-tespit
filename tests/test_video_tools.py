from datetime import datetime, timezone

import pytest

from scripts.process_video import GpsTrack, TrackAggregator, load_gps_csv, load_gpx

GPX = """<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
<trkpt lat="40.1900" lon="29.0600"><time>2026-10-26T09:15:00Z</time></trkpt>
<trkpt lat="40.1910" lon="29.0600"><time>2026-10-26T09:15:10Z</time></trkpt>
</trkseg></trk></gpx>"""


def test_gps_interpolation_and_clamping():
    g = GpsTrack([0, 10], [40.19, 40.20], [29.06, 29.08])
    assert g.at(5) == pytest.approx((40.195, 29.07))
    assert g.at(-3) == (40.19, 29.06)
    assert g.at(99) == (40.20, 29.08)


def test_load_csv(tmp_path):
    p = tmp_path / "gps.csv"
    p.write_text("t_saniye,enlem,boylam\n10,40.2,29.1\n0,40.1,29.0\n")
    g = load_gps_csv(p)
    assert g.times == [0, 10] and g.at(5) == pytest.approx((40.15, 29.05))


def test_load_gpx_aligned_to_video_start(tmp_path):
    p = tmp_path / "tur.gpx"
    p.write_text(GPX)
    start = datetime(2026, 10, 26, 9, 15, 5, tzinfo=timezone.utc)  # video GPS'ten 5 sn sonra başlıyor
    g = load_gpx(p, start)
    assert g.times == [-5, 5]
    assert g.at(0) == pytest.approx((40.1905, 29.06))


def test_track_aggregator_keeps_best_frame_and_drops_flicker():
    agg = TrackAggregator(min_frames=3)
    for i, area_side in enumerate([10, 50, 30]):  # çukur yaklaşıyor, sonra kadrajdan çıkıyor
        agg.update(1, "D40", 0.8, (0, 0, area_side, area_side), i, lambda i=i: f"kare{i}".encode())
    agg.update(2, "D00", 0.9, (0, 0, 40, 40), 5, lambda: b"tek")  # tek karelik titreme
    results = agg.results()
    assert [tid for tid, _ in results] == [1]
    st = results[0][1]
    assert st.best_frame_idx == 1 and st.best_image == b"kare1" and st.frames == 3
