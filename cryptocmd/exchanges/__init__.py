"""Pluggable exchange backends for cryptocmd.

Backends auto-register through :mod:`cryptocmd.exchanges.registry`. Third
parties can add a new exchange by shipping their own module and registering via
the ``cryptocmd.exchanges`` entry point or
:func:`cryptocmd.exchanges.registry.register_backend`.
"""

from .backend import Backend
from .base import ExchangeScraper
from .registry import (
    BackendAlreadyRegistered,
    UnknownBackend,
    available_backends,
    get_backend,
    register_backend,
    unregister_backend,
)

__all__ = [
    "Backend",
    "BackendAlreadyRegistered",
    "ExchangeScraper",
    "UnknownBackend",
    "available_backends",
    "get_backend",
    "register_backend",
    "unregister_backend",
]
