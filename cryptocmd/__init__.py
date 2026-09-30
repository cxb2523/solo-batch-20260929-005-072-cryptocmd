from .__version__ import __version__
from .core import *  # noqa: F401,F403
from .core import CmcScraper
from .exchanges import (
    Backend,
    ExchangeScraper,
    available_backends,
    get_backend,
    register_backend,
    unregister_backend,
)

# Default exchange when callers do not name one: the legacy CMC path.
DEFAULT_EXCHANGE = "cmc"


def get_scraper(exchange=DEFAULT_EXCHANGE, *args, **kwargs):
    """Return a scraper instance for *exchange* with the remaining arguments.

    The factory only picks a backend by exchange name; all data logic lives in
    the backend modules. Every backend exposes the same CmcScraper-compatible
    surface (headers, row/index layout, CSV dialect).
    """
    return get_backend(exchange).scraper_class(*args, **kwargs)


# Verbose alias retained for readability at call sites.
Scraper = get_scraper

__all__ = [
    "DEFAULT_EXCHANGE",
    "Backend",
    "CmcScraper",
    "ExchangeScraper",
    "Scraper",
    "__version__",
    "available_backends",
    "get_backend",
    "get_scraper",
    "register_backend",
    "unregister_backend",
]
