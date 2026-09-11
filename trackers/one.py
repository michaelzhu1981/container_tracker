from models import CanonicalEvent
from trackers.base import BaseTracker, TrackerError


class OneTracker(BaseTracker):
    carrier_code = "ONEY"
    timeline_order = "oldest_first"

    async def open_page(self) -> None:
        raise TrackerError("ONEY tracker is not implemented yet.", "SELECTOR")

    async def search(self, container: str) -> None:
        raise TrackerError("ONEY tracker is not implemented yet.", "SELECTOR")

    async def parse_events(self) -> list[CanonicalEvent]:
        return []
