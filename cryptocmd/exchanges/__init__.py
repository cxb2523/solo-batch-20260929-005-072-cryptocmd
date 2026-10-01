"""Exchange backend registry.

``cryptocmd/__init__.py`` only dispatches to a backend by exchange name;
the backends themselves live in ``cryptocmd/exchanges/``. Built-in backends
register themselves below. Third-party backends can be plugged in from
anywhere without touching cryptocmd's core::

    import cryptocmd

    cryptocmd.register_exchange("myexchange", my_backend)

A backend must expose::

    fetch_ohlcv(symbol, start_date=None, end_date=None, fiat="USD")
        -> (headers, rows)

where ``headers``/``rows`` follow the CMC schema (see
``cryptocmd.exchanges.bitstamp.HEADERS``).
"""

from __future__ import annotations

__all__ = [
    "available_exchanges",
    "fetch_ohlcv",
    "get_exchange",
    "register_exchange",
]

_BACKENDS = {}


def register_exchange(name, backend):
    """Register *backend* under *name* (third-party registration hook).

    Returns the backend so it can be used as a decorator.
    """
    key = str(name).strip().lower()
    if not key:
        raise ValueError("exchange name cannot be empty")
    if not hasattr(backend, "fetch_ohlcv"):
        raise TypeError(
            f"backend for {name!r} must expose fetch_ohlcv(symbol, start_date, "
            "end_date, fiat)"
        )
    _BACKENDS[key] = backend
    return backend


def get_exchange(name):
    """Look up the backend registered under *name*."""
    key = str(name).strip().lower()
    try:
        return _BACKENDS[key]
    except KeyError:
        raise KeyError(
            "Unknown exchange {!r}. Available: {}".format(
                name, ", ".join(sorted(_BACKENDS)) or "<none>"
            )
        )


def available_exchanges():
    """Return the sorted names of all registered exchanges."""
    return sorted(_BACKENDS)


def fetch_ohlcv(exchange, symbol, start_date=None, end_date=None, fiat="USD"):
    """Dispatch an OHLCV fetch to the backend registered for *exchange*."""
    return get_exchange(exchange).fetch_ohlcv(
        symbol, start_date=start_date, end_date=end_date, fiat=fiat
    )


class _CmcBackend:
    """Adapter exposing the legacy CmcScraper through the exchange protocol.

    The CMC code path itself is untouched; this only adapts its interface.
    """

    name = "cmc"

    def fetch_ohlcv(self, symbol, start_date=None, end_date=None, fiat="USD"):
        from ..core import CmcScraper

        scraper = CmcScraper(
            symbol,
            start_date=start_date,
            end_date=end_date,
            fiat=fiat,
            order_ascending=False,
        )
        return scraper.get_data()


register_exchange("cmc", _CmcBackend())

# Built-in exchange backends register themselves on import.
from . import bitstamp

register_exchange("bitstamp", bitstamp)
