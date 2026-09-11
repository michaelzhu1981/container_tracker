from trackers.cma import CmaTracker
from trackers.hapag import HapagTracker
from trackers.maersk import MaerskTracker
from trackers.msc import MscTracker
from trackers.one import OneTracker
from trackers.yangming import YangMingTracker

TRACKERS = {
    "HLCU": HapagTracker,
    "YMJA": YangMingTracker,
    "ONEY": OneTracker,
    "MAEU": MaerskTracker,
    "MSCU": MscTracker,
    "CMDU": CmaTracker,
}
