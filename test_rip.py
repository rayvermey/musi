"""Tests voor het auto-rip-subsysteem (``musi/rip.py``).

Deze tests dekken de *pure* logica — ``_slugify``, ``_unique``, ``video_id``, de
inline ``_organize`` (met een gefake-de mutagen-laatlaag), en de ``rips.json``-dedup.
De echte yt-dlp-extractie wordt hier niet aangeroepen (geen netwerk/cookies in CI).
"""
from __future__ import annotations

import asyncio
import json

import musi.rip as rip_mod
from musi.models import Track
from musi.rip import Ripper, _slugify, _unique, video_id


# ---- pure helpers --------------------------------------------------
def test_slugify_strips_illegal_collapses_truncates():
    assert _slugify("") == ""
    assert _slugify("a/b:c?d|e") == "a_b_c_d_e"
    assert _slugify("  hi   there  ") == "hi there"
    assert _slugify("name...  ") == "name"
    assert len(_slugify("x" * 300)) == 120
    # unicode blijft staan (Bløf blijft Bløf)
    assert _slugify("Bløf") == "Bløf"


def test_unique_appends_suffix_on_collision(tmp_path):
    p = tmp_path / "f.mp3"
    assert _unique(p) == p               # nog vrij
    p.write_text("x")
    p2 = _unique(p)
    assert p2 == tmp_path / "f (2).mp3"
    p2.write_text("x")
    assert _unique(p) == tmp_path / "f (3).mp3"


# ---- video_id ------------------------------------------------------
def test_video_id_from_extra_url_and_youtube():
    assert video_id(Track("youtube", "mpv",
                          "https://www.youtube.com/watch?v=ABcd1234", "t",
                          extra={"id": "ABcd1234"})) == "ABcd1234"
    # geen extra → uit de watch-URL
    assert video_id(Track("youtube", "mpv",
                          "https://www.youtube.com/watch?v=ZZ123&t=10", "t")) == "ZZ123"
    # youtu.be short-URL
    assert video_id(Track("youtube", "mpv",
                          "https://youtu.be/shortID9", "t")) == "shortID9"
    # niet-YouTube → None
    assert video_id(Track("local", "mpv", "/home/ray/Music/x.mp3", "x")) is None


# ---- _organize (inline) -------------------------------------------
class _FakeTags(dict):
    """Dict die zich gedraagt als een mutagen easy-tag-map (list-waarden)."""


class _FakeMf:
    def __init__(self, tags):
        self.tags = tags


def _patch_mutagen(monkeypatch, tags):
    """Vervang ``mutagen.File`` in rip.py door een fake die ``tags`` teruggeeft."""
    monkeypatch.setattr(rip_mod, "File", lambda _p, easy=True: _FakeMf(tags))


def test_organize_moves_into_artist_album_using_tags(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    # yt-dlp schrijft direct in music_dir — geen staging meer
    src = music / "Whatever [vid1].mp3"
    src.write_bytes(b"x")

    tags = _FakeTags({"title": ["Café del Mar"],
                      "artist": ["Energy 52"],
                      "album": ["Greatest Hits"]})
    _patch_mutagen(monkeypatch, tags)

    ripper = Ripper(music, cache)
    dest = ripper._organize(src)

    assert dest == music / "Energy 52" / "Greatest Hits" / "Café del Mar.mp3"
    assert dest.exists()        # bestand verplaatst
    assert not src.exists()     # weg uit music_dir-root


def test_organize_falls_back_to_unknown_dirs_when_tagless(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    src = music / "Mooie Titel [vid2].mp3"
    src.write_bytes(b"x")

    _patch_mutagen(monkeypatch, _FakeTags())  # geen tags

    ripper = Ripper(music, cache)
    dest = ripper._organize(src)

    # artiest/album onbekend → _unknown_artist/_no_album; titel uit bestandsnaam
    assert dest.parent == music / "_unknown_artist" / "_no_album"
    assert dest.name == "Mooie Titel [vid2].mp3"
    assert dest.exists()


def test_organize_avoids_overwrite_on_name_clash(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    # vanaf nu rip'd yt-dlp direct in music_dir — geen staging meer
    src = music / "dup [vid3].mp3"
    src.write_bytes(b"x")
    # bestemming bestaat al (eerder geripte track met zelfde titel)
    clash_dir = music / "Art" / "_no_album"
    clash_dir.mkdir(parents=True)
    (clash_dir / "Song.mp3").write_bytes(b"oud")

    tags = _FakeTags({"title": ["Song"], "artist": ["Art"]})
    _patch_mutagen(monkeypatch, tags)

    ripper = Ripper(music, cache)
    dest = ripper._organize(src)

    assert dest == clash_dir / "Song (2).mp3"
    assert dest.exists()
    assert (clash_dir / "Song.mp3").read_bytes() == b"oud"  # origineel onaangetast


def test_cleanup_partial_download_removes_part_files(tmp_path):
    """Na een time-out of yt-dlp-exit≠0 ruimt ``_cleanup_partial_download`` eventuele
    ``[vid].mp3.part``-bestanden op. Volledige mp3's van een eerdere sessie blijven
    staan — daarvoor is ``rips.json``-dedup."""
    music = tmp_path / "Music"
    music.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    # half-bestand dat yt-dlp zou nalaten bij time-out
    (music / "Café del Mar [abc123].mp3.part").write_bytes(b"x")
    # volledige mp3 van een eerder geslaagde rip — moet NIET verwijderd worden
    full = music / "Vorig Nummer [xyz789].mp3"
    full.write_bytes(b"x")

    ripper = Ripper(music, cache)
    ripper._cleanup_partial_download("abc123")

    assert not (music / "Café del Mar [abc123].mp3.part").exists()
    assert full.exists()  # niet aangeraakt


# ---- rips.json dedup ----------------------------------------------
def test_rip_returns_exists_when_already_cached(tmp_path):
    """Pre-populeer rips.json + het bestand → rip() moet direct 'exists' teruggeven
    zónder yt-dlp aan te roepen (de dedup-pas gaat vóór de extractie)."""
    music = tmp_path / "Music"
    music.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    existing = music / "Art" / "_no_album" / "Song.mp3"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"x")
    (cache / "rips.json").write_text(json.dumps(
        {"vidX": {"path": str(existing), "title": "Song"}}))

    ripper = Ripper(music, cache)
    track = Track("youtube", "mpv",
                  "https://www.youtube.com/watch?v=vidX", "Song",
                  extra={"id": "vidX"})

    res = asyncio.run(ripper.rip(track))
    assert res.status == "exists"
    assert res.path == existing
