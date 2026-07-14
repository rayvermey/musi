"""Zoekprovider-ABC: één uniform async-interface voor alle bronnen.

Elke bron (lokaal, YouTube, Spotify) implementeert ``SearchProvider`` met één
``async search(query)``-methode die een lijst ``Track`` teruggeeft. De UI (zoek-
tab, library-tabs) praat alleen tegen dit interface en hoeft niet te weten hoe
een bron zoekt — registreer providers in ``services.py`` en wijs ze aan op naam.

YouTube heeft naast ``search`` ook library-methoden (``subscriptions``/enz.);
deze ABC definieert alleen de minimale zoek-contract.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Track


class SearchProvider(ABC):
    """Abstracte zoekprovider. Subclasses zetten ``name``/``label`` en
    implementeren ``search``.

    Attributes:
        name: source-naam (``"local"``/``"youtube"``/``"spotify"``) — moet
            matchen met ``Track.source`` en met de key in ``Services.providers``.
        label: mensleesbaar, bv. "Lokaal" (voor de UI).
    """

    name: str  # "local" | "youtube" | "spotify"
    label: str  # mensleesbaar, bv. "Lokaal"

    @abstractmethod
    async def search(self, query: str, limit: int = 50,
                     sort: str = "relevance") -> list[Track]:
        """Zoek tracks; return een (mogelijk leeg) lijst.

        Args:
            query: zoekterm (reeds gestript door aanroeper mag aannomen; maar
                defensief strippen is prima).
            limit: max aantal resultaten.
            sort: sorteervolgorde — ``"relevance"`` (default) of ``"date"``
                (nieuwste eerst). Bron-specifiek: alleen YouTube ondersteunt
                ``"date"`` (server-side op uploaddatum); lokale library en
                Spotify negeren de parameter.

        Returns:
            Lijst Track (bron-specifieke velden in ``extra``). Leeg bij geen
            resultaten of een (opgevangen) fout — raise liever niet; log 'm en
            return [], zodat één falende bron niet de hele zoekactie breekt.
        """
        ...
