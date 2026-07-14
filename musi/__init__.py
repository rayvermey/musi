"""musi — alles-in-één muziek-TUI (lokaal + YouTube + Spotify) voor Niri/Wayland.

Architectuur: één orchestrator met een uniforme queue die per track kiest tussen
twee engines — mpv (via JSON-IPC) voor lokaal/YouTube, en spotifyd (via MPRIS/dbus)
voor Spotify. Albumhoezen via sixel in foot.
"""
__version__ = "0.1.0"
