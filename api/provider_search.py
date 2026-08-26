from __future__ import annotations

from typing import Any
from urllib.parse import quote


def encoded_query(value: str) -> str:
    """Encode provider search text without turning spaces into invalid URLs."""
    return quote(value.strip(), safe="")


def search_entries(payload: Any) -> list[dict[str, Any]]:
    """Extract search cards from both Gaana's legacy and newer envelopes."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    stack: list[Any] = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(reversed(value))
            continue
        if not isinstance(value, dict):
            continue
        identifier = value.get("seo") or value.get("seokey")
        if identifier:
            identifier = str(identifier)
            if identifier not in seen:
                seen.add(identifier)
                found.append(value)
        for key in ("gr", "gd", "data", "results", "items", "tracks", "entities"):
            child = value.get(key)
            if isinstance(child, (dict, list)):
                stack.append(child)
    return found
