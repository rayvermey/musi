"""Auto-rip — zet een spelende YouTube-track om in een mp3 in de lokale library.

Waarom deze module bestaat: ``musi`` streamt YouTube-audio via mpv
(``--ytdl-format=bestaudio/best``) maar bewaart niets op schijf. Ray wil dat elke
YouTube-track die hij écht afspeelt automatisch als mp3 wordt opgeslagen in
``~/Music``, georganiseerd als ``<Artiest>/<Album>/<Titel>.mp3`` — zodat de lokale
library steeds groeit met wat hij beluistert.

Deze module is **UI-vrij** (geen Textual) en dus los testbaar. De UI-laag
(``app/musi_app.py``) bepaalt *wanneer* 'm rip't (bij een nieuwe YouTube-track na een
korte grace-periode, met sessie-dedup) en roept hier ``Ripper.rip(track)`` aan.

Pipeline (één ``rip()``-aanroep):
  1. **dedup** — ``cache_dir/rips.json`` (video_id → pad). Bestaat de entry én het
     bestand → ``status="exists"`` (geen re-download, ook niet over sessies heen).
  2. **extract** — ``yt-dlp -x --audio-format mp3`` met ``--embed-metadata`` (titel,
     uploader=artiest, …) + ``--embed-thumbnail`` (albumhoes). Output gaat rechtstreeks
     naar ``music_dir/<Titel> [<id>].mp3`` — dezelfde template als ``~/bin/yt``, geen
     staging-map. ``--cookies-from-browser`` mee als 'm gezet is (members-only / age-gated).
     Max ``MAX_CONCURRENT`` gelijktijdige downloads (semaphore) zodat snel-spelen door
     een queue de bandbreedte niet overstroomt.
  3. **organize** — inline (mutagen, al een dep): lees embedded tags, bereken
     ``music_dir/<slug(artist)|_unknown_artist>/<slug(album)|_no_album>/<slug(title)>.mp3``,
     ``mkdir`` + ``shutil.move``. Conventie identiek aan ``muziek-organiseer`` zodat de
     library uniform blijft. We roepen dat script zelf niet aan omdat 't óók z'n
     bestemming binnen ``--src`` kiest en daarmee geen specifiek-bestand-only flow heeft.
  4. **registreren** — pad in ``rips.json`` opslaan (volgende keer → ``exists``).

Faalt 't (geen video-id, time-out, yt-dlp-exit≠0)? Dan ``status="failed"`` + een
reden; we raisen niet (de UI toont 'm als notify). Bij een rip-fout na een gedeeltelijk
geschreven mp3 verwijderen we 't zelf — anders blijft 't als orphan in ``~/Music`` staan.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from mutagen import File

if TYPE_CHECKING:
    from .models import Track

log = logging.getLogger(__name__)

# Max aantal gelijktijdige yt-dlp-extracties. Snel-spelen door een queue (waarbij
# meerdere tracks de grace-periode overleven) zou anders tientallen downloads
# tegelijk starten. 2 is een zachte balans.
MAX_CONCURRENT = 2

# yt-dlp time-out (seconden). 1 uur dekt ruim de langste mixes/streams die je als
# track afspeelt. Bij overschrijding kapt yt-dlp af en laten we de half-schrijf
# opruimen.
RIP_TIMEOUT = 3600.0

# Fallback-mappen (identiek aan muziek-organiseer) voor tracks zonder artiest/album-tag.
NO_ARTIST = "_unknown_artist"
NO_ALBUM = "_no_album"

# Max lengte van één slug-component (spiegelt muziek-organiseer SLUG_MAX ≈ 120).
SLUG_MAX = 120

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS = re.compile(r"\s+")


def _slugify(name: str) -> str:
    """Bestandsnaam-veilige slug: vervang illegale FS-tekens door ``_``, vouw
    whitespace, strip leading/trailing punten/spaties, kap op ``SLUG_MAX``.
    Lege input → ``""`` (caller kiest de fallback-map). Spiegelt
    ``muziek-organiseer.slugify`` zodat bestandsnamen tussen beide consistent zijn."""
    if not name:
        return ""
    name = _ILLEGAL.sub("_", name)
    name = _WS.sub(" ", name).strip()
    name = name.rstrip(". ")
    if not name:
        return ""
    return name[:SLUG_MAX]


def _unique(path: Path) -> Path:
    """Als ``path`` al bestaat: geef ``<naam> (2).<ext>``, ``(3)``, … tot 't vrij is.
    Voorkomt overschrijven bij naamconflicten (bv. twee video's met dezelfde titel)."""
    if not path.exists():
        return path
    stem, ext = path.stem, path.suffix
    i = 2
    while True:
        cand = path.parent / f"{stem} ({i}){ext}"
        if not cand.exists():
            return cand
        i += 1


def video_id(track: "Track") -> str | None:
    """YouTube video-id van een track — eerst uit ``track.extra["id"]`` (altijd gevuld
    door de YouTube-zoekprovider), anders uit de watch-URL (``?v=`` of ``youtu.be/<id>``).
    ``None`` als 'm niet te herleiden valt (geen YouTube, of afwijkende URL)."""
    vid = (track.extra or {}).get("id")
    if vid:
        return str(vid)
    try:
        parsed = urlparse(track.uri)
    except Exception:
        return None
    v = parse_qs(parsed.query).get("v")
    if v:
        return v[0]
    if parsed.netloc.endswith("youtu.be") and parsed.path.strip("/"):
        return parsed.path.strip("/")
    return None


@dataclass
class RipResult:
    """Uitkomst van één ``rip()``-aanroep.

    Attributes:
        path: het uiteindelijke pad onder ``music_dir`` (alleen bij ``done``/``exists``).
        status: ``"done"`` (geript+georganiseerd), ``"exists"`` (al aanwezig, geskipped)
            of ``"failed"`` (iets misgegaan).
        reason: korte omschrijving (bij ``failed``) ofwel leeg.
    """

    path: Path | None
    status: str   # "done" | "exists" | "failed"
    reason: str = ""


class Ripper:
    """Houdt yt-dlp + organize + dedup bij voor het auto-rip'en van YouTube-tracks.

    Eén instantie per app-sessie (gebouwd in ``MusiApp.__init__``). De UI roept
    ``rip(track)`` aan per track; dedup en de semaphore leven hier.
    """

    def __init__(self, music_dir: Path, cache_dir: Path,
                 cookies_from_browser: str = "", yt_bin: str = "yt-dlp") -> None:
        """Args:
            music_dir: de lokale muziekmap (``cfg.music_dir``); bestemming van de mp3
                én van de daaropvolgende organize-move.
            cache_dir: cache-root (``cfg.cache_dir``); hier woont alleen ``rips.json``.
            cookies_from_browser: browser-naam (``"vivaldi"``/``"firefox"``/…) of leeg
                voor publiek. Wordt 1:1 doorgegeven aan yt-dlp ``--cookies-from-browser``.
            yt_bin: yt-dlp-binary (standaard op PATH).
        """
        self._music_dir = Path(music_dir)
        self._cache_dir = Path(cache_dir)
        self._rips_json = self._cache_dir / "rips.json"
        self._cookies = cookies_from_browser or ""
        self._bin = yt_bin
        self._lock = asyncio.Lock()  # serialize rips.json-schrijven (zie _save_cache_entry)

    # ---- rips.json dedup-cache --------------------------------------
    def _load_cache(self) -> dict:
        """Lees ``rips.json`` als dict (video_id → ``{"path","title"}``).
        Beschadigd/afwezig → lege dict (nooit fataal; dedup start dan blanco)."""
        try:
            if self._rips_json.exists():
                return json.loads(self._rips_json.read_text("utf-8"))
        except Exception as e:
            log.warning("rips.json lezen mislukt (%s) — start met lege cache", e)
        return {}

    async def _save_cache_entry(self, vid: str, path: Path, title: str) -> None:
        """Voeg/vervang één entry in ``rips.json`` (lees-modify-schrijf onder een lock
        zodat twee gelijktijdige rips elkaar niet overschrijven)."""
        async with self._lock:
            data: dict = {}
            try:
                if self._rips_json.exists():
                    data = json.loads(self._rips_json.read_text("utf-8"))
            except Exception:
                data = {}
            data[vid] = {"path": str(path), "title": title}
            self._rips_json.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), "utf-8"
            )

    # ---- publieke API -----------------------------------------------
    async def rip(self, track: "Track") -> RipResult:
        """Rip één YouTube-track naar mp3 in ``music_dir`` en registreer 'm.

        Returns altijd een ``RipResult`` (raised niet) — de UI beslist wat 'm met de
        status doet (notify/stil/erro)."""
        vid = video_id(track)
        if not vid:
            return RipResult(None, "failed", "geen video-id gevonden")

        # 1) dedup — al geript (vorige sessie) én nog op schijf?
        entry = self._load_cache().get(vid)
        if entry:
            p = Path(entry.get("path", ""))
            if p.exists():
                return RipResult(p, "exists", "al in ~/Music")

        # 2) extract: yt-dlp schrijft direct in music_dir met dezelfde template
        #    als ``~/bin/yt``: ``<Titel> [<id>].mp3``. ``--no-overwrites`` zorgt dat
        #    een re-rip (dedup-cache mist het bestand door externe verplaatsing) niet
        #    zonder waarschuwing overschrijft.
        self._music_dir.mkdir(parents=True, exist_ok=True)
        url = track.uri
        template = str(self._music_dir / "%(title).200B [%(id)s].%(ext)s")
        args = [
            self._bin,
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "--no-playlist", "--no-overwrites", "--no-warnings", "--no-progress",
            "--embed-metadata", "--embed-thumbnail",
            "-o", template,
            url,
        ]
        if self._cookies:
            args += ["--cookies-from-browser", self._cookies]
        args += ["--js-runtimes", "node:/usr/bin/node", "--remote-components", "ejs:npm"]

        log.info("rip start: %s — %s", vid, url)
        async with self._staging_sem():
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=RIP_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                # Wacht kort zodat 't zombie-proces opruimt — voorkomt dat we de
                # half-geschreven mp3 later niet kunnen lezen/schrappen.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                # Probeer een eventueel half-bestand op te ruimen op zijn vermoedelijke
                # bestemming. yt-dlp gebruikt ``<file>.mp3.part`` tijdens schrijven; die
                # verwijderen we ook.
                self._cleanup_partial_download(vid)
                log.warning("rip time-out na %ds voor %s", int(RIP_TIMEOUT), vid)
                return RipResult(None, "failed", f"time-out ({int(RIP_TIMEOUT)}s)")
            if proc.returncode != 0:
                err = stderr.decode(errors="replace")[-400:] if stderr else ""
                reason = (err.strip().splitlines()[-1]
                          if err.strip() else f"yt-dlp exit {proc.returncode}")
                # Bij een failed rip kan er tóch een (deel-)bestand op schijf staan
                # als gevolg van een eerdere poging van dezelfde vid; ruim op om
                # orphan-bestanden te voorkomen.
                self._cleanup_partial_download(vid)
                log.warning("rip faalde (exit %s) voor %s: %s", proc.returncode, vid, reason)
                return RipResult(None, "failed", reason)

        # 3) organiseer het zojuist geripte bestand
        #    Het pad is deterministisch via de template; we zoeken niet (geen
        #    glob-bracket-trap). Als het bestand er niet is, is er iets misgegaan
        #    zonder non-zero exit (zou niet moeten).
        new_file = self._music_dir / f"_ytdlp_unknown.mp3"  # placeholder, overschreven hieronder
        # Geen eenvoudige reconstructie van de bestandsnaam — yt-dlp bepaalt 'm uit
        # `%(title)s`. Daarom: pak het enige mp3-bestand in music_dir dat eindigt op
        # ``[<vid>].mp3`` (na een geslaagde rip is dat precies één file).
        suffix = f"[{vid}].mp3"
        candidates = [p for p in self._music_dir.iterdir()
                      if p.is_file() and p.name.endswith(suffix)]
        if not candidates:
            log.warning("rip slaagde zonder aantoonbare mp3 met suffix %s in %s",
                        suffix, self._music_dir)
            return RipResult(None, "failed", "geen mp3 aangetroffen na yt-dlp")
        new_file = max(candidates, key=lambda p: p.stat().st_mtime)
        try:
            final = await asyncio.to_thread(self._organize, new_file)
        except Exception as e:
            log.warning("organize faalde voor %s: %s", new_file, e)
            return RipResult(None, "failed", f"organize: {e}")

        # 4) registreer
        await self._save_cache_entry(vid, final, track.title)
        log.info("rip klaar: %s → %s", vid, final)
        return RipResult(final, "done", "")

    def _staging_sem(self) -> asyncio.Semaphore:
        """Semaphore die de gelijktijdige yt-dlp-downloads begrenst. Lazy gemaakt: de
        ``Ripper`` wordt in ``MusiApp.__init__`` geconstrueerd (vóór de event-loop draait),
        en een asyncio.Semaphore bindt pas bij eerste gebruik aan de loop — dus maken we
        'm op first-call zodat 'm zeker aan de app-loop hangt."""
        sem = getattr(self, "_sem", None)
        if sem is None:
            sem = asyncio.Semaphore(MAX_CONCURRENT)
            self._sem = sem  # type: ignore[attr-defined]
        return sem

    def _cleanup_partial_download(self, vid: str) -> None:
        """Verwijder eventuele half-geschreven bestanden van een gefaalde rip.
        yt-dlp schrijft naar ``...mp3.part`` tijdens downloaden — die mag blijven
        liggen als de volgende rip opnieuw begint (yt-dlp overschrijft 'm), maar
        als het om een time-out gaat ruimt 't prettiger op. Een volledig bestand
        van een eerdere sessie laten we met rust — dat regelt ``rips.json``-dedup."""
        if not self._music_dir.exists():
            return
        suffix_part = f"[{vid}].mp3.part"
        try:
            for p in self._music_dir.iterdir():
                if p.is_file() and p.name.endswith(suffix_part):
                    try:
                        p.unlink()
                    except OSError:
                        continue
        except OSError:
            pass

    # ---- organize ---------------------------------------------------
    def _organize(self, new_file: Path) -> Path:
        """Lees embedded tags, bereken de bestemming onder ``music_dir`` en verplaats
        het net-ripte bestand daarheen. Conventie identiek aan ``muziek-organiseer``:
        ``<artist>/<album>/<title>.mp3`` met ``_unknown_artist``/``_no_album`` fallback
        en `` (2)``-suffix bij naamconflicten.

        Wordt in een worker-thread gedraaid (``asyncio.to_thread`` in ``rip``) omdat 't
        mutagen + ``shutil.move`` doet.
        """
        title = artist = album = ""
        try:
            mf = File(str(new_file), easy=True)
            if mf is not None and mf.tags:
                def g(key: str) -> str:
                    v = mf.tags.get(key) if mf.tags else None
                    if not v:
                        return ""
                    return str(v[0]).strip() if isinstance(v, list) else str(v).strip()
                title = g("title")
                artist = g("artist")
                album = g("album")
        except Exception as e:
            log.debug("tags lezen van %s faalde (%s) — valt terug op bestandsnaam",
                      new_file.name, e)

        if not title:
            title = new_file.stem
        artist_s = _slugify(artist) or NO_ARTIST
        album_s = _slugify(album) or NO_ALBUM
        title_s = _slugify(title) or "onbekend"

        dest_dir = self._music_dir / artist_s / album_s
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique(dest_dir / f"{title_s}.mp3")
        shutil.move(str(new_file), str(dest))
        return dest