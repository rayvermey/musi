"""Albumhoezen — ophalen, decoden, in cache schrijven.

Bron → lokaal bestand:
* **lokale track**: probeer embedded APIC (mutagen) → anders sibling
  ``cover.*``/``folder.*`` in de map → anders ``None``;
* **YouTube**: download de thumbnail-URL (``yt_<hash>.jpg``);
* **Spotify**: download de albumhoes-URL (``spotify_<hash>.jpg``).

Resultaat: altijd een pad naar een leesbaar afbeeldingsbestand, of ``None`` als
de bron ontbreekt of de download faalt. De render-laag (``NowPlaying`` in
``app/musi_app.py``) doet de rest: textual-image kiest sixel/kitty/halfblock
afhankelijk van de terminal-capability.

Caching: externe downloads worden onder ``cache_dir`` op een stabiele naam
(sha256-hash van de URL) weggeschreven en hergebruikt — één download per URL.
Lokale embedded art wordt telkens uitgepakt naar een bestandsnaam op basis van
het bronpad (geen nette cache; dat is OK omdat ``NowPlaying`` 'm per track maar
één keer per track-wissel opvraagt).
"""
from __future__ import annotations

import hashlib
import logging
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

from mutagen import File as MutagenFile

log = logging.getLogger(__name__)

# Lazy: optionele PIL/Image-decode is niet per se nodig om bytes op te halen,
# alleen voor resize/compressie — niet aanwezig is OK want de renderer leest
# de bytes zelf.


def _safe_name(url: str) -> str:
    """Stabiele, bestandssysteem-veilige cache-naam voor een URL (sha256, 16
    tekens). Zorgt dat dezelfde URL altijd dezelfde cache-file oplevert."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _download(url: str, dest: Path) -> bool:
    """Download ``url`` → ``dest``. ``True`` bij succes, ``False`` bij een
    netwerk-/OS-fout (gelogd op debug). Korte timeout (8s) zodat een trage
    thumb-server de UI niet ophoudt."""
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={"User-Agent": "musi/0.1"})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            data = resp.read()
        dest.write_bytes(data)
        return True
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.debug("download mislukte %s: %s", url, e)
        return False


def fetch(track, cache_dir: Path) -> Path | None:
    """Geef een pad terug naar een afbeeldingsbestand voor ``track``, of
    ``None`` bij falen/afwezige bron.

    Bron-keuze:
      * lokaal: probeer embedded art (APIC/covr/picture via mutagen), anders een
        sibling ``cover.*``/``folder.*`` in de map;
      * youtube/spotify: download ``track.art_url`` (met cache op URL-hash).

    Args:
        track: de Track waarvoor een hoes gezocht wordt.
        cache_dir: cache-map (wordt aangemaakt als 'm nog niet bestond).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Lokale track: bekijk het audiobestand zelf of de map
    if track.source == "local" and track.uri:
        path = Path(track.uri)
        # embedded art via mutagen
        try:
            mf = MutagenFile(str(path))
            for key in ("APIC:", "APIC:Cover", "covr", "picture"):
                pics = (mf.tags.get(key) if mf and mf.tags else None)
                if not pics:
                    continue
                pic = pics[0]
                data = getattr(pic, "data", None)
                if not data:
                    continue
                ext = (getattr(pic, "mime", "") or "image/jpeg").split("/")[-1]
                out = cache_dir / f"local_{path.stem}_{track.uri.__hash__()}.{ext}"
                out.write_bytes(data)
                return out
        except Exception as e:
            log.debug("mutagen-art mislukte voor %s: %s", path, e)
        # sibling cover-bestanden
        if path.parent.exists():
            for name in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png"):
                cand = path.parent / name
                if cand.exists():
                    return cand
        return None

    # Externe bronnen: download (met cache op URL-hash)
    if track.source in ("youtube", "spotify") and track.art_url:
        url = track.art_url
        ext = "jpg"
        if ".png" in url.lower():
            ext = "png"
        out = cache_dir / f"{track.source}_{_safe_name(url)}.{ext}"
        if out.exists() and out.stat().st_size > 0:
            return out  # cache-hit
        if _download(url, out):
            return out
    return None
