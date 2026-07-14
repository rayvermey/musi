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
                    # playlist-CUD + Liked Songs-mutatie (toegevoegd voor
                    # playlist-beheer; vereist eenmalige browser-re-consent):
                    "playlist-modify-public", "playlist-modify-private",
                    "user-library-modify",
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

    # ---- mutaties (playlist-CUD + Liked Songs) -----------------------------
    # Convention: ``(success: bool, error: str | None)`` — UI kan uniform
    # dispathen op success/notify bij failure. ``last_error`` wordt gezet op
    # failure voor parity met de read-methoden.

    async def add_to_saved_tracks(self, uris: list[str]) -> tuple[bool, str | None]:
        """Voeg tracks toe aan Liked Songs. ``uris`` zijn ``spotify:track:…``."""
        if not uris:
            return True, None
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)
        try:
            await asyncio.to_thread(partial(self._sp.current_user_saved_tracks_add, uris))
            self.last_error = None
            return True, None
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)

    async def remove_from_saved_tracks(self, uris: list[str]) -> tuple[bool, str | None]:
        """Verwijder tracks uit Liked Songs. Idempotent — niet-aanwezige tracks
        worden genegeerd door Spotify."""
        if not uris:
            return True, None
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)
        try:
            await asyncio.to_thread(partial(self._sp.current_user_saved_tracks_delete, uris))
            self.last_error = None
            return True, None
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)

    async def create_playlist(self, name: str, public: bool = True,
                              description: str = "") -> dict | None:
        """Maak een nieuwe playlist aan voor de ingelogde user. Geeft de verse
        playlist-dict terug (incl. ``uri``, ``id``, ``name``, ``owner``,
        ``tracks: {total: 0}``) of ``None`` bij failure.

        ``public=True`` zet 'm op openbaar; met de nieuwe scopes mag dat. Voor
        private playlists: ``public=False``.
        """
        name = (name or "").strip()
        if not name:
            return None
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return None
        try:
            # spotipy's ``user_playlist_create`` heeft de user_id als eerste
            # argument; we gebruiken ``me`` zodat 't bij de ingelogde user komt.
            user_id = (await asyncio.to_thread(self._sp.current_user))["id"]
            pl = await asyncio.to_thread(
                partial(self._sp.user_playlist_create, user_id, name,
                        public=public, description=description or None)
            )
            self.last_error = None
            return pl
        except Exception as e:
            self.last_error = str(e)
            return None

    async def add_to_playlist(self, playlist_uri: str, uris: list[str]
                              ) -> tuple[bool, str | None]:
        """Voeg tracks toe aan een playlist (gegeven als ``spotify:playlist:…``)."""
        if not uris:
            return True, None
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)
        try:
            await asyncio.to_thread(partial(self._sp.playlist_add_items, playlist_uri, uris))
            self.last_error = None
            return True, None
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)

    async def remove_from_playlist(self, playlist_uri: str, uris: list[str]
                                   ) -> tuple[bool, str | None]:
        """Verwijder alle occurrences van ``uris`` uit de playlist."""
        if not uris:
            return True, None
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)
        try:
            await asyncio.to_thread(
                partial(self._sp.playlist_remove_all_occurrences, playlist_uri, uris)
            )
            self.last_error = None
            return True, None
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)

    async def rename_playlist(self, playlist_uri: str, *, name: str | None = None,
                              public: bool | None = None) -> tuple[bool, str | None]:
        """Wijzig playlist-naam en/of public/private. Geef alleen de velden mee
        die je wilt wijzigen (``None`` = ongewijzigd)."""
        if name is None and public is None:
            return True, None
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)
        try:
            kwargs: dict[str, object] = {}
            if name is not None:
                kwargs["name"] = name
            if public is not None:
                kwargs["public"] = public
            await asyncio.to_thread(
                partial(self._sp.playlist_change_details, playlist_uri, **kwargs)
            )
            self.last_error = None
            return True, None
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)

    async def delete_playlist(self, playlist_uri: str) -> tuple[bool, str | None]:
        """Unfollow de playlist — verdwijnt uit de account van de ingelogde user.

        Voor eigen playlists is dit equivalent aan verwijderen. Voor gevolgde
        playlists van anderen haalt het alleen de follow weg; de playlist zelf
        blijft bestaan bij de maker.
        """
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)
        try:
            await asyncio.to_thread(
                partial(self._sp.current_user_unfollow_playlist, playlist_uri)
            )
            self.last_error = None
            return True, None
        except Exception as e:
            self.last_error = str(e)
            return False, str(e)

    async def playlist_contains(self, playlist_uri: str, uris: list[str]
                                ) -> list[bool]:
        """Voor elk van ``uris``: bepaal of 't in de playlist zit (zelfde volgorde).

        Spotify staat max 100 URIs per call toe; we chunken.
        """
        out: list[bool] = []
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return []
        for i in range(0, len(uris), 100):
            chunk = uris[i:i + 100]
            try:
                res = await asyncio.to_thread(
                    partial(self._sp.playlist_contains, playlist_uri, chunk)
                )
            except Exception as e:
                log.warning("playlist_contains mislukte: %s", e)
                # failure → conservatief "weet niet" (False) voor alle resterende
                out.extend([False] * len(chunk))
                continue
            # spotipy geeft een list[bool] terug van gelijke lengte als chunk
            for j, present in enumerate(res):
                if j < len(out):
                    out[j] = bool(present)
                else:
                    out.append(bool(present))
            # als res te kort was, vul aan met False
            while len(out) < i + len(chunk):
                out.append(False)
        self.last_error = None
        return out

    async def saved_track_count(self) -> int:
        """Aantal Liked Songs. ``current_user_saved_tracks(limit=1)`` is genoeg
        om het totaal te lezen zonder alles te pagineren."""
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return 0
        try:
            res = await asyncio.to_thread(
                partial(self._sp.current_user_saved_tracks, limit=1)
            )
            self.last_error = None
            return int((res or {}).get("total") or 0)
        except Exception as e:
            self.last_error = str(e)
            return 0

    # ---- artiest + album + genre ----------------------------------------
    # Alle read-methoden voor de nieuwe zoekflows (``/spotify`` Top-resultaat,
    # ``/spotify-artiest``, ``/spotify-genre``). Geen mutaties hier — die
    # zitten in de playlist-sectie hierboven.

    async def search_all(self, query: str, limit: int = 20
                         ) -> dict[str, Any]:
        """Multi-type zoek (track + artist + album) voor de Top-resultaat-kaart
        in ``/spotify``. Geeft ``{"tracks": [...], "artists": [...], "albums": [...]}``
        terug; elk lijst-element is een onbewerkte Spotify-dict (de UI haalt
        er een badge/cover/naam uit). Bij één lege subset is dat geen
        failure — alleen de collectieve ``last_error`` wordt op None gezet
        bij success.
        """
        query = (query or "").strip()
        if not query:
            return {"tracks": [], "artists": [], "albums": []}
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return {"tracks": [], "artists": [], "albums": []}
        try:
            res = await asyncio.to_thread(partial(
                self._sp.search, q=query, type="track,artist,album",
                limit=limit))
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)
            return {"tracks": [], "artists": [], "albums": []}
        return {
            "tracks": (res or {}).get("tracks", {}).get("items") or [],
            "artists": (res or {}).get("artists", {}).get("items") or [],
            "albums": (res or {}).get("albums", {}).get("items") or [],
        }

    async def artist(self, artist_id_or_uri: str) -> dict[str, Any] | None:
        """Haal één artiest op (dict met o.a. ``name``, ``followers``,
        ``images``, ``genres``, ``popularity``). Geeft None bij fout/missing.
        Accepteert zowel bare IDs (``0OdUWJ0sBjDrqHygGUXeCF``) als URIs
        (``spotify:artist:0OdUWJ0sBjDrqHygGUXeCF``)."""
        artist_id = self._strip_uri(artist_id_or_uri, "artist")
        if not artist_id:
            return None
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return None
        try:
            res = await asyncio.to_thread(partial(self._sp.artist, artist_id))
            self.last_error = None
            return res
        except Exception as e:
            self.last_error = str(e)
            return None

    async def artist_top_tracks(self, artist_id_or_uri: str,
                                 market: str = "US") -> list[dict[str, Any]]:
        """Spotify's 'Top tracks' (≤10) voor een artiest in een bepaalde markt.
        Geeft een lijst track-dicts terug (geschikt voor ``_to_track``)."""
        artist_id = self._strip_uri(artist_id_or_uri, "artist")
        if not artist_id:
            return []
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return []
        try:
            res = await asyncio.to_thread(partial(
                self._sp.artist_top_tracks, artist_id, country=market))
            self.last_error = None
            return (res or {}).get("tracks") or []
        except Exception as e:
            self.last_error = str(e)
            return []

    async def artist_albums(self, artist_id_or_uri: str, limit: int = 50,
                            groups: str = "album,single,compilation"
                            ) -> list[dict[str, Any]]:
        """Albums/singles/compilaties van een artiest. ``groups`` bepaalt welke
        album-typen worden meegenomen; standaard alles behalve 'appears_on'.

        Geeft een lijst album-dicts terug (volledig; voor o.a. cover-URLs).
        Wordt gepagineerd tot ``limit`` of tot Spotify stopt.
        """
        artist_id = self._strip_uri(artist_id_or_uri, "artist")
        if not artist_id:
            return []
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return []
        out: list[dict[str, Any]] = []
        offset = 0
        page = min(50, limit)
        while offset < limit:
            try:
                res = await asyncio.to_thread(partial(
                    self._sp.artist_albums, artist_id,
                    include_groups=groups, limit=page, offset=offset))
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
                break
            items = (res or {}).get("items") or []
            if not items:
                break
            out.extend(items)
            offset += len(items)
            if len(items) < page:
                break
        return out

    async def album_full(self, album_id_or_uri: str) -> dict[str, Any] | None:
        """Haal één album op incl. tracks, label, release-datum, cover.
        Geeft None bij fout/missing.

        Accepteert zowel bare ID als ``spotify:album:…`` URI.
        """
        album_id = self._strip_uri(album_id_or_uri, "album")
        if not album_id:
            return None
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return None
        try:
            res = await asyncio.to_thread(partial(self._sp.album, album_id))
            self.last_error = None
            return res
        except Exception as e:
            self.last_error = str(e)
            return None

    async def categories(self, country: str = "US", limit: int = 50
                         ) -> list[dict[str, Any]]:
        """Spotify's top-level genre-categorieën ('Rock', 'Hip-Hop', 'Workout',
        …). Geeft een lijst van category-dicts terug (``id``, ``name``,
        ``icons``). Locale-gevoelig; default US.

        Houd er rekening mee dat ``category_playlists`` (drill-down) door
        Spotify is verwijderd (404 vanaf eind 2024); we doen de
        drill-down via ``search_playlists_by_tag`` (zie hieronder) op de
        category-naam of -slug als tag.
        """
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return []
        out: list[dict[str, Any]] = []
        offset = 0
        page = min(50, limit)
        while offset < limit:
            try:
                res = await asyncio.to_thread(partial(
                    self._sp.categories, country=country,
                    limit=page, offset=offset))
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
                break
            items = ((res or {}).get("categories") or {}).get("items") or []
            if not items:
                break
            out.extend(items)
            offset += len(items)
            if len(items) < page:
                break
        return out

    async def search_playlists_by_tag(self, tag: str, limit: int = 20
                                      ) -> list[dict[str, Any]]:
        """Zoek playlists die getagd zijn met ``tag`` — Spotify's
        playlist-search accepteert de query-tag (bv. ``q='rock'``). Geeft
        playlist-dicts terug (volledig, incl. images).

        Dit is de workaround voor de door Spotify verwijderde
        ``category_playlists``-endpoint: we gebruiken een vrij-tekst-query
        op de category-naam (bv. ``rock``, ``hip-hop``, ``jazz``).
        """
        tag = (tag or "").strip().strip('"')
        if not tag:
            return []
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return []
        try:
            res = await asyncio.to_thread(partial(
                self._sp.search, q=tag, type="playlist", limit=limit))
            self.last_error = None
            return ((res or {}).get("playlists") or {}).get("items") or []
        except Exception as e:
            self.last_error = str(e)
            return []

    async def search_artists_by_tag(self, tag: str, limit: int = 20
                                    ) -> list[dict[str, Any]]:
        """Zoek artiesten die getagd zijn met ``tag`` — Spotify staat de
        speciale query ``tag:"rock"`` (universeler dan ``genre:``) toe in
        de artist-search. Geeft een lijst artist-dicts terug (volledig;
        voor o.a. cover-URLs).

        ``tag`` is een losse tag-string ('rock', 'jazz', 'dutch', etc.).
        Werkt voor tags die Spotify ook als artiest-tag kent (anders
        lege lijst).
        """
        tag = (tag or "").strip().strip('"')
        if not tag:
            return []
        q = f'tag:"{tag}"'
        try:
            await asyncio.to_thread(self._ensure_client)
        except Exception as e:
            self.last_error = str(e)
            return []
        try:
            res = await asyncio.to_thread(partial(
                self._sp.search, q=q, type="artist", limit=limit))
            self.last_error = None
            return ((res or {}).get("artists") or {}).get("items") or []
        except Exception as e:
            self.last_error = str(e)
            return []

    @staticmethod
    def _strip_uri(value: str, kind: str) -> str:
        """Helper: haal de ID uit een Spotify-URI (``spotify:<kind>:<id>``) of
        geef de waarde ongewijzigd terug als die geen URI-patroon matcht.
        ``kind`` is 'artist', 'album', 'playlist', of 'track' (ter info —
        niet gebruikt voor validatie)."""
        if not value:
            return ""
        if value.startswith("spotify:"):
            parts = value.split(":")
            if len(parts) >= 3:
                return parts[-1]
        return value
