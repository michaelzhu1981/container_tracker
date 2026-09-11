from models import CanonicalEvent
from trackers.base import BaseTracker, TrackerError


class MaerskTracker(BaseTracker):
    carrier_code = "MAEU"
    timeline_order = "oldest_first"

    async def open_page(self) -> None:
        raise TrackerError("MAEU tracker is not implemented yet.", "SELECTOR")

    async def search(self, container: str) -> None:
        raise TrackerError("MAEU tracker is not implemented yet.", "SELECTOR")

    async def parse_events(self) -> list[CanonicalEvent]:
        return []
