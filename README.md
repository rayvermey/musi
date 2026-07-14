# musi

Alles-in-één muziek-TUI voor de terminal: **lokaal + YouTube + Spotify** vanuit één
uniforme queue. Gebouwd voor Niri/Wayland + foot (sixel-albumhoezen).

Architectuur in één zin: één *orchestrator* met één queue kiest per track tussen
twee engines — **mpv** (JSON-IPC) voor lokaal/YouTube, **spotifyd** (MPRIS/dbus)
voor Spotify. De app blijft werken als één van de engines ontbreekt.

## Vereisten (al aanwezig op deze machine)

mpv, yt-dlp, Python ≥ 3.12, PipeWire+pipewire-pulse, foot (voor sixel-hoezen),
mutagen/rich/PyGObject/dbus-python, playerctl, yay. De Python-deps
(textual, textual-image, spotipy, dasbus) staan in `.venv/` (al geïnstalleerd).

## Status

Volledig gebouwd in 3 fases:

| Fase | Onderdeel | Status |
|---|---|---|
| 0 | Fundering, venv, config, launcher | ✅ |
| 1a | Engine-ABC + mpv-engine (JSON-IPC) | ✅ |
| 1b | Lokale library (sqlite FTS5 + mutagen) | ✅ |
| 1c | YouTube-zoek (yt-dlp) | ✅ |
| 1d | Queue + orchestrator + engine-handoff | ✅ |
| 1e | Textual-UI: Search/Queue/Library-tabs + now-playing | ✅ |
| 2a | spotipy PKCE-OAuth + zoek + library-methoden | ✅ |
| 2b | spotifyd-engine (MPRIS via dasbus) | ✅ |
| 2c | Orchestrator-handoff + Spotify library-view | ✅ |
| 3  | Albumhoezen (sixel/halfblock), MPRIS-placeholder, polish | ✅ |

Live Spotify speelt zodra je de drie stappen hieronder doorloopt.

## Setup (één keer, ~5 min) — voor Spotify

1. **Installeer spotifyd** + (optioneel) **mpv-mpris**:
   ```sh
   yay -S spotifyd mpv-mpris
   ```
   `mpv-mpris` zorgt dat je statusbalk de mpv van `musi` ziet wanneer je lokaal
   of YouTube speelt. spotifyd heeft MPRIS al ingebouwd.

2. **Maak een Spotify dev-app** — https://developer.spotify.com/dashboard →
   *Create app*. Vul in:
   - **App name**: vrij
   - **Redirect URI**: `http://127.0.0.1:8888/callback`
   - **APIs used**: kies "Web API"

3. **Vul je client_id in** — open `~/.config/musi/config.toml`:
   ```toml
   [spotify]
   client_id = "JOUW_CLIENT_ID_HIER"
   redirect_port = 8888
   ```
   Geen client_secret nodig — we gebruiken PKCE-OAuth.

4. **Configureer spotifyd** — `~/.config/spotifyd/spotifyd.conf`:
   ```toml
   [global]
   backend = pulseaudio
   device_name = musi-spotifyd
   bitrate = 320
   volume_controller = softvol
   use_mpris = true
   ```
   Login: bij eerste start opent spotifyd een browser voor OAuth (cache in
   `~/.cache/spotifyd/`). Of vul `username`/`password` in dezelfde sectie in.

5. **Start spotifyd als user-service:**
   ```sh
   systemctl --user enable --now spotifyd
   ```

6. **Eerste Spotify-login in musi** — bij de eerste Spotify-zoekactie toont
   spotipy een URL die je in een browser plakt, inlogt, en de redirect-URL
   terug in musi plakt. Daarna cached de token in `~/.cache/musi/spotify-token.json`.

## Gebruik

```sh
musi                # TUI starten
musi doctor         # config + setup-status tonen
musi --log INFO     # met verbose logging (anders WARNING)
```

## Toetsen (TUI)

| Toets | Actie |
|---|---|
| `/` | Focus op zoekveld |
| `1` / `2` / `3` | Tab Search / Queue / Library |
| `enter` | Speel geselecteerde track nu |
| `a` | Voeg toe aan queue |
| `spatie` | Play / Pauze |
| `n` / `p` | Volgende / Vorige |
| `+` / `-` | Volume omhoog / omlaag |
| `c` | Wis queue |
| `v` | Video aan/uit (YouTube-tracks) |
| `q` | Quit (sluit mpv netjes af) |

### Zoek-prefixen

In het zoekveld kun je combinaties van bron- en sorteer-prefixen typen
(op elke positie in de query; tokens worden gestript):

| Prefix | Effect |
|---|---|
| `/yt` `/youtube` | Alleen YouTube doorzoeken |
| `/lokaal` `/local` `/l` | Alleen lokale library |
| `/spotify` `/sp` | Alleen Spotify (tracks + Top-resultaat-kaart) |
| `/spotify-artiest` `/spotify-artist` | Drill: Spotify-artiest + top-tracks |
| `/spotify-genre` | Drill: Spotify-categorieën → artiesten/playlists |
| `!date` `!new` `!nieuw` | YouTube-resultaten sorteren op uploaddatum, nieuwste eerst; toont ook kolommen Datum/Views/Likes |
| `--limit=N` `limit=N` | Aantal artiesten/playlists voor `/spotify-genre` (1-50, default 20) |

Voorbeelden: `!date lofi` (YouTube+Spotify+local, YT-gesorteerd), `/yt !date coldplay`
(alleen YouTube, gesorteerd op datum).

#### `/spotify` met Top-resultaat-kaart

`/spotify Herman Brood` toont boven de reguliere 20 tracks een kaart-rij
met de **artiest** (drill via Enter → top-tracks van die artiest) en het
**album** (drill via Enter → albumtracks). Track-rijen eronder zijn
afspeelbaar en savable (druk `s`).

#### `/spotify-artiest <naam>`

`/spotify-artiest Herman Brood` toont de **artiest** als eerste rij +
diens **10 top-tracks** in de Spotify-markt US. Drill op de artiest-rij
opent nogmaals de top-tracks (handig na een langere zoekslag).

#### `/spotify-genre <categorie> [--limit=N]`

`/spotify-genre` zonder argument toont **alle** Spotify-categorieën (Rock,
Hip-Hop, Jazz, …). Met argument filtert het op die naam. Enter op een
categorie drillt naar een detail-pagina met een view-toggle:

- **Artiesten**: Spotify's `tag:"<genre>"`-search — werkt universeel.
  Met `--limit=20` (default) krijg je 20 artiesten.
- **Playlists**: vrij-tekst-`q=<genre>` search — werkt voor populaire
  categorieën (rock, pop, jazz). Voor obscure tags kan dit leeg zijn.

In de detail-pagina: `A` schakelt naar Artiesten-view, `P` naar
Playlists-view. Drill op een item-rij (`i:<n>`) opent de artiest of laadt
de playlist-tracks. Boven de rijen staat een `→ Tab: [A]rtiesten ·
[P]laylists`-regel waarin je met Enter de view kunt togglen.

**Workaround voor Spotify's verwijderde browse-endpoints**: Spotify heeft
`category_playlists` (drill-down) eind 2024 deprecated. musi gebruikt
daarom de playlist-`search` als workaround — voor populaire genres
(rrock/pop/jazz) levert dat tientallen echte playlists terug.

**Trade-off `!date`**: YouTube's search-extractor geeft geen upload-datum terug
in de standaard flat-playlist-modus (zelfs non-flat niet). musi lost dat op
door de watch-URLs vlug op te halen en daarna **parallel** de upload-datum per
video te fetchen (~1-3s/video). Voor 8 resultaten ≈ 8s, voor 15 ≈ 15s. Bij
`!date` toont de UI kort een notificatie zodat de "zoeken…"-spinner niet voor
hang wordt aangezien.

**Extra kolommen bij `!date`**: de zoek-tabel krijgt drie kolommen extra
— Datum (`YYYY-MM-DD`), Views (`1.2M`/`12K`-formaat) en Likes. Alleen bij
`!date` omdat we de data in die modus toch al ophalen; in de standaard
relevance-modus zou elke search ~9s extra kosten om die alsnog te fetchen,
en dat willen we niet opleggen. Tracks zonder YouTube-data (lokaal/Spotify)
tonen `—` zodat de tabel uniform blijft.

## YouTube-subscriptions & video

Je YouTube-abonnementen en -favorieten zijn beschikbaar via **Library > YouTube**.
Vereist dat je in je browser ingelogd bent op YouTube, en dat musi je
cookies kan lezen:

```toml
# ~/.config/musi/config.toml
[youtube]
cookies_from_browser = "firefox"   # of "chrome", "chromium", "brave", "edge"
```

`yt-dlp` leest je YouTube-sessie rechtstreeks uit de browser — geen
platte-tekst cookies op disk. Exporteer je abo's opnieuw als je uitlogt.

**Library > YouTube** toont vier feeds:
- **Subscriptions** — recente uploads van je kanalen
- **Favorieten** — je eigen "Favorieten" playlist
- **Watch Later** — je WL-lijst
- **History** — recent bekeken

Enter op een feed laadt de video-lijst; Enter op een video speelt 'm.

**Video bekijken** (`V`): speel een YouTube-track, druk `V` → een apart
mpv-venster toont de video. Druk `V` opnieuw om het te sluiten. De
audio-mpv blijft doorlopen; het videovenster is puur een viewer.

## Automatisch rippen naar mp3

Elke YouTube-track die je **écht afspeelt** wordt op de achtergrond als mp3
opgeslagen in je `music_dir` (standaard `~/Music`), georganiseerd als
`<Artiest>/<Album>/<Titel>.mp3`, en geïndexeerd in de lokale library. Zo groeit
je verzameling vanzelf met wat je beluistert.

- **Direct in `~/Music`**: yt-dlp schrijft de mp3 meteen naar `~/Music/<Titel>
  [<videoId>].mp3` — dezelfde template als `~/bin/yt`. Geen staging-map meer;
  geen extra verplaatsing ná de download.
- **Grace-periode (~5s)**: de rip start pas nadat de track ~5 seconden speelt én
  dan nog steeds de actieve track is. Zo trigger skippen door een queue geen
  tientallen downloads. (Hardcoded in `_rip_after_grace` in `app/musi_app.py`.)
- **Dedup**: `~/.cache/musi/rips.json` (video-id → pad) onthoudt wat al geript is;
  een re-play wordt overgeslagen. Bestand verwijderen = opnieuw rippen.
- **Organize**: na rip leest musi de embedded tags (gezet door
  `yt-dlp --embed-metadata`) en verplaatst het bestand naar `<artiest>/<album>/`
  (geen album → `_no_album`, geen artiest → `_unknown_artist`) — dezelfde
  conventie als `~/bin/muziek-organiseer`, zodat de library uniform blijft.
  Inline (mutagen), niet door het script aangeroepen — dat zou namelijk ook
  *andere* losse files in `~/Music` raken (`--limit 1` zou niet veilig zijn op
  een niet-lege bron).
- **Cookies**: leest `cookies_from_browser` uit `[youtube]` in `config.toml`,
  zodat ook members-only / age-gated video's rippen.
- **Time-out**: 1 uur per rip. Genoeg voor de langste mixes. Bij overschrijding
  ruimt musi eventuele half-geschreven `.mp3.part`-bestanden op.
- **Spotify / lokaal**: niet van toepassing (Spotify niet te rippen; lokaal is
  al een bestand). De rip loopt volledig los van de playback — een mislukte rip
  raakt nooit het afspelen.

## Notities

- **Eén engine tegelijk actief**: de orchestrator stopt de andere engine voor
  hij een nieuwe track start, zodat er nooit twee audiostromen botsen.
- **Albumhoezen** worden ge-cached in `~/.cache/musi/art/` (lokaal uit tags of
  sibling cover.jpg, anders gedownload van YouTube/Spotify). foot toont ze via
  sixel; andere terminals tonen een kleur-tekst-kader.
- **MPRIS-statusbalk**: na `yay -S mpv-mpris` toont `playerctl -p mpv,spotifyd`
  now-playing; `musi`'s eigen MPRIS-bron is een placeholder (TODO: volledige
  service-implementatie).
- **Out of scope** (later): SoundCloud/Bandcamp expliciet (vallen al onder
  yt-dlp), last.fm-scrobbling, queue-persistentie, EQ, podcasts.

## Playlist-beheer (Spotify)

Druk op een track-rij in **Zoeken**, **Queue** of in een Library-drill-down
op `s` om 'm op te slaan in een Spotify-playlist. Er opent een picker met:

- **♡ Liked Songs** — sla direct op in je Liked Songs
- je eigen en gevolgde playlists — typ om te filteren
- **+ Nieuwe playlist…** — typ bovenaan een naam en druk Enter; de playlist
  wordt aangemaakt en het nummer er direct aan toegevoegd

Op een playlist-rij in `#lib-spotify-meta` (Library > Spotify):

- `r` — hernoem de playlist (Input-modal)
- `D` — verwijder de playlist uit je account (Spotify laat de maker 'm
  behouden als je 'm alleen volgde)

### Eerste keer na deze update

Bij de eerste save-actie moet je **opnieuw inloggen** via de Spotify-tab in
musi — Spotify vraagt in de browser om toestemming voor de nieuwe scopes
(`playlist-modify-public`, `playlist-modify-private`, `user-library-modify`).
Daarna wordt de nieuwe token automatisch gecached en heb je er geen omkijken
meer naar.

Als je weet dat OAuth geforceerd is (oude token heeft de oude scopes), kun
je ook vooraf de cache wissen:

```
rm ~/.cache/musi/spotify-token.json
```