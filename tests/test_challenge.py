from trackers.base import challenge_code


def test_cloudflare_visible_text():
    assert (
        challenge_code("checking your browser\nWhy did this happen?") == "CLOUDFLARE"
    )
    assert challenge_code("Security Check\nManaged Challenge") == "CLOUDFLARE"


def test_captcha_visible_text():
    assert challenge_code("Please complete the hCaptcha") == "CAPTCHA"


def test_normal_tracking_page_is_not_a_challenge():
    assert challenge_code("Cargo Tracking\nSearch\nContainer No.") is None
    assert challenge_code("Cookie Policy and Cloudflare CDN mention") is None
