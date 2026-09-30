"""Backend descriptor advertised by every exchange module."""

from __future__ import annotations


class Backend:
    """Describes a data-source backend.

    :param name: primary exchange key.
    :param scraper_class: scraper class compatible with
        :class:`cryptocmd.exchanges.base.ExchangeScraper`.
    :param aliases: additional case-insensitive keys resolving to this backend.
    """

    def __init__(self, name, scraper_class, aliases=()):
        self.name = name
        self.scraper_class = scraper_class
        self.names = (name,) + tuple(aliases)

    def __repr__(self):
        return f"<Backend {self.name}>"
