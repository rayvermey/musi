"""Spotify-zoekprovider via spotipy (PKCE-OAuth).

Bij eerste aanroep: start een PKCE-OAuth-flow (geen client_secret nodig) via
een lokale callback-server op de redirect_uri (``open_browser=True`` opent
automatisch de browser). De token-cache voorkomt herhaaldelijk inloggen. Daarna:
web-API-zoek + library-endpoints (liked songs, opgeslagen albums, playlists,
album/playlist-tracks).

Fout-zichtbaarheid: een OAuth-fout (fout client_id, niet-geregistreerde
redirect-uri) wordt in ``_ensure_client`` geforceerd (één ``current_user()``
call) en in ``last_error`` onthouden. De UI leest dat uit en toont de echte
oorzaak i.p.v. een stil leeg Spotify-tabblad ("Spotify doet niets").

Paginering: alle library-methoden pagineren met de endpoint-eigen page-size en
stoppen zodra een pagina leeg is of de ``limit`` bereikt. Fouten per pagina worden
gelogd en breken de harvest af (return wat we tot dan toe hebben).
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from ..models import Track
from .base import SearchProvider

log = logging.getLogger(__name__)


def _to_track(item: dict[str, Any]) -> Track:
    """Spotify track-object → Track. Kiest de kleinste album-image ≥300px (of de
    grootste als er geen ≥300px is) als ``art_url``."""
    art = ""
    images = (item.get("album") or {}).get("images") or []
    if images:
        # Spotify levert images oplopend in formaat; pak de kleinste ≥ 300px
        # (of de grootste als geen ≥ 300px beschikbaar is).
        candidates = [im for im in images if im.get("width") and im["width"] >= 300]
        chosen = (candidates[0] if candidates else images[-1])["url"]
        art = chosen
    artists = ", ".join(a["name"] for a in (item.get("artists") or []))
    return Track(
        source="spotify",
        engine="spotifyd",
        uri=item["uri"],
        title=item.get("name") or "(onbekend)",
        artist=artists,
        album=(item.get("album") or {}).get("name") or "",
        duration=float((item.get("duration_ms") or 0) / 1000.0),
        art_url=art,
        extra={"id": item.get("id") or ""},
    )


class SpotifySearch(SearchProvider):
    """Spotify via spotipy. De spotipy-client wordt lazy geïnitialiseerd: pas
    bij de eerste echte call (zoek/library) — de eerste keer triggert dat de
    PKCE-OAuth-flow."""

    name = "spotify"
    label = "Spotify"

    def __init__(self, client_id: str, redirect_uri: str, cache_path) -> None:
        """Args:
            client_id: dev-app Client ID (leeg → Spotify uit; ``_ensure_client``
                zet een heldere fout in ``last_error``).
            redirect_uri: OAuth-redirect-URI (moet exact matchen met de dev-app;
                zie ``config.py``).
            cache_path: pad naar de PKCE-token-cache.
        """
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._cache_path = cache_path
        self._sp = None  # lazy geïnit; eerste call forceert OAuth-flow
        # laatste autorisatiefout (object-string), zodat de UI de échte oorzaak kan
        # tonen in plaats van een stil/leeg Spotify-tabblad ("Spotify doet niets").
        self.last_error: str | None = None

    def _ensure_client(self):
        """Initialiseer de spotipy-client (lazy). Eerste aanroep triggert de
        PKCE-OAuth-flow + één goedkope ``current_user()``-call, zodat een fout
        client_id of niet-geregistreerde redirect-uri hier op één plek falen met
        een duidelijke fout (i.p.v. pas verstrooid per API-call). Daarna praat
        de gecachte token mee.

        Raist bij een auth-fout en zet ``self.last_error``.
        """
        if self._sp is not None:
            return
        # import binnen de functie: spotipy is een optionele afhankelijkheid voor
        # wie Spotify niet gebruikt, en maakt `musi` starten zonder spotifyd sneller.
        import spotipy
        from spotipy.oauth2 import SpotifyPKCE

        if not self._client_id:
            self.last_error = (
                "client_id ontbreekt — zet 'm in ~/.config/musi/config.toml "
                "([spotify] client_id = \"...\") na het aanmaken van een dev-app op "
                "developer.spotify.com."
            )
            raise RuntimeError(self.last_error)
        try:
            auth = SpotifyPKCE(
                client_id=self._client_id,
                redirect_uri=self._redirect_uri,
                open_browser=True,         # opent browser + lokale callback-server (geen stdin-prompt)
                cache_path=str(self._cache_path),
                scope=" ".join([
                    "user-read-private", "user-read-email", "user-library-read",
                    "playlist-read-private", "playlist-read-collaborative",
                    # playback-start gaat via Web API (MPRIS OpenUri is stuk in spotifyd 0.4.2):
                    "user-modify-playback-state", "user-read-playback-state",
                ]),
            )
            client = spotipy.Spotify(auth_manager=auth)
            # Forceer de OAuth-token-ruil (+ 1 goedkope call) HIER, zodat een fout
            # client_id of een niet-geregistreerde redirect_uri op één plek falen met
            # een duidelijke fout — i.p.v. pas verstrooid per afzonderlijke API-call.
            # (Eén keer per sessie; daarna praat de token uit de cache mee.)
            client.current_user()
        except Exception as e:
            self.last_error = f"{e}"
            raise
        self._sp = client
        self.last_error = None

    def get_client(self):
        """Geef de (lazy geïnitialiseerde) spotipy-client — triggert OAuth bij
        eerste aanroep. Wordt o.a. door de spotifyd-engine gebruikt voor
        ``start_playback`` (Web API)."""
        self._ensure_client()
        return self._sp

    async def search(self, query: str, limit: int = 20,
                     sort: str = "relevance") -> list[Track]:
        """Zoek tracks (``type="track"``). Auth- of zoek-fouten → lege lijst +
        log (één falende bron mag de hele zoekactie niet breken). ``sort`` wordt
        genegeerd — de Web API's ``/search`` heeft geen datum-sort voor tracks."""
        query = query.strip()
        if not query:
            return []
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            log.warning("Spotify-auth niet beschikbaar: %s", e)
            return []
        try:
            res = await asyncio.to_thread(
                partial(self._sp.search, q=query, type="track", limit=limit)
            )
        except Exception as e:
            log.warning("Spotify-zoek mislukte: %s", e)
            return []
        return [_to_track(t) for t in (res.get("tracks") or {}).get("items") or []]

    # ---- library ------------------------------------------------------
    async def saved_tracks(self, limit: int = 200) -> list[Track]:
        """'Liked songs' — opgeslagen nummers (gepagineerd)."""
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            log.warning("Spotify-auth niet beschikbaar: %s", e)
            return []
        out: list[Track] = []
        offset = 0
        page_size = 50
        while offset < limit:
            try:
                res = await asyncio.to_thread(
                    partial(self._sp.current_user_saved_tracks, limit=page_size, offset=offset)
                )
            except Exception as e:
                log.warning("saved_tracks mislukte: %s", e)
                break
            items = (res or {}).get("items") or []
            if not items:
                break
            for it in items:
                t = it.get("track")
                if t:
                    out.append(_to_track(t))
            offset += len(items)
        return out

    async def saved_albums(self, limit: int = 100) -> list[dict[str, Any]]:
        """Opgeslagen albums — lijst van ruwe album-dicts ``{name, artists,
        images, uri, ...}`` (volledig, voor o.a. hoes-lookup in de UI)."""
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            log.warning("Spotify-auth niet beschikbaar: %s", e)
            return []
        out: list[dict[str, Any]] = []
        offset = 0
        page_size = 20
        while offset < limit:
            try:
                res = await asyncio.to_thread(
                    partial(self._sp.current_user_saved_albums, limit=page_size, offset=offset)
                )
            except Exception as e:
                log.warning("saved_albums mislukte: %s", e)
                break
            items = (res or {}).get("items") or []
            if not items:
                break
            for it in items:
                alb = it.get("album") or {}
                if alb:
                    out.append(alb)
            offset += len(items)
        return out

    async def user_playlists(self, limit: int = 100) -> list[dict[str, Any]]:
        """Eigen playlists + followed playlists (volledige dicts)."""
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            log.warning("Spotify-auth niet beschikbaar: %s", e)
            return []
        out: list[dict[str, Any]] = []
        offset = 0
        page_size = 50
        while offset < limit:
            try:
                res = await asyncio.to_thread(
                    partial(self._sp.current_user_playlists, limit=page_size, offset=offset)
                )
            except Exception as e:
                log.warning("user_playlists mislukte: %s", e)
                break
            items = (res or {}).get("items") or []
            if not items:
                break
            out.extend(items)
            offset += len(items)
        return out

    async def playlist_tracks(self, playlist_uri: str, limit: int = 200) -> list[Track]:
        """Tracks binnen een playlist (gepagineerd)."""
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            log.warning("Spotify-auth niet beschikbaar: %s", e)
            return []
        out: list[Track] = []
        offset = 0
        page_size = 100
        while offset < limit:
            try:
                res = await asyncio.to_thread(
                    partial(self._sp.playlist_items, playlist_uri, limit=page_size, offset=offset)
                )
            except Exception as e:
                log.warning("playlist_items mislukte: %s", e)
                break
            items = (res or {}).get("items") or []
            if not items:
                break
            for it in items:
                t = it.get("track")
                if t:
                    out.append(_to_track(t))
            offset += len(items)
        return out

    async def album_tracks(self, album_uri: str, limit: int = 300, art_url: str = "") -> list[Track]:
        """Tracks binnen een album (gepagineerd).

        De ``album_tracks``-endpoint levert vereenvoudigde track-objecten zónder
        'album'-veld (geen hoes/titel mee), dus kunnen we ``_to_track`` niet
        hergebruiken. De album-hoes wordt via ``art_url`` door de aanroeper
        meegegeven, vanuit de opgeslagen album-metadata (UI houdt een cache bij
        van volledige album-dicts).
        """
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            log.warning("Spotify-auth niet beschikbaar: %s", e)
            return []
        out: list[Track] = []
        offset = 0
        page_size = 50
        while offset < limit:
            try:
                res = await asyncio.to_thread(
                    partial(self._sp.album_tracks, album_uri, limit=page_size, offset=offset)
                )
            except Exception as e:
                log.warning("album_tracks mislukte: %s", e)
                break
            items = (res or {}).get("items") or []
            if not items:
                break
            for it in items:
                artists = ", ".join(a["name"] for a in (it.get("artists") or []))
                out.append(Track(
                    source="spotify",
                    engine="spotifyd",
                    uri=it.get("uri") or "",
                    title=it.get("name") or "(onbekend)",
                    artist=artists,
                    album="",
                    duration=float((it.get("duration_ms") or 0) / 1000.0),
                    art_url=art_url,
                    extra={"id": it.get("id") or ""},
                ))
            offset += len(items)
        return out
