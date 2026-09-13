from trackers.cma import CmaTracker
from trackers.cosco import CoscoTracker
from trackers.hapag import HapagTracker
from trackers.hmm import HmmTracker
from trackers.maersk import MaerskTracker
from trackers.msc import MscTracker
from trackers.one import OneTracker
from trackers.oocl import OoclTracker
from trackers.yangming import YangMingTracker
from trackers.zim import ZimTracker

TRACKERS = {
    "HLCU": HapagTracker,
    "YMJA": YangMingTracker,
    "ONEY": OneTracker,
    "MAEU": MaerskTracker,
    "MSCU": MscTracker,
    "CMDU": CmaTracker,
    "OOLU": OoclTracker,
    "HDMU": HmmTracker,
    "COSU": CoscoTracker,
    "ZIMU": ZimTracker,
}
