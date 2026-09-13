"""Detect active browser challenges, excluding SDKs and cookie disclosures."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser


_CLOUDFLARE_TEXT = (
    r"checking your browser|managed challenge|verify (?:that )?you are human|"
    r"security check|attention required[\s\S]*cloudflare|errors\.edgesuite\.net|"
    r"access denied[\s\S]*(?:edgesuite|akamai|reference #)|"
    r"abnormal connection|access to this site has been limited"
)
_CAPTCHA_TEXT = (
    r"(?:please\s+)?(?:complete|solve|pass)\s+(?:the\s+|this\s+|a\s+)?"
    r"(?:hcaptcha|recaptcha|captcha)|(?:hcaptcha|recaptcha|captcha)\s+"
    r"(?:challenge|required|verification)|confirm you are human"
)
_FRAME_PATTERN = (
    r"captcha-delivery\.com|datadome captcha|hcaptcha\.com|"
    r"recaptcha/(?:api2|enterprise)/(?:anchor|bframe)|^recaptcha$"
)


def _text_code(text: str) -> str | None:
    if re.search(_CLOUDFLARE_TEXT, text, re.I):
        return "CLOUDFLARE"
    if re.search(_CAPTCHA_TEXT, text, re.I):
        return "CAPTCHA"
    return None


class _ChallengeHTML(HTMLParser):
    """Conservative snapshot fallback; live detection also checks computed visibility."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.text: list[str] = []
        self.frame_code: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        style = re.sub(r"\s+", "", attrs.get("style") or "").lower()
        hidden = bool(self.stack and self.stack[-1][1]) or (
            tag in {"script", "style", "noscript", "template"}
            or "hidden" in attrs
            or attrs.get("aria-hidden") == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )
        if not hidden and tag == "iframe":
            frame = f"{attrs.get('src', '')} {attrs.get('title', '')}".lower()
            if "size=invisible" not in frame:
                if re.search(_FRAME_PATTERN, frame, re.I):
                    self.frame_code = "CAPTCHA"
                elif "challenges.cloudflare.com" in frame:
                    self.frame_code = "CLOUDFLARE"
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.text.append(data)


def challenge_code(text: str) -> str | None:
    """Require a challenge prompt or iframe, never a provider name in arbitrary HTML."""
    if re.search(r"<[a-zA-Z][\s\S]*?>", text):
        parser = _ChallengeHTML()
        parser.feed(text)
        return parser.frame_code or _text_code(" ".join(parser.text))
    return _text_code(text)


# Detection and waiting use exactly the same predicate. Walk shadow roots as
# carrier web components may contain the challenge iframe.
CHALLENGE_CODE_JS = """() => {
    const text = (document.body && document.body.innerText) || '';
    const visible = el => {
        const rect = el.getBoundingClientRect();
        if (rect.width < 20 || rect.height < 20) return false;
        for (let node = el; node; node = node.parentElement || node.getRootNode().host) {
            const style = getComputedStyle(node);
            if (node.hidden || node.getAttribute('aria-hidden') === 'true' ||
                style.display === 'none' || style.visibility === 'hidden' ||
                style.opacity === '0') return false;
        }
        return true;
    };
    const roots = [document];
    for (let i = 0; i < roots.length; i++) {
        for (const el of roots[i].querySelectorAll('*')) {
            if (el.shadowRoot) roots.push(el.shadowRoot);
            if (el.tagName !== 'IFRAME' || !visible(el)) continue;
            const frame = ((el.getAttribute('src') || '') + ' ' + (el.title || '')).toLowerCase();
            if (frame.includes('size=invisible')) continue;
            if (new RegExp(FRAME_PATTERN, 'i').test(frame)) return 'CAPTCHA';
            if (frame.includes('challenges.cloudflare.com')) return 'CLOUDFLARE';
        }
    }
    if (new RegExp(CLOUDFLARE_TEXT, 'i').test(text)) return 'CLOUDFLARE';
    if (new RegExp(CAPTCHA_TEXT, 'i').test(text)) return 'CAPTCHA';
    return null;
}""".replace("FRAME_PATTERN", json.dumps(_FRAME_PATTERN)).replace(
    "CLOUDFLARE_TEXT", json.dumps(_CLOUDFLARE_TEXT)
).replace("CAPTCHA_TEXT", json.dumps(_CAPTCHA_TEXT))

CHALLENGE_GONE_JS = """() => !!document.body &&
    !!document.body.innerText.trim() && !(DETECT)()
""".replace("DETECT", CHALLENGE_CODE_JS)
