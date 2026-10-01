"""cryptocmd — exchange-dispatching entry point.

This module only picks a backend by exchange name; the fetching logic lives
in ``cryptocmd/exchanges/``. The legacy CoinMarketCap path
(``CmcScraper``) is re-exported unchanged, and is also reachable through the
dispatch table as the ``cmc`` backend. New exchanges are added by dropping
a file into ``cryptocmd/exchanges/`` and registering it — or from third
parties via ``cryptocmd.register_exchange`` — without touching this module.
"""

from .__version__ import __version__  # noqa: F401
from .core import *
from .exchanges import (  # noqa: F401
    available_exchanges,
    fetch_ohlcv,
    get_exchange,
    register_exchange,
)
