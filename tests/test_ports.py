from ports import display_port, normalize_key


def test_normalize_key():
    assert normalize_key("Yantian, China") == "YANTIAN CHINA"


def test_cnytn_alias():
    assert display_port("CNYTN") == "YANTIAN"
    assert display_port("YICT") == "YANTIAN"


def test_unknown_location_stays_normalized():
    assert display_port("Singapore") == "SINGAPORE"
