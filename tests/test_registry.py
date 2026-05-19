"""Sanity tests for the sources registry.

Network-free: confirms that imports resolve, the registry is non-empty,
and every registered class has the required metadata fields. Network
tests (actual downloads) are intentionally separate and skipped by
default.
"""

from __future__ import annotations

from winetone.sources import SOURCES, get
from winetone.sources.base import Source


def test_registry_non_empty():
    assert len(SOURCES) > 0


def test_every_source_has_metadata():
    for name, cls in SOURCES.items():
        assert cls.name == name, f"name mismatch for {cls.__name__}"
        assert cls.description, f"{name}: empty description"
        assert cls.homepage.startswith("http"), f"{name}: bad homepage"


def test_get_returns_source_instance():
    name = next(iter(SOURCES))
    src = get(name)
    assert isinstance(src, Source)
    assert src.name == name
