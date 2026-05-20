"""Local web demo for WineTone.

A FastAPI + Jinja2 + HTMX app that lets anyone with the repo:

  1. Pick a username (no real auth — this is a local demo).
  2. Search the canonical wine catalog and add personal labels.
  3. Fit a personal projection (via the auto-detected backend).
  4. Get personalized recommendations from free-text queries.

Run with:   winetone serve
"""

from winetone.web.app import build_app

__all__ = ["build_app"]
