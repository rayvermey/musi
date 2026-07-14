"""Services — concrete draaiende engines, zoekproviders en library.

Eén plek (``Services``-dataclass + ``build_*``-functies) die de bedrading
opzet, zodat de CLI/app niet zelf elke engine/provider hoeft te instantiëren.
Hier "weten" we welke zoekprovider bij welke engine hoort en hoe de library
wordt gebouwd.

Twee builds:
* ``build_phase1`` — alleen mpv + lokale library + YouTube/lokaal-zoek. Geen
  Spotify. (Nuttig voor wie Spotify niet gebruikt, of voor testen.)
* ``build_full`` — alles, incl. spotifyd-engine + Spotify-zoekprovider. De
  spotifyd-engine wordt *lazy* gestart in ``cli._run_tui``: ontbreekt
  spotifyd-config of een client_id, dan blokkeert dat niet het opstarten.

Engines worden niet hier gestart — ``Orchestrator.start()`` (in cli) doet dat.
``build_*`` construeert alleen de objecten en koppelt ze.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config
from .engines.mpv_engine import MpvEngine
from .library import Library
from .search.base import SearchProvider
from .search.local import LocalSearch
from .search.spotify import SpotifySearch
from .search.youtube import YouTubeSearch

log = logging.getLogger(__name__)


@dataclass
class Services:
    """Alle draaiende onderdelen, verzameld voor de CLI/app.

    Attributes:
        mpv: de mpv-engine (JSON-IPC) voor lokaal + YouTube.
        library: de lokale muziek-library (sqlite).
        providers: mapping source-naam → SearchProvider (``local``/``youtube``,
            optioneel ``spotify``). Gebruikt door de zoek-tab en de Library-tabs.
        spotify: de Spotify-zoekprovider (``None`` in phase-1).
        spotify_engine: de spotifyd-engine (``None`` in phase-1). Type is
            ``object`` om een late import-cyclus te vermijden; in praktijk een
            ``SpotifyEngine``.
    """

    mpv: MpvEngine
    library: Library
    providers: dict[str, SearchProvider]
    spotify: SpotifySearch | None = None
    spotify_engine: object | None = None  # SpotifyEngine; lazy import om cycles te vermijden


def build_phase1(cfg: Config) -> Services:
    """Pre-Spotify-build (fase 1): mpv + library + lokaal/YouTube-zoek.

    Nuttig als je Spotify niet gebruikt of voor testen zonder spotipy. De
    Spotify-tab zal 'm netjes melden dat 'm niet beschikbaar is.
    """
    mpv = MpvEngine(cfg.mpv_socket)
    library = Library(cfg.library_db, cfg.music_dir)
    providers: dict[str, SearchProvider] = {
        "local": LocalSearch(library),
        "youtube": YouTubeSearch(cookies_from_browser=cfg.yt_cookies_from_browser),
    }
    return Services(mpv=mpv, library=library, providers=providers)


def build_full(cfg: Config) -> Services:
    """Volledige build met Spotify — engines worden lazy gestart in cli.start()
    zodat ontbrekende spotifyd-config of client_id niet het opstarten blokkeert.

    Bouwt naast phase-1 ook:
      * de Spotify-zoekprovider (PKCE-OAuth, lazy client);
      * de spotifyd-engine (MPRIS over dbus). De engine krijgt een callable die
        de spotipy-client oplevert (nodig voor ``start_playback`` via de Web
        API — MPRIS ``OpenUri`` is onbetrouwbaar, zie spotify_engine.py).
    """
    from .engines.spotify_engine import SpotifyEngine  # lazy import
    mpv = MpvEngine(cfg.mpv_socket)
    library = Library(cfg.library_db, cfg.music_dir)
    providers: dict[str, SearchProvider] = {
        "local": LocalSearch(library),
        "youtube": YouTubeSearch(cookies_from_browser=cfg.yt_cookies_from_browser),
        "spotify": SpotifySearch(
            client_id=cfg.spotify_client_id,
            redirect_uri=cfg.spotify_redirect_uri,
            cache_path=cfg.spotify_token_cache,
        ),
    }
    spotify_engine = SpotifyEngine(
        spotify_provider=providers["spotify"].get_client,  # type: ignore[union-attr]
        device_name=cfg.spotify_device_name,
    )
    return Services(
        mpv=mpv, library=library, providers=providers,
        spotify=providers["spotify"],  # type: ignore[assignment]
        spotify_engine=spotify_engine,
    )
