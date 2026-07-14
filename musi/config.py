"""Configuratie voor musi — leest ``~/.config/musi/config.toml`` (stdlib ``tomllib``).

Eén dataclass ``Config`` houdt alle paden + credentials bij. ``load()`` leest de
TOML (als 'm bestaat) gemerged over verstandige defaults; ontbrekende velden
vallen terug, zodat musi ook met een lege of minimale config start.

XDG-paden: ``XDG_CONFIG_HOME`` / ``XDG_CACHE_HOME`` worden gerespecteerd (via
``_xdg``), met fallback op ``~/.config`` resp. ``~/.cache``. Alles onder de
cache-dir (sqlite-db, mpv-socket, token-cache, art-cache) wordt consistent
afgeleid van de effectieve cache_dir, zodat één pad-wijziging alles meeneemt.

Config-bestand-velden (alle optioneel):

    music_dir = "~/Music"                  # te indexeren muziekmap

    [spotify]
    client_id       = "..."                # dev-app Client ID (PKCE, geen secret)
    redirect_port   = 8888                 # lokale OAuth-callback-poort
    device_name     = "musi-spotifyd"      # moet matchen met spotifyd.conf

    [youtube]
    cookies_from_browser = "vivaldi"       # leeg = publiek; anders bv firefox/chrome
                                           # (yt-dlp leest live cookies voor subs/fav/WL)

Zie ``README.md`` voor de volledige setup-stappen per bron.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _xdg(env: str, default_subpath: str) -> Path:
    """Los een XDG-pad op: voorkeur de omgevingsvariabele ``env`` (bv.
    ``XDG_CACHE_HOME``), anders ``~/<default_subpath>``."""
    base = os.environ.get(env)
    if base:
        return Path(base)
    return Path.home() / default_subpath


def default_config_dir() -> Path:
    """``~/.config/musi`` (of ``$XDG_CONFIG_HOME/musi``)."""
    return _xdg("XDG_CONFIG_HOME", ".config") / "musi"


def default_cache_dir() -> Path:
    """``~/.cache/musi`` (of ``$XDG_CACHE_HOME/musi``)."""
    return _xdg("XDG_CACHE_HOME", ".cache") / "musi"


# Scopes die we nodig hebben: library readen, playback-status, playlists.
# Let op: ``user-modify-playback-state`` + ``user-read-playback-state`` zijn
# vereist voor de Web API ``start_playback`` (MPRIS ``OpenUri`` is stuk in
# spotifyd 0.4.x — zie ``engines/spotify_engine.py``).
SPOTIFY_SCOPES = (
    "user-read-private",
    "user-read-email",
    "user-read-currently-playing",
    "user-read-playback-state",
    "user-library-read",
    "playlist-read-private",
    "playlist-read-collaborative",
)


@dataclass
class Config:
    """Alle paden + credentials die musi nodig heeft.

    Alle velden hebben defaults zodat de dataclass ook zonder TOML te
    construeren is (handig voor tests). ``load()`` vult 'm vanuit de TOML.
    """

    music_dir: Path                 # te indexeren muziekverzameling
    cache_dir: Path                 # cache-root (art, db, socket, tokens)
    config_dir: Path                # waar de config.toml woont
    mpv_socket: Path                # JSON-IPC-socket voor de mpv-engine
    library_db: Path                # sqlite-db voor de lokale library
    spotify_client_id: str          # PKCE dev-app Client ID ("" = Spotify uit)
    spotify_redirect_port: int      # lokale OAuth-callback-poort
    spotify_device_name: str        # moet matchen met spotifyd.conf device_name
    spotify_token_cache: Path       # PKCE-token-cache (zodat je 1× inlogt)
    yt_cookies_from_browser: str    # leeg = geen cookies; bv "firefox"/"chrome"

    @property
    def spotify_redirect_uri(self) -> str:
        """Volledige OAuth-redirect-URI. **Moet exact kloppen** met wat in de
        dev-app op developer.spotify.com staat — Spotify matcht byte-voor-byte.
        We gebruiken ``127.0.0.1`` (niet ``localhost``) om mismatch te voorkomen."""
        return f"http://127.0.0.1:{self.spotify_redirect_port}/callback"

    @property
    def spotify_scopes(self) -> tuple[str, ...]:
        """De OAuth-scopes die musi vraagt (zie ``SPOTIFY_SCOPES``)."""
        return SPOTIFY_SCOPES


def load(config_path: Path | None = None) -> Config:
    """Laad config: TOML (als aanwezig) gemerged over defaults.

    Ontbrekende velden vallen terug op verstandige defaults. ``cache_dir`` en
    alle cache-afhankelijke paden worden consistent herberekend uit de
    effectieve cache_dir (één pad-wijziging neemt alles mee).

    Args:
        config_path: optioneel expliciet pad (voor tests); ``None`` =
            ``~/.config/musi/config.toml``.

    Returns:
        Een gevulde ``Config``. Fysiek wordt de cache_dir aangemaakt als 'm nog
        niet bestond (andere paden worden lazy aangemaakt door wie 'm gebruikt).
    """
    path = config_path or (default_config_dir() / "config.toml")

    # --- defaults -----------------------------------------------------
    music_dir = Path.home() / "Music"
    cache_dir = default_cache_dir()
    spotify_client_id = ""
    spotify_redirect_port = 8888
    spotify_device_name = "musi-spotifyd"
    yt_cookies_from_browser = ""

    # --- TOML over de defaults heen leggen ----------------------------
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        if "music_dir" in data:
            music_dir = Path(str(data["music_dir"])).expanduser()
        if "cache_dir" in data:
            cache_dir = Path(str(data["cache_dir"])).expanduser()
        spotify = data.get("spotify", {})
        if spotify.get("client_id"):
            spotify_client_id = str(spotify["client_id"])
        if spotify.get("redirect_port"):
            spotify_redirect_port = int(spotify["redirect_port"])
        if spotify.get("device_name"):
            spotify_device_name = str(spotify["device_name"])
        # [youtube] sectie — leeg = geen cookies (publieke content werkt
        # prima voor zoek; subscriptions/favorieten vereisen cookies).
        youtube = data.get("youtube", {})
        if youtube.get("cookies_from_browser"):
            yt_cookies_from_browser = str(youtube["cookies_from_browser"])

    cache_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        music_dir=music_dir,
        cache_dir=cache_dir,
        config_dir=path.parent,
        mpv_socket=cache_dir / "mpv.sock",
        library_db=cache_dir / "library.db",
        spotify_client_id=spotify_client_id,
        spotify_redirect_port=spotify_redirect_port,
        spotify_device_name=spotify_device_name,
        spotify_token_cache=cache_dir / "spotify-token.json",
        yt_cookies_from_browser=yt_cookies_from_browser,
    )
