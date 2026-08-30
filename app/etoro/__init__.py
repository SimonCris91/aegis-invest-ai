"""Legacy Step 3 eToro read-only compatibility surface.

The canonical broker abstraction now lives under app.brokers. This package stays
read-only for older tests and migration compatibility.
"""

from app.etoro.errors import EtoroDataError
from app.etoro.fake import FakeEtoroReadClient
from app.etoro.ports import EtoroReadClient

__all__ = ["EtoroDataError", "EtoroReadClient", "FakeEtoroReadClient"]
