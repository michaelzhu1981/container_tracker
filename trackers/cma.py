from models import CanonicalEvent
from trackers.base import BaseTracker, TrackerError


class CmaTracker(BaseTracker):
    carrier_code = "CMDU"
    timeline_order = "oldest_first"

    async def open_page(self) -> None:
        raise TrackerError("CMDU tracker is not implemented yet.", "SELECTOR")

    async def search(self, container: str) -> None:
        raise TrackerError("CMDU tracker is not implemented yet.", "SELECTOR")

    async def parse_events(self) -> list[CanonicalEvent]:
        return []
