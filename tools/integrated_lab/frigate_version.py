"""Parse the plain-text version response exposed by Frigate's /api/version."""

from __future__ import annotations

import re


_FRIGATE_VERSION = re.compile(r"0\.18(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?\Z")


def parse_frigate_version(body: str) -> str:
    version = body.strip()
    if not _FRIGATE_VERSION.fullmatch(version):
        raise ValueError("unexpected Frigate version response")
    return version
