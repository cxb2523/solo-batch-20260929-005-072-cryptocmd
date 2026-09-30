"""CoinMarketCap backend — the legacy path, registered unchanged."""

from __future__ import annotations

from ..core import CmcScraper
from .backend import Backend

BACKEND = Backend("cmc", CmcScraper, aliases=("coinmarketcap", "coinmarketcap.com"))
