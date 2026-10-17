"""The ingress page: charts drawn in the browser from the /api JSON. Dark by default."""

from __future__ import annotations

from pathlib import Path

PANEL = (Path(__file__).resolve().parent / "panel.html").read_bytes()
