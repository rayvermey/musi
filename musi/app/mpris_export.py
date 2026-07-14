"""MPRIS-export: één gezamenlijke MPRIS-speler voor de Orchestrator.

Status: **intentioneel minimaal** (placeholder).

Een volledige dasbus-Service met ``Play``/``Pause``/``Next``/``SetPosition`` en
``PropertiesChanged``-signalen vergt ~200 regels Interface/Publishable-code;
voor de statusbalk is dat niet per se nodig omdat beide onderliggende engines
al MPRIS aanbieden:

* **mpv** + de ``mpv-mpris``-plugin verschijnt vanzelf als MPRIS-speler zodra
  musi speelt — je bar toont dan ``"mpv"``. Zie README "Setup" (``yay -S
  mpv-mpris``).
* **spotifyd** heeft MPRIS al ingebouwd.

Deze module is **voorbereid** op het publiceren van een verenigde
``org.mpris.MediaPlayer2.musi``-bus — playerctl zou daar dan aan koppelen en
zou niet meer hoeven te raden of 'm ``mpv`` of ``spotifyd`` moet volgen. De
huidige implementatie logt alleen de status.

Voor de statusbalk volstaat dus: ``mpv-mpris`` installeren en in je bar
``playerctl -p spotifyd,mpv,firefox,...`` aanroepen (of DMS' eigen
MPRIS-widget, die alle spelers op de session-bus automatisch oppikt).
"""
from __future__ import annotations

import logging

from ..orchestrator import Orchestrator
from ..engines.base import EngineStatus

log = logging.getLogger(__name__)

# De bus-naam die musi zou claimen als 'm ooit een echte MPRIS-service
# publiceert. Let op: per MPRIS-spec moet een unieke busnaam gebruikt worden,
# en indien er al een andere speler met dezelfde naam bestaat mag de claim
# mislukken — geen probleem voor de placeholder.
BUS_NAME = "org.mpris.MediaPlayer2.musi"


def _map_status(s: EngineStatus) -> str:
    """EngineStatus → MPRIS-PlaybackStatus-string (``"Playing"``/``"Paused"``/
    ``"Stopped"``). Onbekende waarden → ``"Stopped"`` (veilige fallback)."""
    return {"playing": "Playing", "paused": "Paused", "stopped": "Stopped"}.get(
        s.value, "Stopped"
    )


class MusiMPRIS:
    """Startpunt voor een verenigde MPRIS-service (lazy; nu enkel log + placeholder).

    Behoudt een referentie naar de orchestrator zodat een toekomstige echte
    implementatie direct aan de events kan koppelen (``state``-events → bus
    properties, ``end-file`` → bus-end-of-track-event)."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orch = orchestrator

    def start(self) -> bool:
        """Logt dat dit een placeholder is en geeft ``True`` terug. Een echte
        implementatie zou hier de dasbus-service opbouwen + publiceren."""
        log.info(
            "MPRIS-export voor bus %s is een placeholder. Voor nu: "
            "`yay -S mpv-mpris` zorgt dat de bar 'mpv' ziet; spotifyd heeft "
            "MPRIS al ingebouwd. Echte service-implementatie is optioneel.",
            BUS_NAME,
        )
        return True

    def stop(self) -> None:
        """No-op (placeholder)."""
        pass