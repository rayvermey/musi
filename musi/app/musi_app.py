"""Textual-TUI voor musi — drie tabs (Zoeken / Queue / Library) + now-playing footer.

Architectuur in één zin: ``MusiApp`` koppelt één ``Orchestrator`` + ``Services``,
vertaalt binnenkomende ``PlaybackEvent``s naar UI-updates (progressie in de
now-playing-bar, queue-view), en stuurt zoek/play-acties terug naar de
orchestrator.

Layout::

    ┌─ Header ─────────────────────────────────────────┐
    │ TabbedContent#tabs                               │
    │   • Zoeken  (SearchPane: Input + resultaten)     │
    │   • Queue    (QueuePane: queue-tabel)            │
    │   • Library  (LibraryPane: Lokaal/YouTube/Spotify)│
    ├─ NowPlaying (gedockt onderaan: hoes + titel + voortgang) ┤
    └─ Footer (toont de actieve keybindings)           ┘

Belangrijke widgets:
* **SearchPane** — zoek-Input + resultaten-DataTable. Bron-prefix (``/yt``,
  ``/lokaal``, ``/spotify``) bepaalt welke providers zoeken; anders alle parallel.
* **QueuePane** — toont de orchestrator-queue met de huidige track gemarkeerd.
* **LibraryPane** — bladeren per bron:
  * **Lokaal** (geneste subtabs): Nummers / Mappen / Albums / Artiesten;
  * **YouTube**: Subscriptions / Favorieten / Watch Later / History (meta+detail);
  * **Spotify**: albums/playlists links, tracks rechts (meta+detail).
* **NowPlaying** — albumhoes (sixel/halfblock via textual-image) + titel/artiest
  + positie/duur + bron-badge.

Keybindings (zie ook de Footer):
  * app-niveau: ``1/2/3`` tabs, ``/`` focus-zoek, ``spatie`` pauze, ``n/p``
    volgende/vorige, ``+/-`` volume, ``c`` queue wissen, ``v`` video-aan/uit,
    ``q`` quit;
  * pane-niveau (Library): ``enter`` spelen/drillen, ``a`` toevoegen, ``A``
    alles-spelen, ``u`` map omhoog.

Textual-gotchas waar deze app tegenaan loopt (uitgebreider in memory
``textual-event-and-tabs``): geen ``on_event`` override; ``Input.Submitted``
bubbelt niet ver, dus handmatig afvangen; geneste ``TabbedContent``s leveren
één ``TabActivated``-stroom (filter op ``pane.id``); en elke ``DataTable`` die
direct in een Vertical/TabPane staat **moet** ``height:1fr`` hebben of 'm
virtualiseert/scrollt niet (de "scrollt maar toont geen volgende rijen"-bug).
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess  # voor DEVNULL bij video-mpv-spawn
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    Static,
    TabbedContent,
    TabPane,
)

from ..models import Track
from ..orchestrator import Orchestrator, PlaybackEvent
from ..rip import Ripper, video_id
from ..services import Services

# textual_image.widget.SixelImage is een echte Textual-Widget die zes-data
# rechtstreeks naar de driver stuurt. Importeren op MODULE-NIVEAU is vereist:
# het widget doet een ``get_cell_size()``-call bij constructie om de
# terminal's pixel-per-cel te meten, en dat moet vóór Textual de terminal
# overneemt. ``renderable.Image`` gooit Textual's pipeline weg (printable-
# segment violation); een Widget-widget niet.
from textual_image.widget import SixelImage as SixelWidget  # noqa: E402
from .. import art as art_mod
from ..modals import ConfirmModal, EditTagsModal, PlaylistNameModal, PlaylistPickerModal


def _fmt_duration(s: float) -> str:
    """Seconden → ``"m:ss"`` (of ``"—:—"`` bij ≤0/onbekend). Voor tabel-cellen en
    de now-playing-bar."""
    if s <= 0:
        return "—:—"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"


def _fmt_upload_date(ud: str | None) -> str:
    """``"YYYYMMDD"`` → ``"YYYY-MM-DD"`` (YouTube-stijl kort), of ``"—"``.

    Accepteert enkel geldige 8-cijferige strings anders ``"—"`` — voorkomt
    dat halfgevulde data een crash veroorzaakt in de tabel-cel."""
    if not ud or len(ud) != 8 or not ud.isdigit():
        return "—"
    return f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}"


def _fmt_count(n: int | None) -> str:
    """YouTube-stijl afkapping voor views/likes: ``"1.2M"`` / ``"12K"`` /
    ``"123"`` / ``"—"`` (bij ``None`` of 0). Eén decimaal bij M en bij kleine
    K-waarden (1.0K–9.9K), geen decimaal bij ≥10K. ``int()`` kapt af in plaats
    van af te ronden, zodat 999 999 → ``"999K"`` (niet ``"1000K"``)."""
    if n is None or n <= 0:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{int(n / 1_000)}K"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _badge_for(track: Track | None) -> str:
    """Bron-badge van een track, of een puntje als 'm None is."""
    return track.badge if track else "·"


def _art_from_collection(coll: dict) -> str:
    """Eerste beschikbare image-URL uit een Spotify album/playlist-dict (voor
    albumhoes-doorplay aan album_tracks). Leeg als 'm er geen heeft."""
    for im in (coll.get("images") or []):
        if im.get("url"):
            return im["url"]
    return ""


class NowPlaying(Static):
    """Footer-balk met titel/artiest + positie/duur + bron-badge + progress-bar.
    Update-reactives (``progress``/``duration``/``status_text``) houden de
    weergave live sync. Bij een nieuwe track (uri-wissel) cache-bust'en we
    de albumhoes en proberen een nieuwe op te halen (``art.fetch``).
    """

    progress: reactive[float] = reactive(0.0)
    duration: reactive[float] = reactive(0.0)
    status_text: reactive[str] = reactive("gestopt")
    track: Track | None = None

    def __init__(self, art_dir, **kw):
        super().__init__(**kw)
        self._art_dir = art_dir
        self._last_track_id = None
        self._art_path = None

    def render(self):
        title = self.track.title if self.track else "—"
        artist = self.track.artist if self.track and self.track.artist else ""
        badge = _badge_for(self.track)
        dur = _fmt_duration(self.duration)
        pos = _fmt_duration(self.progress)
        bar_w = 30
        if self.duration > 0:
            filled = max(0, min(bar_w, round(self.progress / self.duration * bar_w)))
            bar = "█" * filled + "░" * (bar_w - filled)
        else:
            bar = "░" * bar_w
        left = f"{badge} {title}" + (f"  —  {artist}" if artist else "")
        right = f"{pos} / {dur}   {self.status_text}"
        line1 = f"{left}    [{right}]"
        line2 = f"{bar}"
        return f"{line1}\n{line2}"

    def update_from_state(self, state) -> None:
        self.track = state.track
        self.progress = state.position
        self.duration = state.duration
        self.status_text = {
            "playing": "▶ afspelen",
            "paused": "❙❙ pauze",
            "stopped": "□ gestopt",
        }.get(state.status.value, state.status.value)
        tid = (state.track.uri if state.track else None)
        if tid != self._last_track_id:
            self._last_track_id = tid
            self._art_path = None
            if state.track is not None:
                try:
                    from ..art import fetch
                    self._art_path = fetch(state.track, self._art_dir)
                except Exception:
                    self._art_path = None


class FloatingCover(Static):
    """Zwevend albumhoes-widget: sixel-rendered album art rechtsboven,
    bovenop de rest van de layout. ``update_from_state`` toont de hoes
    van de huidige track en wist 'm als er geen is."""

    DEFAULT_CSS = """
    FloatingCover {
        dock: top;
        layer: "floating";
        background: transparent;
        overflow: hidden;
        width: auto;
        height: auto;
    }
    FloatingCover SixelWidget {
        max-width: 20;
        max-height: 20;
        align: right top;
    }
    """

    def __init__(self, art_dir, **kw):
        super().__init__(**kw)
        self._art_dir = Path(art_dir)
        self._last_track_id: str | None = None
        self._art_path: str | None = None
        self._cover: SixelWidget | None = None

    def compose(self):
        self._cover = SixelWidget()
        yield self._cover

    def update_from_state(self, state) -> None:
        tid = (state.track.uri if state.track else None)
        if tid != self._last_track_id:
            self._last_track_id = tid
            self._art_path = None
            if state.track is not None:
                try:
                    from ..art import fetch
                    path = fetch(state.track, self._art_dir)
                    if path:
                        self._art_path = str(path)
                except Exception:
                    self._art_path = None
        if self._cover is not None and self.is_mounted:
            try:
                self._cover.image = self._art_path
            except Exception:
                pass


class SearchPane(Vertical):
    """Zoektab: een Input + een resultaten-DataTable (+ bron-label).

    De Input accepteert een optionele **bron-prefix** om per-bron te zoeken:
    ``/yt``/``/youtube`` (YouTube), ``/lokaal``/``/local``/``/l`` (lokaal),
    ``/spotify``/``/sp`` (Spotify). Zonder prefix zoeken alle providers parallel.

    ``enter`` is een priority-binding zodat 'm vóór de DataTable's eigen
    Enter-binding komt — zie ``action_play_now`` voor de focus-afhankelijke
    dispatch (Input gefocust → zoek; tabel gefocust → speel geselecteerde track).
    """

    BINDINGS = [
        # priority=True: wordt gecheckt vóór de DataTable's eigen Enter-binding
        # ("Select cells under the cursor"). Zonder priority eet de DataTable
        # Enter op en komt play_now nooit aan.
        Binding("enter", "play_now", "Speel/Open", priority=True),
        Binding("a", "add_to_queue", "Toevoegen", priority=False),
        # Spotify-save: opent de PlaylistPickerModal (Liked Songs / eigen
        # playlist / nieuwe playlist). Werkt op de resultaten-tabel.
        Binding("s", "save_to_playlist", "Opslaan"),
    ]

    def __init__(self, app: "MusiApp") -> None:
        super().__init__()
        self._app = app

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Zoek…  (/bron: /lokaal /yt /spotify · /date = YouTube nieuwste eerst + kolommen datum/views/likes)",
                    id="search-input")
        with Horizontal():
            yield Label("bron: alle", id="search-source-label")
        yield DataTable(id="results-table", cursor_type="row")

    # Kolom-sets voor de resultaten-tabel. Met ``stats`` toont de ``!date``-modus
    # de YouTube-statistieken (datum, views, likes); zonder blijft de tabel op
    # 5 kolommen. We herbouwen kolommen alleen als de set verandert om de
    # cursor-rij en sort-state niet te storen bij gelijke set.
    _COLS_PLAIN = ("♪", "Titel", "Artiest", "Duur", "Bron")
    _COLS_STATS = ("♪", "Titel", "Artiest", "Duur", "Bron", "Datum", "Views", "Likes")

    def on_mount(self) -> None:
        tbl = self.query_one("#results-table", DataTable)
        self._setup_columns(show_stats=False)
        self._cols_now = list(self._COLS_PLAIN)

    def _setup_columns(self, show_stats: bool) -> None:
        tbl = self.query_one("#results-table", DataTable)
        cols = self._COLS_STATS if show_stats else self._COLS_PLAIN
        tbl.clear(columns=True)
        tbl.add_columns(*cols)
        self._cols_now = list(cols)

    def watch_results(self, results: list[Track], show_stats: bool = False) -> None:
        """Render resultaten in de tabel. Bij ``show_stats=True`` (vanuit
        ``!date``-modus) krijgt de tabel 3 extra kolommen Datum/Views/Likes;
        anders blijft 't op 5. Tracks zonder YouTube-stats tonen ``"—"`` —
        blijft uniform."""
        tbl = self.query_one("#results-table", DataTable)
        cols = self._COLS_STATS if show_stats else self._COLS_PLAIN
        # Alleen kolommen herbouwen als de set verandert; anders alleen rijen
        # clear'en (zo blijft de cursor-positie bij 'n re-render binnen dezelfde
        # zoekopdracht behouden).
        if list(cols) != getattr(self, "_cols_now", None):
            self._setup_columns(show_stats)
        else:
            tbl.clear()
        for i, t in enumerate(results):
            if show_stats:
                tbl.add_row(t.badge, t.title, t.artist, _fmt_duration(t.duration),
                            t.source, _fmt_upload_date(t.upload_date),
                            _fmt_count(t.view_count), _fmt_count(t.like_count),
                            key=str(i))
            else:
                tbl.add_row(t.badge, t.title, t.artist, _fmt_duration(t.duration),
                            t.source, key=str(i))

    # ---- Spotify-prefix drill-renders -----------------------------------
    # ``watch_spotify_top_result`` plakt een Top-resultaat-rij boven de
    # reguliere track-rijen. Rij-key prefixes:
    #   ``a:<uri>`` = artiest, ``al:<uri>`` = album (drill-keys),
    #   ``t:<i>``   = echte track (afspeelbaar, savable).
    # De action-handlers dispatchen op basis van het key-prefix.

    def watch_spotify_top_result(self, query: str, top_artist: dict | None,
                                 top_album: dict | None,
                                 tracks: list[Track]) -> None:
        """Top-resultaat-kaart boven de track-lijst. Bij geen artiest én geen
        album: val terug op gewone ``watch_results``."""
        if not top_artist and not top_album:
            self.watch_results(tracks)
            return
        tbl = self.query_one("#results-table", DataTable)
        cols = self._COLS_PLAIN
        if list(cols) != getattr(self, "_cols_now", None):
            self._setup_columns(False)
        else:
            tbl.clear()
        # header-rij met de zoekterm
        tbl.add_row("", f"🔍 '{query}'", "", "", "Top-resultaat",
                    key="h:top")
        # artiest-rij (drill-key)
        if top_artist is not None:
            artist_name = top_artist.get("name") or "?"
            followers = top_artist.get("followers", {}).get("total") or 0
            genres = (top_artist.get("genres") or [])[:2]
            tbl.add_row(
                "🎤", artist_name,
                f"{followers:,} volgers" + (f" · {', '.join(genres)}" if genres else ""),
                "", "artiest", key=f"a:{top_artist.get('uri', '')}",
            )
        # album-rij (drill-key)
        if top_album is not None:
            alb_name = top_album.get("name") or "?"
            alb_artists = ", ".join(a["name"] for a in (top_album.get("artists") or []))
            yr = (top_album.get("release_date") or "")[:4]
            tbl.add_row(
                "💿", alb_name, alb_artists, yr or "—", "album",
                key=f"al:{top_album.get('uri', '')}",
            )
        # tracks onder de kaart (zoals normaal, maar met prefix t:)
        for i, t in enumerate(tracks):
            tbl.add_row(t.badge, t.title, t.artist,
                        _fmt_duration(t.duration), t.source, key=f"t:{i}")

    def watch_artist_top(self, artist: dict, top_tracks: list[Track]) -> None:
        """Render de top-tracks van een artiest (gevolg op /spotify-artiest).
        Rij 0 is een **drillbare** artiest-info-rij (key ``a:<uri>``) —
        Enter drillt naar de discografie (albums/singles/compilaties).
        Rijen 1+ zijn de top-tracks (afspeelbaar + savable).
        """
        tbl = self.query_one("#results-table", DataTable)
        if list(self._COLS_PLAIN) != getattr(self, "_cols_now", None):
            self._setup_columns(False)
        else:
            tbl.clear()
        name = artist.get("name") or "?"
        followers = artist.get("followers", {}).get("total") or 0
        genres = (artist.get("genres") or [])[:3]
        tbl.add_row(
            "🎤", name,
            f"{followers:,} volgers" + (f" · {', '.join(genres)}" if genres else ""),
            "", "artiest (Enter = discografie)",
            key=f"a:{artist.get('uri', '')}",
        )
        for i, t in enumerate(top_tracks):
            tbl.add_row(t.badge, t.title, t.artist,
                        _fmt_duration(t.duration), t.source, key=f"t:{i}")

    def watch_artist_detail(self, artist: dict,
                             albums: list[dict]) -> None:
        """Render de discografie van een artiest: header (informatief) +
        albums/singles/compilaties/appears_on als drill-rijen ``al:<uri>``.

        Rij 0 = header ``h:ad`` (Enter doet niets — informatief). Rijen 1+
        zijn albums (jaar als derde kolom, type-badge in de eerste kolom).

        Albums worden gegroepeerd op type met tussen-headers (``h:sg-albums``,
        ``h:sg-singles``, etc.) zodat de gebruiker kan onderscheiden wat
        hij/zij ziet. Sub-headers tellen mee in de rij-iterate maar skippen
        in Enter-dispatch (zelfde ``h:``-prefix als de artiest-header).
        """
        tbl = self.query_one("#results-table", DataTable)
        if list(self._COLS_PLAIN) != getattr(self, "_cols_now", None):
            self._setup_columns(False)
        else:
            tbl.clear()
        name = artist.get("name") or "?"
        followers = artist.get("followers", {}).get("total") or 0
        genres = (artist.get("genres") or [])[:3]
        # header
        tbl.add_row(
            "🎤", name,
            f"{followers:,} volgers" + (f" · {', '.join(genres)}" if genres else ""),
            "", f"{len(albums)} releases — Tab ↓",
            key="h:ad",
        )
        # groepeer albums per type
        groups: dict[str, list[dict]] = {
            "album": [], "single": [], "compilation": [], "appears_on": [],
        }
        for alb in albums:
            t = (alb.get("album_type") or "album").lower()
            groups.setdefault(t, []).append(alb)
        group_labels = [
            ("album", "💿 Albums"),
            ("single", "🎵 Singles & EPs"),
            ("compilation", "📀 Compilaties"),
            ("appears_on", "👥 Appears on"),
        ]
        for key, label in group_labels:
            items = groups.get(key, [])
            if not items:
                continue
            # sub-header (skip in Enter-dispatch)
            tbl.add_row(
                "", f"   {label}  ({len(items)})", "", "",
                "sectie-header",
                key=f"h:sg-{key}",
            )
            # albums in deze groep
            for alb in items:
                alb_name = alb.get("name") or "?"
                alb_artists = ", ".join(
                    a["name"] for a in (alb.get("artists") or [])
                    if a.get("name"))
                if not alb_artists:
                    alb_artists = name
                yr = (alb.get("release_date") or "")[:4] or "—"
                tbl.add_row(
                    "💿", alb_name, alb_artists, yr,
                    key or "album",
                    key=f"al:{alb.get('uri', '')}",
                )

    def watch_genre_categories(self, query: str,
                               categories: list[dict]) -> None:
        """Render Spotify's top-level genre-categorieën in de resultaten-tabel.
        Rij-keys zijn ``g:<id>`` (drill-keys)."""
        tbl = self.query_one("#results-table", DataTable)
        if list(self._COLS_PLAIN) != getattr(self, "_cols_now", None):
            self._setup_columns(False)
        else:
            tbl.clear()
        tbl.add_row("", f"🎵 Spotify-categorieën"
                     f"{' (filter: ' + query + ')' if query else ''}",
                     "", "", "kies een categorie", key="h:cat")
        for c in categories:
            tbl.add_row("📁", c.get("name") or "?",
                        c.get("id") or "", "", "categorie (drill)",
                        key=f"g:{c.get('id', '')}")

    def watch_genre_detail(self, category: dict, kind: str,
                            items: list, header: str = "") -> None:
        """Render een genre-detail-pagina (artiesten óf playlists). Rij-keys:
        ``i:<index>`` (afspeelbaar als Track) of speciale keys voor de
        categorie-header en view-selector.

        ``kind`` is ``"artists"`` of ``"playlists"``.
        """
        tbl = self.query_one("#results-table", DataTable)
        if list(self._COLS_PLAIN) != getattr(self, "_cols_now", None):
            self._setup_columns(False)
        else:
            tbl.clear()
        cat_name = category.get("name") or "?"
        view = "Artiesten" if kind == "artists" else "Playlists"
        tbl.add_row("", f"🎵 {cat_name} — {view}"
                     + (f"  ·  {header}" if header else ""),
                     "", "", "drill of speel",
                     key="h:gd")
        # View-toggle: 2 speciale rijen (druk 'a' voor artiesten, 'p' voor playlists)
        tbl.add_row("", "→ Tab: [A]rtiesten · [P]laylists",
                     "", "", "view-toggle", key="v:toggle")
        for i, it in enumerate(items):
            if kind == "artists":
                tbl.add_row(
                    "🎤",
                    it.get("name") or "?",
                    f"{(it.get('followers') or {}).get('total', 0):,} volgers",
                    "", "artiest (drill)",
                    key=f"i:{i}",
                )
            else:
                # playlist-dict met images + owner
                owner = ((it.get("owner") or {}).get("display_name")) or "—"
                tracks_total = (it.get("tracks") or {}).get("total") or 0
                tbl.add_row(
                    "💿",
                    it.get("name") or "?",
                    f"{owner} · {tracks_total} tracks",
                    "", "playlist (drill)",
                    key=f"i:{i}",
                )

    async def action_play_now(self) -> None:
        # priority=True op deze Enter-binding onderschept Enter óók als de
        # zoek-Input focus heeft — Input.Submitted wordt daarmee geblokkeerd
        # (priority-binding wordt vóór de focused widget's bindings gecheckt).
        # Fix: als de Input focus heeft, doe de zoekactie direct (en sla de
        # Input.Submitted-bubbling over, die in deze Textual-versie niet
        # betrouwbaar bij de parent aankomt).
        si = self.query_one("#search-input", Input)
        if si.has_focus:
            await self._app._on_search_submit(si.value)
            return
        # dispatch op rij-key-prefix:
        #   t:<i>        → echte track afspelen
        #   a:<uri>      → Spotify-artiest drillen
        #   al:<uri>     → Spotify-album drillen
        #   g:<id>       → genre-categorie drillen
        #   v:toggle     → view-switch in genre-detail
        #   i:<n>        → item in genre-detail
        #   h:*          → header-rij, doe niets
        key = self._selected_key()
        if key is None:
            return
        if key.startswith("h:"):
            return  # header-rij, geen actie
        if key.startswith("a:"):
            asyncio.create_task(self._app._open_spotify_artist_by_uri(key[2:]))
            return
        if key.startswith("al:"):
            asyncio.create_task(self._app._open_spotify_album_by_uri(key[3:]))
            return
        if key.startswith("g:"):
            view = getattr(self._app, "_genre_view", "artists")
            asyncio.create_task(self._app._open_spotify_genre_detail(
                key[2:], view=view))
            return
        if key == "v:toggle":
            asyncio.create_task(self._app._toggle_genre_detail_view())
            return
        if key.startswith("i:"):
            try:
                idx = int(key[2:])
            except ValueError:
                return
            asyncio.create_task(self._app._open_genre_detail_item(idx))
            return
        # fallback: oude numerieke key (zoekresultaten zonder prefix)
        if key.isdigit():
            i = int(key)
            results: list[Track] = self._app.results
            if i < len(results):
                await self._app.play_track(results[i])

    async def action_add_to_queue(self) -> None:
        i = self._selected_index()
        if i is None:
            return
        self._app.orchestrator.queue_add(self._app.results[i])

    async def action_save_to_playlist(self) -> None:
        """`s` op een zoekresultaat: open de picker (Liked Songs / playlist /
        nieuwe). Alleen zinvol voor Spotify-tracks; lokale/YT-tracks geven een
        duidelijke notify.

        Werkt op de top-resultaat-kaart (track-key ``t:<i>``) en op de
        artiest-top-tracks. Skip alle drill-keys (artiest/album/genre)."""
        key = self._selected_key()
        if key is None or not key.startswith("t:"):
            self._app.notify(
                "Selecteer een afspeelbaar Spotify-nummer (geen drill-rij).",
                severity="warning",
            )
            return
        try:
            i = int(key[2:])
        except ValueError:
            return
        results: list[Track] = self._app.results
        if i >= len(results):
            return
        track = results[i]
        if track.source != "spotify":
            self._app.notify(
                f"Alleen Spotify-tracks zijn savable: '{track.title}' is {track.source}.",
                severity="warning",
            )
            return
        self._app.open_save_picker([track.uri], track.title)

    def _selected_index(self) -> int | None:
        """Achterwaartse compat: geeft de int-index terug van een ``t:<i>``-
        of pure-numerieke key. Voor de nieuwe dispatch gebruik je
        ``_selected_key``."""
        key = self._selected_key()
        if key is None:
            return None
        if key.startswith("t:"):
            try:
                return int(key[2:])
            except ValueError:
                return None
        if key.isdigit():
            try:
                return int(key)
            except ValueError:
                return None
        return None

    def _selected_key(self) -> str | None:
        tbl = self.query_one("#results-table", DataTable)
        if tbl.row_count == 0:
            return None
        try:
            return tbl.coordinate_to_cell_key(tbl.cursor_coordinate).row_key.value
        except Exception:
            return None


class QueuePane(Vertical):
    """Queue-tab: één DataTable met de orchestrator-queue. De actieve track
    krijgt een ``▶``-markering. ``render_queue`` wordt aangeroepen vanuit de App
    bij elk queue-event."""

    BINDINGS = [
        # Spotify-save: opent de PlaylistPickerModal (Liked Songs / eigen
        # playlist / nieuwe playlist). Werkt op de queue-rij.
        Binding("s", "save_to_playlist", "Opslaan"),
    ]

    def __init__(self, app: "MusiApp") -> None:
        super().__init__()
        self._app = app

    def compose(self) -> ComposeResult:
        yield DataTable(id="queue-table", cursor_type="row")

    def on_mount(self) -> None:
        tbl = self.query_one("#queue-table", DataTable)
        tbl.add_columns("♪", "Titel", "Artiest", "Duur")

    def render_queue(self, queue) -> None:
        tbl = self.query_one("#queue-table", DataTable)
        tbl.clear()
        for i, t in enumerate(queue.tracks):
            mark = "▶" if i == queue.index else " "
            tbl.add_row(f"{mark} {t.badge}", t.title, t.artist,
                        _fmt_duration(t.duration), key=str(i))

    def _selected_index(self) -> int | None:
        tbl = self.query_one("#queue-table", DataTable)
        if tbl.row_count == 0:
            return None
        try:
            return int(tbl.coordinate_to_cell_key(
                tbl.cursor_coordinate).row_key.value)
        except Exception:
            return None

    async def action_save_to_playlist(self) -> None:
        """`s` op een queue-rij: open de picker voor die track (alleen Spotify)."""
        i = self._selected_index()
        if i is None:
            self._app.notify("Geen track in de queue geselecteerd.",
                             severity="warning")
            return
        queue = self._app.orchestrator.queue
        if i >= len(queue.tracks):
            return
        track = queue.tracks[i]
        if track.source != "spotify":
            self._app.notify(
                f"Alleen Spotify-tracks zijn savable "
                f"('{track.title}' is {track.source}).",
                severity="warning",
            )
            return
        self._app.open_save_picker([track.uri], track.title)


class LibraryPane(Vertical):
    """Library-tabbladen: Lokaal (met subtabs Nummers/Mappen/Albums/Artiesten/Genre),
    YouTube (Subscriptions/Favorieten/Watch Later/History) en Spotify
    (Liked Songs + albums/playlists-drill-down).

    Drill-down-patroon: meta-tabel links (categorieën zoals mappen of albums)
    + detail-tabel rechts (tracks van de geselecteerde categorie). Bij focus
    op de detail-tabel speelt ``enter`` de track; bij focus op de meta-tabel
    drillt 'm naar de detail-tabel (en geeft 'm focus).

    **Lazy loading**: elk tabblad (en de Lokaal-subtabs) wordt pas gevuld bij
    de eerste activatie, via ``on_tabbed_content_tab_activated`` (gefilterd op
    ``event.pane.id``). Dit voorkomt onnodige yt-dlp- / API-calls bij opstart
    (Spotify-tabblad zou bv. anders OAuth triggeren bij elke start).

    **Bindings** (op pane-niveau; werken als de pane of een kind focus heeft):
      * ``enter`` (priority) → ``action_play_now``: context-dispatch op focus.
      * ``a`` → ``action_add_to_queue``: leaf-track appenden, meta-rij =
        heel album/artiest/map toevoegen (queue_extend).
      * ``A`` → ``action_play_all_context``: vervang queue + speel alles van
        huidige map / album / artiest / geladen detail-tracks.
      * ``u`` → ``action_go_up``: één niveau omhoog in de Mappen-tab.
    """

    DEFAULT_CSS = """
    /* Begrens elke DataTable die direct in een Vertical/TabPane staat, anders
       neemt hij auto-hoogte = alle rijen en virtualiseert/scrollt niet. */
    #lib-local-table { height: 1fr; }
    #lib-local-tabs { height: 1fr; }
    #lib-local { height: 1fr; }
    #lib-folders-wrap { height: 1fr; }
    #lib-folders-path { height: 1; padding: 0 1; background: $boost; }
    #lib-folders-table { height: 1fr; }
    #lib-albums, #lib-artists { height: 1fr; }
    #lib-albums-meta, #lib-artists-meta { width: 2fr; height: 1fr; }
    #lib-albums-detail, #lib-artists-detail {
        width: 3fr;
        border: round $accent;
    }
    /* Genre-tab: meta links (genres-lijst), detail rechts met zelfgemaakte
       tab-knoppen + 1 DataTable. Geen Textual-TabbedContent hier — die
       claimde in een Horizontal de hele parent-breedte en drukte de meta-
       tabel weg. Harde breedte `32` op de meta-wrapper. */
    LibraryPane #lib-genres { height: 1fr; }
    LibraryPane #lib-genres-left { width: 32; height: 1fr; }
    LibraryPane #lib-genres-meta { height: 1fr; }
    LibraryPane #lib-genres-hint {
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    LibraryPane #lib-genres-right { height: 1fr; }
    LibraryPane #lib-genres-tabbar { height: 3; padding: 0 1; }
    LibraryPane #lib-genres-detail { height: 1fr; border: round $accent; }
    LibraryPane #lib-genres-detail-hint {
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    LibraryPane #lib-spotify-split { height: 1fr; }
    LibraryPane #lib-spotify-left { width: 2fr; }
    LibraryPane #lib-spotify-meta { height: 1fr; }
    LibraryPane #lib-detail-table {
        width: 3fr;
        border: round $accent;
    }
    /* YouTube library-tab — meta links, detail rechts; zelfde patroon als Spotify */
    LibraryPane #lib-yt-split { height: 1fr; }
    LibraryPane #lib-yt-left { width: 2fr; }
    LibraryPane #lib-yt-meta { height: 1fr; }
    LibraryPane #lib-yt-detail {
        width: 3fr;
        border: round $accent;
    }
    LibraryPane #lib-yt-hint {
        height: 1; padding: 0 1; background: $boost;
    }
    """

    # NOTE: `#lib-local-table { height: 1fr }` hierboven is niet optioneel.
    # Zonder een begrensde hoogte neemt de DataTable auto-hoogte aan = alle
    # rijen (hier tot 500), groeit dus 500 regels ver buiten z'n viewport, en
    # virtualiseert/scrollt niet (scroll_y blijft 0 — de cursor schiet naar de
    # laatste rij maar het scherm toont steeds dezelfde bovenste regels; de 1fr
    # parent clip't de overflow weg). Met 1fr vult de tabel z'n TabPane en
    # scrollt hij intern. #lib-detail-table heeft dit niet nodig: die staat in
    # een Horizontal met height:1fr, die z'n children verticaal uitrekt.

    BINDINGS = [
        # priority=True: wordt gecheckt vóór de DataTable's eigen Enter-binding
        # ("Select cells under the cursor"). Zonder priority eet de DataTable
        # Enter op en komt play_now nooit aan.
        Binding("enter", "play_now", "Speel/Open", priority=True),
        Binding("a", "add_to_queue", "Toevoegen", priority=False),
        Binding("A", "play_all_context", "Alles spelen"),
        Binding("u", "go_up", "Map omhoog"),
        # Tag-editor & delete-remove. `e` is alleen zinvol op een echte
        # track-rij (niet op meta-rijen als album/artiest); de actie zelf
        # checkt focus en dispatcht. `d` verwijdert: lokaal = bestand wissen.
        # YT-playlists worden via youtube.com verwijderd (zie action_delete).
        Binding("e", "edit_tags", "Tags wijzigen"),
        Binding("d", "delete_or_remove", "Verwijderen"),
        # Spotify-playlist-beheer (track-niveau + playlist-niveau):
        # `s` op een track-rij → picker (Liked Songs / playlist / nieuwe).
        # `r` + `D` op een playlist-meta rij → hernoem / verwijder.
        Binding("s", "save_to_playlist", "Opslaan"),
        Binding("r", "rename_playlist", "Hernoem playlist"),
        Binding("D", "delete_playlist", "Verwijder playlist"),
    ]

    def __init__(self, app: "MusiApp") -> None:
        super().__init__()
        self._app = app
        # huidige rij-keuzes per tabel (cursor → Track of uri)
        self._tracks: list = []   # Spotify- of YouTube-drill-track-lijst
        self._local_tracks: list = []  # Library > Lokaal > Nummers-tab
        self._spotify_loaded = False
        # cache: opgeslagen albums + playlists, voor drill-down (hoes-URL lookup)
        self._albums_by_uri: dict[str, dict] = {}
        self._playlists_by_uri: dict[str, dict] = {}
        # ---- Library > YouTube-tab ----
        self._yt_loaded = False
        # type-prefix in #lib-yt-meta (sub: / fav: / wl: / h:) → naam + benodigde
        # methode op YouTubeSearch (zie _open_yt_collection).
        self._yt_meta_rows: list[dict] = []
        # ---- Lokaal-subtabs (Mappen / Albums / Artiesten) ----
        # Mappen: huidige map als tuple componenten onder music_dir (() = root);
        # entries = rijen in de folder-tabel: ("dir", naam) of ("track", Track).
        self._folder_rel: tuple[str, ...] = ()
        self._folder_entries: list[tuple[str, object]] = []
        self._folder_loaded = False
        # Albums / Artiesten: meta-lijsten + geladen detail-tracks
        self._albums: list[dict] = []
        self._album_tracks: list[Track] = []
        self._albums_loaded = False
        self._artists: list[dict] = []
        self._artist_tracks: list[Track] = []
        self._artists_loaded = False
        # ---- Genre (Lokaal > Genre) ----
        # Eén geselecteerd genre + drie data-lijsten (tracks/artists/albums);
        # de detail-zijde toont ALTIJD dezelfde `#lib-genres-detail`-DataTable,
        # waarvan we de kolommen + rijen verversen op basis van
        # ``self._genre_view`` (één van "tracks"/"artists"/"albums").
        # Tab-wissel via Button-klik (#gn-btn-*) → ``_set_genre_view``.
        # Bij drill van artiest→tracks of album→tracks wordt de tracks-view
        # geforceerd + de detail-tabel opnieuw gevuld met de gefilterde data.
        self._genres: list[dict] = []                  # [{"genre","count"}]
        self._genre_name: str = ""                     # geselecteerde genre
        self._genre_view: str = "tracks"               # actieve sub-view
        self._genre_tracks: list[Track] = []
        self._genre_artists: list[dict] = []
        self._genre_albums: list[dict] = []
        self._genres_loaded = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Genre-tab-knoppen: wissel tussen Tracks / Artiesten / Albums view
        en refresh de detail-tabel. Alleen actief als de knop tot de
        Genre-tabbar behoort (anders zou hij ook reageren op andere knoppen
        elders in de app)."""
        bid = event.button.id
        if bid not in ("gn-btn-tracks", "gn-btn-artists", "gn-btn-albums"):
            return
        view = bid.removeprefix("gn-btn-")
        if view == self._genre_view:
            return  # al actief
        self._set_genre_view(view)
        self._render_genre_detail()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Lazy-load per subtab: bij eerste openen data vullen + focus op de
        tabel zodat Enter meteen werkt. Wordt voor álle (geneste) TabbedContents
        in de subtree afgevuurd, dus filteren op pane.id."""
        pid = getattr(event.pane, "id", None)
        if pid == "lib-spotify":
            if not self._spotify_loaded:
                self._spotify_loaded = True
                asyncio.create_task(self.refresh_spotify(self._app))
            self.query_one("#lib-spotify-meta", DataTable).focus()
        elif pid == "ll-folders":
            if not self._folder_loaded:
                self._folder_loaded = True
                asyncio.create_task(self._load_folder(()))
            else:
                self.query_one("#lib-folders-table", DataTable).focus()
        elif pid == "ll-albums":
            if not self._albums_loaded:
                self._albums_loaded = True
                asyncio.create_task(self._init_albums())
            else:
                self.query_one("#lib-albums-meta", DataTable).focus()
        elif pid == "ll-artists":
            if not self._artists_loaded:
                self._artists_loaded = True
                asyncio.create_task(self._init_artists())
            else:
                self.query_one("#lib-artists-meta", DataTable).focus()
        elif pid == "ll-genres":
            # Eerste activatie vult de genre-meta; volgende activaties
            # hervatten focus (tab-state van #lib-genres-tabs blijft
            # staan, dus de gebruiker ziet de laatst-bekeken sub-tab).
            if not self._genres_loaded:
                self._genres_loaded = True
                asyncio.create_task(self._init_genres())
            else:
                self.query_one("#lib-genres-meta", DataTable).focus()
        elif pid == "lib-youtube":
            if not self._yt_loaded:
                self._yt_loaded = True
                # vul de meta-tabel met de 4 vaste feeds (geen netwerk-call
                # hier — pas bij Enter wordt de YouTubeSearch aangeroepen).
                self._render_yt_meta()
            self.query_one("#lib-yt-meta", DataTable).focus()
        # Geen gn-* elif meer — de Genre-detail-view wordt gewisseld via
        # Button-kliks (#gn-btn-*) i.p.v. via Textual's TabbedContent.

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="lib-local"):
            with TabPane("Lokaal", id="lib-local"):
                # Lokaal is zelf ook een TabbedContent: Nummers / Mappen / Albums
                # / Artiesten. TabActivated filtert op pane-id (zie memory-punt #4).
                with TabbedContent(id="lib-local-tabs", initial="ll-tracks"):
                    with TabPane("Nummers", id="ll-tracks"):
                        # Platte lijst van de 500 laatst-toegevoegde tracks.
                        yield DataTable(id="lib-local-table", cursor_type="row")
                    with TabPane("Mappen", id="ll-folders"):
                        # Breadcrumb boven, folder-tabel onder. Vertical met
                        # 1fr-wrap zodat de tabel z'n viewport vult (anders
                        # neemt hij auto-hoogte aan = alle rijen = scroll-bug;
                        # zie memory-punt #8).
                        with Vertical(id="lib-folders-wrap"):
                            yield Label("📁 Music", id="lib-folders-path")
                            yield DataTable(id="lib-folders-table",
                                            cursor_type="row")
                    with TabPane("Albums", id="ll-albums"):
                        # Spotify-sjabloon: meta-links + detail-rechts (Horizontal
                        # met height:1fr rekt de detail-tabel verticaal uit).
                        with Horizontal(id="lib-albums"):
                            yield DataTable(id="lib-albums-meta",
                                            cursor_type="row")
                            yield DataTable(id="lib-albums-detail",
                                            cursor_type="row")
                    with TabPane("Artiesten", id="ll-artists"):
                        with Horizontal(id="lib-artists"):
                            yield DataTable(id="lib-artists-meta",
                                            cursor_type="row")
                            yield DataTable(id="lib-artists-detail",
                                            cursor_type="row")
                    with TabPane("Genre", id="ll-genres"):
                        # Genres-lijst links + detail-rechts. De detail-zijde
                        # is een **Vertical met zelfgemaakte tab-knoppen** +
                        # **één DataTable** (waarvan we de inhoud refreshen
                        # op knop-klik). Dit vermijdt Textual's
                        # ``TabbedContent``-in-``Horizontal``-bug (claimt
                        # de hele parent-breedte en drukt de meta-tabel weg).
                        with Horizontal(id="lib-genres"):
                            with Vertical(id="lib-genres-left"):
                                yield DataTable(id="lib-genres-meta",
                                                cursor_type="row")
                                yield Label(
                                    "(kies een genre)",
                                    id="lib-genres-hint",
                                )
                            with Vertical(id="lib-genres-right"):
                                with Horizontal(id="lib-genres-tabbar"):
                                    yield Button("Tracks", id="gn-btn-tracks",
                                                 variant="primary")
                                    yield Button("Artiesten",
                                                 id="gn-btn-artists")
                                    yield Button("Albums",
                                                 id="gn-btn-albums")
                                yield DataTable(id="lib-genres-detail",
                                                cursor_type="row")
                                yield Static(
                                    "(kies een genre om te beginnen)",
                                    id="lib-genres-detail-hint",
                                )
            with TabPane("Spotify", id="lib-spotify"):
                # albums/playlists links, track-lijst rechts — altijd beide
                # zichtbaar, zodat drill-down niet in een weggedrukte tabel
                # onder de vouw verdwijnt.
                with Horizontal(id="lib-spotify-split"):
                    with Vertical(id="lib-spotify-left"):
                        yield DataTable(id="lib-spotify-meta", cursor_type="row")
                        yield Label("(Spotify-tabbladen vullen zodra ingelogd)",
                                    id="lib-spotify-hint")
                    yield DataTable(id="lib-detail-table", cursor_type="row")
            with TabPane("YouTube", id="lib-youtube"):
                # Subs/Favorieten/Watch Later/History links (meta),
                # video-lijst rechts (detail). Vereist cookies_from_browser
                # in config voor subscriptions/favorites/etc.
                with Horizontal(id="lib-yt-split"):
                    with Vertical(id="lib-yt-left"):
                        yield DataTable(id="lib-yt-meta", cursor_type="row")
                        yield Label("(zet [youtube] cookies_from_browser in config.toml "
                                    "voor subscriptions)", id="lib-yt-hint")
                    yield DataTable(id="lib-yt-detail", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#lib-local-table", DataTable).add_columns("♪", "Titel", "Artiest", "Album", "Duur")
        # Lokaal-subtabs (Mappen / Albums / Artiesten / Genre)
        self.query_one("#lib-folders-table", DataTable).add_columns("Naam", "Type", "#")
        self.query_one("#lib-albums-meta", DataTable).add_columns("Album", "Artiest", "#")
        self.query_one("#lib-albums-detail", DataTable).add_columns("♪", "Titel", "Artiest", "Album", "Duur")
        self.query_one("#lib-artists-meta", DataTable).add_columns("Artiest", "#")
        self.query_one("#lib-artists-detail", DataTable).add_columns("♪", "Titel", "Album", "Duur")
        self.query_one("#lib-genres-meta", DataTable).add_columns("Genre", "#")
        # Genre-detail is één DataTable waarvan we de kolommen switchen
        # op basis van de actieve sub-view (tracks/artists/albums).
        # Initiële kolommen zijn voor de Tracks-view; latere schakelaars
        # via _render_genre_detail() herbouwen de kolommen.
        self._ensure_genre_detail_columns("tracks")
        # Spotify
        self.query_one("#lib-spotify-meta", DataTable).add_columns("Naam", "Type", "Eigenaar")
        self.query_one("#lib-detail-table", DataTable).add_columns("♪", "Titel", "Artiest", "Album", "Duur")
        # YouTube
        self.query_one("#lib-yt-meta", DataTable).add_columns("Naam", "Type")
        self.query_one("#lib-yt-detail", DataTable).add_columns("♪", "Titel", "Kanaal", "Duur")

    def render_library(self, tracks: list[Track]) -> None:
        tbl = self.query_one("#lib-local-table", DataTable)
        tbl.clear()
        for i, t in enumerate(tracks):
            tbl.add_row(t.badge, t.title, t.artist, t.album,
                        _fmt_duration(t.duration), key=f"l:{i}")
        # apart van self._tracks (Spotify-drill); anders zou Lokaal-tab de
        # drill-tracks overschrijven.
        self._local_tracks: list[Track] = tracks

    async def refresh_spotify(self, app: "MusiApp") -> None:
        """Vul de Spotify-tabel met Liked songs en lijst van playlists/albums."""
        sp = app.services.providers.get("spotify")
        if sp is None:
            return
        # laat de UI niet blokkeren — fetch in threads
        try:
            saved = await sp.saved_tracks(limit=200)
            albums = await sp.saved_albums(limit=100)
            playlists = await sp.user_playlists(limit=100)
        except Exception as e:
            app.notify(f"Spotify-library ophalen mislukte: {e}", severity="warning")
            return
        # Een autorisatiefout (bijv. invalid_client door fout client_id, of een
        # niet-geregistreerde redirect_uri) wordt in de provider opgevangen en levert
        # lege lijsten op — toon de échte oorzaak i.p.v. een stilletjes leeg tabblad.
        if not saved and not albums and not playlists and getattr(sp, "last_error", None):
            err = sp.last_error
            self.query_one("#lib-spotify-hint", Label).update(f"⚠ Spotify-login mislukt: {err}")
            app.notify(
                f"Spotify-login mislukt: {err}\n"
                "Check client_id + redirect_uri in ~/.config/musi/config.toml.",
                severity="error",
            )
            return
        # detail-tabel vullen met Liked songs
        dt = self.query_one("#lib-detail-table", DataTable)
        dt.clear()
        self._tracks = saved
        for i, t in enumerate(saved):
            dt.add_row(t.badge, t.title, t.artist, t.album,
                       _fmt_duration(t.duration), key=f"s:{i}")
        # cache de volledige album-/playlist-objecten zodat drill-down de
        # albumhoes (art_url) kan doorgeven aan de geladen tracks.
        self._albums_by_uri = {a.get("uri"): a for a in albums}
        self._playlists_by_uri = {p.get("uri"): p for p in playlists}
        # meta-tabel met albums + playlists
        mt = self.query_one("#lib-spotify-meta", DataTable)
        mt.clear()
        for i, a in enumerate(albums):
            mt.add_row((a.get("name") or "?"), "album",
                       ", ".join(ar["name"] for ar in a.get("artists") or []), key=f"a:{a.get('uri')}")
        for i, p in enumerate(playlists):
            owner = (p.get("owner") or {}).get("display_name") or "—"
            mt.add_row((p.get("name") or "?"), "playlist", owner,
                       key=f"p:{p.get('uri')}")
        hint = self.query_one("#lib-spotify-hint", Label)
        hint.update(
            f"Liked songs: {len(saved)}  ·  Albums: {len(albums)}  ·  "
            f"Playlists: {len(playlists)}  —  Enter op een album/playlist opent 'm"
        )

    async def action_play_now(self) -> None:
        # Enter is contextbewust op basis van FOCUS — eerst leaf/detail (speel
        # één track), daarna meta/folder (drill of daal af).
        local = self.query_one("#lib-local-table", DataTable)
        if local.has_focus:
            idx = self._row_index(local, "l:")
            if idx is not None and idx < len(self._local_tracks):
                await self._app.play_track(self._local_tracks[idx])
            return

        # Lokaal > Mappen: subfolder → daal af, track → speel.
        folders = self.query_one("#lib-folders-table", DataTable)
        if folders.has_focus:
            idx = self._row_index(folders, "f:")
            if idx is not None and idx < len(self._folder_entries):
                kind, val = self._folder_entries[idx]
                if kind == "dir":
                    await self._folder_descend(str(val))
                else:
                    await self._app.play_track(val)  # type: ignore[arg-type]
            return

        # Lokaal > Albums: meta → drill, detail → speel.
        alb_detail = self.query_one("#lib-albums-detail", DataTable)
        if alb_detail.has_focus:
            idx = self._row_index(alb_detail, "at:")
            if idx is not None and idx < len(self._album_tracks):
                await self._app.play_track(self._album_tracks[idx])
            return

        # Lokaal > Artiesten.
        art_detail = self.query_one("#lib-artists-detail", DataTable)
        if art_detail.has_focus:
            idx = self._row_index(art_detail, "rt:")
            if idx is not None and idx < len(self._artist_tracks):
                await self._app.play_track(self._artist_tracks[idx])
            return

        # Lokaal > Genre: detail-tabel (één) + meta.
        gt = self.query_one("#lib-genres-detail", DataTable)
        if gt.has_focus:
            if self._genre_view == "tracks":
                idx = self._row_index(gt, "gt:")
                if idx is not None and idx < len(self._genre_tracks):
                    await self._app.play_track(self._genre_tracks[idx])
            elif self._genre_view == "artists":
                idx = self._row_index(gt, "ga:")
                if idx is not None and idx < len(self._genre_artists):
                    await self._drill_genre_artist(idx)
            else:  # albums
                idx = self._row_index(gt, "gl:")
                if idx is not None and idx < len(self._genre_albums):
                    await self._drill_genre_album(idx)
            return

        # Spotify track-lijst (drill of Liked songs).
        detail = self.query_one("#lib-detail-table", DataTable)
        if detail.has_focus:
            if not self._tracks:
                return
            idx = self._selected_detail_index()
            if idx is None:
                return
            await self._app.play_track(self._tracks[idx])
            return

        # Lokaal > Albums-meta: drill album.
        alb_meta = self.query_one("#lib-albums-meta", DataTable)
        if alb_meta.has_focus:
            idx = self._row_index(alb_meta, "al:")
            if idx is not None and idx < len(self._albums):
                await self._drill_album(idx)
            return

        # Lokaal > Artiesten-meta: drill artiest.
        art_meta = self.query_one("#lib-artists-meta", DataTable)
        if art_meta.has_focus:
            idx = self._row_index(art_meta, "ar:")
            if idx is not None and idx < len(self._artists):
                await self._drill_artist(idx)
            return

        # Lokaal > Genre-meta: drill genre → vult 3 sub-tabs.
        gn_meta = self.query_one("#lib-genres-meta", DataTable)
        if gn_meta.has_focus:
            idx = self._row_index(gn_meta, "gn:")
            if idx is not None and idx < len(self._genres):
                await self._drill_genre(idx)
            return

        # Library > YouTube: detail speelt, meta drillt.
        yt_detail = self.query_one("#lib-yt-detail", DataTable)
        if yt_detail.has_focus:
            idx = self._yt_detail_index()
            if idx is not None and idx < len(self._tracks):
                await self._app.play_track(self._tracks[idx])
            return
        yt_meta = self.query_one("#lib-yt-meta", DataTable)
        if yt_meta.has_focus:
            await self._open_yt_collection()
            return

        # Spotify meta: drill album/playlist.
        meta = self.query_one("#lib-spotify-meta", DataTable)
        try:
            meta_key = meta.coordinate_to_cell_key(meta.cursor_coordinate).row_key.value
        except Exception:
            meta_key = None
        if meta_key and (meta_key.startswith("a:") or meta_key.startswith("p:")):
            await self._open_spotify_collection()

    def _row_index(self, tbl: DataTable, prefix: str) -> int | None:
        """Algemene helper: geef de int-index uit de cursor-rij-key van tbl,
        mits die met `prefix` begint (anders None)."""
        if tbl.row_count == 0:
            return None
        try:
            key = tbl.coordinate_to_cell_key(tbl.cursor_coordinate).row_key.value
        except Exception:
            return None
        if not key or not key.startswith(prefix):
            return None
        try:
            return int(key[len(prefix):])
        except ValueError:
            return None

    async def _open_spotify_collection(self) -> None:
        """Laad de tracks van het geselecteerde album/playlist in de track-lijst."""
        meta = self.query_one("#lib-spotify-meta", DataTable)
        try:
            key = meta.coordinate_to_cell_key(meta.cursor_coordinate).row_key.value
        except Exception:
            return
        if not key or not self._app:
            return
        sp = self._app.services.providers.get("spotify")
        if sp is None:
            return
        uri = key[2:]  # strip prefix "a:" of "p:"; de rest is de Spotify-URI
        try:
            if key.startswith("a:"):
                art = _art_from_collection(self._albums_by_uri.get(uri, {}))
                tracks = await sp.album_tracks(uri, limit=300, art_url=art)
                label = "album"
            elif key.startswith("p:"):
                tracks = await sp.playlist_tracks(uri, limit=300)
                label = "playlist"
            else:
                return
        except Exception as e:
            self._app.notify(f"Collectie laden mislukte: {e}", severity="warning")
            return
        self._tracks = tracks
        dt = self.query_one("#lib-detail-table", DataTable)
        dt.clear()
        for i, t in enumerate(tracks):
            dt.add_row(t.badge, t.title, t.artist, t.album,
                       _fmt_duration(t.duration), key=f"s:{i}")
        dt.focus()  # volgende Enter speelt een nummer uit de track-lijst
        self.query_one("#lib-spotify-hint", Label).update(
            f"{label.capitalize()}: {len(tracks)} nummers geladen — "
            "Enter = spelen, a = toevoegen aan queue"
        )

    async def action_add_to_queue(self) -> None:
        # `a`: voeg toe aan queue.
        # - leaf-tabel (lokaal/folder-track/album-detail/artist-detail/spotify-detail) → die track.
        # - meta/folder-subfolder → álles van dat album/artiest/map (queue_extend, 1 event).
        local = self.query_one("#lib-local-table", DataTable)
        if local.has_focus:
            idx = self._row_index(local, "l:")
            if idx is not None and idx < len(self._local_tracks):
                self._app.orchestrator.queue_add(self._local_tracks[idx])
            return

        folders = self.query_one("#lib-folders-table", DataTable)
        if folders.has_focus:
            idx = self._row_index(folders, "f:")
            if idx is not None and idx < len(self._folder_entries):
                kind, val = self._folder_entries[idx]
                if kind == "dir":
                    tracks = await self._run(
                        self._app.services.library.folder_tracks,
                        (*self._folder_rel, str(val)),
                    )
                    self._app.orchestrator.queue_extend(tracks)
                    self._app.notify(f"+{len(tracks)} uit map '{val}'")
                else:
                    self._app.orchestrator.queue_add(val)  # type: ignore[arg-type]
            return

        alb_detail = self.query_one("#lib-albums-detail", DataTable)
        if alb_detail.has_focus:
            idx = self._row_index(alb_detail, "at:")
            if idx is not None and idx < len(self._album_tracks):
                self._app.orchestrator.queue_add(self._album_tracks[idx])
            return

        art_detail = self.query_one("#lib-artists-detail", DataTable)
        if art_detail.has_focus:
            idx = self._row_index(art_detail, "rt:")
            if idx is not None and idx < len(self._artist_tracks):
                self._app.orchestrator.queue_add(self._artist_tracks[idx])
            return

        # Lokaal > Genre: detail-tabel (één) — dispatch per view.
        gt = self.query_one("#lib-genres-detail", DataTable)
        if gt.has_focus:
            if self._genre_view == "tracks":
                idx = self._row_index(gt, "gt:")
                if idx is not None and idx < len(self._genre_tracks):
                    self._app.orchestrator.queue_add(self._genre_tracks[idx])
            elif self._genre_view == "artists":
                idx = self._row_index(gt, "ga:")
                if idx is not None and idx < len(self._genre_artists):
                    artist = self._genre_artists[idx]["artist"]
                    tracks = await self._run(
                        self._app.services.library.genre_artist_tracks,
                        self._genre_name, artist)
                    self._app.orchestrator.queue_extend(tracks)
                    self._app.notify(
                        f"+{len(tracks)} '{artist}' in '{self._genre_name}'")
            else:  # albums
                idx = self._row_index(gt, "gl:")
                if idx is not None and idx < len(self._genre_albums):
                    album = self._genre_albums[idx]["album"]
                    tracks = await self._run(
                        self._app.services.library.genre_album_tracks,
                        self._genre_name, album)
                    self._app.orchestrator.queue_extend(tracks)
                    self._app.notify(
                        f"+{len(tracks)} album '{album}' in '{self._genre_name}'")
            return
        # Lokaal > Genre-meta: voeg álle tracks van dat genre toe (queue_extend).
        gn_meta = self.query_one("#lib-genres-meta", DataTable)
        if gn_meta.has_focus:
            idx = self._row_index(gn_meta, "gn:")
            if idx is not None and idx < len(self._genres):
                tracks = await self._run(
                    self._app.services.library.genre_tracks,
                    self._genres[idx]["genre"])
                self._app.orchestrator.queue_extend(tracks)
                self._app.notify(
                    f"+{len(tracks)} genre '{self._genres[idx]['genre']}'")
            return

        # Spotify drill-detail.
        if not self._tracks:
            return
        idx = self._selected_detail_index()
        if idx is None:
            return
        self._app.orchestrator.queue_add(self._tracks[idx])

    # ---- playlist-beheer --------------------------------------
    # `s` op een track-rij → picker (Liked Songs / bestaande playlist / nieuwe).
    # Focus-dispatch volgt hetzelfde patroon als `action_play_now`: alle
    # track-tabellen worden afgelopen, en degene die focus heeft wint.
    async def action_save_to_playlist(self) -> None:
        track = self._focused_saveable_track()
        if track is None:
            self._app.notify(
                "Selecteer een Spotify-nummer om op te slaan.",
                severity="warning",
            )
            return
        if track.source != "spotify":
            self._app.notify(
                f"Alleen Spotify-tracks zijn savable ('{track.title}' is {track.source}).",
                severity="warning",
            )
            return
        self._app.open_save_picker([track.uri], track.title)

    def _focused_saveable_track(self) -> Track | None:
        """Geef de Track van de gefocuste track-rij in de LibraryPane, of
        ``None`` als de focus op een meta-tabel of een niet-track-tabel ligt.
        Doorloopt dezelfde tabellen als ``action_play_now`` (lokaal/folder/
        albums-detail/artists-detail/genres-detail/spotify-detail/yt-detail).
        """
        local = self.query_one("#lib-local-table", DataTable)
        if local.has_focus:
            idx = self._row_index(local, "l:")
            if idx is not None and idx < len(self._local_tracks):
                return self._local_tracks[idx]

        folders = self.query_one("#lib-folders-table", DataTable)
        if folders.has_focus:
            idx = self._row_index(folders, "f:")
            if idx is not None and idx < len(self._folder_entries):
                kind, val = self._folder_entries[idx]
                if kind == "track":
                    return val  # type: ignore[return-value]

        alb_detail = self.query_one("#lib-albums-detail", DataTable)
        if alb_detail.has_focus:
            idx = self._row_index(alb_detail, "at:")
            if idx is not None and idx < len(self._album_tracks):
                return self._album_tracks[idx]

        art_detail = self.query_one("#lib-artists-detail", DataTable)
        if art_detail.has_focus:
            idx = self._row_index(art_detail, "rt:")
            if idx is not None and idx < len(self._artist_tracks):
                return self._artist_tracks[idx]

        gt = self.query_one("#lib-genres-detail", DataTable)
        if gt.has_focus and self._genre_view == "tracks":
            idx = self._row_index(gt, "gt:")
            if idx is not None and idx < len(self._genre_tracks):
                return self._genre_tracks[idx]

        detail = self.query_one("#lib-detail-table", DataTable)
        if detail.has_focus:
            idx = self._selected_detail_index()
            if idx is not None and idx < len(self._tracks):
                return self._tracks[idx]

        yt_detail = self.query_one("#lib-yt-detail", DataTable)
        if yt_detail.has_focus:
            idx = self._yt_detail_index()
            if idx is not None and idx < len(self._tracks):
                return self._tracks[idx]

        return None

    # `r` op een playlist-meta rij → PlaylistNameModal → sp.rename_playlist.
    async def action_rename_playlist(self) -> None:
        sel = self._focused_playlist()
        if sel is None:
            self._app.notify(
                "Selecteer een playlist in #lib-spotify-meta om te hernoemen.",
                severity="warning",
            )
            return
        uri, name = sel
        # opslaan context vóór push (zie deadlock-fix in musi-project.md).
        self._pending_rename_uri = uri
        self._pending_rename_current = name
        self._app.push_screen(
            PlaylistNameModal("Playlist hernoemen", name),
            callback=self._on_rename_result,
        )

    def _on_rename_result(self, new_name: str | None) -> None:
        uri = getattr(self, "_pending_rename_uri", None)
        self._pending_rename_uri = None
        cur = getattr(self, "_pending_rename_current", "")
        self._pending_rename_current = ""
        if not new_name or uri is None:
            return
        if new_name == cur:
            return
        asyncio.create_task(self._apply_rename(uri, new_name))

    async def _apply_rename(self, uri: str, new_name: str) -> None:
        sp = self._app.services.providers.get("spotify")
        if sp is None:
            self._app.notify("Spotify niet beschikbaar.", severity="error")
            return
        ok, err = await sp.rename_playlist(uri, name=new_name)
        if not ok:
            self._app.notify(f"Hernoemen mislukt: {err}", severity="error")
            return
        self._app.notify(f"Hernoemd: '{new_name}'")
        await self._refresh_playlists_after_mutation()

    # `D` op een playlist-meta rij → ConfirmModal → sp.delete_playlist.
    async def action_delete_playlist(self) -> None:
        sel = self._focused_playlist()
        if sel is None:
            self._app.notify(
                "Selecteer een playlist in #lib-spotify-meta om te verwijderen.",
                severity="warning",
            )
            return
        uri, name = sel
        self._pending_delete_playlist_uri = uri
        self._pending_delete_playlist_name = name
        self._app.push_screen(
            ConfirmModal(
                "Playlist uit je account verwijderen?",
                f"“{name}”\n\n"
                "Voor eigen playlists = wissen. Voor gevolgde playlists "
                "= unfollow (de maker houdt 'm; jij niet meer)."
            ),
            callback=self._on_delete_playlist_confirm,
        )

    def _on_delete_playlist_confirm(self, ok: bool) -> None:
        uri = getattr(self, "_pending_delete_playlist_uri", None)
        name = getattr(self, "_pending_delete_playlist_name", "")
        self._pending_delete_playlist_uri = None
        self._pending_delete_playlist_name = ""
        if not ok or uri is None:
            return
        asyncio.create_task(self._apply_delete_playlist(uri, name))

    async def _apply_delete_playlist(self, uri: str, name: str) -> None:
        sp = self._app.services.providers.get("spotify")
        if sp is None:
            self._app.notify("Spotify niet beschikbaar.", severity="error")
            return
        ok, err = await sp.delete_playlist(uri)
        if not ok:
            self._app.notify(f"Verwijderen mislukt: {err}", severity="error")
            return
        self._app.notify(f"Verwijderd: '{name}'")
        await self._refresh_playlists_after_mutation()

    def _focused_playlist(self) -> tuple[str, str] | None:
        """Cursor in #lib-spotify-meta op een ``p:<uri>``-rij → (uri, naam).
        Voor de andere rijen (album-meta, Liked Songs, niets) → ``None``."""
        meta = self.query_one("#lib-spotify-meta", DataTable)
        if not meta.has_focus:
            return None
        if meta.row_count == 0:
            return None
        try:
            key = meta.coordinate_to_cell_key(
                meta.cursor_coordinate).row_key.value
        except Exception:
            return None
        if not key or not key.startswith("p:"):
            return None
        uri = key[2:]
        # naam uit de cache (gevall de cache is out-of-date, val terug op uri)
        name = (self._playlists_by_uri.get(uri, {}) or {}).get("name") or uri
        return uri, name

    async def _refresh_playlists_after_mutation(self) -> None:
        """Herlaad alleen ``user_playlists()`` na een playlist-CUD — sneller
        dan de volledige ``refresh_spotify``-route (die ook saved_tracks +
        albums opnieuw haalt). Update de cache + meta-tabel + hint."""
        sp = self._app.services.providers.get("spotify")
        if sp is None:
            return
        try:
            playlists = await sp.user_playlists(limit=100)
        except Exception as e:
            log.warning("playlists-refresh na mutatie mislukte: %s", e)
            return
        self._playlists_by_uri = {p.get("uri"): p for p in (playlists or [])}
        mt = self.query_one("#lib-spotify-meta", DataTable)
        mt.clear()
        for p in (playlists or []):
            owner = (p.get("owner") or {}).get("display_name") or "—"
            mt.add_row(
                (p.get("name") or "?"), "playlist", owner,
                key=f"p:{p.get('uri')}",
            )
        # albums-rij laten we staan (deze refresh doet alleen playlists);
        # de albums-meta-rijen staan vóór de playlists in de tabel en worden
        # door dit stuk code NIET aangeraakt — we renderen alleen de
        # playlist-rijen opnieuw. Maar mt.clear() gooit alles weg, dus we
        # zouden albums + Liked Songs verliezen. Daarom: haal ook
        # saved-albums op als we die eerder laadden.
        if self._albums_by_uri:
            try:
                albums = await sp.saved_albums(limit=100)
            except Exception:
                albums = list(self._albums_by_uri.values())
            self._albums_by_uri = {a.get("uri"): a for a in (albums or [])}
            # render albums vóór playlists
            for a in (albums or []):
                artists = ", ".join(
                    ar["name"] for ar in a.get("artists") or [])
                mt.add_row(
                    (a.get("name") or "?"), "album", artists,
                    key=f"a:{a.get('uri')}",
                )
            # her-render playlists erna
            for p in (playlists or []):
                owner = (p.get("owner") or {}).get("display_name") or "—"
                mt.add_row(
                    (p.get("name") or "?"), "playlist", owner,
                    key=f"p:{p.get('uri')}",
                )
            # hint
            saved_count = len(self._local_tracks)  # placeholder; echte counts:
            # we callen saved_tracks(limit=1) om alleen het totaal te krijgen
            try:
                saved_total = await sp.saved_track_count()
            except Exception:
                saved_total = 0
            self.query_one("#lib-spotify-hint", Label).update(
                f"Liked songs: {saved_total}  ·  Albums: {len(albums or [])}  ·  "
                f"Playlists: {len(playlists or [])}"
            )
        else:
            # geen albums in cache → alleen playlists tonen, korte hint
            self.query_one("#lib-spotify-hint", Label).update(
                f"Playlists: {len(playlists or [])}"
            )

    # ---- tag-editor + delete ------------------------------------
    # `e` op een lokale track-rij → EditTagsModal → mutagen write + DB update.
    # `d` op een lokale track-rij → ConfirmModal → os.unlink + DB delete.
    # YT-playlist-verwijderen is uit deze app gehaald: YouTube weigert
    # `playlist_edit_ajax` met 401 voor niet-browser-clients.
    #
    # Modal-flow via **callback** (geen `wait_for_dismiss`). De action awaited
    # NIET op de dismiss-waarde: Textual's `_on_key` await de action op de App-
    # pump, dus elke action die wacht op een modal-blocking-Future **deadlockt**:
    # de button-press die `dismiss()` zou moeten triggeren kan niet verwerkt
    # worden terwijl de App-pump in de action zit. Eerder experiment met
    # ``run_worker + push_screen(wait_for_dismiss=True)`` en
    # ``push_screen_wait`` (asyncio.shield) had exact hetzelfde deadlock-
    # symptoom — werkende Unit-test met directe-dismiss (``dismiss(True)``
    # in ``on_mount``) bleek vals-positief omdat die de button-press-stap
    # omzeilt. De fix: modal pushen via ``push_screen(modal, callback=cb)``
    # (synchroon, geen await), action keert meteen terug, dismiss-waarde
    # komt binnen via ``cb`` op de App-pump. Zie ``test_deadlock.py``.

    async def action_edit_tags(self) -> None:
        track = self._focused_local_track()
        if track is None:
            self._app.notify("Geen lokale track geselecteerd (alleen tags "
                             "van lokale nummers zijn bewerkbaar).")
            return
        lib = self._app.services.library
        # Huidige tags uit het bestand lezen (genre zit niet in de DB).
        try:
            current = await self._run(lib.read_tags, track.uri)
        except Exception as e:
            self._app.notify(f"Tags lezen mislukte: {e}", severity="error")
            return
        # Sla de context op voor de callback; de action moet non-blocking
        # terugkeren — zie de deadlock-docstring bovenaan dit blok.
        self._pending_edit_track = track
        self._app.push_screen(
            EditTagsModal(track.uri, **current),
            callback=self._on_edit_tags_result,
        )

    def _on_edit_tags_result(self, result) -> None:
        """Dismiss-callback van EditTagsModal. Draait op de App-pump."""
        track = getattr(self, "_pending_edit_track", None)
        self._pending_edit_track = None
        if not track or not result:
            return  # geannuleerd of context kwijt
        # Het zware werk (file-write + DB) gaat via een aparte task zodat
        # de App-pump niet blokkeert. ``self._run`` await ``asyncio.to_thread``.
        asyncio.create_task(self._apply_tag_edits(track, result))

    async def _apply_tag_edits(self, track, result: dict) -> None:
        lib = self._app.services.library
        old_genre = track.genre
        try:
            await self._run(lib.update_tags, track.uri,
                            title=result.get("title", ""),
                            artist=result.get("artist", ""),
                            album=result.get("album", ""),
                            genre=result.get("genre", ""))
        except Exception as e:
            self._app.notify(f"Tags schrijven mislukte: {e}", severity="error")
            return
        # In-memory Track bijwerken zodat de UI de nieuwe waarden toont.
        if title := result.get("title"):
            track.title = title
        if artist := result.get("artist"):
            track.artist = artist
        if album := result.get("album"):
            track.album = album
        # Genre: ook bijwerken + cache van de Genre-tab invalideren als de
        # genre-tag daadwerkelijk veranderde (nieuwe genres, tellingen, etc.).
        if (new_genre := result.get("genre", "")) != old_genre:
            track.genre = new_genre
            self._genres_loaded = False
        await self._rerender_focused_local_table()
        self._app.notify(f"Tags bijgewerkt: {os.path.basename(track.uri)}")

    async def action_delete_or_remove(self) -> None:
        # YT-playlist-verwijderen is uit deze app gehaald: YouTube weigert
        # `playlist_edit_ajax` met 401 voor niet-browser-clients (zelfs met
        # geldige cookies). Verwijder ze via youtube.com of de YouTube-app.
        # `d` werkt nu nog wel voor lokale nummers (ConfirmModal + unlink).
        track = self._focused_local_track()
        if track is None:
            self._app.notify("Geen nummer geselecteerd om te verwijderen "
                             "(YT-playlists: verwijderen via youtube.com).")
            return
        # Veiligheidscheck: speelt 'ie nu? mpv houdt de fd vast, dus unlink
        # laat de file onzichtbaar op schijf tot mpv 'm sluit — verwarrend.
        cur = self._app.orchestrator.state.track
        if cur and cur.uri == track.uri:
            self._app.notify(f"Speelt nu — kan '{track.title}' niet wissen.",
                             severity="warning")
            return
        path = track.uri
        title = track.title or os.path.basename(path)
        # Sla context op voor de dismiss-callback; action moet non-blocking
        # terugkeren — anders deadlock (zie de deadlock-docstring bovenaan dit blok).
        self._pending_delete_path = path
        self._pending_delete_title = title
        self._app.push_screen(
            ConfirmModal("Bestand definitief wissen?", f"{title}\n{path}"),
            callback=self._on_delete_confirm,
        )

    def _on_delete_confirm(self, ok: bool) -> None:
        """Dismiss-callback van ConfirmModal (delete)."""
        path = getattr(self, "_pending_delete_path", None)
        title = getattr(self, "_pending_delete_title", "")
        self._pending_delete_path = None
        self._pending_delete_title = ""
        if not ok or path is None:
            return
        # Het zware werk (file-delete + DB) gaat via een aparte task.
        asyncio.create_task(self._apply_delete(path, title))

    async def _apply_delete(self, path: str, title: str) -> None:
        lib = self._app.services.library
        try:
            await self._run(lib.delete_track, path)
        except Exception as e:
            self._app.notify(f"Wissen mislukte: {e}", severity="error")
            return
        await self._rerender_focused_local_table()
        self._app.notify(f"Verwijderd: {title}")

    def _focused_local_track(self) -> Track | None:
        """Geef de Track van de gefocuste rij in een lokale track-tabel, of
        ``None`` als de focus elders ligt (meta-rij, YT, Spotify)."""
        local = self.query_one("#lib-local-table", DataTable)
        if local.has_focus:
            idx = self._row_index(local, "l:")
            if idx is not None and idx < len(self._local_tracks):
                return self._local_tracks[idx]
            return None
        folders = self.query_one("#lib-folders-table", DataTable)
        if folders.has_focus:
            idx = self._row_index(folders, "f:")
            if idx is not None and idx < len(self._folder_entries):
                kind, val = self._folder_entries[idx]
                if kind == "track":
                    return val  # type: ignore[return-value]
            return None
        alb = self.query_one("#lib-albums-detail", DataTable)
        if alb.has_focus:
            idx = self._row_index(alb, "at:")
            if idx is not None and idx < len(self._album_tracks):
                return self._album_tracks[idx]
            return None
        art = self.query_one("#lib-artists-detail", DataTable)
        if art.has_focus:
            idx = self._row_index(art, "rt:")
            if idx is not None and idx < len(self._artist_tracks):
                return self._artist_tracks[idx]
            return None
        return None

    async def _rerender_focused_local_table(self) -> None:
        """Herlaad de gefocuste lokale track-tabel vanuit de library (na
        edit/delete). Cursor-positie reset naar rij 0 — acceptabel voor de
        zelden-gebruikte-mutatie-flow."""
        local = self.query_one("#lib-local-table", DataTable)
        if local.has_focus:
            tracks = await self._run(self._app.services.library.recent)
            self.render_library(tracks)
            return
        folders = self.query_one("#lib-folders-table", DataTable)
        if folders.has_focus:
            await self._load_folder(self._folder_rel)
            return
        alb = self.query_one("#lib-albums-detail", DataTable)
        if alb.has_focus and self._album_tracks:
            album = self._album_tracks[0].album
            tracks = await self._run(self._app.services.library.album_tracks, album)
            self._album_tracks = tracks
            alb.clear()
            for i, t in enumerate(tracks):
                alb.add_row(t.badge, t.title, t.artist, t.album,
                            _fmt_duration(t.duration), key=f"at:{i}")
            return
        art = self.query_one("#lib-artists-detail", DataTable)
        if art.has_focus and self._artist_tracks:
            artist = self._artist_tracks[0].artist
            tracks = await self._run(self._app.services.library.artist_tracks, artist)
            self._artist_tracks = tracks
            art.clear()
            for i, t in enumerate(tracks):
                art.add_row(t.badge, t.title, t.album,
                            _fmt_duration(t.duration), key=f"rt:{i}")
            return
        # Lokaal > Genre: her-drill van het huidige genre zodat alle drie de
        # data-lijsten de geüpdatete genre-waarde weerspiegelen. _drill_genre
        # doet drie queries + rendert de actieve view.
        gn_meta = self.query_one("#lib-genres-meta", DataTable)
        gn_detail = self.query_one("#lib-genres-detail", DataTable)
        if (gn_meta.has_focus or gn_detail.has_focus) \
                and self._genre_name and self._genres:
            for i, g in enumerate(self._genres):
                if g["genre"] == self._genre_name:
                    await self._drill_genre(i)
                    return
            # Genre bestaat niet meer — clear + invalideer zodat de
            # Genre-tab bij volgende activatie opnieuw wordt ingeladen.
            self._genres_loaded = False
            self._genre_name = ""
            gn_detail.clear()

    # ---- Lokaal-subtabs helpers --------------------------------------
    async def _run(self, fn, *args, **kwargs):
        """Library-call in een thread om de UI-loop niet te blokkeren."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _load_folder(self, rel: tuple[str, ...]) -> None:
        """Vul #lib-folders-table met de directe kinderen van map `rel`."""
        lib = self._app.services.library
        subs, tracks = await self._run(lib.folder_entries, rel)
        self._folder_rel = tuple(rel)
        tbl = self.query_one("#lib-folders-table", DataTable)
        tbl.clear()
        entries: list[tuple[str, object]] = []
        # mappen eerst (alfabetisch), dan tracks
        for name, cnt in subs:
            i = len(entries)
            entries.append(("dir", name))
            tbl.add_row(f"📁 {name}", "map", f"{cnt}×", key=f"f:{i}")
        for t in tracks:
            i = len(entries)
            entries.append(("track", t))
            tbl.add_row(f"♪ {t.title}", t.artist or "", _fmt_duration(t.duration),
                        key=f"f:{i}")
        self._folder_entries = entries
        self.query_one("#lib-folders-path", Label).update(
            "📁 " + "/".join(["Music", *self._folder_rel])
        )
        tbl.focus()
        tbl.move_cursor(row=0)

    async def _folder_descend(self, name: str) -> None:
        await self._load_folder((*self._folder_rel, name))

    async def _folder_ascend(self) -> None:
        if self._folder_rel:
            await self._load_folder(self._folder_rel[:-1])

    async def _init_albums(self) -> None:
        lib = self._app.services.library
        albums = await self._run(lib.albums)
        self._albums = albums
        mt = self.query_one("#lib-albums-meta", DataTable)
        mt.clear()
        for i, a in enumerate(albums):
            mt.add_row(a["album"], a["artist"], f"{a['count']}", key=f"al:{i}")
        mt.focus()
        mt.move_cursor(row=0)

    async def _drill_album(self, idx: int) -> None:
        lib = self._app.services.library
        album = self._albums[idx]["album"]
        tracks = await self._run(lib.album_tracks, album)
        self._album_tracks = tracks
        dt = self.query_one("#lib-albums-detail", DataTable)
        dt.clear()
        for i, t in enumerate(tracks):
            dt.add_row(t.badge, t.title, t.artist, t.album,
                       _fmt_duration(t.duration), key=f"at:{i}")
        dt.focus()
        dt.move_cursor(row=0)
        self._app.notify(f"{album}: {len(tracks)} nummers")

    async def _init_artists(self) -> None:
        lib = self._app.services.library
        artists = await self._run(lib.artists)
        self._artists = artists
        mt = self.query_one("#lib-artists-meta", DataTable)
        mt.clear()
        for i, a in enumerate(artists):
            mt.add_row(a["artist"], f"{a['count']}", key=f"ar:{i}")
        mt.focus()
        mt.move_cursor(row=0)

    async def _drill_artist(self, idx: int) -> None:
        lib = self._app.services.library
        artist = self._artists[idx]["artist"]
        tracks = await self._run(lib.artist_tracks, artist)
        self._artist_tracks = tracks
        dt = self.query_one("#lib-artists-detail", DataTable)
        dt.clear()
        for i, t in enumerate(tracks):
            dt.add_row(t.badge, t.title, t.album,
                       _fmt_duration(t.duration), key=f"rt:{i}")
        dt.focus()
        dt.move_cursor(row=0)
        self._app.notify(f"{artist}: {len(tracks)} nummers")

    # ---- Lokaal > Genre helpers ---------------------------------------
    async def _init_genres(self) -> None:
        """Vul de genres-meta-tabel met genres uit de library. Bij eerste
        activatie; daarna wordt alleen opnieuw ingeladen als cache-
        invalidatie ``self._genres_loaded`` reset (zie _apply_tag_edits)."""
        lib = self._app.services.library
        self._genres = await self._run(lib.genres)
        mt = self.query_one("#lib-genres-meta", DataTable)
        mt.clear()
        for i, g in enumerate(self._genres):
            mt.add_row(g["genre"], f"{g['count']}", key=f"gn:{i}")
        if self._genres:
            mt.focus()
            mt.move_cursor(row=0)
        # Hint aanpassen: totaal aantal genres + cue voor rescan-indien-leeg
        hint = self.query_one("#lib-genres-hint", Label)
        if not self._genres:
            hint.update("⚠ Geen genres gevonden — draai een rescan "
                        "(Library > acties) om genre-tags in te lezen.")
        else:
            hint.update(f"{len(self._genres)} genres — Enter drillt naar "
                        "tracks/artiesten/albums van dat genre.")

    async def _drill_genre(self, idx: int) -> None:
        """Vul de drie data-lijsten met data van het geselecteerde genre:
        tracks / artiesten / albums. De detail-tabel toont de actieve view
        (initieel 'tracks'); de gebruiker switcht via de tab-knoppen."""
        lib = self._app.services.library
        self._genre_name = self._genres[idx]["genre"]
        tracks = await self._run(lib.genre_tracks, self._genre_name)
        artists = await self._run(lib.genre_artists, self._genre_name)
        albums = await self._run(lib.genre_albums, self._genre_name)
        self._genre_tracks = tracks
        self._genre_artists = artists
        self._genre_albums = albums
        # Standaard terug naar tracks-view bij verse drill.
        self._set_genre_view("tracks")
        self._render_genre_detail()
        self._app.notify(
            f"Genre '{self._genre_name}': {len(tracks)} tracks · "
            f"{len(artists)} artiesten · {len(albums)} albums"
        )

    async def _drill_genre_artist(self, idx: int) -> None:
        """Drill vanuit de Artiesten-view: laadt de genre-tracks van die
        artiest, switcht naar tracks-view, vult de detail-tabel."""
        lib = self._app.services.library
        artist = self._genre_artists[idx]["artist"]
        tracks = await self._run(lib.genre_artist_tracks,
                                 self._genre_name, artist)
        self._genre_tracks = tracks
        self._set_genre_view("tracks")
        self._render_genre_detail()
        self._app.notify(
            f"{artist} in '{self._genre_name}': {len(tracks)} tracks"
        )

    async def _drill_genre_album(self, idx: int) -> None:
        """Drill vanuit de Albums-view: laadt de genre-tracks van dat album."""
        lib = self._app.services.library
        album = self._genre_albums[idx]["album"]
        tracks = await self._run(lib.genre_album_tracks,
                                 self._genre_name, album)
        self._genre_tracks = tracks
        self._set_genre_view("tracks")
        self._render_genre_detail()
        self._app.notify(
            f"Album '{album}' in '{self._genre_name}': {len(tracks)} tracks"
        )

    def _set_genre_view(self, view: str) -> None:
        """Switch de actieve sub-view en update de button-varianten.

        De variant="primary" op de actieve button benadrukt 'm visueel;
        de andere krijgen variant="default" (Button default)."""
        self._genre_view = view
        for v, btn_id in (("tracks", "gn-btn-tracks"),
                          ("artists", "gn-btn-artists"),
                          ("albums",  "gn-btn-albums")):
            try:
                btn = self.query_one(f"#{btn_id}", Button)
                btn.variant = "primary" if v == view else "default"
            except Exception:
                pass  # button niet gevonden — niet kritiek

    def _ensure_genre_detail_columns(self, view: str) -> None:
        """Stel de juiste kolommen in voor de detail-tabel op basis van de
        view. Textual's DataTable laat niet toe om kolommen te wissen, dus
        we clearen en add_columns opnieuw — add_columns is idempotent als
        de namen matchen, anders voegt-ie dupes toe. Om dat te vermijden
        skippen we als de huidige kolommen al kloppen."""
        try:
            dt = self.query_one("#lib-genres-detail", DataTable)
        except Exception:
            return  # compose nog niet klaar
        if view == "tracks":
            cols = ("♪", "Titel", "Artiest", "Album", "Duur")
        elif view == "artists":
            cols = ("Artiest", "Albums", "Tracks")
        else:  # albums
            cols = ("Album", "Artiest", "#")
        # dt.columns is dict[ColumnKey, Column]; Column.label is Text.
        existing = [str(c.label) for c in dt.columns.values()]
        if list(existing) == list(cols):
            return  # al goed — voorkomt duplicate-column-errors bij re-render
        dt.clear(columns=True)
        dt.add_columns(*cols)

    def _render_genre_detail(self) -> None:
        """Vul #lib-genres-detail met de rijen van de actieve view."""
        self._ensure_genre_detail_columns(self._genre_view)
        dt = self.query_one("#lib-genres-detail", DataTable)
        dt.clear()
        hint = self.query_one("#lib-genres-detail-hint", Static)
        if self._genre_view == "tracks":
            for i, t in enumerate(self._genre_tracks):
                dt.add_row(t.badge, t.title, t.artist, t.album,
                           _fmt_duration(t.duration), key=f"gt:{i}")
            hint.update(f"{len(self._genre_tracks)} tracks in '{self._genre_name}'")
        elif self._genre_view == "artists":
            for i, a in enumerate(self._genre_artists):
                dt.add_row(a["artist"], f"{a['album_count']}",
                           f"{a['track_count']}", key=f"ga:{i}")
            hint.update(f"{len(self._genre_artists)} artiesten — "
                        f"Enter drillt naar hun genre-tracks")
        else:  # albums
            for i, a in enumerate(self._genre_albums):
                dt.add_row(a["album"], a["artist"], f"{a['count']}",
                           key=f"gl:{i}")
            hint.update(f"{len(self._genre_albums)} albums — "
                        f"Enter drillt naar hun genre-tracks")
        dt.focus()
        dt.move_cursor(row=0)

    # ---- key bindings (BINDINGS) ------------------------------------
    def action_go_up(self) -> None:
        """Mappen-tab: één niveau omhoog (no-op als we al in root staan).
        Werkt alleen als focus in de Mappen-tab is."""
        folders = self.query_one("#lib-folders-table", DataTable)
        if folders.has_focus and self._folder_rel:
            asyncio.create_task(self._folder_ascend())

    async def action_play_all_context(self) -> None:
        """Speel alles van huidige context — vervangt queue + speel index 0.
        Werkt in Mappen (huidige map recursief), Albums/Artiesten-meta (geheel),
        en op detail-tabellen (alle geladen tracks)."""
        lib = self._app.services.library
        orch = self._app.orchestrator
        folders = self.query_one("#lib-folders-table", DataTable)
        if folders.has_focus:
            tracks = await self._run(lib.folder_tracks, self._folder_rel)
            await orch.play_all(tracks)
            self._app.notify(f"▶ {len(tracks)} uit huidige map")
            return
        alb_meta = self.query_one("#lib-albums-meta", DataTable)
        if alb_meta.has_focus:
            idx = self._row_index(alb_meta, "al:")
            if idx is not None and idx < len(self._albums):
                tracks = await self._run(lib.album_tracks, self._albums[idx]["album"])
                await orch.play_all(tracks)
                self._app.notify(f"▶ album '{self._albums[idx]['album']}' ({len(tracks)})")
            return
        art_meta = self.query_one("#lib-artists-meta", DataTable)
        if art_meta.has_focus:
            idx = self._row_index(art_meta, "ar:")
            if idx is not None and idx < len(self._artists):
                tracks = await self._run(lib.artist_tracks, self._artists[idx]["artist"])
                await orch.play_all(tracks)
                self._app.notify(f"▶ artiest '{self._artists[idx]['artist']}' ({len(tracks)})")
            return
        alb_detail = self.query_one("#lib-albums-detail", DataTable)
        if alb_detail.has_focus and self._album_tracks:
            await orch.play_all(self._album_tracks)
            self._app.notify(f"▶ {len(self._album_tracks)} album-tracks")
            return
        art_detail = self.query_one("#lib-artists-detail", DataTable)
        if art_detail.has_focus and self._artist_tracks:
            await orch.play_all(self._artist_tracks)
            self._app.notify(f"▶ {len(self._artist_tracks)} artiest-tracks")
            return
        # Lokaal > Genre: meta = heel genre; detail = huidige view.
        gn_meta = self.query_one("#lib-genres-meta", DataTable)
        if gn_meta.has_focus:
            idx = self._row_index(gn_meta, "gn:")
            if idx is not None and idx < len(self._genres):
                genre = self._genres[idx]["genre"]
                tracks = await self._run(lib.genre_tracks, genre)
                await orch.play_all(tracks)
                self._app.notify(f"▶ genre '{genre}' ({len(tracks)})")
            return
        gt = self.query_one("#lib-genres-detail", DataTable)
        if gt.has_focus:
            if self._genre_view == "tracks" and self._genre_tracks:
                await orch.play_all(self._genre_tracks)
                self._app.notify(
                    f"▶ {len(self._genre_tracks)} tracks in '{self._genre_name}'")
            elif self._genre_view == "artists":
                idx = self._row_index(gt, "ga:")
                if idx is not None and idx < len(self._genre_artists):
                    artist = self._genre_artists[idx]["artist"]
                    tracks = await self._run(lib.genre_artist_tracks,
                                             self._genre_name, artist)
                    await orch.play_all(tracks)
                    self._app.notify(
                        f"▶ {artist} in '{self._genre_name}' ({len(tracks)})")
            elif self._genre_view == "albums":
                idx = self._row_index(gt, "gl:")
                if idx is not None and idx < len(self._genre_albums):
                    album = self._genre_albums[idx]["album"]
                    tracks = await self._run(lib.genre_album_tracks,
                                             self._genre_name, album)
                    await orch.play_all(tracks)
                    self._app.notify(
                        f"▶ album '{album}' in '{self._genre_name}' ({len(tracks)})")

    def _selected_detail_index(self) -> int | None:
        return self._row_index(self.query_one("#lib-detail-table", DataTable), "s:")

    # ---- Library > YouTube helpers ------------------------------------
    def _render_yt_meta(self) -> None:
        """Vul #lib-yt-meta met de 4 vaste feeds (subs/favorites/WL/history).
        Geen netwerk-call — pas bij drill (Enter) wordt YouTubeSearch aangeroepen."""
        feeds = [
            ("sub:", "📺 Subscriptions", "subscriptions"),
            ("fav:", "★ Favorieten", "favorites"),
            ("wl:",  "📜 Watch Later", "watch_later"),
            ("h:",   "▶ History", "history"),
        ]
        self._yt_meta_rows = [{"key": k, "label": label, "method": method}
                              for k, label, method in feeds]
        mt = self.query_one("#lib-yt-meta", DataTable)
        mt.clear()
        for row in self._yt_meta_rows:
            mt.add_row(row["label"], row["method"].replace("_", " "), key=row["key"])
        # hint aanpassen op basis van cookies-config
        hint = self.query_one("#lib-yt-hint", Label)
        yt = self._app.services.providers.get("youtube")
        cfb = getattr(yt, "_cookies_from_browser", "") if yt else ""
        if not cfb:
            hint.update("⚠ zet [youtube] cookies_from_browser in config.toml "
                        "voor subscriptions/favorieten")

    async def _open_yt_collection(self) -> None:
        """Drill: lees geselecteerde meta-rij → laad bijbehorende feed."""
        meta = self.query_one("#lib-yt-meta", DataTable)
        try:
            key = meta.coordinate_to_cell_key(meta.cursor_coordinate).row_key.value
        except Exception:
            return
        if not key:
            return
        row = next((r for r in self._yt_meta_rows if r["key"] == key), None)
        if not row:
            return
        yt = self._app.services.providers.get("youtube")
        if yt is None:
            self._app.notify("YouTube-provider niet beschikbaar", severity="error")
            return
        method = getattr(yt, row["method"], None)
        if method is None:
            self._app.notify(f"YouTube-feed '{row['method']}' ontbreekt", severity="error")
            return
        self.query_one("#lib-yt-hint", Label).update(f"{row['label']}: laden…")
        try:
            tracks = await method(limit=100)
        except Exception as e:
            self._app.notify(f"YouTube laden mislukte: {e}", severity="warning")
            self.query_one("#lib-yt-hint", Label).update(
                f"⚠ {row['label']}: {e}"
            )
            return
        self._tracks = tracks
        dt = self.query_one("#lib-yt-detail", DataTable)
        dt.clear()
        for i, t in enumerate(tracks):
            dt.add_row(t.badge, t.title, t.artist, _fmt_duration(t.duration),
                       key=f"yt:{i}")
        self.query_one("#lib-yt-hint", Label).update(
            f"{row['label']}: {len(tracks)} video's — Enter = spelen, V = video"
        )
        dt.focus()
        dt.move_cursor(row=0)
        if not tracks:
            self._app.notify(
                f"Geen video's in {row['label']}. "
                "Ingelogd in je browser? cookies_from_browser in config.toml?",
                severity="warning",
            )

    def _yt_detail_index(self) -> int | None:
        return self._row_index(self.query_one("#lib-yt-detail", DataTable), "yt:")


class MusiApp(App):
    """De Textual-app: Header + TabbedContent(Zoeken/Queue/Library) +
    NowPlaying-footer + Footer met keybindings.

    Lifecycle (``cli._run_tui``):
      1. ``MusiApp(orch, sv, cfg)`` — krijgt orchestrator + services + config.
      2. ``on_mount`` koppelt de orchestrator-event-callback, geeft de LibraryPane
         een app-referentie (die had 'm niet tijdens compose), en start een
         achtergrondtaak voor de library-rescan + render.
      3. ``app.run_async()`` draait tot de gebruiker ``q`` drukt.

    Wat het doet met binnenkomende events (``_on_orch_event``):
      * ``"state"`` → update NowPlaying (track/positie/duur/status).
      * ``"queue"`` → render QueuePane opnieuw.
      * ``"error"`` → log (de UI toont 'm via de statusbalk; engine-fouten komen
        zelden voor want elke engine call wordt in de orchestrator opgevangen).

    De V-toets (``action_toggle_video``) spawnt een aparte mpv-instantie als
    **video-viewer** naast de audio-engine — zie de methode voor details.
    """

    CSS = """
    Screen { layout: vertical; layers: floating above; }
    #main { height: 1fr; }
    NowPlaying { dock: bottom; height: 3; padding: 0 1; background: $boost; }
    SearchPane, QueuePane, LibraryPane { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_search", "Zoeken"),
        Binding("1", "tab('search')", "Zoeken"),
        Binding("2", "tab('queue')", "Queue"),
        Binding("3", "tab('library')", "Library"),
        Binding("space", "toggle_pause", "Play/Pause"),
        Binding("n", "next", "Volgende"),
        Binding("p", "prev", "Vorige"),
        Binding("plus", "vol_up", "Volume +"),
        Binding("minus", "vol_down", "Volume -"),
        Binding("c", "clear_queue", "Wis queue"),
        Binding("v", "toggle_video", "Video"),
    ]

    results: reactive[list[Track]] = reactive(list, recompose=False)

    def __init__(self, orchestrator: Orchestrator, services: Services, cfg) -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self.services = services
        self.cfg = cfg
        # V-toets: aparte mpv-instantie als pure video-viewer (geen IPC, geen
        # orchestrator-koppeling). De hoofd-mpv speelt door — alleen deze
        # tweede mpv wordt aan/uit gezet.
        self._video_proc: asyncio.subprocess.Process | None = None
        # ---- auto-rip: elke YouTube-track die écht speelt → mp3 in ~/Music ----
        # De UI bepaalt wánneer (nieuwe YT-track + ~5s grace), Ripper doet de
        # extractie+organize. Sessie-sets deduppen; rips.json dedup't cross-sessie.
        self._ripper = Ripper(cfg.music_dir, cfg.cache_dir, cfg.yt_cookies_from_browser)
        self._rip_pending: set[str] = set()   # video-ids met lopende grace-timer
        self._rip_inflight: set[str] = set()  # video-ids die nu downlowaden
        self._rip_done: set[str] = set()      # video-ids die deze sessie klaar zijn

    def compose(self) -> ComposeResult:
        # Albumhoes-overlay (rechtsboven) — dit widget zit op de "floating" layer
        # zodat het boven alles rendered, ongeacht de layout-volgorde.
        yield FloatingCover(self.cfg.cache_dir / "art", id="floating-cover")
        yield Header(show_clock=False)
        with TabbedContent(id="tabs", initial="tab-search"):
            with TabPane("Zoeken", id="tab-search"):
                yield SearchPane(self)
            with TabPane("Queue", id="tab-queue"):
                yield QueuePane(self)
            with TabPane("Library", id="tab-library"):
                yield LibraryPane(None)  # zodra compose zonder app; via on_mount zetten
        # NowPlaying eerst (dock:bottom vult van onderaf), Footer daarna — anders
        # zou Textual beide widgets op dezelfde y=laatste-rij zetten en elkaar
        # op één pixel overdekken.
        yield NowPlaying(self.cfg.cache_dir / "art", id="now-playing")
        yield Footer()

    # ---- acties -----------------------------------------------------
    async def on_mount(self) -> None:
        self.title = "musi"
        self.sub_title = "lokaal · YouTube · Spotify"
        # orchestrator-event koppeling
        self.orchestrator._event_cb = self._on_orch_event  # type: ignore[attr-defined]
        # library-tabbladen krijgen een app-referentie (na constructie)
        lib = self.query_one(LibraryPane)
        lib._app = self
        # library vullen (async, in thread)
        asyncio.create_task(self._refresh_library())
        # Spotify-library wordt LAZY geladen bij het openen van de Spotify-tab
        # (anders triggert elke opstart de OAuth-browser-flow).

    async def _refresh_library(self) -> None:
        # Herscan (pikt nieuwe/gewijzigde bestanden op; snel via mtime-cache)
        # en daarna recente tracks laden + renderen. In threads om de UI niet
        # te blokkeren. Loopt vanuit on_mount, dus het scherm bestaat hier.
        try:
            await asyncio.to_thread(self.services.library.rescan)
            tracks = await asyncio.to_thread(self.services.library.recent, 500)
            self.query_one(LibraryPane).render_library(tracks)
        except Exception as e:
            log.warning("library-refresh mislukt: %s", e)

    async def _refresh_spotify_library(self) -> None:
        try:
            await self.query_one(LibraryPane).refresh_spotify(self)
        except Exception as e:
            self.notify(f"Spotify-library: {e}", severity="warning")

    # ---- playlist-picker (Spotify save) ---------------------------------
    # ``open_save_picker`` is de single entry-point voor alle 3 de panes
    # (SearchPane / QueuePane / LibraryPane). Het haalt de SpotifySearch op,
    # checkt of die er is, en pusht de modal met een dismiss-callback. Per
    # de modal-deadlock-fix in memory NOOIT ``await push_screen(..., wait_for_dismiss=True)``
    # vanuit een action — altijd callback-vorm.

    def open_save_picker(self, uris: list[str], title: str) -> None:
        """Open de PlaylistPickerModal om ``uris`` op te slaan. UI-melding als
        Spotify niet beschikbaar is."""
        sp = self.services.providers.get("spotify")
        if sp is None:
            self.notify("Spotify niet beschikbaar — check je config / OAuth.",
                        severity="error")
            return
        if not uris:
            self.notify("Geen tracks om op te slaan.", severity="warning")
            return
        self._pending_save_uris = list(uris)
        self.push_screen(
            PlaylistPickerModal(sp, title or "(onbekend)"),
            callback=self._on_picker_result,
        )

    def _on_picker_result(self, result) -> None:
        """Dismiss-callback van PlaylistPickerModal. Dispatcht op basis van het
        resultaat-type en start de zware I/O in een aparte task zodat de
        dismiss-callback non-blocking terugkeert (consistent met andere
        modal-callbacks; zie deadlock-fix in memory)."""
        uris = list(getattr(self, "_pending_save_uris", []) or [])
        self._pending_save_uris = []
        if not result or not uris:
            return
        kind = result[0]
        # eerste actie: een directe notify zodat de gebruiker niet wacht op
        # de trage playlist-refresh. De zware I/O draait op de achtergrond.
        asyncio.create_task(self._apply_picker_result(kind, result, uris))

    async def _apply_picker_result(self, kind: str, result: tuple, uris: list[str]
                                   ) -> None:
        sp = self.services.providers.get("spotify")
        if sp is None:
            self.notify("Spotify niet beschikbaar.", severity="error")
            return
        n = len(uris)
        try:
            if kind == "liked":
                ok, err = await sp.add_to_saved_tracks(uris)
                if not ok:
                    self.notify(f"Liked Songs mislukt: {err}", severity="error")
                    return
                label = "Liked Songs"
            elif kind == "playlist":
                uri = result[1]
                ok, err = await sp.add_to_playlist(uri, uris)
                if not ok:
                    self.notify(f"Toevoegen mislukt: {err}", severity="error")
                    return
                pl_name = (
                    self.query_one(LibraryPane)._playlists_by_uri.get(uri, {})
                    or {}).get("name") or uri
                label = pl_name
            elif kind == "new":
                name = result[1]
                pl = await sp.create_playlist(name)
                if pl is None:
                    self.notify(f"Aanmaken mislukt: {sp.last_error or 'onbekend'}",
                                severity="error")
                    return
                new_uri = pl.get("uri") or ""
                ok, err = await sp.add_to_playlist(new_uri, uris)
                if not ok:
                    self.notify(
                        f"Playlist aangemaakt, maar nummers toevoegen mislukte: {err}",
                        severity="error")
                    return
                label = name
            else:
                self.notify(f"Onbekend picker-resultaat: {kind!r}",
                            severity="error")
                return
        except Exception as e:
            self.notify(f"Spotify-actie fout: {e}", severity="error")
            return
        self.notify(f"{n} nummer(s) toegevoegd aan '{label}'")
        # Refresh alleen de playlists-rij van het Spotify-tabblad zodat de
        # nieuwe playlist/track-counts zichtbaar worden. ~300-500ms.
        try:
            await self.query_one(LibraryPane)._refresh_playlists_after_mutation()
        except Exception as e:
            log.warning("Playlist-refresh na save mislukte: %s", e)

    def _on_orch_event(self, ev: PlaybackEvent) -> None:
        if ev.kind == "state" and ev.state is not None:
            self.query_one(NowPlaying).update_from_state(ev.state)
            self.query_one(FloatingCover).update_from_state(ev.state)
            # auto-rip: bij een YouTube-track plan een rip na de grace-periode.
            # _schedule_rip dedup't op video-id (goedkoop, want dit vuurt bij
            # elke positie-update). Spotify/lokaal worden hier stil genegeerd.
            track = ev.state.track
            if track is not None and track.source == "youtube":
                self._schedule_rip(track)
        elif ev.kind == "queue" and ev.queue is not None:
            self.query_one(QueuePane).render_queue(ev.queue)

    # ---- auto-rip (YouTube → mp3 in ~/Music) -------------------------
    def _schedule_rip(self, track: Track) -> None:
        """Plan een auto-rip voor een YouTube-track ná een korte grace-periode
        (zie ``_rip_after_grace``). Dedup op video-id binnen de sessie:
        pending/inflight/done → negeer. ``create_task`` zodat de handler direct
        terugkeert (deze methode draait in de orchestrator state-callback)."""
        vid = video_id(track)
        if (not vid or vid in self._rip_pending
                or vid in self._rip_inflight or vid in self._rip_done):
            return
        self._rip_pending.add(vid)
        asyncio.create_task(self._rip_after_grace(track, vid))

    async def _rip_after_grace(self, track: Track, vid: str,
                               delay: float = 5.0) -> None:
        """Wacht ``delay`` seconden; ript alleen als ``vid`` dan nog de actieve,
        spelende track is. Zo trigger skippen door een queue (binnen de grace)
        geen tientallen downloads. Bij overslaan halen we 'm uit ``_rip_pending``
        zodat een latere herhaling 't wél opnieuw mag proberen.

        Na de rip: ``library.rescan`` (pikt het nieuwe bestand op) + notify.
        Fouten raken nooit de playback (de rip loopt volledig los van mpv)."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._rip_pending.discard(vid)
            raise
        # Nog steeds dezelfde track, en nog steeds aan 't spelen?
        cur = self.orchestrator.state.track
        playing = self.orchestrator.state.status.value == "playing"
        if cur is None or video_id(cur) != vid or not playing:
            self._rip_pending.discard(vid)
            log.info("rip grace afgebroken voor %s (niet meer actief)", vid)
            return
        self._rip_pending.discard(vid)
        self._rip_inflight.add(vid)
        try:
            res = await self._ripper.rip(track)
        except Exception as e:
            self._rip_inflight.discard(vid)
            self._rip_done.add(vid)
            self.notify(f"Rip mislukt: {e}", severity="error")
            log.warning("rip faalde voor %s: %s", vid, e)
            return
        self._rip_inflight.discard(vid)
        self._rip_done.add(vid)
        if res.status == "done" and res.path is not None:
            try:
                await asyncio.to_thread(self.services.library.rescan)
            except Exception as e:
                log.warning("library-rescan na rip mislukte: %s", e)
            try:
                rel = os.path.relpath(res.path, self.cfg.music_dir)
            except ValueError:
                rel = str(res.path)  # ander volume → geen relpath
            self.notify(f"💾 opgeslagen: {rel}")
            log.info("rip opgeslagen: %s", res.path)
        elif res.status == "exists":
            log.debug("rip skipped (bestaat al): %s", vid)
        else:
            self.notify(f"Rip mislukt: {res.reason}", severity="error")
            log.warning("rip faalde voor %s: %s", vid, res.reason)

    async def _on_search_submit(self, query: str) -> None:
        query = (query or "").strip()
        if not query:
            return
        label = self.query_one("#search-source-label", Label)
        label.update("zoeken…")
        # Token-gebaseerd parsen van bron-prefix (``/yt`` …) én sort-flag
        # (``/date``/``!date`` …) én ``--limit=N`` (Spotify-specifiek). Beide
        # mogen op elke positie en in willekeurige volgorde staan — "/yt /date
        # lofi", "/date /yt lofi" en "!date /yt lofi" werken allemaal. We
        # strippen de herkende tokens eruit; wat overblijft is de zoekterm
        # (originele casing behouden).
        SOURCE_PREFIX = {
            "/yt": "youtube", "/youtube": "youtube",
            "/lokaal": "local", "/local": "local", "/l": "local",
            "/spotify": "spotify", "/sp": "spotify",
            # Drill-prefixes (Spotify-specifiek): openen een aparte flow,
            # geen reguliere track-search.
            "/spotify-artiest": "spotify-artist", "/spotify-artist": "spotify-artist",
            "/spotify-genre": "spotify-genre",
        }
        SORT_FLAGS = {
            "!date": "date", "/date": "date",
            "!new": "date", "/new": "date",
            "!nieuw": "date", "/nieuw": "date",
        }
        source: str | None = None
        sort = "relevance"
        spotify_limit: int | None = None
        kept: list[str] = []
        for tok in query.split():
            low = tok.lower()
            if low in SOURCE_PREFIX:
                if source is None:
                    source = SOURCE_PREFIX[low]
                # dubbele bron-prefix negeren (niet als zoekterm gebruiken)
            elif low in SORT_FLAGS:
                sort = SORT_FLAGS[low]
            elif low.startswith("--limit=") or low.startswith("limit="):
                # ``--limit=50`` of ``limit=50`` — alleen voor Spotify-flows.
                val = low.split("=", 1)[1]
                try:
                    spotify_limit = max(1, min(50, int(val)))
                except ValueError:
                    pass
            else:
                kept.append(tok)
        query = " ".join(kept)
        if not query:
            return
        # Speciale drill-prefixes: openen hun eigen UI-flow in plaats van een
        # reguliere zoekactie.
        if source == "spotify-artist":
            asyncio.create_task(self._open_spotify_artist(query))
            label.update(f"bron: spotify-artiest · {query}")
            return
        if source == "spotify-genre":
            asyncio.create_task(self._open_spotify_genre(query, spotify_limit or 20))
            label.update(f"bron: spotify-genre · {query} · limit={spotify_limit or 20}")
            return
        # "datum" yt-dlp laat elke video resolv'en (flat-playlist levert geen
        # upload-timestamp); ~30-60s voor 20 resultaten. Waarschuw de gebruiker
        # één keer, zodat de "zoeken…" spinner niet voor een hang lijkt.
        if sort == "date" and (source in (None, "youtube")):
            self.notify("Sorteren op datum duurt ~30-60s (yt-dlp resolved elke video)…",
                        timeout=4)

        sel = self.services.providers
        tasks: dict[str, Any] = {}
        if source in (None, "local"):
            tasks["local"] = sel["local"].search(query, limit=25)
        if source in (None, "youtube"):
            tasks["youtube"] = sel["youtube"].search(query, limit=15, sort=sort)
        if source in (None, "spotify") and "spotify" in sel:
            tasks["spotify"] = sel["spotify"].search(query, limit=spotify_limit or 20)
        names = list(tasks.keys())
        got = await asyncio.gather(*[tasks[n] for n in names])
        by_source = dict(zip(names, got))
        # volgorde: lokaal → youtube → spotify. Voor Spotify met
        # ``search_all`` (Top-resultaat-kaart): we vervangen de gewone
        # ``search``-call door ``search_all`` en voegen de top-result-rij
        # vooraf.
        self.results = results = (by_source.get("local", [])
                                  + by_source.get("youtube", [])
                                  + by_source.get("spotify", []))
        bron = source or "alle"
        delen = " · ".join(f"{len(by_source.get(n, []))} {n}" for n in names)
        sort_txt = " · sort: datum" if sort == "date" else ""
        label.update(f"bron: {bron} · {delen}{sort_txt}")
        # Extra Datum/Views/Likes-kolommen tonen bij ``!date`` (alleen dáár
        # heeft YouTube die data opgehaald — andere modi zouden de kolommen
        # altijd leeg tonen).
        show_stats = (sort == "date")
        if source == "spotify" and "spotify" in sel and not show_stats:
            # Top-resultaat-kaart-flow: extra rij boven de tracks.
            asyncio.create_task(self._spotify_search_with_top_result(
                query, results, spotify_limit or 20))
            return
        self.query_one(SearchPane).watch_results(results, show_stats=show_stats)
        self._focus_results_table()

    async def _spotify_search_with_top_result(self, query: str,
                                              tracks: list, limit: int) -> None:
        """Haalt een extra ``search_all`` om de Top-resultaat-kaart-rij boven
        de bestaande tracks te tonen. Bij lege artiest-/album-resultaten
        gedragen we ons als een gewone track-search (graceful fallback)."""
        sp = self.services.providers.get("spotify")
        if sp is None:
            self.query_one(SearchPane).watch_results(tracks)
            return
        try:
            res = await sp.search_all(query, limit=limit)
        except Exception as e:
            log.warning("search_all mislukte: %s", e)
            self.query_one(SearchPane).watch_results(tracks)
            self._focus_results_table()
            return
        # plak de top-result-rij aan de tracks-tabel vast. De top-rij is
        # een pseudo-Track met source=='spotify-top' zodat de rest van de
        # UI 'm kan onderscheiden van echte tracks.
        top_artist = (res.get("artists") or [None])[0]
        top_album = (res.get("albums") or [None])[0]
        # render via SearchPane — helper hieronder laat de SearchPane de
        # top-rij tonen boven de tracks.
        try:
            self.query_one(SearchPane).watch_spotify_top_result(
                query, top_artist, top_album, tracks)
        except AttributeError:
            # SearchPane heeft de helper nog niet; toon alleen tracks.
            self.query_one(SearchPane).watch_results(tracks)
        self._focus_results_table()

    async def _open_spotify_artist(self, query: str) -> None:
        """``/spotify-artiest <naam>`` — toont Top-artiest in de resultaten +
        10 top-tracks van die artiest onder elkaar (in de track-tabel,
        afspeelbaar). Drill op Enter: zie action_play_now."""
        sp = self.services.providers.get("spotify")
        if sp is None:
            self.notify("Spotify niet beschikbaar.", severity="error")
            return
        try:
            res = await sp.search_all(query, limit=5)
        except Exception as e:
            self.notify(f"Spotify-artiest-zoek mislukt: {e}", severity="error")
            return
        top_artist = (res.get("artists") or [None])[0]
        if top_artist is None:
            self.notify(f"Geen artiest gevonden voor '{query}'.",
                        severity="warning")
            return
        # haal top-tracks op (≤10, market US — kan uitgebreid worden)
        try:
            top_tracks = await sp.artist_top_tracks(top_artist["id"])
        except Exception as e:
            self.notify(f"Top-tracks mislukt: {e}", severity="warning")
            top_tracks = []
        # Zet ze als pseudo-Track in de resultaten-tabel
        from ..search.spotify import _to_track
        track_objs = [_to_track(t) for t in (top_tracks or [])]
        # toon via SearchPane: pseudo-artiest-rij + tracks
        try:
            self.query_one(SearchPane).watch_artist_top(
                top_artist, track_objs)
        except AttributeError:
            self.query_one(SearchPane).watch_results(track_objs)
        self._focus_results_table()

    async def _open_spotify_genre(self, query: str, limit: int) -> None:
        """``/spotify-genre <naam> [--limit=N]`` — toont Spotify's top-level
        categorieën die matchen op ``query``. Enter op een categorie drillt
        naar het detail-scherm met twee sub-tabs (Artiesten / Playlists).

        Bij lege ``query`` tonen we alle categorieën.

        **Fallback bij geen categorie-match**: Spotify's ``categories``-
        endpoint bevat niet alle genres (New Age, IDM, Vaporwave, …). Als
        ``q`` niet matcht, renderen we in plaats daarvan een directe
        fallback-detail-pagina met ``search_artists_by_tag(q)`` +
        ``search_playlists_by_tag(q)`` — dezelfde view-toggle UX als bij
        een categorie-detail.
        """
        sp = self.services.providers.get("spotify")
        if sp is None:
            self.notify("Spotify niet beschikbaar.", severity="error")
            return
        try:
            cats = await sp.categories(limit=50)
        except Exception as e:
            self.notify(f"Genre-categorieën mislukt: {e}", severity="error")
            return
        if not cats:
            self.notify("Spotify-categorieën niet beschikbaar.", severity="warning")
            return
        # filter op query als die niet leeg is
        q = (query or "").strip().lower()
        if q:
            cats = [c for c in cats if q in (c.get("name") or "").lower()]
        if not cats:
            # Fallback: render direct het genre-detail (artiesten +
            # playlists) op de query. Geen Spotify-categorie-ID maar de
            # vrije tekst-tag — werkt universeel.
            cat_dummy = {"id": f"tag:{q}", "name": query}
            self.notify(
                f"'{query}' is geen Spotify-categorie — valt terug op tag-search.",
                timeout=4)
            await self._open_spotify_genre_detail(
                category_id=f"tag:{q}", view="artists")
            return
        try:
            self.query_one(SearchPane).watch_genre_categories(query, cats)
            self._focus_results_table()
        except AttributeError:
            self.notify("Genre-categorieën weergave niet geïmplementeerd.",
                        severity="warning")

    # ---- drill-helpers (gebruikt door SearchPane row-key-dispatch) ------
    # ``_selected_key`` in SearchPane geeft een rij-key terug; afhankelijk
    # van het prefix (``a:`` / ``al:`` / ``g:`` / ``i:``) roept deze methoden
    # aan om door te drillen of de view te switchen. Bij foutmelding tonen
    # we via ``self.notify`` zodat de gebruiker niet naar logs hoeft te kijken.

    async def _open_spotify_artist_by_uri(self, uri_or_id: str) -> None:
        """Drill op een artiest-rij in de Search-resultaten: haalt de
        discografie (albums + singles + compilaties + appears_on) en
        rendert ze via ``watch_artist_detail``.

        ``uri_or_id`` is een ``spotify:artist:<id>`` URI of kale hex-ID.
        Vanuit deze view kan de gebruiker doordrillen op een album
        (key ``al:<uri>``) om de track-lijst te laden.
        """
        sp = self.services.providers.get("spotify")
        if sp is None:
            self.notify("Spotify niet beschikbaar.", severity="error")
            return
        try:
            artist = await sp.artist(uri_or_id)
        except Exception as e:
            self.notify(f"Artiest ophalen mislukt: {e}", severity="error")
            return
        if not artist:
            self.notify("Artiest niet gevonden.", severity="warning")
            return
        try:
            albums = await sp.artist_albums(
                artist.get("id") or uri_or_id,
                limit=50,
                groups="album,single,compilation,appears_on",
            )
        except Exception as e:
            self.notify(f"Discografie mislukt: {e}", severity="error")
            return
        try:
            self.query_one(SearchPane).watch_artist_detail(artist, albums)
            self._current_drill_artist = artist
            self.results = []  # drill-view heeft geen track-results
            label = self.query_one("#search-source-label", Label)
            label.update(
                f"bron: spotify-discografie · {artist.get('name', '?')} "
                f"· {len(albums)} releases")
            self._focus_results_table()
        except Exception as e:
            self.notify(f"Render-fout: {e}", severity="error")

    async def _open_spotify_album_by_uri(self, uri_or_id: str) -> None:
        """Drill op een album-rij: haalt het album + alle tracks en rendert
        ze als reguliere tracks. Voor het gemak converteren we album-tracks
        via ``_to_track`` met de albumhoes als ``art_url``."""
        sp = self.services.providers.get("spotify")
        if sp is None:
            self.notify("Spotify niet beschikbaar.", severity="error")
            return
        try:
            alb = await sp.album_full(uri_or_id)
            if alb is None:
                self.notify("Album niet gevonden.", severity="warning")
                return
            tracks = await sp.album_tracks(
                alb.get("uri", ""), limit=200,
                art_url=_art_from_collection(alb))
        except Exception as e:
            self.notify(f"Album ophalen mislukt: {e}", severity="error")
            return
        if not tracks:
            self.notify("Album heeft geen afspeelbare tracks.",
                        severity="warning")
            return
        self.results = tracks
        self.query_one(SearchPane).watch_results(tracks)
        self._focus_results_table()
        label = self.query_one("#search-source-label", Label)
        label.update(
            f"bron: spotify-album · {alb.get('name', '?')} · {len(tracks)} tracks")

    async def _open_spotify_genre_detail(self, category_id: str,
                                         view: str = "artists") -> None:
        """Drill in een genre-categorie: haal artiesten (via tag-search) en
        playlists (via vrij-tekst-search) en render ze in de sub-tab.

        ``view`` bepaalt welke subset meteen zichtbaar is (``artists`` of
        ``playlists``). De app-scope ``self._genre_view`` onthoudt de
        gekozen view voor latere toggle-actie.

        Accepteert ook ``tag:<naam>`` als ``category_id`` voor de
        fallback-flow (genre is geen Spotify-categorie). In dat geval
        wordt de tag-letterlijk als zoekterm gebruikt — werkt universeel
        voor genres die buiten Spotify's categorie-lijst vallen (New
        Age, IDM, Vaporwave, …).
        """
        sp = self.services.providers.get("spotify")
        if sp is None:
            self.notify("Spotify niet beschikbaar.", severity="error")
            return
        # categorie of tag-fallback
        tag_override: str | None = None
        if category_id.startswith("tag:"):
            tag_override = category_id[4:]
            cat = {"id": category_id, "name": tag_override}
        else:
            try:
                all_cats = await sp.categories(limit=50) or []
            except Exception:
                all_cats = []
            cat = next((c for c in all_cats if c.get("id") == category_id), None)
            if cat is None:
                cat = {"id": category_id, "name": category_id}
        if tag_override is not None:
            tag = tag_override.lower()
        else:
            tag = (cat.get("name") or category_id).lower()
            # verwijder 'Music' suffix (Spotify's "Workout Music" → tag "workout")
            tag = tag.replace(" music", "").strip()
        # haal artiesten + playlists
        self._genre_view = view
        try:
            if view == "artists":
                items = await sp.search_artists_by_tag(tag, limit=20)
                kind = "artists"
            else:
                items = await sp.search_playlists_by_tag(tag, limit=20)
                kind = "playlists"
        except Exception as e:
            self.notify(f"Genre-detail mislukt: {e}", severity="error")
            return
        # filter None-items (Spotify geeft soms None terug in playlists)
        items = [x for x in (items or []) if x]
        self._genre_pending = (cat, kind, items)
        try:
            self.query_one(SearchPane).watch_genre_detail(
                cat, kind, items,
                header=f"{len(items)} {kind} voor '{cat.get('name', '?')}'")
            label = self.query_one("#search-source-label", Label)
            label.update(f"bron: spotify-genre · {cat.get('name', '?')} · {kind}")
            self._focus_results_table()
        except Exception as e:
            self.notify(f"Render-fout: {e}", severity="error")

    async def _toggle_genre_detail_view(self) -> None:
        """Toggle tussen Artiesten / Playlists in een genre-detail-pagina."""
        view = getattr(self, "_genre_view", "artists")
        pending = getattr(self, "_genre_pending", None)
        if pending is None:
            return
        new_view = "playlists" if view == "artists" else "artists"
        cat, _, _ = pending
        await self._open_spotify_genre_detail(cat.get("id", ""), view=new_view)

    async def _open_genre_detail_item(self, idx: int) -> None:
        """Drill op een item-rij in de genre-detail-pagina (afhankelijk van
        de huidige view): bij ``artists`` → drill naar artiest;
        bij ``playlists`` → drill naar playlist (laad tracks en render)."""
        pending = getattr(self, "_genre_pending", None)
        if pending is None:
            return
        cat, kind, items = pending
        if idx >= len(items):
            return
        item = items[idx]
        if kind == "artists":
            uri = item.get("uri") or item.get("id") or ""
            await self._open_spotify_artist_by_uri(uri)
        else:
            uri = item.get("uri") or ""
            if not uri:
                self.notify("Geen playlist-URI.", severity="warning")
                return
            sp = self.services.providers.get("spotify")
            if sp is None:
                self.notify("Spotify niet beschikbaar.", severity="error")
                return
            try:
                tracks = await sp.playlist_tracks(uri, limit=200)
            except Exception as e:
                self.notify(f"Playlist-tracks mislukt: {e}", severity="error")
                return
            tracks = [t for t in (tracks or []) if t]
            if not tracks:
                self.notify("Playlist is leeg.", severity="warning")
                return
            self.results = tracks
            self.query_one(SearchPane).watch_results(tracks)
            self._focus_results_table()
            label = self.query_one("#search-source-label", Label)
            label.update(
                f"bron: spotify-playlist · {item.get('name', '?')} · {len(tracks)} tracks")

    async def play_track(self, track: Track) -> None:
        # wis de queue en begin met deze track, of voeg toe aan queue?
        # We kiezen: als de queue leeg is, zet 'm erin en speel; anders voeg toe en speel.
        self.orchestrator.queue.clear()
        self.orchestrator.queue_add(track)
        await self.orchestrator.play_index(0)

    # ---- actie-handlers (BINDINGS) -----------------------------------
    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def _focus_results_table(self) -> None:
        """Verplaats focus naar de #results-tabel en zet de cursor op de
        eerste zinvolle rij (skip header-rijen zoals ``h:top`` /
        ``h:cat`` / ``h:gd``). Wordt aangeroepen na elke ``watch_*``-render
        zodat de volgende Enter meteen drill/speel-trigger werkt — anders
        moet de gebruiker eerst handmatig Tab/↓ doen om focus bij de
        tabel te krijgen.
        """
        try:
            tbl = self.query_one("#results-table", DataTable)
        except Exception:
            return
        if tbl.row_count == 0:
            return
        tbl.focus()
        # zoek eerste rij zonder h:-prefix
        target = 0
        for i in range(tbl.row_count):
            try:
                k = tbl.coordinate_to_cell_key((i, 0)).row_key.value
                if k and not k.startswith("h:"):
                    target = i
                    break
            except Exception:
                pass
        tbl.move_cursor(row=target)

    async def action_toggle_pause(self) -> None:
        """Spatie: wissel pauze/hervat op de actieve engine (no-op als er niets
        speelt — zie ``orchestrator.toggle_pause`` voor het waarom)."""
        await self.orchestrator.toggle_pause()

    async def action_next(self) -> None:
        """``n``: speel de volgende track (queue door)."""
        await self.orchestrator.next()

    async def action_prev(self) -> None:
        """``p``: speel de vorige track."""
        await self.orchestrator.prev()

    async def action_vol_up(self) -> None:
        """``+``: volume +5 (geklemd op 100)."""
        cur = self.orchestrator.state.volume
        await self.orchestrator.set_volume(min(100.0, cur + 5))

    async def action_vol_down(self) -> None:
        """``-``: volume -5 (geklemd op 0)."""
        cur = self.orchestrator.state.volume
        await self.orchestrator.set_volume(max(0.0, cur - 5))

    async def action_clear_queue(self) -> None:
        """Wis de hele queue."""
        self.orchestrator.queue_clear()

    async def action_toggle_video(self) -> None:
        """V-toets: spawn een aparte mpv-instantie als **video-viewer** voor de
        huidige YouTube-track; tweede V sluit het venster weer.

        Werkt alleen als er een YouTube-track speelt. Bij niet-YouTube of geen
        actieve track: notify + geen actie. Bij een lopende video-mpv: terminate
        (kill na 2s timeout).

        De video-mpv krijgt ``--ytdl-format=best`` (gecombineerde stream tot
        1080p) — **niet** ``bestaudio`` (audio-only → leeg venster) en **niet**
        ``bestvideo+bestaudio`` (twee DASH-streams die mpv niet altijd via EDL
        kan muxen). De audio-mpv in de orchestrator blijft doorlopen — er
        kunnen dus kort twee keer dezelfde stream lopen, maar dat is voor YouTube
        acceptabel (en de voordelen van een robuuste viewer wegen zwaarder).

        Bij het sluiten van de audio-track of de app: de video-mpv sluit
        **niet** automatisch (kan bij live-uitzendingen nog relevant zijn);
        gebruiker sluit met V opnieuw. Zie README "YouTube-subscriptions & video".
        """
        # Al draait er een video-mpv → dichtgooien.
        if self._video_proc is not None:
            try:
                self._video_proc.terminate()
                try:
                    await asyncio.wait_for(self._video_proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self._video_proc.kill()
            except ProcessLookupError:
                pass
            self._video_proc = None
            self.notify("Video gesloten")
            return

        # Geen video-proc → spawnen voor de huidige YouTube-track.
        track = self.orchestrator.state.track
        if track is None or track.source != "youtube":
            self.notify("Video: alleen voor YouTube-tracks (speel er eerst een)",
                        severity="warning")
            return
        # Argumenten: zelfde ytdl-opties als de audio-mpv, maar nu mét video
        # in een apart Wayland-window (`--vo=gpu --force-window`). Belangrijk:
        # `--ytdl-format=best` (een GEcombineerde video+audio stream), NIET
        # bestaudio (audio-only → leeg venster) en NIET bestvideo+bestaudio
        # (twee DASH-streams die mpv moet muxen — faalt regelmatig). `best`
        # levert één bestand met beeld+geluid, tot 1080p. Geen IPC, geen idle:
        # één-shot, stopt als de track/stream eindigt.
        args: list[str] = [
            "mpv",
            "--no-terminal",
            "--vo=gpu",
            "--force-window",
            "--ytdl=yes",
            "--ytdl-format=best",
            track.uri,
        ]
        log.info("V-toets: video-mpv start voor %s", track.uri)
        try:
            self._video_proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.notify("mpv niet gevonden — kan video niet openen", severity="error")
            self._video_proc = None
            return
        self.notify(f"▶ video: {track.title}")

    def action_tab(self, name: str) -> None:
        """Wissel naar een top-level tabblad op naam (``search``/``queue``/``library``).
        Gekoppeld aan de toetsen ``1/2/3``."""
        self.query_one("#tabs", TabbedContent).active = f"tab-{name}"