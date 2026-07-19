"""YouTube-zoekprovider via yt-dlp (geen API-key nodig).

Twee soorten aanvragen, alle via ``yt-dlp … --flat-playlist -J`` (één JSON-object
naar stdout, zónder de video's te resolv'en — snel):

* **Zoek** — ``yt-dlp "ytsearch{N}:{query}" --flat-playlist -J``.
* **Feeds** — een YouTube-URL (kanaal, playlist, subscriptions-feed) opgeven.
  ``https://www.youtube.com/feed/subscriptions`` levert de subfeed; de
  favorieten/watch-later/history-playlists via ``?list=FL``/``WL``/``HL``.

De resulterende watch-URL's voeden we later aan de mpv-engine (mpv roept intern
yt-dlp aan voor de audio-stream).

Cookies: veel feeds vereisen dat je ingelogd bent. Zet daarom
``cookies_from_browser`` (uit ``[youtube]`` in config.toml) — yt-dlp leest dan je
YouTube-sessie **live uit de cookie-database van die browser** (geen
platte-tekst cookie-file op schijf). Leeg = publiek (zoek werkt, subs/fav/WL
blijven leeg). Zie README "YouTube-subscriptions & video".

Resultaten worden genormaliseerd naar ``Track``: artist = uploader/kanaal;
``extra`` bevat ``id`` (video-id) en ``channel_id`` (voor eventuele
kanaal-drill-down).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
from typing import Iterable
from urllib.parse import quote_plus

from ..models import Track
from .base import SearchProvider

log = logging.getLogger(__name__)

SEARCH_TIMEOUT = 45.0  # seconden — yt-dlp kan traag zijn bij grootte feeds


class YouTubeFeedUnavailable(RuntimeError):
    """YouTube weigert deze specifieke feed (FL/WL/HL-stijl).

    YouTube serveert de virtual system-playlists (``?list=FL`` voor Favorieten,
    ``?list=WL`` voor Watch Later, ``?list=HL`` voor History) niet meer als
    externe feed — dat is een YouTube-side beslissing, geen cookie-probleem.
    We laten 't expliciet falen zodat de UI een eerlijke melding kan tonen
    ("YouTube serveert deze virtual playlist niet meer") i.p.v. de generieke
    "0 resultaten — ben je ingelogd?"-hint.

    ``kind`` is ``"unviewable"`` (WL/HL) of ``"bad_request"`` (FL).
    """
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

# Non-flat ("date"-sort) heeft een eigen, veel ruimere time-out: yt-dlp moet
# elke video-pagina resolv'en voor z'n upload-datum, wat ~1-3s/video kost.
# Voor 20 video's ~30-60s; 180s is de veilige bovengrens.
SEARCH_TIMEOUT_DATE = 180.0

# Max aantal gelijktijdige per-video fetches in ``_search_by_date``. We halen
# een pool van ``2 × limit`` video's op en resolven die parallel; onbegrensd
# (30 tegelijk) trigert YouTube-throttling waardoor juist wéér video's
# ontbreken. ~8 tegelijk is een goeie balans (voldoende snel, zacht genoeg).
MAX_PARALLEL_DATE_FETCH = 8


def _entry_upload_ts(entry: dict) -> int:
    """Grootste geldige upload-datum uit een non-flat yt-dlp-entry als unix
    epoch (seconden). Bron-velden, in volgorde van voorkeur:

    * ``timestamp`` — unix epoch (yt-dlp search-extractor, zelden gezet).
    * ``release_timestamp`` — officiële release (kan in de toekomst liggen
      bij previews; search-extractor geeft 'm meestal niet).
    * ``upload_date`` — ``"YYYYMMDD"``-string (altijd gezet bij een
      video-pagina-fetch). Vallen we hier op terug voor de parallelle
      per-video fetches in ``_search_by_date``.

    Geeft 0 bij niets gevonden — die komt dan onderaan bij aflopende
    sortering."""
    ts = int(entry.get("timestamp") or 0)
    if ts:
        return ts
    rts = int(entry.get("release_timestamp") or 0)
    if rts:
        return rts
    ud = entry.get("upload_date")
    if ud and len(str(ud)) == 8 and str(ud).isdigit():
        # "YYYYMMDD" → midnight UTC als epoch (seconden).
        s = str(ud)
        y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
        return int(datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc).timestamp())
    return 0


class YouTubeSearch(SearchProvider):
    """YouTube via yt-dlp: zoek + subscriptions/favorieten/watch-later/history/
    kanaal/playlist. Allemaal hetzelfde ``--flat-playlist -J``-patroon."""

    name = "youtube"
    label = "YouTube"

    def __init__(self, ytdlp_bin: str = "yt-dlp",
                 cookies_from_browser: str = "") -> None:
        """Args:
            ytdlp_bin: yt-dlp-binary (standaard op PATH).
            cookies_from_browser: browser-naam (``"firefox"``/``"chrome"``/...) of
                leeg voor publiek. Zie module-docstring voor cookies-flow.
        """
        self._bin = ytdlp_bin
        self._cookies_from_browser = cookies_from_browser or ""

    # ---- publieke API -------------------------------------------------
    async def search(self, query: str, limit: int = 20,
                     sort: str = "relevance") -> list[Track]:
        """Zoek YouTube-video's op ``query``.

        ``sort="date"`` toont de meest recente video's eerst. YouTube's gewone
        ``ytsearch`` sorteert op relevantie (oude populaire video's verdringen
        recente uploads), dus gebruiken we in ``_search_by_date`` een recency-
        gefilterde pool (``sp=CAISAhAB``) en sorteren we de datum zélf — zie
        daar voor de details. Duurt ~20-40s voor 15 resultaten (elke video moet
        geresolved worden); ``SEARCH_TIMEOUT_DATE`` dekt het.
        """
        query = query.strip()
        if not query:
            return []
        if sort == "date":
            return await self._search_by_date(query, limit)
        data = await self._run_yt_dlp([f"ytsearch{limit}:{query}"])
        return self._feed_to_tracks(data)

    async def _search_by_date(self, query: str, limit: int) -> list[Track]:
        """Meest recente video's eerst.

        Twee problemen die we hier oplossen:

        1. **Relevantie verdringt recency** — ``ytsearchN:`` sorteert op
           relevantie, dus de top-N bevat populaire *oude* video's, niet de
           nieuwste. Oplossing: zoek via de URL met ``sp=CAISAhAB``
           (YouTube's "Sorteren op uploaddatum"-filter). Dat levert alléén
           video's (geen kanalen/playlists) én bias-t sterk richting recente
           uploads.
        2. **YouTube's ``sp``-sortering is via yt-dlp niet strikt** — we kunnen
           de volgorde niet zomaar vertrouwen. Oplossing: haal een **pool van
           ``2 × limit``** op (recentere video's die lager in de lijst staan
           worden zo alsnog gevangen), resolven elke upload-datum parallel, en
           sorteren daarna **lokaal** op datum desc.

        Robuustheid: zelfs als YouTube de ``sp`` ooit negeert/wijzigt, blijven
        de resultaten datum-gesorteerd (uit een relevance-pool = oud gedrag);
        levert de ``sp``-URL niets, dan vallen we terug op ``ytsearch``.
        """
        # Stap 1: brede recency-pool (videos-only) ophalen, flat.
        pool = max(limit * 2, 20)
        data = await self._run_yt_dlp(
            [f"https://www.youtube.com/results?search_query={quote_plus(query)}"
             f"&sp=CAISAhAB"],
            extra_args=["--playlist-items", f"1:{pool}"],
        )
        entries = [e for e in (data.get("entries") or []) if e]
        # Fallback: levert de sp-URL niets (bv. token gewijzigd), dan gewone
        # ytsearch. Liever relevance-met-datum dan helemaal niets.
        if not entries:
            data = await self._run_yt_dlp([f"ytsearch{pool}:{query}"])
            entries = [e for e in (data.get("entries") or []) if e]
        if not entries:
            return []
        # Stap 2: parallel de upload-datum + stats per video ophalen. Elke
        # watch-URL krijgt een eigen ``yt-dlp``-call (geen flat-playlist, enkel
        # die ene video) zodat ``upload_date``/``view_count``/``like_count``
        # beschikbaar komen. De semaphore houdt 't op ``MAX_PARALLEL_DATE_FETCH``
        # tegelijk — onbegrensd trigert YouTube-throttling (→ ontbrekende
        # video's, precies wat we proberen te voorkomen).
        sem = asyncio.Semaphore(MAX_PARALLEL_DATE_FETCH)

        async def _date_for(entry: dict) -> tuple[dict, int]:
            url = entry.get("url") or ""
            if not url:
                return entry, 0
            async with sem:
                meta = await self._run_yt_dlp(
                    [url], timeout=SEARCH_TIMEOUT_DATE, flat=False,
                )
            # yt-dlp geeft voor één watch-URL de video-data als top-level dict
            # (geen "entries"-wrapper). Neem de video-pagina-stats over op de
            # flat-entry — die had geen upload-datum, en like_count (en bij
            # live-streams ook view_count) ontbreken in flat-ytsearch.
            for k in ("timestamp", "release_timestamp",
                      "upload_date", "view_count", "like_count"):
                val = (meta or {}).get(k)
                if val is not None:
                    entry[k] = val
            return entry, _entry_upload_ts(entry)

        dated = await asyncio.gather(*(_date_for(e) for e in entries))
        # Stap 3: lokaal sorteren op datum desc (zonder datum onderaan) en de
        # ``limit`` meest recente houden.
        dated_entries = sorted((d[0] for d in dated),
                               key=_entry_upload_ts, reverse=True)[:limit]
        return self._feed_to_tracks({"entries": dated_entries})

    def _check_feed_error(self, data: dict, label: str) -> None:
        """Raise ``YouTubeFeedUnavailable`` als ``_run_yt_dlp`` een getagd
        virtual-playlist-failure achterliet. Anders stilletjes door (lege
        lijst = "geen video's gevonden", wat voor echte subscriptions-feed
        een werkbaar resultaat is).

        Geeft de UI genoeg detail om een eerlijke foutmelding te tonen
        ("YouTube serveert 'Favorieten' niet meer als virtual playlist")
        i.p.v. de misleidende "Ingelogd in je browser?"-hint.
        """
        err = data.get("_yt_error")
        if not err:
            return
        if err == "bad_request":
            raise YouTubeFeedUnavailable(
                "bad_request",
                f"YouTube weigerde {label} (HTTP 400) — "
                f"virtual playlist wordt niet meer geserveerd")
        if err == "unviewable":
            raise YouTubeFeedUnavailable(
                "unviewable",
                f"YouTube gaf '{label}' als unviewable — "
                f"virtual playlist wordt niet meer geserveerd")
        # Onbekende tag: laat 't op de generieke manier falen via {}.

    async def subscriptions(self, limit: int = 100) -> list[Track]:
        """Recente uploads uit je YouTube-subscriptions. **Vereist cookies** —
        zonder login levert de feed een lege lijst."""
        data = await self._run_yt_dlp(
            ["https://www.youtube.com/feed/subscriptions"],
            extra_args=["--playlist-items", f"1:{limit}"],
        )
        self._check_feed_error(data, "Subscriptions")
        return self._feed_to_tracks(data, playlist_id="subscriptions")

    async def favorites(self, limit: int = 100) -> list[Track]:
        """Eigen 'Favorieten' playlist (``?list=FL``). **Vereist cookies.**

        Sinds 2024 weigert YouTube ``?list=FL`` met HTTP 400 — de virtual
        Favorieten-playlist wordt niet meer geserveerd aan externe clients.
        We raisen dan ``YouTubeFeedUnavailable("bad_request", …)`` zodat de
        UI dat netjes kan tonen."""
        data = await self._run_yt_dlp(
            ["https://www.youtube.com/playlist?list=FL"],
            extra_args=["--playlist-items", f"1:{limit}"],
        )
        self._check_feed_error(data, "Favorieten")
        return self._feed_to_tracks(data, playlist_id="FL")

    async def watch_later(self, limit: int = 100) -> list[Track]:
        """Eigen 'Watch Later' playlist (``?list=WL``). **Vereist cookies.**

        YouTube classificeert deze als ``"unviewable"`` voor externe clients;
        we raisen ``YouTubeFeedUnavailable("unviewable", …)``."""
        data = await self._run_yt_dlp(
            ["https://www.youtube.com/playlist?list=WL"],
            extra_args=["--playlist-items", f"1:{limit}"],
        )
        self._check_feed_error(data, "Watch Later")
        return self._feed_to_tracks(data, playlist_id="WL")

    async def history(self, limit: int = 50) -> list[Track]:
        """Eigen 'History' (recent bekeken, ``?list=HL``). **Vereist cookies.**
        YouTube geeft deze meestal als 'unviewable' terug (zie watch_later)."""
        data = await self._run_yt_dlp(
            ["https://www.youtube.com/playlist?list=HL"],
            extra_args=["--playlist-items", f"1:{limit}"],
        )
        self._check_feed_error(data, "History")
        return self._feed_to_tracks(data, playlist_id="HL")

    async def channel(self, channel_id_or_url: str, limit: int = 50) -> list[Track]:
        """Recente videos van één kanaal (publiek, of via cookies als privé).

        ``channel_id_or_url`` mag een volledige URL zijn of alleen een channel-id
        (dan bouwen we de ``/videos``-URL zelf).
        """
        url = channel_id_or_url
        if not url.startswith("http"):
            url = f"https://www.youtube.com/channel/{channel_id_or_url}/videos"
        data = await self._run_yt_dlp(
            [url], extra_args=["--playlist-items", f"1:{limit}"],
        )
        return self._feed_to_tracks(data)

    async def playlist(self, playlist_url: str, limit: int = 100) -> list[Track]:
        """Een willekeurige YouTube-playlist (eigen of publiek van een ander)."""
        data = await self._run_yt_dlp(
            [playlist_url],
            extra_args=["--playlist-items", f"1:{limit}"],
        )
        return self._feed_to_tracks(data)

    # ---- intern -------------------------------------------------------
    def _yt_dlp_args(self, extra: Iterable[str] = (),
                     flat: bool = True) -> list[str]:
        """Standaard yt-dlp-args voor zoek + feed-fetches: ``flat=True`` geeft
        watch-URLs terug zonder resolv'en (snel, geen upload-datum per entry);
        ``flat=False`` resolved elke video (~1-3s/video) zodat ``timestamp``
        beschikbaar komt — gebruikt voor ``sort="date"``.

        Cookies-flag erbij als ``cookies_from_browser`` gezet is. Bij cookies
        hangen we ook ``--extractor-args youtubetab:skip=authcheck`` aan: de
        ``youtube:tab``-extractor waarschuwt "may not extract correctly without
        a successful webpage download" zodra 'ie ingelogde playlists ziet, en
        zonder die flag behandelt 'ie de waarschuwing als fataal (exit 1, stdout
        ``null``) óók als de cookies prima werken — dat is precies wat maakt
        dat subscriptions/Favorites leeg lijken terwijl je wél ingelogd bent.
        Met ``skip=authcheck`` gaat 'm door en gebruikt 'ie de cookies die 'ie
        net zélf uit de browser las."""
        args: list[str] = [
            self._bin,
            "--no-warnings",
            "--skip-download",
            "-J",                   # één JSON-object naar stdout
        ]
        if flat:
            args.append("--flat-playlist")
        if self._cookies_from_browser:
            # yt-dlp leest YouTube-cookies direct uit de browser; geen
            # plaintext cookies in eigen files.
            args.extend(["--cookies-from-browser", self._cookies_from_browser])
            # Sla de auth-check over — anders weigert yt-dlp de hele feed
            # bij ingelogde playlists (subscriptions/Favorites) terwijl de
            # cookies gewoon werken.
            args.extend(["--extractor-args", "youtubetab:skip=authcheck"])
        args.extend(extra)
        return args

    async def _run_yt_dlp(self, urls: list[str],
                          extra_args: Iterable[str] = (),
                          timeout: float = SEARCH_TIMEOUT,
                          flat: bool = True) -> dict:
        """Spawn yt-dlp met de gegeven URLs + flags; returnt de JSON als dict.

        Bij fout/time-out/ongeldige JSON retourneren we ``{}`` (zodat callers
        via ``_feed_to_tracks`` netjes een lege lijst krijgen) i.p.v. te raisen
        — één falende call mag de UI niet breken.

        Args:
            urls: te fetchen URLs (1 of meer; worden vóór extra_args geplaatst).
            extra_args: extra yt-dlp-vlaggen (bv ``--playlist-items 1:N``).
            timeout: max wachttijd.
            flat: ``True`` (default) = ``--flat-playlist`` (snel, geen datum);
                ``False`` = resolved entries (trager, mét timestamp) voor
                datum-sortering. **Belangrijk:** geef ``flat=flat`` altijd door
                aan ``_yt_dlp_args`` — een verdwaalde call zónder maakt dat de
                per-video datum-fetch hieronder stiekem wél flat blijft (geen
                upload-datum/views/likes).

        Speciale gevallen (terug in ``data`` zodat de UI ze kan onderscheiden
        van "geen resultaat"):

        * ``{"_yt_error": "unviewable"}`` — YouTube zegt dat dit type playlist
          (bv ``?list=WL``/``?list=HL``) niet meer als virtual playlist
          opvraagbaar is; gebruiker moet z'n eigen playlist-URL gebruiken.
        * ``{"_yt_error": "bad_request"}`` — YouTube weigerde de aanroep met
          HTTP 400 (bv ``?list=FL`` sinds 2024).
        * Anders ``{}`` bij falen (lege lijst, UI toont generieke melding).
        """
        cmd = self._yt_dlp_args(urls + list(extra_args), flat=flat)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("yt-dlp time-out voor %s", urls)
            return {}
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")
            log.warning("yt-dlp exit %s voor %s: %s",
                        proc.returncode, urls, err[:300])
            # Onderscheid virtual-playlist-failures (YouTube-side) van
            # generieke fouten — anders bagatelliseren we een echte 400 tot
            # "0 resultaten, ben je ingelogd?".
            if "This playlist type is unviewable" in err:
                return {"_yt_error": "unviewable"}
            if "HTTP Error 400" in err or "Bad Request" in err:
                return {"_yt_error": "bad_request"}
            return {}
        # sitecustomize.py (in deze venv) print ``SITECUSTOMIZE_LOADED`` /
        # ``SILENCED_DASBUS`` op stdout vóór het échte script-output. Omdat
        # ``yt-dlp`` ook in deze venv draait, krijgt élke yt-dlp-aanroep die
        # prefix. We skippen niet-JSON-voorvoegsel door pas vanaf de eerste
        # ``{`` te parsen — robuust tegen willekeurige pre-output en blijft
        # werken als sitecustomize later wegvalt.
        raw = stdout.decode(errors="replace")
        start = raw.find("{")
        if start < 0:
            log.warning("yt-dlp gaf geen JSON terug voor %s", urls)
            return {}
        try:
            return json.loads(raw[start:])
        except json.JSONDecodeError:
            log.warning("yt-dlp gaf geen geldige JSON terug voor %s", urls)
            return {}

    def _feed_to_tracks(
        self, data: dict, playlist_id: str | None = None,
    ) -> list[Track]:
        """Zet een yt-dlp JSON-object om naar ``list[Track]``.

        Sleutels per entry: ``id`` (video-id), ``url`` (watch-URL, al dan niet
        expliciet — anders bouwen we 'm uit id), ``title``, ``uploader``/``channel``,
        ``duration``, ``thumbnails``/``thumbnail``, ``channel_id``,
        en alleen bij datum-mode (``_search_by_date``/toekomstige pads):
        ``upload_date`` (``"YYYYMMDD"``), ``view_count``, ``like_count``.

        ``playlist_id`` is optioneel: als gezet komt 'm in ``Track.extra["yt_playlist_id"]``
        zodat de UI weet uit welke playlist een track afkomstig is. Voor
        search-resultaten is dit niet relevant.

        Velden die ontbreken blijven ``None`` op Track (UI toont ``"—"``).
        """
        tracks: list[Track] = []
        for entry in data.get("entries") or []:
            if entry is None:
                continue
            vid = entry.get("id")
            url = entry.get("url") or (
                f"https://www.youtube.com/watch?v={vid}" if vid else ""
            )
            if not url:
                continue
            thumbs = entry.get("thumbnails") or []
            art = (thumbs[0].get("url") if thumbs and "url" in thumbs[0]
                   else (entry.get("thumbnail") or ""))
            channel_id = (entry.get("channel_id")
                          or entry.get("uploader_id")
                          or entry.get("channel_url") or "")
            # YYYYMMDD of None (yt-dlp laat 'm weg als 't niet beschikbaar is)
            ud_raw = entry.get("upload_date")
            upload_date = ud_raw if (ud_raw and len(str(ud_raw)) == 8
                                     and str(ud_raw).isdigit()) else None
            extra = {"id": vid, "channel_id": channel_id}
            if playlist_id:
                extra["yt_playlist_id"] = playlist_id
            tracks.append(
                Track(
                    source="youtube",
                    engine="mpv",
                    uri=url,
                    title=entry.get("title") or "(geen titel)",
                    artist=(entry.get("uploader")
                            or entry.get("channel")
                            or "YouTube"),
                    album="",
                    duration=float(entry.get("duration") or 0),
                    art_url=art,
                    upload_date=upload_date,
                    view_count=int(entry["view_count"]) if entry.get("view_count") else None,
                    like_count=int(entry["like_count"]) if entry.get("like_count") else None,
                    extra=extra,
                )
            )
        return tracks
