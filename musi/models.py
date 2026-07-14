"""Datamodel — Track is de universele eenheid die door de hele app stroomt.

Alle bronnen (lokaal, YouTube, Spotify) normaliseren naar één `Track`-dataclass.
Hierdoor hoeven queue, orchestrator, engines en UI alleen maar tegen Track aan te
programmeren en niet per bron afwijken. Een Track combineert:

* **identiteit** — `uri` is het adres dat de engine nodig heeft om 'm af te spelen
  (voor mpv een bestandspad of watch-URL; voor spotifyd een `spotify:track:...`);
* **metadata** — titel/artiest/album/duur voor de weergave;
* **routing** — `source` (waar komt 'm vandaan, voor de badge) en `engine`
  (welke engine speelt 'm af: "mpv" of "spotifyd"). De orchestrator kiest de
  engine op basis van `track.engine`;
* **albumhoes** — `art_url` is een URL of lokaal pad dat de art-laag kan ophalen;
* **vrije ruimte** — `extra` voor bron-specifieke velden (YouTube video-id,
  Spotify track-id, kanaal-id voor drill-down) zonder de dataclass te moeten
  uitbreiden.
* **optionele YouTube-stats** — `upload_date` (``"YYYYMMDD"``-string of
  ``None``), `view_count` en `like_count` (``int | None``). Alleen YouTube-
  searches in ``!date``-modus vullen deze (parallel per-video fetch); andere
  modi/bronnen laten ze leeg. De zoek-UI toont ze voor alle tracks (lege
  cellen → ``"—"``) zodat de kolommen uniform blijven.

Deze module is opzettelijk klein en afhankelijkheids-vrij: bijna elk ander
musi-module importeert Track, dus hier mag geen zware import staan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Track:
    """Eén afspeelbaar item, bron-onafhankelijk.

    Attributes:
        source: herkomst-label, gebruikt voor de badge-symbooltjes en de
            search-bron-prefix. Een van ``"local"``, ``"youtube"``, ``"spotify"``
            (of een eigen label voor toekomstige bronnen).
        engine: welke engine deze track afspeelt — ``"mpv"`` (lokaal + YouTube)
            of ``"spotifyd"`` (Spotify via MPRIS). De orchestrator leest dit
            veld om de juiste engine te activeren; zie ``orchestrator.py``.
        uri: het afspeeladres zoals de engine 'm verwacht:
            * mpv/lokaal  → absoluut bestandspad (``/home/ray/Music/...``);
            * mpv/YouTube → ``https://www.youtube.com/watch?v=...`` (mpv roept
              intern yt-dlp aan voor de audio-stream);
            * spotifyd    → ``spotify:track:...`` (gebruikt door de Web API
              ``start_playback``; MPRIS ``OpenUri`` is onbetrouwbaar in spotifyd
              0.4.x, zie ``engines/spotify_engine.py``).
        title: weergave-titel (altijd ingevuld; fallback op bestandsnaam).
        artist: artiest/tenkanaal, mag leeg zijn.
        album: album-naam, mag leeg zijn (lokaal vaak ingevuld; YouTube altijd
            leeg — daar is het "kanaal" de artiest).
        duration: speelduur in seconden; ``0.0`` = onbekend (UI toont dan "—:—").
        art_url: URL of lokaal pad naar een albumhoes/thumb. Leeg = geen hoes;
            de art-laag (``art.py``) probeert 'm alsnog te vullen.
        extra: optionele, bron-specifieke velden. Gebruikte keys:
            ``id`` (YouTube video-id / Spotify track-id), ``channel_id``
            (YouTube, voor eventuele kanaal-drill-down).
    """

    source: str            # "local" | "youtube" | "spotify"
    engine: str            # "mpv" | "spotifyd"
    uri: str               # mpv: bestandspad of url ; spotifyd: "spotify:track:..."
    title: str
    artist: str = ""
    album: str = ""
    duration: float = 0.0  # seconden (0 = onbekend)
    art_url: str = ""      # url/pad voor album-hoes
    extra: dict[str, Any] = field(default_factory=dict)
    # YouTube-stats (alleen ingevuld bij ``!date``-searches en eventueel andere
    # toekomstige YT-modi; andere bronnen laten ze op None).
    # ``upload_date`` is ``"YYYYMMDD"`` zoals yt-dlp 'm levert — de UI formatteert
    # naar ``"YYYY-MM-DD"`` voor leesbaarheid.
    upload_date: str | None = None
    view_count: int | None = None
    like_count: int | None = None

    def display(self) -> str:
        """Eén-regel weergave voor lijsten: ``"Titel — Artiest"`` (of alleen
        titel als de artiest leeg is). Gebruikt door plekken die geen aparte
        kolom per veld hebben."""
        by = f" — {self.artist}" if self.artist else ""
        return f"{self.title}{by}"

    @property
    def badge(self) -> str:
        """Klein symbooltje dat de bron aangeeft, voor in tabel-cellen en de
        now-playing-bar. Onbekende bron → puntje."""
        return {"local": "♪", "youtube": "▶", "spotify": "♫"}.get(self.source, "·")
