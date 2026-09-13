from config import chrome_profile_dir, default_headed_for, query_delay_seconds
from runner import should_relaunch_browser_per_box, update_circuit
from cli import build_parser


def test_hlcu_does_not_relaunch_browser_per_box():
    assert should_relaunch_browser_per_box("HLCU") is False
    assert should_relaunch_browser_per_box("YMJA") is False


def test_circuit_trips_after_two_cloudflare_failures():
    streak, tripped = update_circuit(0, "CLOUDFLARE")
    assert (streak, tripped) == (1, False)
    streak, tripped = update_circuit(streak, "CLOUDFLARE")
    assert (streak, tripped) == (2, True)


def test_circuit_trips_on_selector_and_resets_on_success():
    streak, tripped = update_circuit(0, "SELECTOR")
    assert tripped is False
    streak, tripped = update_circuit(streak, None)
    assert (streak, tripped) == (0, False)
    streak, tripped = update_circuit(1, "PARSE")
    assert (streak, tripped) == (0, False)


def test_challenge_carrier_defaults():
    assert default_headed_for("HLCU", False) is True
    assert default_headed_for("YMJA", False) is False
    assert default_headed_for("YMJA", True) is True
    assert chrome_profile_dir("HLCU").name == "chrome_hlcu"
    hlcu_lo, hlcu_hi = query_delay_seconds("HLCU")
    ymja_lo, ymja_hi = query_delay_seconds("YMJA")
    assert hlcu_lo >= 5
    assert ymja_hi <= 4


def test_cli_no_wait_challenge_flag():
    parser = build_parser()
    args = parser.parse_args(["--no-wait-challenge"])
    assert args.no_wait_challenge is True
    default = parser.parse_args([])
    assert default.no_wait_challenge is False
