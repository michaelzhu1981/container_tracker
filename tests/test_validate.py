from validate import iso6346_check_digit_ok


def test_iso6346_known_example():
    assert iso6346_check_digit_ok("CSQU3054383") is True
    assert iso6346_check_digit_ok("CSQU3054380") is False


def test_iso6346_hapag_dummy_serial():
    assert iso6346_check_digit_ok("HLXU1234561") is True
    assert iso6346_check_digit_ok("HLXU1234567") is False
