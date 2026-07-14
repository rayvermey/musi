"""Lokale zoekprovider — delegeert naar de sqlite-FTS5-library.

De FTS-query is snel, maar we draaien 'm in een thread (``asyncio.to_thread``)
om de event-loop niet te blokkeren (vooral relevant bij grote libraries en
gelijktijdig scannen).
"""
from __future__ import annotations

import asyncio

from ..library import Library
from ..models import Track
from .base import SearchProvider


class LocalSearch(SearchProvider):
    """Zoekprovider over de lokale library (FTS5). Houdt een referentie naar de
    gedeelde ``Library`` (dezelfde instantie als waar de scan op draait)."""

    name = "local"
    label = "Lokaal"

    def __init__(self, library: Library) -> None:
        self._lib = library

    async def search(self, query: str, limit: int = 100,
                     sort: str = "relevance") -> list[Track]:
        """FTS5-zoek in een thread (blokt de loop niet). ``sort`` wordt genegeerd
        — de lokale library zoekt altijd op relevantie (geen datum-bewust sort)."""
        return await asyncio.to_thread(self._lib.search, query, limit)
