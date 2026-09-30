"""
Backend registry for crypto data sources.

Adding an exchange does **not** require touching this file:

* a built-in backend ships as a module in :mod:`cryptocmd.exchanges` whose
  top-level ``BACKEND`` attribute is a :class:`~cryptocmd.exchanges.base.Backend`
  instance (auto-discovered);
* a third-party backend may register at runtime with :func:`register_backend`
  or declaratively through the ``cryptocmd.exchanges`` entry point group.
"""

from __future__ import annotations

import importlib
import pkgutil

ENTRY_POINT_GROUP = "cryptocmd.exchanges"


class BackendAlreadyRegistered(ValueError):
    """Raised when an exchange name is registered twice."""


class UnknownBackend(KeyError):
    """Raised when no backend is registered for the requested exchange."""


_REGISTRY: dict[str, object] = {}


def register_backend(name, backend, override=False):
    """Register *backend* under exchange *name* (case-insensitive).

    :param name: canonical exchange key, e.g. ``"bitstamp"``.
    :param backend: object exposing ``scraper_class`` (see
        :class:`cryptocmd.exchanges.base.Backend`).
    :param override: allow replacing an existing registration. Required by the
        test-suite to re-import packages without erroring out.
    :return: the registered backend.
    """
    key = name.lower()
    if key in _REGISTRY and not override:
        raise BackendAlreadyRegistered(name)
    _REGISTRY[key] = backend
    return backend


def unregister_backend(name):
    """Remove a registered exchange. Mainly useful for tests."""
    _REGISTRY.pop(name.lower(), None)


def get_backend(name):
    """Return the backend registered as *name* or raise :class:`UnknownBackend`."""
    _discover()
    key = (name or "").lower()
    if key not in _REGISTRY:
        raise UnknownBackend(
            "Unknown exchange '{}'. Available: {}".format(
                name, ", ".join(sorted(_REGISTRY)) or "(none)"
            )
        )
    return _REGISTRY[key]


def available_backends():
    """Return a sorted tuple of registered exchange keys, discovering first."""
    _discover()
    return tuple(sorted(_REGISTRY))


def _discover():
    """Auto-load built-in modules and third-party entry points (once each)."""
    if getattr(_discover, "_done", False):
        return
    _discover._done = True

    package = importlib.import_module(__package__ or "cryptocmd.exchanges")
    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name.startswith("_") or module_info.name in ("base", "common"):
            continue
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        backend = getattr(module, "BACKEND", None)
        if backend is not None:
            for alias in getattr(backend, "names", (module_info.name,)):
                register_backend(alias, backend, override=True)

    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - Python < 3.8
        return
    for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
        backend = entry_point.load()
        register_backend(entry_point.name, backend, override=True)
