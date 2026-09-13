from config import SUPPORTED_CARRIERS
from trackers import TRACKERS
from validate import carrier_supported, iso6346_check_digit_ok


def test_iso6346_known_example():
    assert iso6346_check_digit_ok("CSQU3054383") is True
    assert iso6346_check_digit_ok("CSQU3054380") is False


def test_iso6346_hapag_dummy_serial():
    assert iso6346_check_digit_ok("HLXU1234561") is True
    assert iso6346_check_digit_ok("HLXU1234567") is False


def test_supported_carriers_include_new_lines():
    assert TRACKERS.keys() == set(SUPPORTED_CARRIERS)
    for carrier in ("OOLU", "HDMU", "COSU", "ZIMU"):
        assert carrier_supported(carrier) is True


def test_iso6346_live_examples():
    assert iso6346_check_digit_ok("OOLU6895702") is True
    assert iso6346_check_digit_ok("DFSU7369437") is True
    assert iso6346_check_digit_ok("CSNU6609294") is True
    assert iso6346_check_digit_ok("TCNU3698035") is True
