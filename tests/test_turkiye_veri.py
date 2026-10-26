from scripts.turkiye_veri_hazirla import CITIES, POS_CONF, classify, in_bursa


def test_pseudo_label_rules():
    assert classify([]) == "hasarsiz"  # model hiçbir şey görmüyor: sağlam yol
    assert classify([(3, POS_CONF + 0.1, [0.5, 0.5, 0.1, 0.1])]) == "hasarli"  # yalnızca çok emin tespitler
    # Bir tane bile emin olunmayan tespit varsa görüntü atılır (şerit çizgisi, rögar kapağı vb.)
    assert classify([(3, 0.9, [0] * 4), (0, 0.3, [0] * 4)]) == "belirsiz"
    assert classify([(0, 0.2, [0] * 4)]) == "belirsiz"


def test_bursa_is_excluded_to_keep_the_test_set_independent():
    assert in_bursa(40.19, 29.06) and in_bursa(40.08, 29.51)  # merkez, İnegöl
    assert not any(in_bursa(lat, lon) for lat, lon in CITIES.values())
