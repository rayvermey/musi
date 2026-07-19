#!/usr/bin/env bash
# install.sh — verse-Arch installer voor musi + Obsidian
#
# Doel: na een schone Arch-install alle software installeren die nodig is
#   • musi (~/projects/musi) goed laat draaien
#   • dashboard.md + zijn Obsidian-modules goed laat werken
#
# Idempotent waar mogelijk: pacman --needed, pip zonder --upgrade,
# ln -sf overschrijft een bestaande symlink. Alleen uitvoeren wat nodig is.
#
# NIET in scope (doet de gebruiker zelf):
#   • ~/.config/spotifyd/spotifyd.conf configureren + spotifyd.service starten
#   • Spotify- / YouTube-cookies / OAuth-flows
#   • Obsidian community-plugins aanzetten (Settings → Community plugins)
#
# Gebruik:  bash install.sh

set -euo pipefail

# ── kleuren / output ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YEL=$'\033[33m'; BLU=$'\033[34m'; RST=$'\033[0m'
else
  BOLD=''; DIM=''; RED=''; GREEN=''; YEL=''; BLU=''; RST=''
fi

say()  { printf '%s\n' "$*"; }
hdr()  { printf '\n%s%s== %s ==%s\n' "$BOLD" "$BLU" "$*" "$RST"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RST" "$*"; }
warn() { printf '  %s!%s %s\n' "$YEL" "$RST" "$*" >&2; }
die()  { printf '%s%sFOUT:%s %s\n' "$BOLD" "$RED" "$RST" "$*" >&2; exit 1; }

# ── pre-flight ─────────────────────────────────────────────────────────────────
hdr "Pre-flight checks"

command -v pacman >/dev/null \
  || die "pacman niet gevonden — dit script is voor Arch Linux."
ok "pacman gevonden"

command -v sudo >/dev/null \
  || die "sudo niet gevonden — installeer sudo of draai als root."
sudo -n true 2>/dev/null \
  || die "sudo zonder wachtwoord werkt niet. Draai met:  sudo bash $0"
ok "sudo beschikbaar (passwordless of al gecached)"

# musi eist python >= 3.12 (zie pyproject.toml).
if ! command -v python3 >/dev/null; then
  die "python3 niet gevonden. Installeer 'python' via pacman."
fi
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if (( PY_MAJOR < 3 || (PY_MAJOR == 3 && PY_MINOR < 12) )); then
  die "Python ${PY_MAJOR}.${PY_MINOR} gevonden, musi vereist >= 3.12."
fi
ok "python3 ${PY_MAJOR}.${PY_MINOR} (>=3.12 ✓)"

MUSI_DIR="$HOME/projects/musi"
[[ -d "$MUSI_DIR" ]] \
  || die "$MUSI_DIR bestaat niet. Dit script hoort bij het musi-project — clone 'm eerst."
[[ -f "$MUSI_DIR/pyproject.toml" ]] \
  || die "$MUSI_DIR/pyproject.toml ontbreekt — ziet er niet uit als een musi-checkout."
ok "musi-project gevonden op $MUSI_DIR"

VENV="$MUSI_DIR/.venv"

# ── 1. systeem-packages ────────────────────────────────────────────────────────
hdr "1/4  Systeem-packages (sudo)"

# mpv       = audio + YouTube-engine (mpv roept intern yt-dlp aan voor streams)
# spotifyd  = Spotify-MPRIS-backend (systemd-user-service wordt meegeleverd)
# obsidian  = client voor dashboard.md en de hele Obsidian_Ray-vault
sudo pacman -S --noconfirm --needed mpv spotifyd obsidian
ok "mpv / spotifyd / obsidian geïnstalleerd (of al aanwezig)"

# ── 2. musi venv ───────────────────────────────────────────────────────────────
hdr "2/4  musi venv ($VENV)"

# --system-site-packages is cruciaal: de venv hergebruikt Arch's
# mutagen / rich / PyGObject / dbus-python / sqlite (GLib-gebonden, hoort
# bij het systeem). Alleen textual + textual-image + spotipy + dasbus + yt-dlp
# en hun transitieve deps komen in de venv zelf.
if [[ ! -d "$VENV" ]]; then
  python3 -m venv --system-site-packages "$VENV"
  ok "venv aangemaakt: $VENV"
else
  ok "venv bestaat al: $VENV"
fi

# ── 3. venv-packages ───────────────────────────────────────────────────────────
hdr "3/4  venv-packages"

# Primaries uit pyproject.toml: textual, textual-image, spotipy, dasbus.
# yt-dlp staat ook in pyproject maar wordt bewust hieronder in z'n eigen
# regel behandeld (is binary én lib).
"$VENV/bin/python" -m pip install --quiet textual textual-image spotipy dasbus
ok "primaries: textual / textual-image / spotipy / dasbus"

# Transitieve deps die bij een verse venv ontbreken maar wel vereist zijn:
#   • typing-extensions   ← textual
#   • requests            ← spotipy
#   • urllib3             ← requests
#   • platformdirs        ← textual
#   • Pygments            ← rich (via system-site-packages, maar rich zelf
#                            zit ook in system) — veilig om dubbel te installeren
#   • yt-dlp              ← binary (musi zoekt 'm op PATH)
"$VENV/bin/python" -m pip install --quiet \
  typing-extensions requests urllib3 platformdirs Pygments yt-dlp
ok "transitieve deps: typing-extensions / requests / urllib3 / platformdirs / Pygments / yt-dlp"

# yt-dlp binary-wrapper.
# musi's YouTubeSearch(ytdlp_bin="yt-dlp") zoekt yt-dlp op PATH.
# De venv heeft 'm wel (./bin/yt-dlp) maar PATH bevat de venv niet.
# → symlink in ~/.local/bin (zit meestal wel in PATH).
# Streepje, NIET underscore (dat is de python-module-naam).
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/yt-dlp" "$HOME/.local/bin/yt-dlp"
ok "yt-dlp wrapper: $HOME/.local/bin/yt-dlp -> $VENV/bin/yt-dlp"

# ── 4. sanity-tests ────────────────────────────────────────────────────────────
hdr "4/4  Sanity-tests"

fail=0
if "$VENV/bin/python" -c "import textual, spotipy, yt_dlp, requests, dasbus" 2>/dev/null; then
  ok "venv-imports: textual, spotipy, yt_dlp, requests, dasbus"
else
  warn "venv-imports faalden — draai handmatig voor details:"
  warn "  $VENV/bin/python -m pip list | grep -E '(textual|spotipy|dasbus|yt-dlp|requests)'"
  fail=1
fi

if command -v mpv >/dev/null; then
  ok "mpv:       $(mpv --version 2>/dev/null | head -1)"
else
  warn "mpv niet gevonden op PATH"
  fail=1
fi

if command -v spotifyd >/dev/null; then
  ok "spotifyd:  $(spotifyd --version 2>/dev/null)"
else
  warn "spotifyd niet gevonden op PATH"
  fail=1
fi

if command -v yt-dlp >/dev/null; then
  ok "yt-dlp:    $(yt-dlp --version 2>/dev/null)"
else
  warn "yt-dlp niet gevonden op PATH"
  fail=1
fi

if command -v obsidian >/dev/null; then
  ok "obsidian:  gevonden op $(command -v obsidian)"
else
  warn "obsidian niet gevonden op PATH — herstart je shell of installeer handmatig"
  fail=1
fi

# ~/.local/bin hoort in PATH te staan (XDG-spec).
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "\$HOME/.local/bin staat niet in \$PATH — yt-dlp wordt niet gevonden." ;;
esac

# ── afsluiting ─────────────────────────────────────────────────────────────────
hdr "Klaar"
if (( fail == 0 )); then
  ok "alles aanwezig."
else
  warn "enkele checks faalden — zie meldingen hierboven."
fi

say ""
say "${BOLD}Volgende stappen (handmatig):${RST}"
say "  1. ${DIM}spotifyd configureren:${RST}"
say "       ~/.config/spotifyd/spotifyd.conf bewerken (client_id/secret/etc.)"
say "       systemctl --user enable --now spotifyd"
say "  2. ${DIM}Spotify / YouTube credentials in musi:${RST}"
say "       ~/.config/musi/config.toml (Spotify client_id/secret + cache_path)"
say "       ~/.config/musi/cookies_from_browser in [youtube] (leeg = publiek)"
say "  3. ${DIM}Obsidian:${RST}"
say "       obsidian starten → vault 'Obsidian_Ray' openen →"
say "       Settings → Community plugins: dataview, templater-obsidian,"
say "       customjs, meta-bind aanzetten."
say "  4. ${DIM}musi starten:${RST}"
say "       $VENV/bin/python -m musi.cli"
say ""