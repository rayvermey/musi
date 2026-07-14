"""Queue — de uniforme wachtrij van Tracks waar de orchestrator uit speelt.

De Queue is een bewust simpele, synchrone datastructuur (geén asyncio): hij houdt
alleen de lijst tracks + de huidige index bij, plus een mutate-API. De
*orchestrator* is verantwoordelijk voor het daadwerkelijk starten van playback
op `queue.current()` en het schuiven van de index; de Queue zelf start niets.

Belangrijke invariant: ``index == -1`` betekent "er is niets actief". Zodra de
eerste track wordt toegevoegd, schuift de index automatisch naar 0 zodat de
orchestrator meteen kan beginnen (``append``/``extend``/``insert_next`` zetten
index van -1 naar 0). ``current()`` geeft ``None`` als er niets geldig actief is.

Verwijderen houdt de index consistent: als je de huidige track verwijdert,
blijft de index op z'n plek wijzen (naar wat nu de volgende is); valt 'm buiten
bereik, dan klemt 'm op de nieuwe laatste track. De orchestrator merkt dit en
reageert daarop (play/stop).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Track


@dataclass
class Queue:
    """Uniforme wachtrij: lijst tracks + huidige index.

    Attributes:
        tracks: de afspeelvolgorde (Track-objecten).
        index: positie van de actieve track; ``-1`` = niets speelt/actief.
    """

    tracks: list[Track] = field(default_factory=list)
    index: int = -1  # -1 = niets speelt

    def __bool__(self) -> bool:
        """Een lege queue is falsy — handig voor ``if queue:``."""
        return bool(self.tracks)

    def current(self) -> Track | None:
        """De actieve track, of ``None`` als de index buiten bereik ligt (bijv.
        bij een lege queue)."""
        if 0 <= self.index < len(self.tracks):
            return self.tracks[self.index]
        return None

    def has_next(self) -> bool:
        """Is er een volgende track ná de huidige? (Bepaalt of 'natuurlijk
        einde' doorschuift of stopt.)"""
        return self.index + 1 < len(self.tracks)

    def has_prev(self) -> bool:
        """Is er een vorige track? ``True`` zodra we niet op de eerste staan."""
        return self.index > 0 and self.tracks[: self.index] != []

    def append(self, track: Track) -> None:
        """Voeg één track achteraan toe. Als er nog niets actief was (index
        ``-1``), wordt dit meteen de actieve track (index → 0)."""
        self.tracks.append(track)
        if self.index == -1:
            self.index = 0

    def extend(self, tracks: list[Track]) -> None:
        """Voeg meerdere tracks achteraan toe (delegeert naar append, zodat de
        index-invariant voor het eerste item bewaard blijft)."""
        for t in tracks:
            self.append(t)

    def insert_next(self, track: Track) -> None:
        """Voeg een track direct ná de huidige in (één-klik "als volgende
        spelen"). Bij een lege queue wordt dit de actieve track."""
        insert_at = max(self.index + 1, 0)
        self.tracks.insert(insert_at, track)
        if self.index == -1:
            self.index = 0

    def remove_at(self, i: int) -> None:
        """Verwijder de track op positie ``i`` en houd de index consistent.

        Verwijder je de actieve track, dan blijft de index wijzen naar wat nu
        op die plek zit (de volgende track) — de orchestrator beslist of die
        gaat spelen of stopt. Verwijder je iets vóór de actieve track, dan
        schuift de index één mee omlaag. Leeg de queue → index weer ``-1``.
        """
        if not (0 <= i < len(self.tracks)):
            return
        was_current = i == self.index
        del self.tracks[i]
        if was_current:
            # index blijft staan, maar kan buiten bereik vallen → orchestrator
            # deteceert dit en zal het opvolgen door play(index) of stop.
            if self.index >= len(self.tracks):
                self.index = len(self.tracks) - 1
        elif i < self.index:
            self.index -= 1
        if not self.tracks:
            self.index = -1

    def clear(self) -> None:
        """Wis de hele queue en reset de index naar ``-1``."""
        self.tracks.clear()
        self.index = -1
