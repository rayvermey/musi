"""Lokale muziek-library: scant een map, leest tags met mutagen en indexeert
alles in een sqlite-db met een FTS5-virtual-table voor snel zoeken.

Twee lagen:
* **scan** — incrementeel per bestand: de mtime wordt vergeleken, zodat alleen
  nieuwe/gewijzigde bestanden opnieuw gelezen hoeven worden. Verwijderde
  bestanden worden opgeruimd. Resultaat: een snelle herscan (enkel gewijzigde
  bestanden) i.p.v. een dure volledige herscan.
* **queries** — ``recent`` (platte lijst), ``search`` (FTS5 full-text), en een
  aantal blader-methoden (``folder_entries``/``albums``/``artists`` + hun
  detail-drills) die de Library > Lokaal-subtabs voeden.

Concurrency: scan én queries draaien vanuit worker-threads (``asyncio.to_thread``
in de app), dus alle DB-toegang wordt geserialiseerd via een **reentrant**
``threading.RLock``. RLock (niet Lock) is verplicht: ``rescan()`` roept intern
``count()`` aan, en met een gewone ``Lock`` zou die re-entry deadlocken.

Schema:
* ``tracks``  — één rij per bestand (path uniek, metadata, mtime, art_path).
* ``tracks_fts`` — FTS5-virtual-table over title/artist/album, met
  ``content='tracks'`` (external-content-tabel) + after-insert/delete/update
  triggers die de FTS-index synchroon houden. Zoeken gebeurt via MATCH, met
  ``ORDER BY rank`` voor relevantie.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from mutagen import File

from .models import Track

# Bestandsextensies die we als audio beschouwen tijdens de scan. Alles buiten
# deze set wordt overgeslagen (geen .txt/.jpg/.cue in de library).
AUDIO_EXTS = {
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".mp4",
    ".wav", ".wma", ".aiff", ".aif", ".ape", ".mpc",
}

# Eén executescript met het volledige schema (idempotent — CREATE IF NOT EXISTS).
# FTS5 met external-content (content='tracks') indexeert title/artist/album
# zonder duplicatie; de drie triggers houden de FTS-index synchroon bij
# insert/delete/update van de tracks-tabel.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks(
    id       INTEGER PRIMARY KEY,
    path     TEXT UNIQUE NOT NULL,
    title    TEXT,
    artist   TEXT,
    album    TEXT,
    genre    TEXT,
    duration REAL DEFAULT 0,
    mtime    REAL DEFAULT 0,
    art_path TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
    title, artist, album,
    content='tracks', content_rowid='id',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS tracks_ai AFTER INSERT ON tracks BEGIN
  INSERT INTO tracks_fts(rowid, title, artist, album)
  VALUES (new.id, new.title, new.artist, new.album);
END;
CREATE TRIGGER IF NOT EXISTS tracks_ad AFTER DELETE ON tracks BEGIN
  INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
  VALUES ('delete', old.id, old.title, old.artist, old.album);
END;
CREATE TRIGGER IF NOT EXISTS tracks_au AFTER UPDATE ON tracks BEGIN
  INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
  VALUES ('delete', old.id, old.title, old.artist, old.album);
  INSERT INTO tracks_fts(rowid, title, artist, album)
  VALUES (new.id, new.title, new.artist, new.album);
END;
"""


@dataclass
class ScanResult:
    """Resultaat van een (incrementele) scan — verandert alleen bij de eerste
    keer of na toevoegingen/wijzigingen/verwijderingen."""

    added: int      # nieuwe bestanden geïndexeerd
    changed: int    # bestaande bestanden met gewijzigde mtime → opnieuw gelezen
    removed: int    # bestanden die niet meer op schijf staan → verwijderd
    total: int      # totaal aantal tracks in de library na de scan


def _read_tags(path: str) -> tuple[str, str, str, str, float] | None:
    """Lees ``(title, artist, album, genre, duration)`` uit een audiobestand via
    mutagen (``easy=True`` voor een eenvoudige tag-view).

    Returns:
        Tuple, of ``None`` als mutagen het bestand niet kan lezen (corrupt of
        geen audio). ``title`` kan hier nog leeg zijn; de scan valt terug op de
        bestandsnaam.
    """
    try:
        mf = File(path, easy=True)
    except Exception:
        return None
    if mf is None:
        return None
    tags = mf.tags or {}

    def getv(key: str) -> str:
        """Eerste waarde van een tag, gestript; leeg als afwezig."""
        v = tags.get(key)
        if not v:
            return ""
        return str(v[0]).strip() if isinstance(v, list) else str(v).strip()

    duration = float(getattr(mf.info, "length", 0.0) or 0.0)
    return getv("title"), getv("artist"), getv("album"), getv("genre"), duration


class Library:
    """Lokale muziek-library: scan + queries over één sqlite-db.

    De DB-verbinding leeft de hele sessie (één bestand). ``check_same_thread=False``
    omdat scan/queries vanuit worker-threads draaien; alle toegang wordt
    geserialiseerd via ``self._lock`` (reentrant — zie module-docstring).
    """

    def __init__(self, db_path: Path, music_dir: Path) -> None:
        """Open (of maak) de sqlite-db en zet het schema klaar.

        Args:
            db_path: pad naar de library.sqlite (meestal onder cache_dir).
            music_dir: de te indexeren muziekmap (wordt bewaard voor
                folder-browsing en als fallback in art-lookup).
        """
        self._db_path = db_path
        self._music_dir = music_dir
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False omdat scan/zoek vanuit worker-threads draaien;
        # alle toegang wordt geserialiseerd via self._lock.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()  # reentrant: rescan() roept count() aan o.b.v. dezelfde lock
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Migratie: bestaande DB's (van voor de Genre-tab) hebben geen
            # ``genre``-kolom. ALTER TABLE is idempotent als we 'm conditioneel
            # maken via PRAGMA. Eerste rescan vult 'm voor alle bestaande tracks.
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(tracks)").fetchall()}
            if "genre" not in cols:
                self._conn.execute("ALTER TABLE tracks ADD COLUMN genre TEXT")
            self._conn.commit()

    def close(self) -> None:
        """Sluit de DB-verbinding netjes (opgeroepen in ``cli._run_tui`` z'n
        finally, zodat er geen stale lock achterblijft)."""
        self._conn.close()

    # ---- tellen / scannen --------------------------------------------
    def count(self) -> int:
        """Aantal tracks in de library."""
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    def rescan(self) -> ScanResult:
        """Incrementeel herscannen (synchroon — roep vanuit een thread aan).

        Per bestand wordt de mtime vergeleken met de opgeslagen waarde; alleen
        nieuwe/gewijzigde bestanden worden opnieuw met mutagen gelezen.
        Bestanden die niet meer op schaal staan worden uit de DB verwijderd.
        Commit gebeurt per 500 wijzigingen (en aan het eind) om de transactie
        niet oneindig te laten groeien bij een grote eerste scan.
        """
        with self._lock:
            return self._rescan_locked()

    def _rescan_locked(self) -> ScanResult:
        """De daadwerkelijke scan-logica (aanname: ``self._lock`` reeds vast)."""
        added = changed = removed = 0
        since_commit = 0
        seen: set[str] = set()

        for root, _dirs, files in os.walk(self._music_dir):
            for fn in files:
                if os.path.splitext(fn)[1].lower() not in AUDIO_EXTS:
                    continue
                path = os.path.join(root, fn)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                seen.add(path)

                # Bekend én ongewijzigd (mtime binnen 1s)? Sla over.
                row = self._conn.execute(
                    "SELECT id, mtime FROM tracks WHERE path = ?", (path,)
                ).fetchone()
                if row and abs(row["mtime"] - mtime) < 1.0:
                    continue

                tags = _read_tags(path)
                if tags is None:
                    continue
                title, artist, album, genre, duration = tags
                if not title:
                    title = os.path.splitext(fn)[0]  # fallback: bestandsnaam zonder ext

                self._conn.execute(
                    """
                    INSERT INTO tracks(path, title, artist, album, genre, duration, mtime, art_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                      title=excluded.title, artist=excluded.artist, album=excluded.album,
                      genre=excluded.genre, duration=excluded.duration, mtime=excluded.mtime
                    """,
                    (path, title, artist, album, genre, duration, mtime, path),
                )
                if row:
                    changed += 1
                else:
                    added += 1
                since_commit += 1
                if since_commit >= 500:
                    self._conn.commit()
                    since_commit = 0

        # verwijderde bestanden opruimen
        for row in self._conn.execute("SELECT id, path FROM tracks"):
            if row["path"] not in seen:
                self._conn.execute("DELETE FROM tracks WHERE id = ?", (row["id"],))
                removed += 1

        self._conn.commit()
        total = self.count()
        return ScanResult(added=added, changed=changed, removed=removed, total=total)

    # ---- conversie + FTS-zoek ----------------------------------------
    def _row_to_track(self, row: sqlite3.Row) -> Track:
        """sqlite-rij → Track (source=local, engine=mpv, uri=bestandspad)."""
        return Track(
            source="local",
            engine="mpv",
            uri=row["path"],
            title=row["title"] or "(onbekend)",
            artist=row["artist"] or "",
            album=row["album"] or "",
            genre=row["genre"] or "",
            duration=float(row["duration"] or 0.0),
            art_url=row["art_path"] or "",
        )

    # ---- mutaties (edit-tags + delete) -------------------------------
    def read_tags(self, path: str) -> dict[str, str]:
        """Lees de huidige tags van een audiobestand voor de edit-modal.

        Retourneert ``{"title","artist","album","genre"}`` (lege string als
        afwezig). Wordt apart van ``_read_tags`` gehouden omdat de scan alleen
        (title,artist,album,duration) nodig heeft — genre zit niet in de DB
        en hoeft dus ook niet bij elke scan ingelezen.

        Raises:
            FileNotFoundError, mutagen.MutagenError: bubbelen op naar de UI.
        """
        mf = File(path, easy=True)
        if mf is None or mf.tags is None:
            return {"title": "", "artist": "", "album": "", "genre": ""}

        def getv(key: str) -> str:
            v = mf.tags.get(key) if mf.tags else None
            if not v:
                return ""
            return str(v[0]).strip() if isinstance(v, list) else str(v).strip()

        return {
            "title": getv("title"),
            "artist": getv("artist"),
            "album": getv("album"),
            "genre": getv("genre"),
        }

    def update_tags(self, path: str, *, title: str | None = None,
                    artist: str | None = None, album: str | None = None,
                    genre: str | None = None) -> None:
        """Schrijf ID3-tags terug naar het audiobestand + update de DB-rij.

        Per formaat de juiste frame-tag-klasse:
        * ``.mp3`` — ``mutagen.id3`` (TIT2/TPE1/TALB/TCON, encoding=3 = UTF-8).
        * ``.flac/.ogg/.opus`` — Vorbis-comment (list-of-strings).
        * ``.m4a/.mp4`` — MP4-tags (\\xa9nam/\\xa9ART/\\xa9alb/\\xa9gen).

        Velden die ``None`` zijn blijven ongemoeid (alleen opgegeven velden
        worden geschreven). Na het schrijven wordt de sqlite-rij geüpdate voor
        title/artist/album + mtime — zodat een volgende ``rescan`` de mutagen-
        wijziging niet overschrijft met de oude cache.

        Raises:
            FileNotFoundError, mutagen.MutagenError, PermissionError: bubbelen
            naar de UI (via de werk-thread).
        """
        with self._lock:
            ext = os.path.splitext(path)[1].lower()
            mf = File(path, easy=False)
            if mf is None:
                raise ValueError(f"Geen audiobestand (mutagen herkent 't niet): {path}")
            if mf.tags is None:
                # Sommige formaten (mp3) hebben geen ID3-header als er nog geen
                # tags waren — maak 'm aan zodat we kunnen schrijven.
                try:
                    mf.add_tags()
                except Exception:
                    pass  # formaten zonder add_tags() hebben al een lege tag-map

            # ---- genre apart (ID3/GENRE is een numeriek frame; bij mp3
            # ---- schrijven we 'm als tekst met encoding=3).
            if ext == ".mp3":
                from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TCON
                try:
                    tags = ID3(path)
                except ID3NoHeaderError:
                    tags = ID3()
                # Verwijder bestaande frames voor de keys die we gaan zetten,
                # anders stapelen we duplicates (add() i.p.v. set()).
                for frame_id, new_text in (("TIT2", title), ("TPE1", artist),
                                           ("TALB", album), ("TCON", genre)):
                    if new_text is None:
                        continue
                    tags.delall(frame_id)
                    tags.add(eval(frame_id)(encoding=3, text=[new_text]))
                tags.save(path)
            elif ext in (".flac", ".ogg", ".opus"):
                # Vorbis-comment: mf.tags is een VComment met list-of-strings.
                for key, new in (("title", title), ("artist", artist),
                                 ("album", album), ("genre", genre)):
                    if new is None:
                        continue
                    mf.tags[key] = [new]
                mf.save()
            elif ext in (".m4a", ".mp4"):
                from mutagen.mp4 import MP4
                mp4 = MP4(path)
                for key, new in (("\xa9nam", title), ("\xa9ART", artist),
                                 ("\xa9alb", album), ("\xa9gen", genre)):
                    if new is None:
                        continue
                    mp4.tags[key] = [new]
                mp4.save()
            else:
                # WAV/AIF/APE/MPC/WMA: schrijven is formaat-specifiek en
                # ondersteunen niet allemaal alle frames. Val terus op een
                # algemene mutagen save (alleen de niet-None velden).
                for key, new in (("title", title), ("artist", artist),
                                 ("album", album), ("genre", genre)):
                    if new is None:
                        continue
                    mf.tags[key] = [new] if isinstance(mf.tags.get(key), list) else new
                mf.save()

            # ---- DB bijwerken (alleen de velden die we hebben gezet) -------
            new_mtime = os.path.getmtime(path)
            sets, params = [], []
            if title is not None:
                sets.append("title=?"); params.append(title)
            if artist is not None:
                sets.append("artist=?"); params.append(artist)
            if album is not None:
                sets.append("album=?"); params.append(album)
            if sets:
                sets.append("mtime=?"); params.append(new_mtime)
                params.append(path)
                self._conn.execute(
                    f"UPDATE tracks SET {', '.join(sets)} WHERE path=?",
                    params,
                )
                self._conn.commit()

    def delete_track(self, path: str) -> None:
        """Wis het audiobestand van schijf en verwijder de DB-rij.

        Geen recursie — alleen het exacte ``path``. De UI checkt vóór het
        toont van de confirm-modal of het pad niet overeenkomt met de
        huidige track (``state.track.uri``); mpv houdt namelijk een fd vast,
        dus de file zou onzichtbaar op schijf blijven tot mpv 'm sluit.

        Raises:
            FileNotFoundError: als de file al weg is — we ruimen dan alsnog
                de DB-rij op (defensief).
            PermissionError: als de file niet te wissen is.
        """
        with self._lock:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass  # al weg — DB-rij alsnog opruimen
            self._conn.execute("DELETE FROM tracks WHERE path=?", (path,))
            self._conn.commit()

    def search(self, query: str, limit: int = 100) -> list[Track]:
        """Full-text-zoek (FTS5) over title/artist/album, relevantie-gesorteerd.
        Synchroon — roep vanuit een thread aan."""
        with self._lock:
            return self._search_locked(query, limit)

    def _search_locked(self, query: str, limit: int) -> list[Track]:
        """Zoek-logica (aanname: lock vast). Bouwt een veilige FTS5-MATCH-string
        via ``_build_fts_query``."""
        fts = _build_fts_query(query)
        if not fts:
            return []
        rows = self._conn.execute(
            """
            SELECT t.* FROM tracks_fts f
            JOIN tracks t ON t.id = f.rowid
            WHERE tracks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts, limit),
        ).fetchall()
        return [self._row_to_track(r) for r in rows]

    def recent(self, limit: int = 200) -> list[Track]:
        """De laatst-toegevoegde tracks (hoogste id eerst) — voedt de platte
        ``Nummers``-lijst in Library > Lokaal."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    # ---- doorbladeren per map / album / artiest ----------------------
    # Deze methoden voeden de Mappen/Albums/Artiesten-subtabs in Library > Lokaal.
    # Mappen worden afgeleid uit de paden t.o.v. music_dir (geen aparte map-tabel).
    def _path_components(self, path: str) -> list[str]:
        """Pad opgesplitst in componenten, relatief t.o.v. music_dir.

        b.v. ``~/Music/Artiest/Album/track.mp3`` → ``['Artiest','Album','track.mp3']``.
        """
        rel = os.path.relpath(path, str(self._music_dir))
        return rel.split(os.sep)

    def _all_rows(self) -> list[sqlite3.Row]:
        """Alle tracks-rijen (voor folder-afleiding in één sweep)."""
        return self._conn.execute(
            "SELECT * FROM tracks"
        ).fetchall()

    def folder_entries(self, rel: tuple[str, ...] = ()):
        """Directe kinderen van map ``rel`` (componenten onder music_dir; ``()`` = root).

        Returns:
            Tuple ``(subfolders, tracks)`` waarbij:
            * ``subfolders`` een alfabetische lijst is van ``(naam, #tracks
              recursief daaronder)`` — de kind-mappen;
            * ``tracks`` de bestanden zijn die *direct* in ``rel`` liggen (niet
              in een submap).

        Logica: per track wordt het mapdeel vergeleken met ``rel``. Mapdeel
        exact ``rel`` → direct bestand; mapdeel begint met ``rel`` + één extra
        component → die extra component is een kind-map.
        """
        rel = tuple(rel)
        n = len(rel)
        with self._lock:
            rows = self._all_rows()
        subdirs: dict[str, int] = {}
        here: list[Track] = []
        for r in rows:
            comps = self._path_components(r["path"])
            if comps[:n] != list(rel):
                continue  # niet (meer) onder deze map
            if len(comps) - 1 == n:
                here.append(self._row_to_track(r))           # bestand direct in rel
            elif len(comps) - 1 > n:
                name = comps[n]                               # naam van de kind-map
                subdirs[name] = subdirs.get(name, 0) + 1
        subs = sorted(subdirs.items(), key=lambda kv: kv[0].lower())
        return subs, here

    def folder_tracks(self, rel: tuple[str, ...] = ()) -> list[Track]:
        """Alle tracks recursief onder map ``rel`` (geordend op pad). Gebruikt
        voor "speel hele map" (``A`` in de Mappen-tab)."""
        rel = tuple(rel)
        n = len(rel)
        with self._lock:
            rows = self._all_rows()
        out = [self._row_to_track(r) for r in rows
               if self._path_components(r["path"])[:n] == list(rel)]
        out.sort(key=lambda t: t.uri)
        return out

    def albums(self) -> list[dict]:
        """Albums met een niet-lege tag; gegroepeerd op album-naam.

        Returns:
            Lijst van ``{"album", "artist", "count"}`` (artist = representatief,
            alphabetically eerste binnen het album), gesorteerd op album. Tracks
            met lege album-tag vallen weg (die staan vaak in ``_no_album``-mappen
            en verschijnen dus niet in de Albums-tab, wel in Mappen/Nummers).
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT album, MIN(artist) AS artist, COUNT(*) AS c
                   FROM tracks
                   WHERE COALESCE(album, '') != ''
                   GROUP BY album
                   ORDER BY album"""
            ).fetchall()
        return [{"album": r["album"], "artist": r["artist"] or "",
                 "count": r["c"]} for r in rows]

    def album_tracks(self, album: str) -> list[Track]:
        """Alle tracks van één album (geordend op pad)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE album = ? ORDER BY path", (album,)
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    def artists(self) -> list[dict]:
        """Artiesten met een niet-lege tag; gegroepeerd op artiest.

        Returns:
            Lijst van ``{"artist", "count"}``, gesorteerd op artiest.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT artist, COUNT(*) AS c
                   FROM tracks
                   WHERE COALESCE(artist, '') != ''
                   GROUP BY artist
                   ORDER BY artist"""
            ).fetchall()
        return [{"artist": r["artist"], "count": r["c"]} for r in rows]

    def artist_tracks(self, artist: str) -> list[Track]:
        """Alle tracks van één artiest (geordend op album, dan pad)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE artist = ? ORDER BY album, path", (artist,)
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    # ---- genre-blader -------------------------------------------------
    # Voeden de Genre-tab in Library > Lokaal. Tracks zonder genre-tag
    # verschijnen NIET in de genre-lijsten (zelfde patroon als albums zonder
    # album-tag in de Albums-tab); ze blijven wel zichtbaar in Nummers/Mappen.
    def genres(self) -> list[dict]:
        """Genres met ≥1 track; gegroepeerd op genre-naam, alfabetisch.

        Returns:
            Lijst van ``{"genre": str, "count": int}``.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT genre, COUNT(*) AS c
                   FROM tracks
                   WHERE COALESCE(genre, '') != ''
                   GROUP BY genre
                   ORDER BY genre"""
            ).fetchall()
        return [{"genre": r["genre"], "count": r["c"]} for r in rows]

    def genre_tracks(self, genre: str) -> list[Track]:
        """Alle tracks van één genre (geordend op artiest, album, pad).
        Identiek filter als ``album_tracks``/``artist_tracks``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE genre = ? ORDER BY artist, album, path",
                (genre,),
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    def genre_artists(self, genre: str) -> list[dict]:
        """Artiesten met ≥1 track in dit genre, met album- + track-count.

        Returns:
            Lijst van ``{"artist": str, "album_count": int, "track_count": int}``,
            gesorteerd op artiest. ``album_count`` = unieke albums van deze
            artiest binnen dit genre (kan albums zonder tag bevatten als ze
            tracks in dit genre hebben).
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT artist,
                          COUNT(DISTINCT album) AS ac,
                          COUNT(*) AS tc
                   FROM tracks
                   WHERE genre = ? AND COALESCE(artist, '') != ''
                   GROUP BY artist
                   ORDER BY artist""",
                (genre,),
            ).fetchall()
        return [{"artist": r["artist"],
                 "album_count": r["ac"],
                 "track_count": r["tc"]} for r in rows]

    def genre_albums(self, genre: str) -> list[dict]:
        """Albums met ≥1 track in dit genre.

        Returns:
            Lijst van ``{"album": str, "artist": str, "count": int}`` (artist
            = eerste niet-lege artiest binnen dat album+genre-combinatie).
            Tracks met lege album-tag vallen weg.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT album, MIN(artist) AS artist, COUNT(*) AS c
                   FROM tracks
                   WHERE genre = ? AND COALESCE(album, '') != ''
                   GROUP BY album
                   ORDER BY album""",
                (genre,),
            ).fetchall()
        return [{"album": r["album"], "artist": r["artist"] or "",
                 "count": r["c"]} for r in rows]

    def genre_artist_tracks(self, genre: str, artist: str) -> list[Track]:
        """Tracks van één artiest binnen één genre. Drill van de Artiesten-
        sub-tab terug naar de Tracks-sub-tab; 't verschilt van
        ``artist_tracks`` doordat 't ook filtert op genre."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM tracks
                   WHERE genre = ? AND artist = ?
                   ORDER BY album, path""",
                (genre, artist),
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    def genre_album_tracks(self, genre: str, album: str) -> list[Track]:
        """Tracks van één album binnen één genre. Drill van de Albums-sub-tab
        terug naar de Tracks-sub-tab."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM tracks
                   WHERE genre = ? AND album = ?
                   ORDER BY path""",
                (genre, album),
            ).fetchall()
            return [self._row_to_track(r) for r in rows]


def _build_fts_query(query: str) -> str:
    """Maak een veilige FTS5-MATCH-string: unicode-tokens met prefix-joker
    (``*``), ruim-gescheiden (impliciete AND). Filtert speciale tekens die MATCH
    anders breken (haakjes, dubbele quotes, operators).

    ``Beat wonder`` → ``"beat* wonder*"``. Lege input → ``""`` (geen match).
    """
    import unicodedata

    tokens: list[str] = []
    for raw in query.split():
        cleaned = "".join(
            ch for ch in unicodedata.normalize("NFKC", raw)
            if ch.isalnum() or ch.isspace()
        ).strip()
        if cleaned:
            tokens.append(f"{cleaned}*")
    return " ".join(tokens)
