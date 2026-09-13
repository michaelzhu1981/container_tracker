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
    "CMDU": CmaTracker,
    "COSU": CoscoTracker,
    "HDMU": HmmTracker,
    "HLCU": HapagTracker,
    "MAEU": MaerskTracker,
    "MSCU": MscTracker,
    "ONEY": OneTracker,
    "OOLU": OoclTracker,
    "YMJA": YangMingTracker,
    "ZIMU": ZimTracker,
}
