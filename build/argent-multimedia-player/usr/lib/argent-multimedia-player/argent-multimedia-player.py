#!/usr/bin/env python3
"""
Argent Multimedia Player
GTK4 + Libadwaita + GStreamer + WebKitGTK

v1.3.0 — Buscador nativo de YouTube (yt-dlp + mpv) con límite 720p.
"""

from __future__ import annotations

import sys, os, re, json, faulthandler, random, shutil, signal, subprocess, threading, time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import unquote, urlparse, urljoin, quote
from urllib.request import urlopen, Request

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Adw, Gio, Gdk, GLib, Gst
# Forzar la identidad de la aplicación para que XFCE asocie el ícono
GLib.set_prgname("com.argent.MultimediaPlayer")
GLib.set_application_name("Argent Multimedia Player")

# Necesario para las miniaturas
try:
    from gi.repository import GdkPixbuf
    HAS_PIXBUF = True
except ImportError:
    HAS_PIXBUF = False

HAS_MUTAGEN = False
try:
    import mutagen
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except ImportError:
    mutagen = None
    MutagenFile = None

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

MEDIA_EXTS = {".mp3",".flac",".ogg",".opus",".wav",".m4a",".aac",".wma",
              ".mp4",".mkv",".webm",".avi",".mov",".m4v",".ts",".m2ts"}
PLAYLIST_EXTS = {".m3u", ".m3u8"}
VIDEO_EXTS = {".mp4",".mkv",".webm",".avi",".mov",".m4v",".ts",".m2ts"}

CRASH_MARKER_TTL_SEC = 60
PLAYBACK_OK_RESET_SEC = 10

# fds pre-abiertos para el signal handler (async-signal-safe)
_crash_marker_fd: int | None = None
_signal_log_fd: int | None = None


def _init_crash_marker_fd() -> None:
    global _crash_marker_fd
    try:
        cfg = Path(GLib.get_user_config_dir()) / "argent-multimedia-player"
        cfg.mkdir(parents=True, exist_ok=True)
        path = cfg / "crash_marker.json"
        try:
            if path.is_file():
                path.unlink()
        except Exception:
            pass
        _crash_marker_fd = os.open(
            str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644
        )
    except Exception as e:
        print("No se pudo pre-abrir crash marker:", e)
        _crash_marker_fd = None


def _init_signal_log_fd() -> None:
    """fd de append para registrar cada señal recibida (diagnóstico)."""
    global _signal_log_fd
    try:
        cfg = Path(GLib.get_user_config_dir()) / "argent-multimedia-player"
        cfg.mkdir(parents=True, exist_ok=True)
        path = cfg / "signals.log"
        _signal_log_fd = os.open(
            str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
        )
    except Exception as e:
        print("No se pudo pre-abrir signals.log:", e)
        _signal_log_fd = None


def ns_to_time(ns: int) -> str:
    if ns <= 0: return "00:00"
    seconds = int(ns // Gst.SECOND)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

def path_to_uri(path: str) -> str:
    return Gio.File.new_for_path(path).get_uri()

def _write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.is_file():
                tmp.unlink()
        except Exception:
            pass
        raise

def _natural_key(text: str) -> list:
    parts = re.split(r"(\d+)", text)
    return [(1, int(t)) if t.isdigit() else (0, t.lower()) for t in parts]

def uri_to_display_name(uri: str) -> str:
    try:
        return Gio.File.new_for_uri(uri).get_basename() or uri
    except Exception:
        return unquote(uri.split("/")[-1] or uri)

def is_media_file(path: str) -> bool:
    return Path(path).suffix.lower() in MEDIA_EXTS

def is_m3u_file(path_or_uri: str) -> bool:
    lower = path_or_uri.lower().split("?")[0].split("#")[0]
    return lower.endswith(".m3u") or lower.endswith(".m3u8")

def get_local_metadata(path: str) -> dict:
    info = {"title": Path(path).stem, "artist": "Desconocido",
            "album": "—", "duration": "—", "duration_sec": 0.0}
    if not HAS_MUTAGEN or not path or path.startswith("http"):
        return info
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None: return info
        tags = audio.tags or {}
        if tags.get("title"): info["title"] = str(tags["title"][0])
        if tags.get("artist"): info["artist"] = str(tags["artist"][0])
        if tags.get("album"): info["album"] = str(tags["album"][0])
        if getattr(audio, "info", None) and getattr(audio.info, "length", None):
            length = float(audio.info.length)
            info["duration_sec"] = length
            info["duration"] = ns_to_time(int(length * Gst.SECOND))
    except Exception:
        pass
    return info

@dataclass
class Track:
    uri: str
    title: str
    artist: str = "Desconocido"
    album: str = "—"
    duration: str = "—"
    kind: str = "local"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        return cls(
            uri=d["uri"],
            title=d.get("title") or uri_to_display_name(d["uri"]),
            artist=d.get("artist") or "—",
            album=d.get("album") or "—",
            duration=d.get("duration") or "—",
            kind=d.get("kind") or "local",
        )

def _split_extinf(line: str) -> tuple[str, str]:
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"': in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            return line[:i], line[i + 1:].strip()
    return line, ""

def _resolve_m3u_url(url: str, base_uri: str | None) -> str:
    if not base_uri or urlparse(url).scheme: return url
    if base_uri.startswith("file://"):
        url = url.replace("\\", "/")
        if url.startswith("/"): return path_to_uri(url)
        return urljoin(base_uri, quote(url, safe="/"))
    return urljoin(base_uri, quote(url, safe="/:%?&=#@+,;~!$'()*"))

def parse_m3u(content: str, base_uri: str | None = None) -> list[dict]:
    entries, current = [], None
    content = content.lstrip("\ufeff")
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for raw in lines:
        line = raw.strip().lstrip("\ufeff")
        if not line: continue
        if line.startswith("#EXTINF:"):
            head, after = _split_extinf(line)
            attr_map = {k.lower(): v for k, v in re.findall(r'([\w-]+)="([^"]*)"', head)}
            group = attr_map.get("group-title", "") or attr_map.get("group", "")
            logo = attr_map.get("tvg-logo", "") or attr_map.get("logo", "")
            name = attr_map.get("tvg-name", "") or attr_map.get("tvg-id", "")
            if after: name = after
            duration = -1
            m = re.match(r"#EXTINF:(-?\d+)", head)
            if m: duration = int(m.group(1))
            current = {"name": name or "Sin nombre", "url": "", "group": group, "logo": logo, "duration": duration}
        elif line.startswith("#"):
            continue
        else:
            url = _resolve_m3u_url(line, base_uri)
            if current is None:
                current = {"name": uri_to_display_name(url), "url": url, "group": "", "logo": "", "duration": -1}
            else:
                current["url"] = url
            if current["url"]: entries.append(current)
            current = None
    return entries

def load_m3u_from_path_or_url(source: str) -> list[dict]:
    content, base = "", source
    try:
        if source.startswith("http://") or source.startswith("https://"):
            req = Request(source, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
            with urlopen(req, timeout=20) as resp:
                content = resp.read().decode("utf-8-sig", errors="replace")
        else:
            path = source
            if source.startswith("file://"):
                path = Gio.File.new_for_uri(source).get_path() or source
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                content = f.read()
            base = path_to_uri(path) if not source.startswith("file://") else source
    except Exception as e:
        print(f"Error cargando M3U: {e}")
        return []
    return parse_m3u(content, base_uri=base)


# ---------------------------------------------------------------------------
# Aplicación principal
# ---------------------------------------------------------------------------

class ArgentMultimediaPlayer(Adw.Application):
    APP_ID = "com.argent.MultimediaPlayer"
    APP_NAME = "Argent Multimedia Player"
    APP_VERSION = "1.3.0"

    EQ_BAND_LABELS = ("31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k")
    EQ_PRESETS = {
        "Plano": [0,0,0,0,0,0,0,0,0,0], "Rock": [4,3,1,0,-1,1,3,4,4,4],
        "Pop": [-1,0,2,4,4,2,0,-1,-1,-1], "Jazz": [3,2,1,2,-1,-1,0,1,2,3],
        "Clásica": [4,3,2,0,-1,-1,0,2,3,4], "Bass": [6,5,4,2,0,-1,-2,-2,-2,-2],
        "Treble": [-2,-2,-1,0,1,2,4,5,6,6], "Vocal": [-2,-1,0,2,4,4,3,1,0,-1],
        "Electrónica": [5,4,1,0,-2,2,1,3,4,5], "Cine": [5,4,2,1,0,0,1,2,3,2],
    }

    def __init__(self):
        # HANDLES_OPEN permite recibir archivos desde el gestor de archivos (Nautilus, etc.)
        super().__init__(application_id=self.APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.connect("activate", self.on_activate)
        self.connect("open", self.on_open)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", self._on_quit_action)
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

        self.config_dir = Path(GLib.get_user_config_dir()) / "argent-multimedia-player"
        self.config_file = self.config_dir / "settings.json"
        self.playlist_file = self.config_dir / "playlist.json"
        self.iptv_file = self.config_dir / "iptv.json"
        self.crashed_uris_file = self.config_dir / "crashed_uris.json"
        self.crash_state_file = self.config_dir / "crash_state.json"
        self.crash_marker_file = self.config_dir / "crash_marker.json"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.settings = self._load_settings()

        self._crashed_uris: dict = {}
        self._current_crash_state: dict = {}
        self._playback_ok_timer_id: int = 0

        self.playlist: list[Track] = []
        self.current_index: int = -1
        self.is_playing: bool = False
        self.is_shuffle: bool = False
        self.duration: int = 0
        self._playlist_dirty: bool = False
        self._save_timeout_id: int = 0
        self._settings_save_timeout_id: int = 0
        self._seek_timeout_id: int = 0
        self._pending_seek_value: float | None = None
        self._prev_volume: float = 70.0
        self._user_seeking: bool = False
        self._current_uri: str = ""
        self._missing_tracks: list[dict] = []

        self._queued_retry_id: int = 0
        self._current_is_live: bool = False
        self._m3u_load_gen: int = 0
        self._pipeline_gen: int = 0
        self._last_play_uri: str = ""
        self._last_play_time: int = 0
        self._yt_search_gen: int = 0

        self.iptv_channels: list[dict] = []
        self._iptv_source: str = ""
        self._iptv_build_index: int = 0
        self._iptv_build_total: int = 0
        self._iptv_build_gen: int = 0
        self.iptv_search_entry: Gtk.SearchEntry | None = None
        self.iptv_group_dropdown = None
        self._iptv_filter_query: str = ""
        self._iptv_filter_group: str = ""
        self._iptv_filter_timeout_id: int = 0
        self._iptv_populating: bool = False

        self._eq_element = None
        self._eq_scales: list = []
        self._eq_updating: bool = False
        self._ui_ready: bool = False
        self._eq_preset_dropdown = None

        self.playbin: Gst.Element | None = None
        self.bus = None
        self.window: Adw.ApplicationWindow | None = None
        self.lbl_titulo: Gtk.Label | None = None
        self.lbl_artista: Gtk.Label | None = None
        self.btn_play: Gtk.Button | None = None
        self.btn_shuffle: Gtk.ToggleButton | None = None
        self.slider_progress: Gtk.Scale | None = None
        self.lbl_time_curr: Gtk.Label | None = None
        self.lbl_time_total: Gtk.Label | None = None
        self.slider_vol: Gtk.Scale | None = None
        self.btn_mute: Gtk.Button | None = None
        self.list_box: Gtk.ListBox | None = None
        self.iptv_list: Gtk.ListBox | None = None
        self.status_label: Gtk.Label | None = None
        self.toast_overlay: Adw.ToastOverlay | None = None
        self.search_entry: Gtk.SearchEntry | None = None
        self._theme_buttons: dict = {}

        self.yt_search_entry: Gtk.SearchEntry | None = None
        self.yt_results_list: Gtk.ListBox | None = None
        self.yt_quality_dropdown = None
        self.yt_spinner: Gtk.Spinner | None = None

        self.inicio_stack: Gtk.Stack | None = None
        self.view_stack: Adw.ViewStack | None = None
        self._nav_to_video: bool = False
        self._video_probe_ticks: int = 0
        self._stopping: bool = False
        self._pending_uri: str | None = None
        self._stop_timeout_id: int = 0
        self._paintable = None
        self.video_picture: Gtk.Picture | None = None
        self.paintable_sink = None

        self._inhibit_cookie: int = 0
        self._ss_cookie: int = 0
        self._ss_proxy = None
        self._ss_interface: str = ""
        self._ss_unavailable: bool = False
        self._ss_inhibiting: bool = False

        self._is_fullscreen: bool = False
        self._apply_theme(self.settings.get("theme", "dark"))

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    def _load_crashed_uris(self):
        self._crashed_uris = {}
        try:
            if self.crashed_uris_file.is_file():
                with open(self.crashed_uris_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict): self._crashed_uris = data
        except Exception as e:
            print("No se pudo leer crashed_uris.json:", e)

    def _save_crashed_uris(self):
        try: _write_json_atomic(self.crashed_uris_file, self._crashed_uris)
        except Exception as e: print("No se pudo guardar crashed_uris.json:", e)

    def _mark_uri_crashed(self, uri: str, title: str = ""):
        if not uri: return
        info = self._crashed_uris.get(uri, {"count": 0, "last": "", "mpv_fallback": False})
        info["count"] = int(info.get("count", 0)) + 1
        info["last"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        info["mpv_fallback"] = True
        info["title"] = title or info.get("title", "")
        self._crashed_uris[uri] = info
        self._save_crashed_uris()
        print(f"[crash-recovery] URI marcada ({info['count']}×): {uri[:80]}")

    def _unmark_uri_crashed(self, uri: str):
        info = self._crashed_uris.get(uri)
        if not info: return
        info["mpv_fallback"] = False
        self._crashed_uris[uri] = info
        self._save_crashed_uris()
        print(f"[crash-recovery] URI perdonada: {uri[:80]}")

    def _should_use_mpv_fallback(self, uri: str) -> bool:
        info = self._crashed_uris.get(uri)
        return bool(info and info.get("mpv_fallback"))

    def _check_crash_recovery(self):
        marker_ok = self.crash_marker_file.is_file()
        state_ok = self.crash_state_file.is_file()
        if marker_ok:
            try:
                with open(self.crash_marker_file, "r", encoding="utf-8") as f:
                    marker = json.load(f)
                if time.time() - float(marker.get("timestamp", 0)) > CRASH_MARKER_TTL_SEC:
                    marker_ok = False
            except Exception:
                marker_ok = False
        if marker_ok and state_ok:
            try:
                with open(self.crash_state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                uri, title = state.get("uri", ""), state.get("title", "")
                if uri:
                    self._mark_uri_crashed(uri, title)
                    self._status(f"Se detectó un cierre abrupto. El siguiente intento con '{title or uri_to_display_name(uri)}' usará mpv.")
            except Exception as e:
                print("Error crash recovery:", e)
        for p in (self.crash_marker_file, self.crash_state_file):
            try:
                if p.is_file(): p.unlink()
            except Exception: pass
        _init_crash_marker_fd()
        _init_signal_log_fd()

    def _write_crash_state(self, uri: str, title: str = ""):
        self._current_crash_state = {"uri": uri, "title": title, "started_at": time.time()}
        try: _write_json_atomic(self.crash_state_file, self._current_crash_state)
        except Exception as e: print("No se pudo escribir crash_state:", e)

    def _clear_crash_state(self):
        self._current_crash_state = {}
        try:
            if self.crash_state_file.is_file(): self.crash_state_file.unlink()
        except Exception: pass

    def _on_playback_ok(self):
        self._playback_ok_timer_id = 0
        uri = self._current_crash_state.get("uri") if self._current_crash_state else None
        if uri and uri in self._crashed_uris: self._unmark_uri_crashed(uri)
        self._clear_crash_state()
        return False

    def _schedule_playback_ok_check(self):
        if self._playback_ok_timer_id:
            GLib.source_remove(self._playback_ok_timer_id)
            self._playback_ok_timer_id = 0
        self._playback_ok_timer_id = GLib.timeout_add_seconds(PLAYBACK_OK_RESET_SEC, self._on_playback_ok)

    # ------------------------------------------------------------------
    # Persistencia y Configuración
    # ------------------------------------------------------------------

    def _default_settings(self) -> dict:
        return {"theme": "dark", "volume": 70, "width": 1100, "height": 720,
                "maximized": False, "last_uri": "", "web_hw": "off",
                "eq_preset": "Plano", "eq_bands": [0.0] * 10, "eq_enabled": True,
                "yt_max_res": "720"}

    def _load_settings(self) -> dict:
        data = self._default_settings()
        try:
            if self.config_file.is_file():
                with open(self.config_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict): data.update(saved)
        except Exception as e:
            print("No se pudo leer settings:", e)
        data.pop("max_quality", None)
        return data

    def _save_settings(self, immediate: bool = False):
        if immediate:
            if self._settings_save_timeout_id:
                GLib.source_remove(self._settings_save_timeout_id)
                self._settings_save_timeout_id = 0
            self._do_save_settings()
            return
        if self._settings_save_timeout_id:
            return
        self._settings_save_timeout_id = GLib.timeout_add_seconds(1, self._flush_settings)

    def _flush_settings(self) -> bool:
        self._settings_save_timeout_id = 0
        self._do_save_settings()
        return False

    def _do_save_settings(self):
        try:
            if self.window:
                self.settings["maximized"] = bool(self.window.is_maximized())
                if not self.settings["maximized"]:
                    try:
                        w, h = self.window.get_width(), self.window.get_height()
                        if w > 100 and h > 100:
                            self.settings["width"], self.settings["height"] = w, h
                    except Exception: pass
            if self.slider_vol: self.settings["volume"] = float(self.slider_vol.get_value())
            _write_json_atomic(self.config_file, self.settings)
        except Exception as e:
            print("No se pudo guardar settings:", e)

    def _apply_theme(self, theme_key: str):
        theme_key = theme_key if theme_key in ("dark", "light", "system") else "dark"
        self.settings["theme"] = theme_key
        try:
            sm = Adw.StyleManager.get_default()
            if theme_key == "dark": sm.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            elif theme_key == "light": sm.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            else: sm.set_color_scheme(Adw.ColorScheme.DEFAULT)
        except Exception as e:
            print("No se pudo aplicar tema:", e)

    # ------------------------------------------------------------------
    # Playlist persistencia
    # ------------------------------------------------------------------

    def _load_playlist(self):
        try:
            if not self.playlist_file.is_file(): return
            with open(self.playlist_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list): return
            existing = {t.uri for t in self.playlist}
            restored = 0
            for item in data:
                if not isinstance(item, dict) or not item.get("uri"): continue
                uri = item["uri"]
                if uri in existing: continue
                if uri.startswith("file://"):
                    path = Gio.File.new_for_uri(uri).get_path()
                    if path and not os.path.isfile(path):
                        self._missing_tracks.append(item)
                        continue
                track = Track.from_dict(item)
                self.playlist.append(track)
                existing.add(track.uri)
                self._add_row_to_list(track)
                restored += 1
            if restored:
                last_uri = self.settings.get("last_uri")
                if last_uri and self.list_box:
                    for i, t in enumerate(self.playlist):
                        if t.uri == last_uri:
                            row = self.list_box.get_row_at_index(i)
                            if row: self.list_box.select_row(row)
                            break
                if self.status_label: self.status_label.set_label(f"Biblioteca: {restored} pistas restauradas")
            self._playlist_dirty = False
        except Exception as e:
            print("No se pudo cargar playlist:", e)

    def _save_playlist(self):
        if self._save_timeout_id:
            GLib.source_remove(self._save_timeout_id)
            self._save_timeout_id = 0
        try:
            payload = [t.to_dict() for t in self.playlist]
            known = {t.uri for t in self.playlist}
            payload += [m for m in self._missing_tracks if m.get("uri") not in known]
            _write_json_atomic(self.playlist_file, payload)
            self._playlist_dirty = False
        except Exception as e:
            print("No se pudo guardar playlist:", e)

    def _mark_playlist_dirty(self):
        self._playlist_dirty = True
        if self._save_timeout_id: return
        self._save_timeout_id = GLib.timeout_add_seconds(2, self._flush_playlist_if_dirty)

    def _flush_playlist_if_dirty(self):
        self._save_timeout_id = 0
        if self._playlist_dirty: self._save_playlist()
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_activate(self, app):
        if self.window is not None:
            self.window.present()
            return
        Gst.init(None)
        self._setup_playbin()
        self.load_custom_css()
        self._load_crashed_uris()

        self.window = Adw.ApplicationWindow(application=app)
        self.window.set_title(self.APP_NAME)
        w = int(self.settings.get("width", 1100))
        h = int(self.settings.get("height", 720))
        self.window.set_default_size(max(800, w), max(500, h))
        self.window.connect("close-request", self.on_close)
        self.window.connect("destroy", self._on_window_destroyed)
        try: self.window.connect("notify::fullscreened", self._on_fullscreened_notify)
        except Exception: pass
        self._set_app_icon()

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._main_box = main_box
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(main_box)
        self.window.set_content(self.toast_overlay)

        self._toolbar = self._create_top_toolbar()
        main_box.append(self._toolbar)

        content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        content_box.set_vexpand(True)
        self._content_box = content_box
        main_box.append(content_box)

        view_stack = Adw.ViewStack()
        self.view_stack = view_stack
        view_stack.set_hexpand(True)
        view_stack.set_vexpand(True)

        pages = [
            (self.create_inicio_view(), "inicio", "Inicio", "go-home-symbolic"),
            (self.create_musica_view(), "musica", "Biblioteca", "audio-x-generic-symbolic"),
            (self.create_iptv_view(), "iptv", "IPTV / M3U", "network-workgroup-symbolic"),
            (self.create_youtube_view(), "youtube", "YouTube", "video-x-generic-symbolic"),
            (self.create_eq_view(), "eq", "Equalizador", "media-playlist-consecutive-symbolic"),
        ]
        for widget, name, title, icon in pages:
            view_stack.add_titled_with_icon(widget, name, title, icon)

        self._sidebar = self._create_compat_sidebar(view_stack, pages)
        content_box.append(self._sidebar)
        content_box.append(view_stack)

        self._controls_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        main_box.append(self._controls_sep)
        self._controls_bar = self.create_controls_bar()
        main_box.append(self._controls_bar)

        vol = float(self.settings.get("volume", 70))
        if self.slider_vol: self.slider_vol.set_value(vol)
        self.set_volume(vol)
        self._update_mute_icon(vol)

        self._setup_drag_drop(self.window)
        self._setup_shortcuts(self.window)
        self._load_playlist()
        GLib.idle_add(self._load_saved_iptv)

        if self.settings.get("maximized", False): self.window.maximize()
        self.window.present()
        self._ui_ready = True
        GLib.timeout_add(250, self._update_progress)
        if os.environ.get("ARGENT_WATCHDOG", "1") != "0":
            self._watchdog_tick()
            GLib.timeout_add_seconds(1, self._watchdog_tick)
        GLib.idle_add(self._check_crash_recovery)

    def on_open(self, app, files, n_files, hint):
        # Si es la primera ejecución, on_activate se encarga de crear la interfaz.
        # Si la app ya estaba abierta, on_activate solo la trae al frente.
        self.on_activate(app)
        
        paths = []
        for f in files:
            path = f.get_path()
            if path:
                paths.append(path)
                
        if paths:
            self.add_files(paths)

    def _on_window_destroyed(self, *_):
        self.window = None

    def _set_app_icon(self):
        icon_name = self.APP_ID
        display = Gdk.Display.get_default()
        if not display or not self.window: return
        try: theme = Gtk.IconTheme.get_for_display(display)
        except Exception: theme = None
        here = Path(__file__).resolve().parent
        extra_dirs = [here,
                      here / "packaging" / "icons" / "hicolor" / "256x256" / "apps",
                      here / "packaging" / "icons" / "hicolor" / "128x128" / "apps",
                      here / "packaging" / "icons" / "hicolor" / "scalable" / "apps",
                      here / "packaging",
                      Path("/usr/share/argent-multimedia-player"),
                      Path("/usr/share/icons/hicolor/256x256/apps"),
                      Path("/usr/share/icons/hicolor/scalable/apps")]
        if theme:
            for d in extra_dirs:
                if d.is_dir():
                    try: theme.add_search_path(str(d))
                    except Exception: pass
        if theme and theme.has_icon(icon_name):
            try:
                self.window.set_icon_name(icon_name)
                Gtk.Window.set_default_icon_name(icon_name)
                return
            except Exception: pass
        for d in extra_dirs:
            for fname in (f"{icon_name}.png", f"{icon_name}.svg", "argent-multimedia-player.png"):
                path = d / fname
                if not path.is_file(): continue
                try:
                    if theme: theme.add_search_path(str(path.parent))
                    name = path.stem
                    if theme and theme.has_icon(name):
                        self.window.set_icon_name(name)
                        Gtk.Window.set_default_icon_name(name)
                    print(f"Icono: {path}")
                    return
                except Exception as e:
                    print(f"No se pudo aplicar icono {path}:", e)

    def _watchdog_tick(self) -> bool:
        try:
            if self.window is None:
                faulthandler.cancel_dump_traceback_later()
                return False
            faulthandler.dump_traceback_later(8, repeat=False)
            return True
        except Exception as e:
            print("watchdog:", e)
            return True

    def _status(self, text: str):
        if self.status_label: self.status_label.set_label(text)
        if self.toast_overlay:
            toast = Adw.Toast.new(text)
            if hasattr(toast, "set_use_markup"): toast.set_use_markup(False)
            self.toast_overlay.add_toast(toast)

    def _on_quit_action(self, *_):
        self._do_cleanup_and_quit()

    def _do_cleanup_and_quit(self):
        self._save_playlist()
        self._save_settings(immediate=True)
        self.is_playing = False
        self._update_screensaver_inhibit()
        if self._stop_timeout_id:
            GLib.source_remove(self._stop_timeout_id)
            self._stop_timeout_id = 0
        self._pending_uri = None
        self._stopping = False
        if self.playbin:
            playbin = self.playbin
            threading.Thread(target=lambda: playbin.set_state(Gst.State.NULL), daemon=True).start()
        if self.bus:
            try: self.bus.remove_signal_watch()
            except Exception: pass
        self._clear_crash_state()
        self.quit()

    def on_close(self, *_):
        self._do_cleanup_and_quit()
        return True

    # ------------------------------------------------------------------
    # Toolbar & Sidebar
    # ------------------------------------------------------------------

    def _create_top_toolbar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        bar.set_margin_start(12)
        bar.set_margin_end(10)
        bar.add_css_class("toolbar")
        bar.add_css_class("argent-toolbar")

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_box.set_halign(Gtk.Align.START)
        title_box.set_hexpand(True)
        title_lbl = Gtk.Label(label=self.APP_NAME)
        title_lbl.add_css_class("heading")
        title_lbl.add_css_class("argent-title")
        title_lbl.set_halign(Gtk.Align.START)
        ver_lbl = Gtk.Label(label=f"v{self.APP_VERSION}")
        ver_lbl.add_css_class("argent-version")
        ver_lbl.add_css_class("dim-label")
        title_box.append(title_lbl)
        title_box.append(ver_lbl)
        bar.append(title_box)

        theme_lbl = Gtk.Label(label="Tema")
        theme_lbl.add_css_class("dim-label")
        bar.append(theme_lbl)
        theme_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        theme_box.add_css_class("linked")
        theme_key = self.settings.get("theme", "dark")
        group_leader = None
        for key, label in (("dark", "Oscuro"), ("light", "Claro"), ("system", "Sistema")):
            btn = Gtk.ToggleButton(label=label)
            if group_leader is None: group_leader = btn
            else: btn.set_group(group_leader)
            if key == theme_key: btn.set_active(True)
            btn.connect("toggled", self._on_theme_button, key)
            self._theme_buttons[key] = btn
            theme_box.append(btn)
        bar.append(theme_box)

        btn_quit = Gtk.Button(label="Salir")
        btn_quit.add_css_class("flat")
        btn_quit.set_tooltip_text("Cerrar la aplicación (Ctrl+Q)")
        btn_quit.connect("clicked", lambda *_: self.activate_action("quit", None))
        bar.append(btn_quit)
        bar.append(Gtk.WindowControls(side=Gtk.PackType.END))
        handle = Gtk.WindowHandle()
        handle.set_child(bar)
        return handle

    def _on_theme_button(self, button: Gtk.ToggleButton, key: str):
        if not button.get_active(): return
        for k, b in self._theme_buttons.items():
            if k != key and b.get_active(): b.set_active(False)
        self._apply_theme(key)
        self._save_settings()

    def _create_compat_sidebar(self, view_stack: Adw.ViewStack, pages: list) -> Gtk.Widget:
        if hasattr(Adw, "ViewStackSidebar"):
            sidebar = Adw.ViewStackSidebar(stack=view_stack)
            sidebar.set_size_request(200, -1)
            return sidebar
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_size_request(200, -1)
        outer.add_css_class("sidebar")
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        list_box.add_css_class("navigation-sidebar")
        name_to_row = {}
        for _widget, name, title, icon in pages:
            row = Gtk.ListBoxRow()
            row.page_name = name
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.set_margin_top(10)
            row_box.set_margin_bottom(10)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)
            row_box.append(Gtk.Image.new_from_icon_name(icon))
            lbl = Gtk.Label(label=title, xalign=0)
            lbl.set_hexpand(True)
            row_box.append(lbl)
            row.set_child(row_box)
            list_box.append(row)
            name_to_row[name] = row
        def on_row_selected(lb, row):
            if row is None: return
            name = getattr(row, "page_name", None)
            if name: view_stack.set_visible_child_name(name)
        list_box.connect("row-selected", on_row_selected)
        first = list_box.get_row_at_index(0)
        if first: list_box.select_row(first)
        def on_stack_changed(stack, _pspec):
            visible = stack.get_visible_child_name()
            row = name_to_row.get(visible)
            if row and list_box.get_selected_row() != row: list_box.select_row(row)
        view_stack.connect("notify::visible-child", on_stack_changed)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(list_box)
        scrolled.set_vexpand(True)
        outer.append(scrolled)
        return outer

    # ------------------------------------------------------------------
    # GStreamer
    # ------------------------------------------------------------------

    def _setup_playbin(self):
        self.playbin = Gst.ElementFactory.make("playbin", "player")
        if not self.playbin:
            print("ERROR: No se pudo crear playbin.")
            return
        self.playbin.set_property("volume", 0.7)
        self.playbin.connect("source-setup", self._on_source_setup)
        self.playbin.connect("element-setup", self._on_element_setup)
        try: self.playbin.set_property("flags", 0x317)
        except Exception: pass
        for prop, val in (("buffer-size", 2 * 1024 * 1024), ("buffer-duration", 2 * Gst.SECOND)):
            if self.playbin.find_property(prop) is not None:
                try: self.playbin.set_property(prop, val)
                except Exception: pass
        try:
            self.paintable_sink = Gst.ElementFactory.make("gtk4paintablesink", "sink")
            if self.paintable_sink:
                if self.paintable_sink.find_property("sync") is not None:
                    self.paintable_sink.set_property("sync", True)
                if self.paintable_sink.find_property("max-lateness") is not None:
                    self.paintable_sink.set_property("max-lateness", 20 * Gst.MSECOND)
                if self.paintable_sink.find_property("qos") is not None:
                    self.paintable_sink.set_property("qos", True)
                self.playbin.set_property("video-sink", self.paintable_sink)
            else:
                print("Advertencia: gtk4paintablesink no encontrado.")
        except Exception as e:
            print("Advertencia gtk4paintablesink:", e)
        self.bus = self.playbin.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_bus_message)
        self._setup_equalizer()

    def _on_source_setup(self, playbin, source):
        if source.find_property("user-agent") is not None:
            source.set_property("user-agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
        name = ""
        try:
            factory = source.get_factory()
            if factory: name = factory.get_name() or ""
        except Exception: pass
        if "soup" in name or "http" in name:
            for prop, val in (("timeout", 12), ("retries", 2)):
                if source.find_property(prop) is not None:
                    try: source.set_property(prop, val)
                    except Exception: pass
            if source.find_property("ssl-strict") is not None and os.environ.get("ARGENT_IPTV_INSECURE", "") == "1":
                try: source.set_property("ssl-strict", False)
                except Exception: pass

    def _on_element_setup(self, playbin, element):
        try:
            name = element.get_factory().get_name() if element.get_factory() else ""
        except Exception:
            return
        if name in ("queue", "queue2", "multiqueue"):
            for prop, val in (("max-size-buffers", 0), ("max-size-bytes", 0), ("max-size-time", 2 * Gst.SECOND)):
                if element.find_property(prop) is not None:
                    try: element.set_property(prop, val)
                    except Exception: pass

    def _recreate_playbin(self):
        print("Recreando playbin (pipeline anterior no respondió)")
        old_bus = self.bus
        old_playbin = self.playbin
        self.bus = None
        self.playbin = None
        self.paintable_sink = None
        self._paintable = None
        self._pipeline_gen += 1
        if self.video_picture:
            try: self.video_picture.set_paintable(None)
            except Exception: pass
            try: self.video_picture.set_visible(False)
            except Exception: pass
        if old_bus:
            try: old_bus.remove_signal_watch()
            except Exception: pass
        if old_playbin:
            def _abandon():
                try: old_playbin.set_state(Gst.State.NULL)
                except Exception: pass
            t = threading.Thread(target=_abandon, daemon=True)
            t.start()
            t.join(0.5)
        self._setup_playbin()
        if self.slider_vol and self.playbin:
            self.set_volume(self.slider_vol.get_value())

    def _is_live_source(self) -> bool:
        uri = (self._current_uri or "").lower().split("?")[0].split("#")[0]
        if not uri: return False
        if uri.startswith("file://"): return False
        if uri.endswith(".m3u8") or uri.endswith(".m3u"): return True
        return bool(getattr(self, "_current_is_live", False))

    def _setup_equalizer(self):
        if not self.playbin: return
        if not self.settings.get("eq_enabled", True):
            try: self.playbin.set_property("audio-filter", None)
            except Exception: pass
            self._eq_element = None
            return
        eq = Gst.ElementFactory.make("equalizer-10bands", "argent-eq")
        if not eq:
            self._eq_element = None
            return
        bands = self.settings.get("eq_bands") or [0.0] * 10
        if not isinstance(bands, list) or len(bands) != 10: bands = [0.0] * 10
        for i, g in enumerate(bands):
            try: eq.set_property(f"band{i}", float(g))
            except Exception: pass
        try:
            self.playbin.set_property("audio-filter", eq)
            self._eq_element = eq
        except Exception as e:
            print("No se pudo asignar equalizador:", e)
            self._eq_element = None

    def _apply_eq_bands(self, bands: list[float], preset_name: str | None = None):
        if len(bands) != 10: return
        bands = [max(-24.0, min(12.0, float(x))) for x in bands]
        self.settings["eq_bands"] = bands
        if preset_name is not None: self.settings["eq_preset"] = preset_name
        if self._eq_element is not None:
            for i, g in enumerate(bands):
                try: self._eq_element.set_property(f"band{i}", g)
                except Exception: pass
        elif self.playbin and self.settings.get("eq_enabled", True):
            self._setup_equalizer()
        self._save_settings()
        if self._eq_scales and not self._eq_updating:
            self._eq_updating = True
            try:
                for sc, g in zip(self._eq_scales, bands): sc.set_value(g)
            finally:
                self._eq_updating = False

    def _on_eq_preset_changed(self, combo, *_):
        if getattr(self, "_ui_ready", False) is not True: return
        names = list(self.EQ_PRESETS.keys())
        if hasattr(combo, "get_selected"):
            idx = combo.get_selected()
            name = names[idx] if 0 <= idx < len(names) else "Plano"
        else:
            name = combo.get_active_id() or "Plano"
        bands = list(self.EQ_PRESETS.get(name, [0.0] * 10))
        self._apply_eq_bands(bands, preset_name=name)
        self._status(f"Equalizador: {name}")

    def _on_eq_band_changed(self, scale, band_index: int):
        if self._eq_updating:
            return
        bands = list(self.settings.get("eq_bands") or [0.0] * 10)
        if len(bands) != 10:
            bands = [0.0] * 10
        bands[band_index] = max(-24.0, min(12.0, float(scale.get_value())))
        self.settings["eq_bands"] = bands
        self.settings["eq_preset"] = "Personalizado"
        if self._eq_element is not None:
            try:
                self._eq_element.set_property(f"band{band_index}", bands[band_index])
            except Exception:
                pass
        elif self.playbin and self.settings.get("eq_enabled", True):
            self._setup_equalizer()
        self._save_settings()

    def _on_eq_enabled_toggled(self, switch, *_):
        enabled = bool(switch.get_active())
        self.settings["eq_enabled"] = enabled
        self._save_settings()
        if self.playbin: self._setup_equalizer()
        self._status("Equalizador activado" if enabled else "Equalizador desactivado")

    def _on_eq_reset(self, *_):
        self._apply_eq_bands([0.0] * 10, preset_name="Plano")
        self._status("Equalizador: Plano")

    # ------------------------------------------------------------------
    # Reproducción
    # ------------------------------------------------------------------

    def on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            self._on_eos()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"GStreamer error: {err} | {debug}")
            msg = err.message or "Error de reproducción"
            low = msg.lower()
            fast_fail = any(x in low for x in ("forbidden", "not found", "404", "403", "unauthorized"))
            self._pending_uri = None
            if fast_fail:
                self._set_playing(False)
                if not self._stopping: self._stop_pipeline_async()
                self._status(f"Canal no disponible: {msg}")
            else:
                self._force_pipeline_reset(reason=f"Error: {msg}")
        elif t == Gst.MessageType.BUFFERING:
            try:
                percent = message.parse_buffering()
                if percent < 100 and self.is_playing:
                    if self.status_label and percent % 25 == 0:
                        self.status_label.set_label(f"Buffering {percent}%…")
                elif percent >= 100 and self.status_label:
                    self.status_label.set_label("Reproduciendo")
            except Exception: pass
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.playbin:
                _, new, _ = message.parse_state_changed()
                if new == Gst.State.PLAYING:
                    self._query_duration()
                    self._update_video_visibility()
        elif t == Gst.MessageType.DURATION_CHANGED:
            self._query_duration()
        return True

    def _update_video_visibility(self):
        if not self.playbin or not self.inicio_stack or self._stopping: return
        if not self.video_picture: return
        try:
            if self.video_picture.get_root() is None: return
        except Exception: return
        try:
            n_video = self.playbin.get_property("n-video")
        except Exception: return
        if n_video > 0 and self.paintable_sink:
            try:
                if self._paintable is None:
                    self._paintable = self.paintable_sink.get_property("paintable")
                if self._paintable and self.video_picture:
                    try: current = self.video_picture.get_paintable()
                    except Exception: current = None
                    if current is not self._paintable:
                        self.video_picture.set_paintable(self._paintable)
                    self.video_picture.set_visible(True)
            except Exception as e:
                print("Error video_visibility:", e)
                return
            try: self.inicio_stack.set_visible_child_name("player")
            except Exception: pass
            self._video_probe_ticks = 0
            if self._nav_to_video and self.view_stack:
                self._nav_to_video = False
                try: self.view_stack.set_visible_child_name("inicio")
                except Exception: pass
        else:
            try:
                if self.video_picture: self.video_picture.set_visible(False)
            except Exception: pass
            try: self.inicio_stack.set_visible_child_name("welcome")
            except Exception: pass

    def _query_duration(self):
        if not self.playbin or self._stopping: return
        ok, duration = self.playbin.query_duration(Gst.Format.TIME)
        if ok and duration > 0:
            self.duration = duration
            if self.lbl_time_total: self.lbl_time_total.set_label(ns_to_time(duration))
            if self.slider_progress: self.slider_progress.set_range(0, max(1, duration / Gst.SECOND))

    def _update_progress(self) -> bool:
        if self.window is None: return False
        if self._stopping: return True
        if self._video_probe_ticks > 0 and self.is_playing:
            self._video_probe_ticks -= 1
            self._update_video_visibility()
        if not self.playbin or not self.is_playing: return True
        self._ss_keepalive = getattr(self, "_ss_keepalive", 0) + 1
        if self._ss_keepalive >= 200:
            self._ss_keepalive = 0
            self._screensaver_keepalive()
        if self._seek_timeout_id or self._user_seeking: return True
        ok, pos = self.playbin.query_position(Gst.Format.TIME)
        if ok:
            if self.lbl_time_curr: self.lbl_time_curr.set_label(ns_to_time(pos))
            if self.slider_progress and self.duration > 0:
                self.slider_progress.set_value(pos / Gst.SECOND)
        return True

    def _screensaver_keepalive(self):
        if not self.is_playing: return
        if not self._inhibit_cookie or (not self._ss_cookie and not self._ss_unavailable):
            self._update_screensaver_inhibit()
        if self._ss_interface == "org.gnome.SessionManager": return
        proxy = self._ss_proxy
        if proxy is None: return
        try: proxy.call_sync("SimulateUserActivity", None, Gio.DBusCallFlags.NONE, 1000, None)
        except Exception: pass

    def _on_eos(self):
        self.stop()
        if self.current_index < 0: return
        if self.playlist: self._on_next()
        else:
            self.current_index = -1
            self._update_now_playing_ui(None)

    def play_uri(self, uri: str, title: str | None = None, artist: str | None = None, live: bool = False):
        if not self.playbin: return
        now = GLib.get_monotonic_time()
        is_mid_launch = self._stopping or (self._pending_uri == uri)
        if (uri == self._last_play_uri
                and (now - self._last_play_time) < 300_000
                and is_mid_launch):
            return
        self._last_play_uri = uri
        self._last_play_time = now
        if not live and self._should_use_mpv_fallback(uri):
            info = self._crashed_uris.get(uri, {})
            count = info.get("count", 1)
            self._status(f"'{title or uri_to_display_name(uri)}' se abre en mpv (GStreamer falló antes, {count}×).")
            self._open_uri_in_mpv(uri, title=title or uri_to_display_name(uri))
            return
        self._current_is_live = live
        if not live: self._write_crash_state(uri, title=title or uri_to_display_name(uri))
        self.duration = 0
        if self.slider_progress:
            self.slider_progress.set_range(0, 100)
            self.slider_progress.set_value(0)
        if self.lbl_time_curr: self.lbl_time_curr.set_label("00:00")
        if self.lbl_time_total: self.lbl_time_total.set_label("00:00")
        self._current_uri = uri
        self._update_now_playing_ui(uri, title=title, artist=artist)
        self._set_playing(True)
        parsed = urlparse(uri)
        self._nav_to_video = (Path(parsed.path).suffix.lower() in VIDEO_EXTS or parsed.scheme not in ("", "file"))
        self._video_probe_ticks = 40
        self._pending_uri = uri
        self._stop_pipeline_async()
        if not live: self._schedule_playback_ok_check()

    def _stop_pipeline_async(self):
        if not self.playbin: return
        if self._stopping: return
        self._stopping = True
        self._pipeline_gen += 1
        gen = self._pipeline_gen
        playbin = self.playbin
        if self._stop_timeout_id:
            GLib.source_remove(self._stop_timeout_id)
            self._stop_timeout_id = 0
        def worker():
            try:
                r = playbin.set_state(Gst.State.READY)
                if r == Gst.StateChangeReturn.FAILURE: raise RuntimeError("READY failure")
            except Exception as e:
                print(f"READY falló ({e}); usando NULL como fallback")
                try: playbin.set_state(Gst.State.NULL)
                except Exception as e2: print("NULL también falló:", e2)
            GLib.idle_add(self._after_pipeline_stopped, gen)
        threading.Thread(target=worker, daemon=True).start()
        self._stop_timeout_id = GLib.timeout_add(4000, self._on_stop_timeout)

    def _on_stop_timeout(self) -> bool:
        self._stop_timeout_id = 0
        if not self._stopping: return False
        print("Timeout: set_state(READY) no respondió → recreando playbin")
        self._force_pipeline_reset(reason="Timeout al detener stream")
        return False

    def _force_pipeline_reset(self, reason: str = ""):
        now = GLib.get_monotonic_time()
        last = getattr(self, "_last_force_reset_us", 0)
        if now - last < 2_000_000:
            self._stopping = False
            if not self._queued_retry_id:
                wait_ms = max(50, (2_000_000 - (now - last)) // 1000 + 50)
                self._queued_retry_id = GLib.timeout_add(wait_ms, self._retry_force_reset)
            return
        self._last_force_reset_us = now
        if self._stop_timeout_id:
            GLib.source_remove(self._stop_timeout_id)
            self._stop_timeout_id = 0
        pending = self._pending_uri
        self._pending_uri = None
        self._stopping = False
        self._recreate_playbin()
        if reason: self._status(reason)
        if pending and self.window is not None: GLib.idle_add(self._replay_pending, pending)
        elif self._current_uri and self.playbin and self.window is not None:
            try: self.playbin.set_property("uri", self._current_uri)
            except Exception: pass

    def _retry_force_reset(self) -> bool:
        self._queued_retry_id = 0
        if self.window is not None and (self._pending_uri or self._stopping):
            self._force_pipeline_reset(reason="")
        return False

    def _replay_pending(self, uri: str) -> bool:
        if not self.playbin or self.window is None: return False
        self._pending_uri = None
        self.playbin.set_property("uri", uri)
        ret = self.playbin.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self._set_playing(False)
            self._status("No se pudo reproducir este stream/archivo")
        else:
            self._set_playing(True)
        return False

    def _after_pipeline_stopped(self, gen: int = 0) -> bool:
        if not self._stopping: return False
        if gen and gen != self._pipeline_gen: return False
        if self._stop_timeout_id:
            GLib.source_remove(self._stop_timeout_id)
            self._stop_timeout_id = 0
        self._stopping = False
        uri, self._pending_uri = self._pending_uri, None
        if not uri or not self.playbin or self.window is None: return False
        try:
            self.playbin.set_property("uri", uri)
            ret = self.playbin.set_state(Gst.State.PLAYING)
        except Exception as e:
            print("Error reanudando pipeline:", e)
            self._set_playing(False)
            return False
        if ret == Gst.StateChangeReturn.FAILURE:
            self._set_playing(False)
            self._status("No se pudo reproducir este stream/archivo")
        return False

    def play_index(self, index: int):
        if 0 <= index < len(self.playlist):
            self.current_index = index
            item = self.playlist[index]
            self.play_uri(item.uri, title=item.title, artist=item.artist)
            self.settings["last_uri"] = item.uri
            self._highlight_current_row()

    def toggle_play_pause(self):
        if not self.playbin: return
        if self.current_index < 0 and not self._current_uri:
            if self.playlist:
                row = self.list_box.get_selected_row() if self.list_box else None
                self.play_index(row.get_index() if row else 0)
            else: self.open_files()
            return
        if self.is_playing:
            if self._stopping: self._pending_uri = None
            else: self.playbin.set_state(Gst.State.PAUSED)
            self._set_playing(False)
        else:
            if self._stopping: self._pending_uri = self._current_uri
            else: self.playbin.set_state(Gst.State.PLAYING)
            self._set_playing(True)

    def _set_playing(self, playing: bool):
        self.is_playing = playing
        if self.btn_play:
            icon = "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
            self.btn_play.set_icon_name(icon)
        self._update_screensaver_inhibit()

    def _get_screensaver_proxy(self):
        if self._ss_proxy is not None: return self._ss_proxy
        if self._ss_unavailable: return None
        try: bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as e:
            print("D-Bus sesión no disponible:", e)
            self._ss_unavailable = True
            return None
        candidates = (
            ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver"),
            ("org.cinnamon.ScreenSaver", "/org/cinnamon/ScreenSaver", "org.freedesktop.ScreenSaver"),
            ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver"),
        )
        for name, path, iface in candidates:
            try:
                proxy = Gio.DBusProxy.new_sync(bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES | Gio.DBusProxyFlags.DO_NOT_CONNECT_SIGNALS, None, name, path, iface, None)
                if proxy.get_name_owner():
                    self._ss_proxy = proxy
                    self._ss_interface = iface
                    print(f"Salvapantallas: usando {name} ({iface})")
                    return proxy
            except Exception: continue
        try:
            proxy = Gio.DBusProxy.new_sync(bus, Gio.DBusProxyFlags.NONE, None, "org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager", None)
            if proxy.get_name_owner():
                self._ss_proxy = proxy
                self._ss_interface = "org.gnome.SessionManager"
                print("Salvapantallas: usando org.gnome.SessionManager")
                return proxy
        except Exception: pass
        print("Salvapantallas: ningún servicio D-Bus disponible")
        self._ss_unavailable = True
        return None

    def _update_screensaver_inhibit(self):
        reason = "Argent Multimedia Player: reproduciendo"
        if self._ss_inhibiting: return
        self._ss_inhibiting = True
        try:
            if self.is_playing and self.window is not None:
                if not self._inhibit_cookie:
                    try:
                        flags = Gtk.ApplicationInhibitFlags.IDLE | Gtk.ApplicationInhibitFlags.SUSPEND
                        cookie = self.inhibit(self.window, flags, reason)
                        self._inhibit_cookie = int(cookie) if cookie else 0
                    except Exception as e: print("Gtk.inhibit falló:", e)
                if not self._ss_cookie and not self._ss_unavailable:
                    proxy = self._get_screensaver_proxy()
                    if proxy is not None:
                        try:
                            if self._ss_interface == "org.gnome.SessionManager":
                                res = proxy.call_sync("Inhibit", GLib.Variant("(susu)", (self.APP_ID, 0, reason, 4 | 8)), Gio.DBusCallFlags.NONE, 3000, None)
                            else:
                                res = proxy.call_sync("Inhibit", GLib.Variant("(ss)", ("argent-multimedia-player", reason)), Gio.DBusCallFlags.NONE, 3000, None)
                            if res is not None:
                                self._ss_cookie = int(res.unpack()[0])
                                print(f"ScreenSaver inhibido (cookie={self._ss_cookie})")
                        except Exception as e:
                            print(f"ScreenSaver.Inhibit falló ({self._ss_interface}): {e}")
                            self._ss_unavailable = True
            else:
                if self._inhibit_cookie:
                    try: self.uninhibit(self._inhibit_cookie)
                    except Exception: pass
                    self._inhibit_cookie = 0
                if self._ss_cookie:
                    proxy = self._ss_proxy
                    if proxy is not None:
                        try: proxy.call_sync("UnInhibit", GLib.Variant("(u)", (self._ss_cookie,)), Gio.DBusCallFlags.NONE, 3000, None)
                        except Exception as e: print("ScreenSaver.UnInhibit falló:", e)
                    self._ss_cookie = 0
        finally: self._ss_inhibiting = False

    def _show_welcome_view(self):
        self._paintable = None
        self._video_probe_ticks = 0
        self._nav_to_video = False
        if self.video_picture:
            try: self.video_picture.set_paintable(None)
            except Exception: pass
            try: self.video_picture.set_visible(False)
            except Exception: pass
        if self.inicio_stack:
            try: self.inicio_stack.set_visible_child_name("welcome")
            except Exception: pass

    def _set_chrome_visible(self, visible: bool):
        for w in (getattr(self, "_toolbar", None), getattr(self, "_sidebar", None),
                  getattr(self, "_controls_sep", None), getattr(self, "_controls_bar", None)):
            if w is not None: w.set_visible(visible)
        if self.video_picture:
            parent = self.video_picture.get_parent()
            if parent is not None:
                if visible:
                    parent.remove_css_class("video-area-fs")
                    parent.add_css_class("video-area")
                else:
                    parent.remove_css_class("video-area")
                    parent.add_css_class("video-area-fs")

    def _toggle_fullscreen(self, *_):
        if not self.window: return
        entering = not (self._is_fullscreen or self.window.is_fullscreen())
        if entering:
            if self.view_stack and self.inicio_stack:
                try:
                    if self.inicio_stack.get_visible_child_name() != "player":
                        if self.is_playing or self._current_uri:
                            self.inicio_stack.set_visible_child_name("player")
                    self.view_stack.set_visible_child_name("inicio")
                except Exception: pass
            self._set_chrome_visible(False)
            self.window.fullscreen()
            self._is_fullscreen = True
        else:
            self.window.unfullscreen()
            self._is_fullscreen = False
            self._set_chrome_visible(True)
        if getattr(self, "btn_fullscreen", None):
            icon = "view-restore-symbolic" if self._is_fullscreen else "view-fullscreen-symbolic"
            try: self.btn_fullscreen.set_icon_name(icon)
            except Exception: pass

    def _on_video_click(self, gesture, n_press, x, y):
        if n_press == 2: self._toggle_fullscreen()

    def _on_fullscreened_notify(self, window, *_):
        try: fs = window.is_fullscreen()
        except Exception: return
        if fs == self._is_fullscreen: return
        self._is_fullscreen = fs
        self._set_chrome_visible(not fs)
        if getattr(self, "btn_fullscreen", None):
            try: self.btn_fullscreen.set_icon_name("view-restore-symbolic" if fs else "view-fullscreen-symbolic")
            except Exception: pass

    def stop(self):
        self._pending_uri = None
        if self._stop_timeout_id:
            GLib.source_remove(self._stop_timeout_id)
            self._stop_timeout_id = 0
        self._stop_pipeline_async()
        self._set_playing(False)
        if self.slider_progress: self.slider_progress.set_value(0)
        if self.lbl_time_curr: self.lbl_time_curr.set_label("00:00")
        if self.lbl_time_total: self.lbl_time_total.set_label("00:00")
        self.duration = 0
        self._show_welcome_view()

    def seek(self, seconds: float):
        if not self.playbin or self.duration <= 0: return
        ns = int(max(0, seconds) * Gst.SECOND)
        self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, ns)

    def _seek_relative(self, delta_sec: float):
        if not self.playbin or self.duration <= 0: return
        ok, pos = self.playbin.query_position(Gst.Format.TIME)
        if not ok: return
        new_ns = max(0, min(self.duration, pos + int(delta_sec * Gst.SECOND)))
        self._user_seeking = True
        self.seek(new_ns / Gst.SECOND)
        GLib.timeout_add(400, self._clear_user_seeking)

    def set_volume(self, value: float):
        if self.playbin:
            self.playbin.set_property("volume", max(0.0, min(1.0, value / 100.0)))

    # ------------------------------------------------------------------
    # Playlist / IPTV / YouTube
    # ------------------------------------------------------------------

    def add_files(self, paths_or_uris: list[str]):
        existing = {t.uri for t in self.playlist}
        added = 0
        first_new = len(self.playlist)
        for item in paths_or_uris:
            if is_m3u_file(item):
                self.load_m3u_source(item)
                continue
            if item.startswith("file://") or "://" in item:
                uri = item
                path = Gio.File.new_for_uri(uri).get_path() if item.startswith("file://") else None
            else:
                path = item
                uri = path_to_uri(item)
            if uri in existing: continue
            meta = get_local_metadata(path) if path else {"title": uri_to_display_name(uri), "artist": "Stream", "album": "—", "duration": "—"}
            track = Track(uri=uri, title=meta["title"], artist=meta["artist"], album=meta["album"], duration=meta["duration"], kind="local")
            self.playlist.append(track)
            existing.add(uri)
            self._add_row_to_list(track)
            added += 1
        if added:
            self._mark_playlist_dirty()
            if self.current_index < 0 and not self.is_playing: self.play_index(first_new)
            if self.status_label: self._status(f"Añadidas {added} pistas · biblioteca guardada")
            if self.search_entry and (self.search_entry.get_text() or "").strip():
                self._filter_playlist(self.search_entry)

    def _add_row_to_list(self, entry: Track):
        if not self.list_box: return
        row = self._create_song_row(entry.title, entry.artist, entry.album, entry.duration, entry.uri)
        self.list_box.append(row)

    def _highlight_current_row(self):
        if not self.list_box or self.current_index < 0: return
        row = self.list_box.get_row_at_index(self.current_index)
        if row: self.list_box.select_row(row)

    def _update_now_playing_ui(self, uri, title=None, artist=None):
        if uri is None:
            if self.lbl_titulo: self.lbl_titulo.set_label("Ninguna pista")
            if self.lbl_artista: self.lbl_artista.set_label("")
            return
        name = title or uri_to_display_name(uri)
        if self.lbl_titulo: self.lbl_titulo.set_label(name)
        if self.lbl_artista: self.lbl_artista.set_label(artist or "Reproduciendo…")

    def _clear_playlist(self, *_):
        self.stop()
        self.playlist.clear()
        self._missing_tracks = []
        self._current_uri = ""
        self.current_index = -1
        if self.list_box:
            while True:
                row = self.list_box.get_row_at_index(0)
                if row is None: break
                self.list_box.remove(row)
        self._update_now_playing_ui(None)
        self._show_welcome_view()
        self._save_playlist()
        if self.status_label: self._status("Playlist vacía")

    def _filter_playlist(self, entry):
        if not self.list_box: return
        query = (entry.get_text() or "").strip().lower()
        row = self.list_box.get_first_child()
        while row is not None:
            next_row = row.get_next_sibling()
            if not query:
                row.set_visible(True)
            else:
                text = getattr(row, "_search_text", "")
                row.set_visible(query in text)
            row = next_row

    def load_m3u_source(self, source: str):
        self._status("Cargando lista M3U…")
        self._m3u_load_gen += 1
        gen = self._m3u_load_gen
        def worker():
            channels = load_m3u_from_path_or_url(source)
            GLib.idle_add(self._on_m3u_loaded, channels, source, gen)
        threading.Thread(target=worker, daemon=True).start()

    def _on_m3u_loaded(self, channels, source=None, gen=0):
        if self.window is None: return False
        if gen and gen != self._m3u_load_gen: return False
        if not channels:
            self._status("No se encontraron canales en el M3U")
            return False
        self.iptv_channels = channels
        if source: self._iptv_source = source
        # Reset filtros al cargar una lista nueva
        if self.iptv_search_entry:
            self.iptv_search_entry.set_text("")
        self._iptv_filter_query = ""
        self._populate_iptv_groups()
        self._status(f"IPTV: cargando {len(channels)} canales…")
        self._rebuild_iptv_list()
        self._save_iptv_async()
        return False

    def _save_iptv_async(self):
        payload = {
            "source": self._iptv_source or "",
            "channels": list(self.iptv_channels),
        }
        def worker():
            try:
                _write_json_atomic(self.iptv_file, payload)
            except Exception as e:
                print("No se pudo guardar IPTV:", e)
        threading.Thread(target=worker, daemon=True).start()

    def _load_saved_iptv(self):
        try:
            if not self.iptv_file.is_file(): return False
            with open(self.iptv_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict): return False
            channels = data.get("channels") or []
            if not channels: return False
            self._iptv_source = str(data.get("source") or "")
            self.iptv_channels = channels
            self._populate_iptv_groups()
            self._rebuild_iptv_list()
            src = self._iptv_source or "lista guardada"
            self._status(f"IPTV restaurada: {len(channels)} canales ({src})")
        except Exception as e: print("No se pudo cargar IPTV guardada:", e)
        return False

    def _clear_iptv(self, *_):
        self.iptv_channels = []
        self._iptv_source = ""
        if self.iptv_search_entry:
            self.iptv_search_entry.set_text("")
        self._iptv_filter_query = ""
        self._iptv_filter_group = ""
        if self.iptv_group_dropdown is not None:
            self._iptv_populating = True
            try:
                self.iptv_group_dropdown.set_model(Gtk.StringList.new(["Todos"]))
                self.iptv_group_dropdown.set_selected(0)
            finally:
                self._iptv_populating = False
        if self.iptv_list:
            while True:
                row = self.iptv_list.get_row_at_index(0)
                if row is None: break
                self.iptv_list.remove(row)
        try:
            if self.iptv_file.is_file(): self.iptv_file.unlink()
        except Exception as e: print("No se pudo borrar iptv.json:", e)
        self._status("Lista IPTV eliminada")

    def _rebuild_iptv_list(self):
        if not self.iptv_list:
            return
        self._iptv_build_gen += 1
        gen = self._iptv_build_gen
        while True:
            row = self.iptv_list.get_row_at_index(0)
            if row is None:
                break
            self.iptv_list.remove(row)
        self._iptv_build_index = 0
        self._iptv_build_total = len(self.iptv_channels)
        GLib.idle_add(self._rebuild_iptv_list_batch, gen)

    def _rebuild_iptv_list_batch(self, gen):
        if gen != self._iptv_build_gen:
            return False
        if not self.iptv_list or self.window is None:
            return False
        batch = 40
        start = self._iptv_build_index
        end = min(start + batch, self._iptv_build_total)
        for i in range(start, end):
            ch = self.iptv_channels[i]
            row = Gtk.ListBoxRow()
            row.channel = ch
            # Cache para el filtro
            row._iptv_group = ch.get("group") or "Sin grupo"
            row._iptv_search_text = f"{ch.get('name') or ''} {ch.get('group') or ''}".lower()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(12)
            box.set_margin_end(12)
            box.append(Gtk.Image.new_from_icon_name("video-display-symbolic"))
            lbl_name = Gtk.Label(label=ch["name"], hexpand=True, halign=Gtk.Align.START)
            lbl_group = Gtk.Label(label=ch.get("group") or "—")
            lbl_group.add_css_class("dim-label")
            box.append(lbl_name)
            box.append(lbl_group)
            row.set_child(box)
            # Si hay filtro activo, aplicar al vuelo
            if self._iptv_filter_query or self._iptv_filter_group:
                row.set_visible(self._iptv_row_matches(row))
            self.iptv_list.append(row)
        self._iptv_build_index = end
        if end < self._iptv_build_total:
            if self.status_label and (end % 500 == 0 or end == batch):
                self.status_label.set_label(f"IPTV: {end}/{self._iptv_build_total} canales…")
            return True
        if self._iptv_filter_query or self._iptv_filter_group:
            self._apply_iptv_filter()
        else:
            self._status(f"IPTV: {self._iptv_build_total} canales cargados")
        return False

    # ------------------------------------------------------------------
    # Filtros IPTV (texto + categoría)
    # ------------------------------------------------------------------

    def _on_iptv_filter_changed(self, *_):
        if self.iptv_search_entry:
            self._iptv_filter_query = (self.iptv_search_entry.get_text() or "").strip().lower()
        if self._iptv_filter_timeout_id:
            GLib.source_remove(self._iptv_filter_timeout_id)
        self._iptv_filter_timeout_id = GLib.timeout_add(150, self._apply_iptv_filter)

    def _on_iptv_group_changed(self, combo, *_):
        if getattr(self, "_iptv_populating", False):
            return
        idx = combo.get_selected()
        if idx is None or idx <= 0:
            self._iptv_filter_group = ""
        else:
            model = combo.get_model()
            if model and idx < model.get_n_items():
                self._iptv_filter_group = model.get_string(idx)
            else:
                self._iptv_filter_group = ""
        if self._iptv_filter_timeout_id:
            GLib.source_remove(self._iptv_filter_timeout_id)
        self._iptv_filter_timeout_id = GLib.timeout_add(150, self._apply_iptv_filter)

    def _iptv_row_matches(self, row) -> bool:
        if self._iptv_filter_group:
            if getattr(row, "_iptv_group", "") != self._iptv_filter_group:
                return False
        if self._iptv_filter_query:
            if self._iptv_filter_query not in getattr(row, "_iptv_search_text", ""):
                return False
        return True

    def _apply_iptv_filter(self) -> bool:
        self._iptv_filter_timeout_id = 0
        if not self.iptv_list:
            return False
        shown = total = 0
        row = self.iptv_list.get_first_child()
        while row is not None:
            next_row = row.get_next_sibling()
            total += 1
            visible = self._iptv_row_matches(row)
            row.set_visible(visible)
            if visible:
                shown += 1
            row = next_row
        if self.status_label and total > 0:
            if self._iptv_filter_query or self._iptv_filter_group:
                self.status_label.set_label(f"IPTV: {shown} de {total} canales")
            else:
                self.status_label.set_label(f"IPTV: {total} canales")
        return False

    def _populate_iptv_groups(self):
        if self.iptv_group_dropdown is None:
            return
        self._iptv_populating = True
        try:
            groups = sorted(
                {(ch.get("group") or "Sin grupo") for ch in self.iptv_channels},
                key=_natural_key,
            )
            items = ["Todos"] + groups
            model = Gtk.StringList.new(items)
            self.iptv_group_dropdown.set_model(model)
            self.iptv_group_dropdown.set_selected(0)
            self._iptv_filter_group = ""
        finally:
            self._iptv_populating = False

    def on_iptv_row_activated(self, list_box, row):
        ch = getattr(row, "channel", None)
        if not ch or not ch.get("url"): return
        self.play_uri(ch["url"], title=ch["name"], artist=ch.get("group") or "IPTV", live=True)
        self.current_index = -1

    def open_m3u_dialog(self, *_):
        dialog = Gtk.FileDialog(title="Abrir lista M3U / IPTV")
        filt = Gtk.FileFilter()
        filt.set_name("Listas M3U")
        filt.add_pattern("*.m3u")
        filt.add_pattern("*.m3u8")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt)
        allf = Gtk.FileFilter()
        allf.set_name("Todos")
        allf.add_pattern("*")
        filters.append(allf)
        dialog.set_filters(filters)
        dialog.set_default_filter(filt)
        dialog.open(parent=self.window, cancellable=None, callback=self._on_m3u_file_chosen)

    def _on_m3u_file_chosen(self, dialog, result):
        try:
            f = dialog.open_finish(result)
            path = f.get_path()
            if path: self.load_m3u_source(path)
        except GLib.Error: pass

    def open_m3u_url_dialog(self, *_):
        entry = Gtk.Entry(placeholder_text="https://ejemplo.com/lista.m3u")
        entry.set_hexpand(True)
        def handle_response(response):
            if response == "ok":
                url = entry.get_text().strip()
                if url: self.load_m3u_source(url)
        if hasattr(Adw, "AlertDialog"):
            dialog = Adw.AlertDialog(heading="Cargar M3U desde URL", body="Pega la URL de una lista M3U/M3U8 (IPTV)")
            dialog.add_response("cancel", "Cancelar")
            dialog.add_response("ok", "Cargar")
            dialog.set_default_response("ok")
            dialog.set_close_response("cancel")
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_extra_child(entry)
            dialog.connect("response", lambda d, r: handle_response(r))
            dialog.present(self.window)
        else:
            dialog = Adw.MessageDialog(transient_for=self.window, heading="Cargar M3U desde URL", body="Pega la URL de una lista M3U/M3U8 (IPTV)")
            dialog.add_response("cancel", "Cancelar")
            dialog.add_response("ok", "Cargar")
            dialog.set_default_response("ok")
            dialog.set_close_response("cancel")
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_extra_child(entry)
            def on_response(d, r):
                handle_response(r)
                d.destroy()
            dialog.connect("response", on_response)
            dialog.present()

    def _setup_drag_drop(self, widget: Gtk.Widget):
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_drop)
        widget.add_controller(drop)

    def on_drop(self, drop_target, value, x, y):
        files = []
        try:
            for f in value.get_files():
                path = f.get_path()
                if path and (is_media_file(path) or is_m3u_file(path)): files.append(path)
        except Exception as e:
            print("Drop error:", e)
            return False
        if files:
            m3us = [p for p in files if is_m3u_file(p)]
            media = [p for p in files if not is_m3u_file(p)]
            for m in m3us: self.load_m3u_source(m)
            if media: self.add_files(media)
            return True
        return False

    def open_files(self, *_):
        dialog = Gtk.FileDialog(title="Abrir archivos multimedia")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        media_f = Gtk.FileFilter()
        media_f.set_name("Multimedia (audio y video)")
        for ext in sorted(MEDIA_EXTS | PLAYLIST_EXTS):
            if hasattr(media_f, "add_suffix"): media_f.add_suffix(ext.lstrip("."))
            else: media_f.add_pattern(f"*{ext}")
        filters.append(media_f)
        m3u_f = Gtk.FileFilter()
        m3u_f.set_name("Listas M3U")
        m3u_f.add_pattern("*.m3u")
        m3u_f.add_pattern("*.m3u8")
        filters.append(m3u_f)
        all_f = Gtk.FileFilter()
        all_f.set_name("Todos")
        all_f.add_pattern("*")
        filters.append(all_f)
        dialog.set_filters(filters)
        dialog.set_default_filter(media_f)
        dialog.open_multiple(parent=self.window, cancellable=None, callback=self._on_open_multiple)

    def _on_open_multiple(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
            paths = [f.get_path() for f in files if f.get_path()]
            if paths: self.add_files(paths)
        except GLib.Error as e:
            if e.code != Gtk.DialogError.DISMISSED: print("Error abriendo:", e)

    def open_folder(self, *_):
        dialog = Gtk.FileDialog(title="Elegir carpeta de música / video")
        dialog.select_folder(parent=self.window, cancellable=None, callback=self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            path = folder.get_path() if folder else None
            if not path: return
            if self.status_label: self._status(f"Escaneando carpeta: {path}…")
            threading.Thread(target=self._scan_folder_worker, args=(path,), daemon=True).start()
        except GLib.Error as e:
            if getattr(e, "code", None) != Gtk.DialogError.DISMISSED: print("Error carpeta:", e)

    def _scan_folder_worker(self, root: str):
        found, m3us = [], []
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort(key=_natural_key)
                for name in sorted(filenames, key=_natural_key):
                    full = os.path.join(dirpath, name)
                    if is_m3u_file(full): m3us.append(full)
                    elif is_media_file(full): found.append(full)
        except Exception as e: print("Error escaneando carpeta:", e)
        GLib.idle_add(self._apply_folder_scan, found, m3us)

    def _apply_folder_scan(self, found, m3us):
        if self.window is None: return False
        for m in m3us: self.load_m3u_source(m)
        if found: self.add_files(found)
        elif self.status_label: self._status("No se encontraron archivos multimedia")
        return False

    def _setup_shortcuts(self, window: Gtk.Window):
        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self.on_key_pressed)
        window.add_controller(controller)

    def on_key_pressed(self, controller, keyval, keycode, state):
        focus = self.window.get_focus() if self.window else None
        in_entry = isinstance(focus, (Gtk.Editable, Gtk.TextView))
        if in_entry:
            if keyval in (Gdk.KEY_space, Gdk.KEY_Left, Gdk.KEY_Right): return False
            if keyval in (Gdk.KEY_n, Gdk.KEY_N, Gdk.KEY_p, Gdk.KEY_P) and not (state & Gdk.ModifierType.CONTROL_MASK): return False
        if keyval == Gdk.KEY_space: self.toggle_play_pause(); return True
        if keyval == Gdk.KEY_Right: self._seek_relative(+5); return True
        if keyval == Gdk.KEY_Left: self._seek_relative(-5); return True
        if keyval in (Gdk.KEY_n, Gdk.KEY_N): self._on_next(); return True
        if keyval in (Gdk.KEY_p, Gdk.KEY_P): self._on_prev(); return True
        if keyval in (Gdk.KEY_o, Gdk.KEY_O) and (state & Gdk.ModifierType.CONTROL_MASK): self.open_files(); return True
        if keyval in (Gdk.KEY_q, Gdk.KEY_Q) and (state & Gdk.ModifierType.CONTROL_MASK): self.activate_action("quit", None); return True
        if keyval == Gdk.KEY_F11: self._toggle_fullscreen(); return True
        if keyval == Gdk.KEY_Escape and (self._is_fullscreen or (self.window and self.window.is_fullscreen())):
            self._toggle_fullscreen(); return True
        if keyval == Gdk.KEY_f and not (state & Gdk.ModifierType.CONTROL_MASK):
            self._toggle_fullscreen(); return True
        return False

    # ------------------------------------------------------------------
    # Vistas
    # ------------------------------------------------------------------

    def create_inicio_view(self) -> Gtk.Widget:
        self.inicio_stack = Gtk.Stack()
        self.inicio_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        welcome_scrolled = Gtk.ScrolledWindow()
        welcome_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.set_margin_top(28)
        box.set_margin_bottom(28)
        box.set_margin_start(32)
        box.set_margin_end(32)
        title = Gtk.Label(label="Argent Multimedia Player")
        title.set_halign(Gtk.Align.START)
        title.add_css_class("title-1")
        title.add_css_class("argent-title")
        box.append(title)
        sub = Gtk.Label(label=f"Reproductor local · IPTV · YouTube (720p por defecto)  ·  v{self.APP_VERSION}")
        sub.set_halign(Gtk.Align.START)
        sub.add_css_class("dim-label")
        box.append(sub)
        group = Adw.PreferencesGroup(title="Inicio rápido")
        for t, s, icon in [
            ("Arrastrá archivos o listas M3U", "Soltá sobre la ventana", "folder-download-symbolic"),
            ("Biblioteca", "Canciones y playlist local", "audio-x-generic-symbolic"),
            ("IPTV / M3U", "Canales y streams en vivo", "network-workgroup-symbolic"),
            ("YouTube", "Buscador nativo de videos (yt-dlp + mpv)", "video-x-generic-symbolic"),
        ]:
            row = Adw.ActionRow(title=t, subtitle=s)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            group.add(row)
        box.append(group)
        self.status_label = Gtk.Label(label="Listo")
        self.status_label.add_css_class("dim-label")
        self.status_label.set_halign(Gtk.Align.START)
        box.append(self.status_label)
        if not HAS_MUTAGEN:
            note = Gtk.Label(label="Tip: instala python3-mutagen para metadata real.")
            note.add_css_class("dim-label")
            note.set_halign(Gtk.Align.START)
            box.append(note)
        welcome_scrolled.set_child(box)
        self.inicio_stack.add_named(welcome_scrolled, "welcome")
        player_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        player_box.add_css_class("view")
        player_box.set_vexpand(True)
        player_box.set_hexpand(True)
        self.video_picture = Gtk.Picture()
        self.video_picture.set_vexpand(True)
        self.video_picture.set_hexpand(True)
        self.video_picture.set_can_shrink(True)
        self.video_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        try:
            click = Gtk.GestureClick.new()
            click.set_button(1)
            click.connect("pressed", self._on_video_click)
            self.video_picture.add_controller(click)
        except Exception: pass
        video_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        video_frame.add_css_class("video-area")
        video_frame.set_vexpand(True)
        video_frame.append(self.video_picture)
        player_box.append(video_frame)
        self.inicio_stack.add_named(player_box, "player")
        return self.inicio_stack

    def create_musica_view(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15)
        box.set_margin_top(24)
        box.set_margin_start(28)
        box.set_margin_end(28)
        lbl = Gtk.Label(label="Biblioteca / Playlist")
        lbl.set_halign(Gtk.Align.START)
        lbl.add_css_class("title-1")
        box.append(lbl)
        action = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_open = Gtk.Button(label="Abrir archivos")
        btn_open.add_css_class("suggested-action")
        btn_open.connect("clicked", self.open_files)
        btn_folder = Gtk.Button(label="Abrir carpeta")
        btn_folder.connect("clicked", self.open_folder)
        btn_clear = Gtk.Button(label="Limpiar")
        btn_clear.connect("clicked", self._clear_playlist)
        self.search_entry = Gtk.SearchEntry(placeholder_text="Buscar en la playlist…")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._filter_playlist)
        action.append(btn_open)
        action.append(btn_folder)
        action.append(btn_clear)
        action.append(self.search_entry)
        box.append(action)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        self.list_box = Gtk.ListBox()
        self.list_box.add_css_class("boxed-list")
        self.list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_box.connect("row-activated", self.on_row_activated)
        scrolled.set_child(self.list_box)
        box.append(scrolled)
        return box

    def create_iptv_view(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15)
        box.set_margin_top(24)
        box.set_margin_start(28)
        box.set_margin_end(28)
        lbl = Gtk.Label(label="IPTV / Listas M3U")
        lbl.set_halign(Gtk.Align.START)
        lbl.add_css_class("title-1")
        box.append(lbl)
        action = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_file = Gtk.Button(label="Abrir M3U local")
        btn_file.add_css_class("suggested-action")
        btn_file.connect("clicked", self.open_m3u_dialog)
        btn_url = Gtk.Button(label="Cargar desde URL")
        btn_url.connect("clicked", self.open_m3u_url_dialog)
        btn_clear = Gtk.Button(label="Borrar lista")
        btn_clear.set_tooltip_text("Quita los canales y borra la lista guardada")
        btn_clear.connect("clicked", self._clear_iptv)
        action.append(btn_file)
        action.append(btn_url)
        action.append(btn_clear)
        box.append(action)

        # ---- Filtros: búsqueda por texto + filtro por categoría ----
        filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.iptv_search_entry = Gtk.SearchEntry(placeholder_text="Buscar canal o grupo…")
        self.iptv_search_entry.set_hexpand(True)
        self.iptv_search_entry.connect("search-changed", self._on_iptv_filter_changed)
        filter_row.append(self.iptv_search_entry)

        group_lbl = Gtk.Label(label="Categoría")
        group_lbl.add_css_class("dim-label")
        filter_row.append(group_lbl)

        if hasattr(Gtk, "DropDown"):
            model = Gtk.StringList.new(["Todos"])
            dd = Gtk.DropDown(model=model)
            dd.set_size_request(180, -1)
            dd.connect("notify::selected", self._on_iptv_group_changed)
            self.iptv_group_dropdown = dd
            filter_row.append(dd)
        box.append(filter_row)

        hint = Gtk.Label(label="Soporta M3U y M3U8 (HLS). La lista se guarda y restaura automáticamente.")
        hint.add_css_class("dim-label")
        hint.set_halign(Gtk.Align.START)
        box.append(hint)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        self.iptv_list = Gtk.ListBox()
        self.iptv_list.add_css_class("boxed-list")
        self.iptv_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.iptv_list.connect("row-activated", self.on_iptv_row_activated)
        scrolled.set_child(self.iptv_list)
        box.append(scrolled)
        return box

    def create_youtube_view(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15)
        box.set_margin_top(24)
        box.set_margin_start(28)
        box.set_margin_end(28)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        lbl = Gtk.Label(label="YouTube / Streams")
        lbl.set_halign(Gtk.Align.START)
        lbl.add_css_class("title-1")
        lbl.set_hexpand(True)
        header.append(lbl)

        q_lbl = Gtk.Label(label="Calidad:")
        q_lbl.add_css_class("dim-label")
        header.append(q_lbl)

        current_q = str(self.settings.get("yt_max_res", "720"))
        q_keys = ("480", "720", "1080", "auto")
        q_labels = ("480p", "720p (Recomendado)", "1080p", "Auto (Puede fallar)")
        if current_q not in q_keys: current_q = "720"

        if hasattr(Gtk, "DropDown"):
            model = Gtk.StringList.new(list(q_labels))
            dropdown = Gtk.DropDown(model=model)
            dropdown.set_selected(q_keys.index(current_q))
            dropdown.connect("notify::selected", self._on_yt_quality_changed)
            self.yt_quality_dropdown = dropdown
            header.append(dropdown)
        box.append(header)

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.yt_search_entry = Gtk.SearchEntry(placeholder_text="Buscar videos, canales o películas...")
        self.yt_search_entry.set_hexpand(True)
        self.yt_search_entry.connect("activate", self._on_yt_search_activate)
        search_box.append(self.yt_search_entry)

        btn_search = Gtk.Button(label="Buscar")
        btn_search.add_css_class("suggested-action")
        btn_search.connect("clicked", lambda *_: self._on_yt_search_activate(self.yt_search_entry))
        search_box.append(btn_search)

        self.yt_spinner = Gtk.Spinner()
        self.yt_spinner.set_visible(False)
        search_box.append(self.yt_spinner)
        box.append(search_box)

        hint = Gtk.Label(label="Doble clic en un resultado para abrirlo en mpv.")
        hint.add_css_class("dim-label")
        hint.set_halign(Gtk.Align.START)
        box.append(hint)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        self.yt_results_list = Gtk.ListBox()
        self.yt_results_list.add_css_class("boxed-list")
        self.yt_results_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.yt_results_list.connect("row-activated", self._on_yt_row_activated)

        scrolled.set_child(self.yt_results_list)
        box.append(scrolled)
        return box

    def create_eq_view(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(20)
        box.set_margin_bottom(16)
        box.set_margin_start(24)
        box.set_margin_end(24)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        lbl = Gtk.Label(label="Equalizador")
        lbl.set_halign(Gtk.Align.START)
        lbl.add_css_class("title-1")
        lbl.set_hexpand(True)
        header.append(lbl)
        sw = Gtk.Switch()
        sw.set_active(bool(self.settings.get("eq_enabled", True)))
        sw.set_valign(Gtk.Align.CENTER)
        sw.connect("notify::active", self._on_eq_enabled_toggled)
        lab_on = Gtk.Label(label="Activo")
        lab_on.add_css_class("dim-label")
        header.append(lab_on)
        header.append(sw)
        box.append(header)
        preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        lab_modo = Gtk.Label(label="Modo")
        lab_modo.add_css_class("dim-label")
        preset_row.append(lab_modo)
        names = list(self.EQ_PRESETS.keys())
        current = str(self.settings.get("eq_preset", "Plano"))
        if current not in names: current = "Plano"
        if hasattr(Gtk, "DropDown"):
            model = Gtk.StringList.new(names)
            dd = Gtk.DropDown(model=model)
            dd.set_selected(names.index(current))
            dd.connect("notify::selected", self._on_eq_preset_changed)
            self._eq_preset_dropdown = dd
            preset_row.append(dd)
        else:
            combo = Gtk.ComboBoxText()
            for n in names: combo.append(n, n)
            combo.set_active_id(current)
            combo.connect("changed", self._on_eq_preset_changed)
            self._eq_preset_dropdown = combo
            preset_row.append(combo)
        btn_reset = Gtk.Button(label="Reset")
        btn_reset.connect("clicked", self._on_eq_reset)
        preset_row.append(btn_reset)
        box.append(preset_row)
        hint = Gtk.Label(label="10 bandas · los cambios se aplican al instante en la reproducción local/IPTV")
        hint.add_css_class("dim-label")
        hint.set_halign(Gtk.Align.START)
        box.append(hint)
        bands_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bands_box.set_halign(Gtk.Align.CENTER)
        bands_box.set_hexpand(True)
        bands = self.settings.get("eq_bands") or [0.0] * 10
        if not isinstance(bands, list) or len(bands) != 10: bands = [0.0] * 10
        self._eq_scales = []
        for i, lab in enumerate(self.EQ_BAND_LABELS):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            col.set_hexpand(True)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -24, 12, 0.5)
            scale.set_inverted(True)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.BOTTOM)
            scale.set_size_request(-1, 180)
            scale.set_value(float(bands[i]))
            scale.connect("value-changed", lambda s, idx=i: self._on_eq_band_changed(s, idx))
            self._eq_scales.append(scale)
            col.append(scale)
            fl = Gtk.Label(label=lab)
            fl.add_css_class("dim-label")
            col.append(fl)
            bands_box.append(col)
        box.append(bands_box)
        return box

    # ------------------------------------------------------------------
    # YouTube Search Logic
    # ------------------------------------------------------------------

    def _on_yt_quality_changed(self, combo, *_):
        if getattr(self, "_ui_ready", False) is not True: return
        if hasattr(combo, "get_selected"):
            idx = combo.get_selected()
            keys = ("480", "720", "1080", "auto")
            key = keys[idx] if 0 <= idx < len(keys) else "720"
        else:
            key = combo.get_active_id() or "720"
        self.settings["yt_max_res"] = key
        self._save_settings()
        self._status(f"Límite YouTube: {key}p" if key != "auto" else "Límite YouTube: Auto")

    def _on_yt_search_activate(self, entry, *_):
        query = entry.get_text().strip()
        if not query: return
        if not shutil.which("yt-dlp"):
            self._status("yt-dlp no está instalado. Ejecutá: sudo apt install yt-dlp")
            return

        while True:
            row = self.yt_results_list.get_row_at_index(0)
            if row is None: break
            self.yt_results_list.remove(row)

        self.yt_spinner.start()
        self.yt_spinner.set_visible(True)

        self._yt_search_gen += 1
        gen = self._yt_search_gen

        def worker():
            try:
                cmd = ["yt-dlp", f"ytsearch15:{query}", "--dump-json", "--flat-playlist"]
                result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=20)
                items = []
                for line in result.stdout.strip().split("\n"):
                    if not line: continue
                    data = json.loads(line)
                    items.append({
                        "id": data.get("id", ""),
                        "title": data.get("title", "Sin título"),
                        "channel": data.get("channel", data.get("uploader", "Desconocido")),
                        "duration": data.get("duration_string", ""),
                        "thumbnail": data.get("thumbnails", [{}])[-1].get("url", "") if data.get("thumbnails") else ""
                    })
                GLib.idle_add(self._on_yt_results, items, gen)
            except Exception as e:
                GLib.idle_add(self._on_yt_search_error, str(e), gen)

        threading.Thread(target=worker, daemon=True).start()

    def _on_yt_search_error(self, error_msg, gen):
        if gen != self._yt_search_gen:
            return False
        self.yt_spinner.stop()
        self.yt_spinner.set_visible(False)
        self._status(f"Error buscando: {error_msg}")
        return False

    def _on_yt_results(self, items, gen):
        if gen != self._yt_search_gen:
            return False
        if self.window is None or self.yt_results_list is None:
            return False
        self.yt_spinner.stop()
        self.yt_spinner.set_visible(False)

        if not items:
            self._status("No se encontraron resultados.")
            return False

        for item in items:
            row = Gtk.ListBoxRow()
            row.yt_id = item["id"]
            row.yt_title = item["title"]

            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            hbox.set_margin_top(8)
            hbox.set_margin_bottom(8)
            hbox.set_margin_start(12)
            hbox.set_margin_end(12)

            img = Gtk.Picture()
            img.set_size_request(160, 90)
            img.set_content_fit(Gtk.ContentFit.COVER)
            img.add_css_class("yt-thumb")
            hbox.append(img)

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.set_valign(Gtk.Align.CENTER)
            lbl_t = Gtk.Label(label=item["title"], halign=Gtk.Align.START, wrap=True, max_width_chars=60, lines=2)
            lbl_t.add_css_class("heading")
            lbl_c = Gtk.Label(label=f"{item['channel']} · {item['duration']}", halign=Gtk.Align.START)
            lbl_c.add_css_class("dim-label")
            vbox.append(lbl_t)
            vbox.append(lbl_c)
            hbox.append(vbox)

            row.set_child(hbox)
            self.yt_results_list.append(row)

            # Usar el ID para una URL de miniatura confiable (JPEG, siempre disponible)
            thumb_url = f"https://i.ytimg.com/vi/{item['id']}/hqdefault.jpg"
            threading.Thread(
                target=self._load_thumbnail,
                args=(img, thumb_url, gen),
                daemon=True,
            ).start()

        return False

    def _apply_thumbnail(self, img_widget, texture, gen):
        if gen != self._yt_search_gen:
            return False
        try:
            # Quitamos la verificación de get_root() para que no se descarte
            # si la UI no se dibujó al 100% todavía.
            img_widget.set_paintable(texture)
        except Exception as e:
            print(f"Error aplicando miniatura: {e}")
        return False

    def _load_thumbnail(self, img_widget, url, gen):
        if gen != self._yt_search_gen:
            return
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=10) as response:
                data = response.read()
            if not HAS_PIXBUF:
                return
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            GLib.idle_add(self._apply_thumbnail, img_widget, texture, gen)
        except Exception as e:
            print(f"Error cargando miniatura ({url}): {e}")

    def _on_yt_row_activated(self, listbox, row):
        yt_id = getattr(row, "yt_id", None)
        title = getattr(row, "yt_title", "")
        if not yt_id: return
        url = f"https://www.youtube.com/watch?v={yt_id}"
        self._open_uri_in_mpv(url, title=title)

    def _open_uri_in_mpv(self, uri: str, title: str = "", position_sec: float = 0.0):
        if not uri: return
        if not shutil.which("mpv"):
            self._status("mpv no instalado. sudo apt install mpv")
            print("mpv no encontrado en PATH")
            return
        if self.is_playing:
            try: self.toggle_play_pause()
            except Exception: pass
        cmd = ["mpv"]
        if shutil.which("yt-dlp"):
            cmd.append("--ytdl=yes")
            if "youtube.com" in uri or "youtu.be" in uri:
                res = self.settings.get("yt_max_res", "720")
                if res != "auto":
                    cmd.append(f"--ytdl-format=bestvideo[height<={res}]+bestaudio/best[height<={res}]")

        if position_sec > 0: cmd.append(f"--start={position_sec:.0f}")
        if title: cmd.append(f"--force-media-title={title}")
        cmd.append(uri)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._status(f"Reproduciendo en mpv: {title or uri[:60]}")
            print(f"mpv lanzado: {' '.join(cmd)}")
            self._show_mpv_fallback_toast(uri, title)
            GLib.timeout_add(2000, self._check_mpv_status, proc)
        except Exception as e:
            print("Error lanzando mpv:", e)
            self._status(f"No se pudo abrir mpv: {e}")

    def _check_mpv_status(self, proc) -> bool:
        try:
            rc = proc.poll()
            if rc is not None and rc != 0:
                self._status(f"mpv terminó con código {rc}")
        except Exception:
            pass
        return False

    def _show_mpv_fallback_toast(self, uri: str, title: str):
        if not self.toast_overlay: return
        toast = Adw.Toast.new(f"'{title or uri_to_display_name(uri)}' abierto en mpv.")
        toast.set_timeout(6)
        try:
            toast.set_button_label("Reintentar en la app")
            toast.connect("button-clicked", lambda *_: self._retry_uri_in_app(uri, title))
        except Exception: pass
        self.toast_overlay.add_toast(toast)

    def _retry_uri_in_app(self, uri: str, title: str = ""):
        self._last_play_uri = ""
        self._last_play_time = 0
        self._unmark_uri_crashed(uri)
        self._status("Reintentando con GStreamer…")
        self.play_uri(uri, title=title)

    # ------------------------------------------------------------------
    # Barra de controles
    # ------------------------------------------------------------------

    def create_controls_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bar.set_margin_top(10)
        bar.set_margin_bottom(10)
        bar.set_margin_start(16)
        bar.set_margin_end(16)
        bar.add_css_class("argent-player-bar")
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_size_request(200, -1)
        self.lbl_titulo = Gtk.Label(label="Ninguna pista", halign=Gtk.Align.START)
        self.lbl_titulo.add_css_class("heading")
        self.lbl_titulo.set_ellipsize(3)
        self.lbl_titulo.set_max_width_chars(28)
        self.lbl_artista = Gtk.Label(label="", halign=Gtk.Align.START)
        self.lbl_artista.add_css_class("dim-label")
        self.lbl_artista.set_ellipsize(3)
        self.lbl_artista.set_max_width_chars(28)
        info.append(self.lbl_titulo)
        info.append(self.lbl_artista)
        bar.append(info)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        center.set_hexpand(True)
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_prev = Gtk.Button.new_from_icon_name("media-skip-backward-symbolic")
        btn_prev.set_tooltip_text("Anterior")
        btn_prev.connect("clicked", self._on_prev)
        btn_rw = Gtk.Button.new_from_icon_name("media-seek-backward-symbolic")
        btn_rw.set_tooltip_text("Retroceder 10s (←)")
        btn_rw.connect("clicked", lambda *_: self._seek_relative(-10))
        self.btn_play = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self.btn_play.add_css_class("suggested-action")
        self.btn_play.add_css_class("pill")
        self.btn_play.add_css_class("argent-play")
        self.btn_play.set_tooltip_text("Reproducir / Pausar (Espacio)")
        self.btn_play.connect("clicked", lambda *_: self.toggle_play_pause())
        btn_ff = Gtk.Button.new_from_icon_name("media-seek-forward-symbolic")
        btn_ff.set_tooltip_text("Adelantar 10s (→)")
        btn_ff.connect("clicked", lambda *_: self._seek_relative(+10))
        btn_next = Gtk.Button.new_from_icon_name("media-skip-forward-symbolic")
        btn_next.set_tooltip_text("Siguiente")
        btn_next.connect("clicked", self._on_next)
        self.btn_shuffle = Gtk.ToggleButton()
        self.btn_shuffle.set_icon_name("media-playlist-shuffle-symbolic")
        self.btn_shuffle.set_tooltip_text("Modo aleatorio")
        self.btn_shuffle.connect("toggled", self._on_shuffle_toggled)
        btn_open = Gtk.Button.new_from_icon_name("document-open-symbolic")
        btn_open.set_tooltip_text("Abrir archivos (Ctrl+O)")
        btn_open.connect("clicked", self.open_files)
        btn_reset = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        btn_reset.set_tooltip_text("Reiniciar motor de video")
        btn_reset.connect("clicked", self._on_reset_pipeline)
        self.btn_fullscreen = Gtk.Button.new_from_icon_name("view-fullscreen-symbolic")
        self.btn_fullscreen.set_tooltip_text("Pantalla completa (F / F11)")
        self.btn_fullscreen.connect("clicked", self._toggle_fullscreen)
        btn_box.append(btn_prev)
        btn_box.append(btn_rw)
        btn_box.append(self.btn_play)
        btn_box.append(btn_ff)
        btn_box.append(btn_next)
        btn_box.append(self.btn_shuffle)
        btn_box.append(btn_open)
        btn_box.append(btn_reset)
        btn_box.append(self.btn_fullscreen)
        progress = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.lbl_time_curr = Gtk.Label(label="00:00")
        self.slider_progress = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.slider_progress.set_hexpand(True)
        self.slider_progress.set_draw_value(False)
        self.slider_progress.connect("change-value", self._on_seek_change)
        self.lbl_time_total = Gtk.Label(label="00:00")
        progress.append(self.lbl_time_curr)
        progress.append(self.slider_progress)
        progress.append(self.lbl_time_total)
        center.append(btn_box)
        center.append(progress)
        bar.append(center)
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        right.set_halign(Gtk.Align.END)
        right.set_size_request(380, -1)

        self.btn_mute = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self.btn_mute.set_tooltip_text("Silenciar / restaurar")
        self.btn_mute.connect("clicked", self._toggle_mute)
        self.slider_vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.slider_vol.set_value(70)
        self.slider_vol.set_size_request(100, -1)
        self.slider_vol.set_draw_value(False)
        self.slider_vol.connect("value-changed", self._on_volume_changed)
        right.append(self.btn_mute)
        right.append(self.slider_vol)
        bar.append(right)
        return bar

    def _on_shuffle_toggled(self, btn):
        self.is_shuffle = btn.get_active()

    def _on_reset_pipeline(self, *_):
        was_playing = self.is_playing
        current = self._current_uri
        self._pending_uri = None
        self._set_playing(False)
        if self.slider_progress: self.slider_progress.set_value(0)
        if self.lbl_time_curr: self.lbl_time_curr.set_label("00:00")
        if self.lbl_time_total: self.lbl_time_total.set_label("00:00")
        self.duration = 0
        if was_playing and current: self._pending_uri = current
        self._force_pipeline_reset(reason="Motor de video reiniciado")

    def _on_prev(self, *_):
        if not self.playlist: return
        if self.current_index > 0: self.play_index(self.current_index - 1)
        else: self.play_index(0)

    def _on_next(self, *_):
        if not self.playlist: return
        if self.is_shuffle and len(self.playlist) > 1:
            next_idx = self.current_index
            while next_idx == self.current_index:
                next_idx = random.randint(0, len(self.playlist) - 1)
            self.play_index(next_idx)
        else:
            if self.current_index + 1 < len(self.playlist): self.play_index(self.current_index + 1)

    def _on_seek_change(self, scale, scroll, value):
        if self.duration <= 0: return False
        self._user_seeking = True
        if self._seek_timeout_id: GLib.source_remove(self._seek_timeout_id)
        self._pending_seek_value = float(value)
        self._seek_timeout_id = GLib.timeout_add(40, self._apply_pending_seek)
        return False

    def _apply_pending_seek(self):
        self._seek_timeout_id = 0
        if self._pending_seek_value is not None:
            self.seek(self._pending_seek_value)
            self._pending_seek_value = None
        GLib.timeout_add(400, self._clear_user_seeking)
        return False

    def _clear_user_seeking(self):
        self._user_seeking = False
        return False

    def _on_volume_changed(self, scale):
        value = scale.get_value()
        if value > 0: self._prev_volume = value
        self.set_volume(value)
        self._update_mute_icon(value)

    def _toggle_mute(self, *_):
        if not self.slider_vol: return
        if self.slider_vol.get_value() > 0:
            self._prev_volume = self.slider_vol.get_value()
            self.slider_vol.set_value(0)
        else: self.slider_vol.set_value(self._prev_volume or 70)

    def _update_mute_icon(self, value: float):
        if not self.btn_mute: return
        if value <= 0: icon = "audio-volume-muted-symbolic"
        elif value < 33: icon = "audio-volume-low-symbolic"
        elif value < 66: icon = "audio-volume-medium-symbolic"
        else: icon = "audio-volume-high-symbolic"
        self.btn_mute.set_icon_name(icon)

    def _create_song_row(self, titulo, artista, album, duracion, uri) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.uri = uri
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        row_box.set_margin_top(10)
        row_box.set_margin_bottom(10)
        row_box.set_margin_start(14)
        row_box.set_margin_end(14)
        row_box.append(Gtk.Image.new_from_icon_name("audio-x-generic-symbolic"))
        lbl_t = Gtk.Label(label=titulo, hexpand=True, halign=Gtk.Align.START)
        lbl_art = Gtk.Label(label=artista, hexpand=True, halign=Gtk.Align.START)
        lbl_art.add_css_class("dim-label")
        lbl_alb = Gtk.Label(label=album, hexpand=True, halign=Gtk.Align.START)
        lbl_alb.add_css_class("dim-label")
        lbl_dur = Gtk.Label(label=duracion)
        lbl_dur.add_css_class("dim-label")
        row_box.append(lbl_t)
        row_box.append(lbl_art)
        row_box.append(lbl_alb)
        row_box.append(lbl_dur)
        row.set_child(row_box)
        row._search_text = f"{titulo} {artista} {album}".lower()
        return row

    def on_row_activated(self, list_box, row):
        uri = getattr(row, "uri", None)
        if uri:
            for i, item in enumerate(self.playlist):
                if item.uri == uri:
                    self.play_index(i)
                    return
        self.play_index(row.get_index())

    def load_custom_css(self):
        css = """
        scale trough { min-height: 4px; border-radius: 3px; }
        scale slider { min-height: 14px; min-width: 14px; margin: -5px; }
        scale highlight { background-color: #3DA9FC; border-radius: 3px; }
        list row { transition: background-color 150ms ease; border-radius: 8px; }
        list row:hover { background-color: alpha(@theme_fg_color, 0.06); }
        list row:selected { background-color: alpha(#3DA9FC, 0.22); }
        .video-area { background-color: #000000; border-radius: 10px; margin: 6px; }
        .video-area-fs { background-color: #000000; border-radius: 0; margin: 0; }
        .argent-toolbar { padding: 2px 0; }
        .argent-player-bar { background-color: alpha(@theme_bg_color, 0.92); }
        .argent-title { font-weight: 700; font-size: 1.12em; letter-spacing: 0.04em; color: #3DA9FC; }
        .argent-version { font-size: 0.85em; opacity: 0.55; }
        .argent-accent { color: #3DA9FC; }
        button.argent-play { min-width: 44px; min-height: 36px; }
        .sidebar { min-width: 168px; }
        .yt-thumb { border-radius: 8px; background-color: #222; }
        """.encode("utf-8")
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_SIGNAL_NAMES = {
    signal.SIGSEGV: "SIGSEGV",
    signal.SIGABRT: "SIGABRT",
    signal.SIGBUS: "SIGBUS",
    signal.SIGFPE: "SIGFPE",
    signal.SIGTRAP: "SIGTRAP",
}


def _fatal_signal_handler(signum, frame):
    """
    Handler de señales fatales. NO sale del proceso.

    Registra el evento en signals.log y en stderr, escribe el crash_marker
    sólo para señales realmente fatales, y vuelve normalmente. El main loop
    continúa (mismo comportamiento efectivo que sys.exit() en signal_notify
    de GI, que tragaba el SystemExit).
    """
    try:
        name = _SIGNAL_NAMES.get(signum, str(signum))
        try:
            os.write(2, f"\n[signal] {name} ({signum}) recibido\n".encode())
        except Exception:
            pass
        if _signal_log_fd is not None:
            try:
                msg = (time.strftime("%Y-%m-%dT%H:%M:%S")
                       + f" signal={signum} name={name}\n").encode()
                os.write(_signal_log_fd, msg)
            except Exception:
                pass
        if _crash_marker_fd is not None and signum in (signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS):
            try:
                payload = ('{"signal": %d, "name": "%s", "timestamp": %.3f}\n'
                           % (int(signum), name, time.time())).encode()
                os.write(_crash_marker_fd, payload)
                os.fsync(_crash_marker_fd)
            except Exception:
                pass
        try:
            faulthandler.dump_traceback()
        except Exception:
            pass
    except Exception:
        pass


if __name__ == "__main__":
    faulthandler.enable()
    for _sig in (signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS, signal.SIGFPE, signal.SIGTRAP):
        try:
            signal.signal(_sig, _fatal_signal_handler)
        except Exception:
            pass
    app = ArgentMultimediaPlayer()
    sys.exit(app.run(sys.argv))
