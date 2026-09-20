"""Runtime configuration for the container tracker."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent

HEADLESS = True
LOCALE = "en-US"
NAV_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 15_000
QUERY_DELAY_SECONDS = (2.0, 4.0)
CHALLENGE_QUERY_DELAY_SECONDS = (5.0, 8.0)
HDMU_QUERY_DELAY_SECONDS = (3.0, 3.0)
EGLV_NAVIGATION_RETRY_DELAY_SECONDS = 3.0
EXCEL_BATCH_SIZE = 5
EXCEL_FLUSH_SECONDS = 10.0
MAX_RETRIES = 1
AUTO_CHALLENGE_WAIT_MS = 25_000
CHALLENGE_WAIT_MS = 180_000
MANUAL_CHROME_WAIT_SECONDS = 600
CURRENT_BROWSER_WAIT_MS = MANUAL_CHROME_WAIT_SECONDS * 1000
MANUAL_CHROME_APPEAR_SECONDS = 30
CHALLENGE_RETRY_DELAYS = (5.0, 15.0)
SUPPORTED_CARRIERS = (
    "CMDU",
    "COSU",
    "EGLV",
    "HDMU",
    "HLCU",
    "MAEU",
    "MSCU",
    "ONEY",
    "OOLU",
    "YMJA",
    "ZIMU",
)
CHALLENGE_CARRIERS = ("HLCU", "MSCU", "MAEU", "CMDU", "OOLU", "ZIMU", "HDMU")
HEADLESS_PARALLEL_CARRIERS = tuple(
    carrier for carrier in SUPPORTED_CARRIERS if carrier not in CHALLENGE_CARRIERS
)
HEADED_SERIAL_CARRIERS = ("OOLU", "MSCU", "MAEU", "HLCU", "CMDU", "HDMU", "ZIMU")
CIRCUIT_BREAK_CODES = frozenset({"CLOUDFLARE", "CAPTCHA", "SELECTOR"})
CIRCUIT_BREAK_STREAK = 2

INPUT_XLSX = ROOT / "input" / "containers.xlsx"
OUTPUT_XLSX = ROOT / "output" / "containers_result.xlsx"
SCREENSHOT_DIR = ROOT / "screenshots"
HTML_DIR = ROOT / "logs" / "html"
LOG_DIR = ROOT / "logs"
SESSION_DIR = ROOT / "sessions"
PORTS_YAML = ROOT / "data" / "ports.yaml"

CARRIER_TIMEOUT_MS = {
    "HLCU": 45_000,
    "YMJA": 45_000,
    "ONEY": 45_000,
    "MAEU": 60_000,
    "MSCU": 60_000,
    "CMDU": 45_000,
    "OOLU": 60_000,
    "HDMU": 45_000,
    "COSU": 45_000,
    "EGLV": 45_000,
    "ZIMU": 60_000,
}

RESULT_COLUMNS = (
    "Container",
    "Carrier",
    "POL",
    "Status",
    "Loaded",
    "Sailed",
    "Vessel",
    "Voyage",
    "ATD",
    "Latest Event",
    "Checked At",
    "Check Result",
    "Error Code",
    "Error",
    "Screenshot",
)

STANDARD_OUTPUT_NAMES = set(RESULT_COLUMNS)

HEADER_ALIASES = {
    "container": "Container",
    "container no.": "Container",
    "container no": "Container",
    "container number": "Container",
    "ctr no": "Container",
    "箱号": "Container",
    "集装箱号": "Container",
    "carrier": "Carrier",
    "carrier code": "Carrier",
    "scac": "Carrier",
    "船公司": "Carrier",
}


def session_path(carrier: str) -> Path:
    return SESSION_DIR / f"{carrier.lower()}.json"


def chrome_profile_dir(carrier: str) -> Path:
    return SESSION_DIR / f"chrome_{carrier.lower()}"


def query_delay_seconds(carrier: str) -> tuple[float, float]:
    if carrier == "HDMU":
        return HDMU_QUERY_DELAY_SECONDS
    if carrier in CHALLENGE_CARRIERS:
        return CHALLENGE_QUERY_DELAY_SECONDS
    return QUERY_DELAY_SECONDS


def default_headed_for(carrier: str, headed: bool) -> bool:
    return bool(headed or carrier in CHALLENGE_CARRIERS)
