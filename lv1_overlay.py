"""
DiGiCo Preamp Overlay for Waves eMotion LV1.

Sits as a frameless, always-on-top, transparent PyQt6 window over LV1
(full-screen, 1920×1080 on the primary monitor).

For each of LV1's 8 pages (layers) it covers the preamp section of all
16 channel strips with:
  • Phantom power toggle button  ("48V" style)
  • DiGiCo channel name
  • Gain value (numeric)
  • Gain rotary knob

Transparent "pass-through" layer-switch buttons sit over LV1's page-selector
buttons so one click updates both LV1 and the overlay simultaneously.

A semi-transparent ⚙ button over the LV1 logo opens the settings panel.

Patch mapping is read from LV1's live autosave database. LV1 stores it
under \\Users\\Public\\Waves Audio\\eMotion\\Sessions\\CurrentLV1.dat on
whichever drive the user pointed Waves at — usually C:, sometimes D:.

Architecture (same thread model as digico_multichannel.py):
    Main thread  — Qt GUI
    OscThread    — OscEngine
    HbThread     — HeartbeatWorker
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import math
import os
import sys
import time
import traceback
from pathlib import Path
from typing import List, Optional

__version__ = "0.1.1"

# Base directory for user-writable files (log, settings).  When running as a
# PyInstaller bundle sys.frozen is True — anchor to the .exe location so
# settings persist next to the binary and the app stays fully portable.
if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).parent


def _bootstrap_single_instance() -> None:
    """Single-instance guard using a Windows named mutex.

    The kernel releases the mutex automatically the instant the process ends
    (crash, kill, or clean exit) — no stale files, no PID to clean up.
    """
    if sys.platform != "win32":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
    kernel32.CreateMutexW.restype  = wt.HANDLE
    handle = kernel32.CreateMutexW(None, False, "Local\\DiGiCoLV1Overlay_v3")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)
    # Keep handle alive for the entire process lifetime so the mutex stays held.
    globals()["_OVERLAY_MUTEX_HANDLE"] = handle


_bootstrap_single_instance()

_LOG_PATH = _BASE_DIR / "lv1_overlay.log"
_fh = logging.FileHandler(str(_LOG_PATH), mode="w", encoding="utf-8", delay=False)
_fh.setFormatter(logging.Formatter(
    "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
))
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fh.formatter)
log = logging.getLogger("overlay")
log.setLevel(logging.INFO)
log.addHandler(_fh)
log.addHandler(_sh)
log.info("=== lv1_overlay starting — log: %s ===", _LOG_PATH)

from PyQt6.QtCore import (
    QFileSystemWatcher, QPoint, QRect, QRectF, QSize, Qt, QThread, QTimer,
    pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import (
    QColor, QCursor, QFont, QFontMetrics, QIntValidator, QPainter, QPainterPath,
    QPen, QBrush, QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDial, QDialogButtonBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from digico_multichannel import (
    OscBridge, OscEngine, HeartbeatWorker,
    ChannelStateStore,
    get_interfaces,
    _osc_gain, _osc_phantom,
    DEFAULT_CONSOLE_IP, DEFAULT_CONSOLE_PORT, DEFAULT_LISTEN_PORT, DEFAULT_N_CHANNELS,
)
import digico_multichannel as _dmc   # live reference so GAIN_MIN/MAX auto-expand works
from lv1_session import Lv1SessionParser, find_autosave

# ── Settings persistence ───────────────────────────────────────────────────────

SETTINGS_FILE = _BASE_DIR / "overlay_settings.json"
PROFILES_FILE = _BASE_DIR / "overlay_profiles.json"

_DEFAULT_SETTINGS: dict = {
    # DiGiCo connection — default to unset (0.0.0.0) so the user must enter
    # the console IP on first run rather than seeing a stale preset address.
    "console_ip":    "0.0.0.0",
    "console_port":  DEFAULT_CONSOLE_PORT,
    "listen_port":   DEFAULT_LISTEN_PORT,
    "bind_ip":       "0.0.0.0",
    "n_channels":    DEFAULT_N_CHANNELS,
    # LV1 autosave — left empty so find_autosave() picks the live path.
    # LV1 normally writes CurrentLV1.dat to C:\Users\Public\Waves Audio\
    # eMotion\Sessions, but users who put Waves on D: have it there instead.
    "lv1_db":        "",
    # LV1 model selection
    # Overlay appearance
    "opacity":       85,   # 0-100 %
    "gain_min":       0.0, # dB floor   — auto-contracts if console reports lower values
    "gain_max":      60.0, # dB ceiling — auto-expands  if console reports higher values
    "sync_names":    False, # send LV1 channel names to DiGiCo on connect/session change
    "load_preamp_with_session": False, # restore saved gain+phantom when session loads
    "verbose_logging": False, # enable debug-level logging for field diagnostics
    # Geometry — calibrated defaults for 1920×1080 LV1 v17 (Factory mixer 1)
    "preamp_x0":     158,   # x of first channel strip left edge
    "preamp_y0":     170,   # y of preamp section top edge
    "preamp_total_w": 1488, # exact width of 16-strip preamp row
    "cell_w":        93,    # width per channel strip (preamp_total_w // 16)
    "cell_h":        103,   # height of preamp overlay cell
    "layer_x":       1773,  # x of layer buttons (right panel)
    "layer_y0":      422,   # y of first layer button
    "layer_w":       93,    # width of layer buttons
    "layer_h":       81,    # height of each layer button
    "layer_gap":     0,     # gap between layer buttons
    "settings_x":    1655,  # x of settings button
    "settings_y":    7,     # y of settings button
    "settings_w":    218,   # width of settings button
    "settings_h":    34,    # height of settings button
    # Tab / spill pixel detection (screen coordinates, 1920×1080 baseline)
    "mixer1_tab_pos": [731, 39],
    "mixer2_tab_pos": [819, 23],
    "spill_btn_pos":  [1818, 339],
    "spill_off_color": None,  # captured at spill calibration on first run
    "spill_on_threshold": 45, # min RGB distance from off-color → spill active
}


# ── Preamp save/load ──────────────────────────────────────────────────────────

SAVES_DIR = _BASE_DIR / "preamp_saves"


def _preamp_save_path(session_name: str, autosave: bool = False) -> Path:
    """Return the JSON save path for the given session name."""
    safe = "".join(c if c.isalnum() or c in " _-." else "_" for c in session_name).strip()
    suffix = ".autosave.json" if autosave else ".json"
    return SAVES_DIR / f"{safe}{suffix}"


def _build_preamp_channels(store: "ChannelStateStore") -> dict:
    channels: dict = {}
    for ch, state in store._data.items():
        channels[str(ch)] = {"gain": state.gain_db, "phantom": state.phantom}
    return channels


def save_preamp_autosave(session_name: str, store: "ChannelStateStore",
                         emo_mtime: float = 0.0) -> None:
    """Write the working preamp state (runs every ~1 s). Stores .emo mtime so
    the next startup can decide whether to load this file or the explicit save."""
    if not session_name:
        return
    channels = _build_preamp_channels(store)
    if not channels:
        return
    SAVES_DIR.mkdir(parents=True, exist_ok=True)
    path = _preamp_save_path(session_name, autosave=True)
    try:
        import datetime
        payload = {
            "session":       session_name,
            "saved_at":      datetime.datetime.now().isoformat(timespec="seconds"),
            "emo_mtime":     emo_mtime,   # .emo mtime at time of save — used for state b/c detection
            "channels":      channels,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.debug("preamp autosave: %s (%d ch)", path.name, len(channels))
    except Exception as exc:
        log.warning("preamp autosave failed: %s", exc)


def save_preamp_explicit(session_name: str, store: "ChannelStateStore") -> None:
    """Write the explicit (LV1-saved) preamp snapshot."""
    if not session_name:
        return
    channels = _build_preamp_channels(store)
    if not channels:
        return
    SAVES_DIR.mkdir(parents=True, exist_ok=True)
    path = _preamp_save_path(session_name, autosave=False)
    try:
        import datetime
        payload = {
            "session":  session_name,
            "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "channels": channels,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.info("preamp explicit save: %s (%d ch)", path.name, len(channels))
    except Exception as exc:
        log.warning("preamp explicit save failed: %s", exc)


# Backward-compat alias used by a few call sites that always want the autosave
# path (on-close save, session-switch outgoing save).
def save_preamp_state(session_name: str, store: "ChannelStateStore",
                      emo_mtime: float = 0.0) -> None:
    save_preamp_autosave(session_name, store, emo_mtime=emo_mtime)


def _parse_preamp_channels(payload: dict) -> dict[int, tuple[float, bool]]:
    result: dict[int, tuple[float, bool]] = {}
    for k, v in payload.get("channels", {}).items():
        try:
            result[int(k)] = (float(v.get("gain", 0.0)), bool(v.get("phantom", False)))
        except (ValueError, TypeError, AttributeError):
            continue
    return result


def load_preamp_state(session_name: str,
                      emo_path: str = "",
                      prefer_explicit: bool = False,
                      ) -> dict[int, tuple[float, bool]]:
    """Return {ch: (gain_db, phantom)} choosing the right save tier.

    prefer_explicit=True  (session switch)
        Always load from the explicit save (.json) — represents the last
        LV1-confirmed state.  Falls back to autosave if no explicit save exists.

    prefer_explicit=False  (startup, state b/c auto-detection)
        Compare the .emo mtime stored in the autosave with the current .emo
        mtime on disk:
          • .emo NEWER  → LV1 was re-opened from an updated save (state b)
                          → use the explicit save (.json)
          • .emo SAME/OLDER → working session continues (state c)
                          → use the autosave (.autosave.json)
        Falls back to whichever file exists if the preferred one is missing.
    """
    if not session_name:
        return {}

    auto_path     = _preamp_save_path(session_name, autosave=True)
    explicit_path = _preamp_save_path(session_name, autosave=False)

    if prefer_explicit:
        # Interactive session switch — always use the explicitly saved snapshot.
        log.info("preamp load: session switch → preferring explicit save")
        path = explicit_path if explicit_path.exists() else auto_path
    else:
        # Startup — detect state b vs state c from .emo mtime.
        use_explicit = False
        if emo_path and Path(emo_path).exists() and auto_path.exists():
            try:
                auto_payload  = json.loads(auto_path.read_text(encoding="utf-8"))
                stored_mtime  = float(auto_payload.get("emo_mtime", 0.0))
                current_mtime = Path(emo_path).stat().st_mtime
                if current_mtime > stored_mtime + 5:   # 5-second tolerance
                    use_explicit = True
                    log.info("preamp load: .emo newer than autosave → state b (explicit save)")
                else:
                    log.info("preamp load: autosave current → state c (autosave)")
            except Exception:
                pass
        path = (explicit_path if explicit_path.exists() else auto_path) if use_explicit \
               else (auto_path if auto_path.exists() else explicit_path)

    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        result  = _parse_preamp_channels(payload)
        log.info("preamp state loaded: %s (%d ch)", path.name, len(result))
        return result
    except Exception as exc:
        log.warning("preamp load failed: %s", exc)
        return {}


def load_settings() -> dict:
    s = dict(_DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def save_settings(d: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except Exception as exc:
        log.warning("[Settings] save failed: %s", exc)


def load_profiles() -> dict:
    """Load per-session profile storage."""
    data = {"by_path": {}, "by_name": {}}
    try:
        with open(PROFILES_FILE) as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            data["by_path"] = dict(raw.get("by_path", {}))
            data["by_name"] = dict(raw.get("by_name", {}))
    except Exception:
        pass
    return data


def save_profiles(d: dict) -> None:
    """Persist per-session profile storage."""
    try:
        with open(PROFILES_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except Exception as exc:
        log.warning("[Profiles] save failed: %s", exc)


def _apply_log_level(verbose: bool) -> None:
    """Switch logger level at runtime."""
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


# ── Windows API helpers ────────────────────────────────────────────────────────

_user32 = ctypes.windll.user32
_gdi32  = ctypes.windll.gdi32

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwTime", wt.DWORD)]

_user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
_user32.GetLastInputInfo.restype = wt.BOOL


def _read_last_input_tick() -> int:
    """Return system tick of last keyboard/mouse input (cheap, no hooks)."""
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if _user32.GetLastInputInfo(ctypes.byref(info)):
        return int(info.dwTime)
    return 0


def _sample_screen_pixel(x: int, y: int) -> int:
    dc = _user32.GetDC(None)
    try:
        return int(_gdi32.GetPixel(dc, int(x), int(y)))
    finally:
        _user32.ReleaseDC(None, dc)


def _pixel_rgb_distance(c1: int, c2: int) -> int:
    if c1 < 0 or c2 < 0:
        return 9999
    return (
        abs((c1 & 0xFF) - (c2 & 0xFF))
        + abs(((c1 >> 8) & 0xFF) - ((c2 >> 8) & 0xFF))
        + abs(((c1 >> 16) & 0xFF) - ((c2 >> 16) & 0xFF))
    )


def _pixel_is_spill_green(c: int) -> bool:
    """LV1 Spill button lit state — green highlight."""
    if c < 0:
        return False
    b, g, r = c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF
    return g > 120 and g > r + 30 and g > b + 20


def _pixel_is_highlight(c: int) -> bool:
    """Heuristic for lit LV1 toolbar buttons (tabs, spill, etc.)."""
    if c < 0:
        return False
    b, g, r = c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF
    if b < 100 and g > 150 and r > 150:
        return True
    if r > 180 and g > 120 and b < 80:
        return True
    return False

WM_NCHITTEST   = 0x0084
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP   = 0x0202
WM_HOTKEY      = 0x0312
HTTRANSPARENT  = -1
HTCLIENT       =  1
MOD_CONTROL    = 0x0002
_HOTKEY_EXIT_ID = 1

N_LAYERS = 8
LAYER_NAMES = [f"Ch {i*16+1}–{i*16+16}" for i in range(N_LAYERS)]


def _post_click_to_lv1(hwnd: int, x: int, y: int) -> None:
    """Post a synthetic left-click to LV1 at client coordinates (x, y).

    NOTE: This is kept as a fallback but is NOT called in normal operation.
    The WNDPROC now returns HTTRANSPARENT for non-calibrating LayerButtons so
    clicks pass through to LV1 directly, making PostMessage unnecessary.
    """
    log.debug("_post_click_to_lv1: hwnd=%d x=%d y=%d", hwnd, x, y)
    lparam = (y << 16) | (x & 0xFFFF)
    _user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lparam)
    _user32.PostMessageW(hwnd, WM_LBUTTONUP,   0, lparam)


# ── WH_MOUSE_LL hook structure ────────────────────────────────────────────────

WH_MOUSE_LL = 14

class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt",          wt.POINT),
        ("mouseData",   wt.DWORD),
        ("flags",       wt.DWORD),
        ("time",        wt.DWORD),
        ("dwExtraInfo", ctypes.c_ulong),
    ]


def _find_lv1_hwnd() -> Optional[int]:
    """Return the HWND of the running eMotion LV1 window, or None."""
    found: list = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        if _user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            _user32.GetWindowTextW(hwnd, buf, 256)
            if "eMotion LV1" in buf.value:
                found.append(int(hwnd))
        return True

    _user32.EnumWindows(cb, 0)
    return found[0] if found else None


# ── Palette helpers ───────────────────────────────────────────────────────────

_CLR_BG        = QColor(18, 20, 28, 255)   # dark panel background
_CLR_BG_HOVER  = QColor(28, 32, 44, 255)
_CLR_PHANTOM   = QColor(200, 48,  48, 255)  # phantom-active red
_CLR_PHANTOM_H = QColor(220, 60,  60, 255)
_CLR_PHANTOM_OFF = QColor(40, 40, 55, 255)
_CLR_LAYER_SEL = QColor(30, 100, 180, 255)  # selected layer blue
_CLR_LAYER_NOR = QColor(25,  28, 40, 220)
_CLR_SETTINGS  = QColor(40,  90, 140, 200)
_CLR_TEXT      = QColor(220, 225, 235)
_CLR_TEXT_DIM  = QColor(140, 150, 165)
_CLR_GAIN      = QColor(80, 200, 120)       # gain value green


# ── OverlayChannel widget ─────────────────────────────────────────────────────

class OverlayChannel(QWidget):
    """
    Overlay cell for one DiGiCo channel.  Displays:
      • Phantom toggle ("48V")
      • Channel name (from DiGiCo)
      • Numeric gain value
      • Rotary gain knob

    Emits want_gain(ch, db) and want_phantom(ch, bool) for OSC sends.
    """

    want_gain    = pyqtSignal(int, float)
    want_phantom = pyqtSignal(int, bool)

    # Sub-element heights as fractions of cell height (computed dynamically)
    _PH_FRAC  = 0.14   # phantom "48V" bar
    _NM_FRAC  = 0.18   # name label
    _GV_FRAC  = 0.14   # gain value text (taller for larger font)

    # Cached fonts — created once per class, not per paintEvent
    _FONT_LABEL = QFont("Arial", 8, QFont.Weight.Bold)
    _FONT_GAIN_H = 0          # last height used to size the gain font
    _FONT_GAIN   = QFont("Segoe UI", 9)

    def __init__(self, slot_idx: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.slot_idx = slot_idx
        self._ch:  Optional[int] = None
        self._ch2: Optional[int] = None   # secondary (R-side) for stereo pairs
        self._name = ""
        self._gain = 0.0
        self._phantom = False
        self._ph_hovered = False
        self._kn_hovered = False
        self._dragging_knob = False
        self._drag_start_y = 0
        self._drag_start_gain = 0.0
        self._last_sent_gain: float = -999.0   # track last OSC-emitted value to throttle
        # Start blocked so the very first frame is transparent until mode gating
        # is applied by OverlayWindow.
        self._blocked = True    # True when LV1 is not in Input mode → fully transparent

        # ── Render cache ──────────────────────────────────────────────────
        self._cache: Optional[QPixmap] = None
        self._cache_state: tuple       = ()    # state snapshot the cache was built for
        self._layout_cache: tuple      = ()
        self._layout_size:  tuple      = (0, 0)

        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setMouseTracking(True)

    # ── public setters ────────────────────────────────────────────────────────

    def set_channel(self, ch: Optional[int], ch2: Optional[int] = None) -> None:
        self._ch  = ch
        self._ch2 = ch2
        self._name = ""
        self._gain = 0.0
        self._phantom = False
        self.update()

    def set_name(self, name: str) -> None:
        self._name = name
        self.update()

    def set_gain(self, gain_db: float) -> None:
        if not self._dragging_knob:
            self._gain = gain_db
            self.update()

    def set_phantom(self, on: bool) -> None:
        self._phantom = on
        self.update()

    # ── geometry helpers ──────────────────────────────────────────────────────

    def _layout(self):
        """Return (ph_y, ph_h, nm_y, nm_h, gv_y, gv_h, kn_y, kn_h) for current size.

        Result is cached per (width, height) so geometry calculations only run
        on resize events rather than every paintEvent.
        """
        sz = (self.width(), self.height())
        if sz == self._layout_size:
            return self._layout_cache
        w, h  = sz
        ph_h  = max(18, round(h * self._PH_FRAC))
        nm_h  = max(20, round(h * self._NM_FRAC))
        gv_h  = max(13, round(h * self._GV_FRAC))
        ph_y  = 1
        nm_y  = ph_y + ph_h + 2
        gv_y  = nm_y + nm_h + 2
        kn_y  = gv_y + gv_h + 2
        kn_h  = max(1, h - kn_y - 2)
        result = (ph_y, ph_h, nm_y, nm_h, gv_y, gv_h, kn_y, kn_h)
        self._layout_cache = result
        self._layout_size  = sz
        return result

    def _ph_rect(self, layout=None) -> QRect:
        if layout is None:
            layout = self._layout()
        ph_y, ph_h, *_ = layout
        return QRect(3, ph_y, self.width() - 6, ph_h)

    def _knob_rect(self, layout=None) -> QRect:
        if layout is None:
            layout = self._layout()
        _, _, _, _, _, _, kn_y, kn_h = layout
        w = self.width()
        sz = min(w - 10, kn_h)
        cx = w // 2
        cy = kn_y + kn_h // 2
        return QRect(cx - sz // 2, cy - sz // 2, sz, sz)

    # ── painting ─────────────────────────────────────────────────────────────

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._cache       = None    # size changed — pixmap is the wrong dimensions
        self._layout_size = (0, 0)  # layout must be recomputed at new size

    def paintEvent(self, _) -> None:
        # Non-MGB slot OR mode is not Input — fully transparent so LV1 shows through.
        if self._ch is None or self._blocked:
            self._cache = None   # free memory while not visible
            # Explicitly erase to transparent.  On Windows DWM, simply returning
            # without drawing leaves the previous frame's pixels on screen as
            # "ghost" content.  CompositionMode_Clear writes alpha=0 directly
            # into the layered window buffer so the old render is erased.
            painter = QPainter(self)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Clear
            )
            painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
            return

        # Build a lightweight state key.  Round gain to the nearest 0.5 dB step
        # so we don't re-render for sub-step floating-point noise.
        state = (self._ch, self._name, round(self._gain * 2),
                 self._phantom, self._ph_hovered, self._kn_hovered)

        if self._cache is None or self._cache_state != state:
            # ── Render into a cached QPixmap (done at most once per state change) ──
            pm = QPixmap(self.size())
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._render(p)
            p.end()
            self._cache       = pm
            self._cache_state = state

        # ── Blit — just one drawPixmap, no per-paint geometry maths ──────────
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._cache)

    def _render(self, p: QPainter) -> None:
        """Full-cell render into an already-active QPainter (used by the cache path)."""
        w, h = self.width(), self.height()
        layout = self._layout()
        ph_y, ph_h, nm_y, nm_h, gv_y, gv_h, kn_y, kn_h = layout
        m = 3   # side margin

        # ── Cell background ───────────────────────────────────────────────
        p.fillRect(0, 0, w, h, _CLR_BG_HOVER if self._kn_hovered else _CLR_BG)

        # ── "48" phantom bar (top) ────────────────────────────────────────
        ph_r = QRect(m, ph_y, w - 2 * m, ph_h)
        if self._phantom:
            ph_bg = _CLR_PHANTOM_H if self._ph_hovered else _CLR_PHANTOM
            ph_fg = _CLR_TEXT
        else:
            ph_bg = QColor(40, 43, 58, 255) if self._ph_hovered else QColor(32, 35, 48, 255)
            ph_fg = _CLR_TEXT_DIM
        p.fillRect(ph_r, ph_bg)
        p.setFont(self._FONT_LABEL)
        p.setPen(ph_fg)
        p.drawText(ph_r, Qt.AlignmentFlag.AlignCenter, "48V")

        # ── Name box (PREAMP-style rectangle) ─────────────────────────────
        nm_r = QRect(m, nm_y, w - 2 * m, nm_h)
        p.fillRect(nm_r, QColor(42, 46, 64, 255))
        p.setPen(QPen(QColor(72, 78, 105), 1))
        p.drawRect(nm_r.adjusted(0, 0, -1, -1))
        name_txt = self._name if self._name else f"CH {self._ch}"
        p.setFont(self._FONT_LABEL)
        p.setPen(_CLR_TEXT)
        fm = p.fontMetrics()
        elided = fm.elidedText(name_txt, Qt.TextElideMode.ElideRight, nm_r.width() - 4)
        p.drawText(nm_r, Qt.AlignmentFlag.AlignCenter, elided)

        # ── Gain value — resize font only when cell height changes ────────
        gv_sz = max(9, round(gv_h * 0.82))
        if gv_sz != OverlayChannel._FONT_GAIN_H:
            OverlayChannel._FONT_GAIN   = QFont("Segoe UI", gv_sz)
            OverlayChannel._FONT_GAIN_H = gv_sz
        p.setFont(OverlayChannel._FONT_GAIN)
        p.setPen(QColor(200, 210, 200))
        gv_r = QRect(0, gv_y, w, gv_h)
        q_gain = round(self._gain * 2) / 2
        p.drawText(gv_r, Qt.AlignmentFlag.AlignCenter, f"{q_gain:.1f}")

        # ── Knob ─────────────────────────────────────────────────────────
        self._draw_knob(p, kn_y, kn_h)

    def _draw_knob(self, p: QPainter, kn_y: int, kn_h: int) -> None:
        w  = self.width()
        sz = min(w - 10, kn_h)
        cx = w // 2
        cy = kn_y + kn_h // 2
        kr = QRect(cx - sz // 2, cy - sz // 2, sz, sz)

        # Shrink slightly so the knob doesn't fill its bounding box edge-to-edge
        shrink = max(2, kr.width() // 7)
        kr = kr.adjusted(shrink, shrink, -shrink, -shrink)

        # Use float centre so all circles share exactly the same origin
        cx = kr.x() + kr.width()  / 2.0
        cy = kr.y() + kr.height() / 2.0
        r  = kr.width() / 2.0

        def _ellipse_f(radius: float) -> QRectF:
            """QRectF centred on (cx,cy) with given radius."""
            return QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

        frac = max(0.0, min(1.0, (self._gain - _dmc.GAIN_MIN) / max(1, _dmc.GAIN_MAX - _dmc.GAIN_MIN)))

        ARC_START = 225   # Qt degrees (CCW from east) = 7-o'clock
        ARC_SPAN  = 270   # total sweep

        # Ring geometry derived from one value — arc and disc always aligned
        ring_w  = max(4.0, r / 4.0)       # ~25 % of radius
        arc_pw  = max(2.0, ring_w - 2.0)  # stroke = ring minus 1 px margin each side
        inner_r = max(1.0, r - ring_w)    # inner disc sits exactly where arc ends

        arc_r   = inner_r + ring_w / 2.0  # arc path centre radius

        # ── 1. Dark outer ring background ─────────────────────────────────
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(50, 53, 66))
        p.drawEllipse(_ellipse_f(r))

        # ── 2. Arc track + active arc (same bounding circle) ──────────────
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(30, 32, 42), arc_pw,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(_ellipse_f(arc_r), ARC_START * 16, -ARC_SPAN * 16)
        if frac > 0:
            p.setPen(QPen(_CLR_GAIN, arc_pw,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(_ellipse_f(arc_r), ARC_START * 16, -round(frac * ARC_SPAN * 16))

        # ── 3. Inner light-gray disc — exact same centre ───────────────────
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(155, 158, 168))
        p.drawEllipse(_ellipse_f(inner_r))

        # ── 4. Indicator dot on inner disc ─────────────────────────────────
        angle_rad = math.radians(ARC_START - frac * ARC_SPAN)
        dot_dist  = max(1.0, inner_r - 4.0)
        dx = cx + dot_dist * math.cos(angle_rad)
        dy = cy - dot_dist * math.sin(angle_rad)
        p.setBrush(QColor(240, 242, 248))
        p.drawEllipse(QRectF(dx - 3, dy - 3, 6, 6))

    # ── mouse events ─────────────────────────────────────────────────────────

    def mousePressEvent(self, ev) -> None:
        if self._ch is None:
            return
        pos = ev.position().toPoint()
        # Phantom button click
        if self._ph_rect().contains(pos):
            new_ph = not self._phantom
            self._phantom = new_ph
            self.update()
            self.want_phantom.emit(self._ch, new_ph)
            if self._ch2 is not None:
                self.want_phantom.emit(self._ch2, new_ph)
            return
        # Knob click: begin drag
        if self._knob_rect().contains(pos):
            self._dragging_knob = True
            self._drag_start_y = pos.y()
            self._drag_start_gain = self._gain
            self.setCursor(Qt.CursorShape.SizeVerCursor)

    def mouseMoveEvent(self, ev) -> None:
        pos = ev.position().toPoint()
        ph_h = self._ph_rect().contains(pos)
        kn_h = self._knob_rect().contains(pos)
        if ph_h != self._ph_hovered or kn_h != self._kn_hovered:
            self._ph_hovered = ph_h
            self._kn_hovered = kn_h
            self.update()

        if self._dragging_knob and self._ch is not None:
            delta = self._drag_start_y - pos.y()          # up = increase
            new_gain = self._drag_start_gain + delta * 0.3
            new_gain = max(_dmc.GAIN_MIN, min(_dmc.GAIN_MAX, new_gain))
            self._gain = new_gain
            self.update()
            # Send real-time OSC — only when quantised value actually changes
            q = round(new_gain * 2) / 2
            if q != self._last_sent_gain:
                self._last_sent_gain = q
                self.want_gain.emit(self._ch, q)
                if self._ch2 is not None:
                    self.want_gain.emit(self._ch2, q)

    def mouseReleaseEvent(self, ev) -> None:
        if self._dragging_knob and self._ch is not None:
            self._dragging_knob = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            # Final emit ensures the console receives the settled value
            q = round(self._gain * 2) / 2
            self.want_gain.emit(self._ch, q)
            if self._ch2 is not None:
                self.want_gain.emit(self._ch2, q)

    def wheelEvent(self, ev) -> None:
        if self._ch is None:
            return
        delta = ev.angleDelta().y()
        step = _dmc.GAIN_STEP if delta > 0 else -_dmc.GAIN_STEP
        # Wheel always moves in clean 0.5 dB steps from the current quantised value
        base = round(self._gain * 2) / 2
        new_gain = max(_dmc.GAIN_MIN, min(_dmc.GAIN_MAX, base + step))
        self._gain = new_gain
        self._last_sent_gain = new_gain
        self.update()
        self.want_gain.emit(self._ch, new_gain)
        if self._ch2 is not None:
            self.want_gain.emit(self._ch2, new_gain)

    def leaveEvent(self, _) -> None:
        self._ph_hovered = False
        self._kn_hovered = False
        self.update()


# ── LayerButton widget ────────────────────────────────────────────────────────

class LayerButton(QWidget):
    """
    Invisible hit-area placed over LV1's page-selector buttons.

    Normal mode  — draws nothing (fully transparent), so LV1 shows through.
                   Still intercepts clicks so the overlay can track the active layer.
    Calibration  — draws the semi-transparent button so the user can see and
                   position the hit areas correctly.

    On click: emits layer_selected(idx) AND forwards the click to LV1 using
    ScreenToClient so the surface actually changes layer too.
    """

    layer_selected = pyqtSignal(int)

    def __init__(self, idx: int, name: str, lv1_hwnd_getter,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._idx = idx
        self._name = name
        self._get_hwnd = lv1_hwnd_getter
        self._selected = (idx == 0)
        self._hovered = False
        self._calibrating = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_selected(self, sel: bool) -> None:
        self._selected = sel
        self.update()

    def set_calibrating(self, on: bool) -> None:
        self._calibrating = on
        self.update()

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        if not self._calibrating:
            # Fully transparent — WNDPROC returns HTTRANSPARENT so the real
            # physical click passes straight to LV1, and LV1 shows through.
            return
        w, h = self.width(), self.height()
        color = _CLR_LAYER_SEL if self._selected else (
            QColor(35, 40, 60, 190) if self._hovered else _CLR_LAYER_NOR
        )
        p.fillRect(0, 0, w, h, color)
        if self._selected:
            p.setPen(QPen(QColor(60, 140, 240, 200), 1))
            p.drawRect(0, 0, w - 1, h - 1)
        p.setFont(QFont("Arial", 8, QFont.Weight.Bold if self._selected else QFont.Weight.Normal))
        p.setPen(_CLR_TEXT)
        p.drawText(QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, self._name)

    def mousePressEvent(self, ev) -> None:
        # Only fires in calibration mode (HTTRANSPARENT in normal mode means
        # the overlay never receives the click; LV1 gets it directly).
        self.layer_selected.emit(self._idx)

    def enterEvent(self, _) -> None:
        self._hovered = True
        self.update()

    def leaveEvent(self, _) -> None:
        self._hovered = False
        self.update()


def _local_ip() -> str:
    """Return the machine's primary LAN IPv4 address (best-effort)."""
    import socket as _socket
    # 1) Outbound interface probe.
    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    # 2) Hostname resolution fallback.
    try:
        hostname = _socket.gethostname()
        for row in _socket.getaddrinfo(hostname, None, _socket.AF_INET):
            ip = row[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    # 3) NIC list fallback from digico_multichannel.
    try:
        for _, ip in get_interfaces():
            if not ip or ip == "0.0.0.0" or ip.startswith("127."):
                continue
            return ip
    except Exception:
        pass
    return "unknown"


# ── SettingsButton widget ─────────────────────────────────────────────────────

_RESIZE_GRIP = 14  # px corner area used for resize in move-mode


class SettingsButton(QWidget):
    """Two stacked buttons in a freely resizable widget.

    Top    : "Ovrly On" / "Ovrly Off" — toggles overlay visibility.
    Bottom : "Settings" with DiGiCo LED on the right — opens settings panel.

    In move-mode the whole widget becomes draggable / resizable (bottom-right
    corner grip).
    """

    clicked          = pyqtSignal()               # settings button clicked
    overlay_toggled  = pyqtSignal(bool)            # overlay on/off toggled
    position_changed = pyqtSignal(int, int, int, int)  # x, y, w, h after drag/resize

    _GAP = 2   # px gap between the two buttons

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hovered_top        = False
        self._hovered_bot        = False
        self._move_mode          = False
        self._dragging           = False
        self._resizing           = False
        self._overlay_on         = True
        self._digico_connected   = False
        self._drag_start         = QPoint()
        self._drag_btn_start     = QPoint()
        self._resize_start_geom: tuple[int, int, int, int] = (0, 0, 0, 0)
        self.setMinimumSize(40, 18)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setMouseTracking(True)

    # ── public API ────────────────────────────────────────────────────────────

    def set_overlay_on(self, on: bool) -> None:
        self._overlay_on = on
        self.update()

    def set_digico_connected(self, connected: bool) -> None:
        if self._digico_connected != connected:
            self._digico_connected = connected
            self.update()

    def set_move_mode(self, on: bool) -> None:
        self._move_mode = on
        self._dragging  = False
        self._resizing  = False
        self.setCursor(Qt.CursorShape.SizeAllCursor if on else Qt.CursorShape.PointingHandCursor)
        self.update()

    # ── geometry helpers ──────────────────────────────────────────────────────

    def _top_rect(self) -> QRect:
        w, h = self.width(), self.height()
        top_h = (h - self._GAP) // 2
        return QRect(0, 0, w, top_h)

    def _bot_rect(self) -> QRect:
        w, h = self.width(), self.height()
        top_h = (h - self._GAP) // 2
        return QRect(0, top_h + self._GAP, w, h - top_h - self._GAP)

    def _in_resize_corner(self, pos: QPoint) -> bool:
        return (self._move_mode and
                pos.x() >= self.width()  - _RESIZE_GRIP and
                pos.y() >= self.height() - _RESIZE_GRIP)

    # ── dynamic font fitting ──────────────────────────────────────────────────

    @staticmethod
    def _fit_font(text: str, max_w: int, max_h: int,
                  bold: bool = True, lo: int = 5, hi: int = 48) -> QFont:
        best = lo
        while lo <= hi:
            mid  = (lo + hi) // 2
            font = QFont("Segoe UI", mid,
                         QFont.Weight.Bold if bold else QFont.Weight.Normal)
            fm   = QFontMetrics(font)
            if fm.horizontalAdvance(text) <= max_w and fm.height() <= max_h:
                best = mid
                lo   = mid + 1
            else:
                hi = mid - 1
        return QFont("Segoe UI", best,
                     QFont.Weight.Bold if bold else QFont.Weight.Normal)

    # ── painting helpers ──────────────────────────────────────────────────────

    def _draw_button(self, p: QPainter, rect: QRect,
                     bg: QColor, border: QColor,
                     text: str, text_col: QColor,
                     hovered: bool) -> None:
        rad = max(3, min(rect.height() // 3, 6))
        path = QPainterPath()
        path.addRoundedRect(rect.x(), rect.y(),
                            rect.width(), rect.height(), rad, rad)
        # hover brightens background slightly
        fill = QColor(bg.red()   + (20 if hovered else 0),
                      bg.green() + (20 if hovered else 0),
                      bg.blue()  + (20 if hovered else 0),
                      bg.alpha())
        p.fillPath(path, fill)
        p.setPen(QPen(border, 1))
        p.drawPath(path)
        font = self._fit_font(text, rect.width() - 8, rect.height() - 4)
        p.setFont(font)
        p.setPen(text_col)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_led(self, p: QPainter, rect: QRect) -> None:
        """Draw a small LED circle inside *rect*, vertically/horizontally centred."""
        led_r = max(2, min(rect.width(), rect.height()) // 2 - 1)
        cx = rect.x() + rect.width()  // 2
        cy = rect.y() + rect.height() // 2
        if self._digico_connected:
            led_col  = QColor(39, 200, 80, 240)
            glow_col = QColor(100, 255, 130, 80)
        else:
            led_col  = QColor(210, 50, 40, 240)
            glow_col = QColor(255, 100, 80, 80)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow_col)
        p.drawEllipse(cx - led_r - 2, cy - led_r - 2,
                      (led_r + 2) * 2, (led_r + 2) * 2)
        p.setBrush(led_col)
        p.drawEllipse(cx - led_r, cy - led_r, led_r * 2, led_r * 2)
        hr = max(1, led_r // 2)
        p.setBrush(QColor(255, 255, 255, 110))
        p.drawEllipse(cx - hr // 2, cy - led_r + 1, hr, hr)

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        if self._move_mode:
            # Single amber block with drag instruction
            path = QPainterPath()
            path.addRoundedRect(0, 0, w, h, 4, 4)
            p.fillPath(path, QColor(180, 100, 15, 210))
            p.setPen(QPen(QColor(255, 200, 80, 220), 1))
            p.drawPath(path)
            font = self._fit_font("Drag / resize", w - 8, h - 4)
            p.setFont(font)
            p.setPen(QColor(255, 230, 120))
            p.drawText(QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "Drag / resize")
            # resize grip corner
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 230, 120, 160))
            pts = [QPoint(w, h - _RESIZE_GRIP), QPoint(w, h), QPoint(w - _RESIZE_GRIP, h)]
            p.drawPolygon(pts)
            return

        # ── Top button: Ovrly On / Ovrly Off ──────────────────────────────────
        tr = self._top_rect()
        if self._overlay_on:
            top_bg     = QColor(21, 128, 61, 220)    # green
            top_border = QColor(74, 222, 128, 180)
            top_text   = "Ovrly On"
        else:
            top_bg     = QColor(153, 27, 27, 220)    # red
            top_border = QColor(248, 113, 113, 180)
            top_text   = "Ovrly Off"
        self._draw_button(p, tr, top_bg, top_border,
                          top_text, QColor(255, 255, 255, 235),
                          self._hovered_top)

        # ── Bottom button: Settings + LED ─────────────────────────────────────
        br = self._bot_rect()
        bot_bg     = QColor(30, 58, 95, 220)     # dark blue
        bot_border = QColor(71, 108, 170, 180)

        # Reserve space for LED on the right
        led_size  = max(6, min(br.height() - 6, br.width() // 5, 14))
        led_pad   = 5
        led_x     = br.right() - led_pad - led_size
        led_rect  = QRect(led_x, br.y() + (br.height() - led_size) // 2,
                          led_size, led_size)
        text_rect = QRect(br.x(), br.y(),
                          led_x - br.x() - 2, br.height())

        # Draw button background + border
        rad = max(3, min(br.height() // 3, 6))
        path = QPainterPath()
        path.addRoundedRect(br.x(), br.y(), br.width(), br.height(), rad, rad)
        fill = QColor(bot_bg.red()   + (20 if self._hovered_bot else 0),
                      bot_bg.green() + (20 if self._hovered_bot else 0),
                      bot_bg.blue()  + (20 if self._hovered_bot else 0),
                      bot_bg.alpha())
        p.fillPath(path, fill)
        p.setPen(QPen(bot_border, 1))
        p.drawPath(path)

        # "Settings" label (left of LED)
        font = self._fit_font("Settings", text_rect.width() - 6, text_rect.height() - 4)
        p.setFont(font)
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, "Settings")

        # LED
        self._draw_led(p, led_rect)

    # ── mouse events ──────────────────────────────────────────────────────────

    def mousePressEvent(self, ev) -> None:
        lp = ev.position().toPoint()
        if self._move_mode:
            if self._in_resize_corner(lp):
                self._resizing          = True
                self._drag_start        = ev.globalPosition().toPoint()
                self._resize_start_geom = (self.x(), self.y(),
                                           self.width(), self.height())
            else:
                self._dragging       = True
                self._drag_start     = ev.globalPosition().toPoint()
                self._drag_btn_start = self.pos()
            ev.accept()
        elif self._top_rect().contains(lp):
            self._overlay_on = not self._overlay_on
            self.overlay_toggled.emit(self._overlay_on)
            self.update()
            ev.accept()
        elif self._bot_rect().contains(lp):
            self.clicked.emit()
            ev.accept()

    def mouseMoveEvent(self, ev) -> None:
        lp = ev.position().toPoint()
        if self._move_mode:
            if self._resizing:
                delta = ev.globalPosition().toPoint() - self._drag_start
                rx, ry, rw, rh = self._resize_start_geom
                new_w = max(self.minimumWidth(),  rw + delta.x())
                new_h = max(self.minimumHeight(), rh + delta.y())
                self.setGeometry(rx, ry, new_w, new_h)
            elif self._dragging:
                delta = ev.globalPosition().toPoint() - self._drag_start
                self.move(self._drag_btn_start + delta)
            else:
                c = (Qt.CursorShape.SizeFDiagCursor if self._in_resize_corner(lp)
                     else Qt.CursorShape.SizeAllCursor)
                self.setCursor(c)
            ev.accept()
            return
        # hover highlighting
        in_top = self._top_rect().contains(lp)
        in_bot = self._bot_rect().contains(lp)
        if in_top != self._hovered_top or in_bot != self._hovered_bot:
            self._hovered_top = in_top
            self._hovered_bot = in_bot
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if self._resizing or self._dragging:
            self._resizing = False
            self._dragging = False
            self.position_changed.emit(self.x(), self.y(),
                                       self.width(), self.height())
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            ev.accept()

    def leaveEvent(self, _) -> None:
        if self._hovered_top or self._hovered_bot:
            self._hovered_top = False
            self._hovered_bot = False
            self.update()


# ── SettingsPanel dialog ──────────────────────────────────────────────────────

class SettingsPanel(QDialog):
    """Settings dialog: DiGiCo connection, overlay appearance, calibration."""

    applied              = pyqtSignal(dict)
    disconnect_requested = pyqtSignal()

    def __init__(self, settings: dict, connected: bool = False,
                 overlay_visible: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Overlay Settings  v{__version__}")
        self.setMinimumWidth(400)
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self._connected = connected
        self._overlay_visible = overlay_visible
        self._lv1_session_name = str(settings.get("lv1_session_name", "")).strip()
        # Auto-detect LV1 session path — stored but not user-editable
        self._lv1_db = find_autosave() or settings.get("lv1_db", "")

        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── DiGiCo connection ─────────────────────────────────────────────────
        dg = QGroupBox("DiGiCo")
        fg = QFormLayout(dg)
        fg.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)

        # IP field — no mask so leading zeros don't linger.
        # textEdited auto-advances the cursor past a dot when the current
        # octet reaches 3 digits, giving the same fast-typing feel.
        # editingFinished strips leading zeros (001 → 1) and extra spaces.
        self._ip = QLineEdit(self._parse_ip(settings.get("console_ip", DEFAULT_CONSOLE_IP)))
        self._ip.setFixedWidth(105)
        self._ip.setMaxLength(15)  # 255.255.255.255

        def _ip_advance(text: str) -> None:
            pos = self._ip.cursorPosition()
            before = text[:pos]
            octet = before.split(".")[-1]
            if len(octet) == 3 and pos < len(text) and text[pos] == ".":
                self._ip.setCursorPosition(pos + 1)

        def _ip_normalise() -> None:
            parts = self._ip.text().split(".")
            if len(parts) == 4:
                try:
                    self._ip.setText(".".join(str(int(p)) if p else "0" for p in parts))
                except ValueError:
                    pass

        self._ip.textEdited.connect(_ip_advance)
        self._ip.editingFinished.connect(_ip_normalise)
        fg.addRow("Console IP:", self._ip)

        # Send + Receive ports on the same row
        _pv = QIntValidator(1, 65535)
        self._send_port = QLineEdit(str(settings.get("console_port", DEFAULT_CONSOLE_PORT)))
        self._send_port.setValidator(_pv)
        self._send_port.setFixedWidth(48)
        self._recv_port = QLineEdit(str(settings.get("listen_port", DEFAULT_LISTEN_PORT)))
        self._recv_port.setValidator(_pv)
        self._recv_port.setFixedWidth(48)
        ports_row = QHBoxLayout()
        ports_row.setSpacing(6)
        ports_row.addWidget(QLabel("Send:"))
        ports_row.addWidget(self._send_port)
        ports_row.addSpacing(10)
        ports_row.addWidget(QLabel("Receive:"))
        ports_row.addWidget(self._recv_port)
        ports_row.addStretch()
        fg.addRow("Ports:", ports_row)

        # Network adapter — prefer concrete NIC IPs, but never leave the combo empty.
        self._nic = QComboBox()
        self._ifaces = [(lbl, ip) for lbl, ip in get_interfaces() if ip != "0.0.0.0"]
        if not self._ifaces:
            # Fallback for edge cases where Windows NIC enumeration fails.
            bind_ip = str(settings.get("bind_ip", "")).strip()
            if bind_ip and bind_ip != "0.0.0.0":
                self._ifaces = [(f"Saved adapter ({bind_ip})", bind_ip)]
            else:
                self._ifaces = [("All interfaces (0.0.0.0)", "0.0.0.0")]
        saved_bind = settings.get("bind_ip", "0.0.0.0")
        sel_idx = 0
        for i, (label, ip) in enumerate(self._ifaces):
            display = label.split("  (")[0] if "  (" in label else label
            self._nic.addItem(display, userData=ip)
            if ip == saved_bind:
                sel_idx = i
        self._nic.setCurrentIndex(sel_idx)
        fg.addRow("Network adapter:", self._nic)

        # Connection button
        conn_row = QHBoxLayout()
        self._conn_btn = QPushButton()
        self._conn_btn.setFixedWidth(110)
        self._update_conn_btn()
        self._conn_btn.clicked.connect(self._on_conn_clicked)
        conn_row.addWidget(self._conn_btn)
        conn_row.addStretch()
        fg.addRow("", conn_row)

        root.addWidget(dg)

        # ── Status ────────────────────────────────────────────────────────────
        sg = QGroupBox("Status")
        sf = QFormLayout(sg)
        self._stat_osc = QLabel("—")
        self._stat_lv1 = QLabel("—")
        self._stat_lv1_session = QLabel(self._lv1_session_name or "—")
        self._stat_ip  = QLabel(_local_ip())
        self._stat_ip.setStyleSheet("color: #7f8c8d;")
        sf.addRow("DiGiCo:", self._stat_osc)
        sf.addRow("LV1:",    self._stat_lv1)
        sf.addRow("LV1 session:", self._stat_lv1_session)
        sf.addRow("Local IP:", self._stat_ip)
        self._nic.currentIndexChanged.connect(self._refresh_local_ip)
        root.addWidget(sg)

        # ── Options ───────────────────────────────────────────────────────────
        og = QGroupBox("Options")
        ol = QVBoxLayout(og)
        self._sync_names_chk = QCheckBox("Sync LV1 channel names → DiGiCo")
        self._sync_names_chk.setChecked(bool(settings.get("sync_names", False)))
        self._sync_names_chk.setToolTip(
            "When enabled, LV1 channel names are sent to DiGiCo via OSC\n"
            "on connect and whenever the LV1 session changes."
        )
        ol.addWidget(self._sync_names_chk)

        self._load_preamp_chk = QCheckBox("Load preamp values with session")
        self._load_preamp_chk.setChecked(bool(settings.get("load_preamp_with_session", False)))
        self._load_preamp_chk.setToolTip(
            "On session load or DiGiCo reconnect, restore the saved gain and\n"
            "phantom power values for this session to the DiGiCo console.\n"
            "Values are auto-saved to the 'preamp_saves' folder next to the app."
        )
        ol.addWidget(self._load_preamp_chk)
        root.addWidget(og)

        # ── Appearance ────────────────────────────────────────────────────────
        ag = QGroupBox("Appearance")
        al = QVBoxLayout(ag)
        ol = QHBoxLayout()
        ol.addWidget(QLabel("Overlay opacity:"))
        self._opacity = QSlider(Qt.Orientation.Horizontal)
        self._opacity.setRange(20, 100)
        self._opacity.setValue(settings.get("opacity", 85))
        self._opacity_lbl = QLabel(f"{self._opacity.value()}%")
        self._opacity_lbl.setFixedWidth(36)
        def _on_opacity_change(v: int) -> None:
            self._opacity_lbl.setText(f"{v}%")
            win = self.parent()
            if isinstance(win, OverlayWindow):
                win._set_opacity(v)
        self._opacity.valueChanged.connect(_on_opacity_change)
        ol.addWidget(self._opacity)
        ol.addWidget(self._opacity_lbl)
        al.addLayout(ol)
        root.addWidget(ag)

        # ── Calibration ───────────────────────────────────────────────────────
        cal_grp = QGroupBox("Alignment")
        cal_lay = QHBoxLayout(cal_grp)

        cal_btn = QPushButton("Calibrate overlay size…")
        cal_btn.setToolTip(
            "Shows an orange rectangle you can drag/resize over LV1's\n"
            "preamp section. Click ✓ Apply inside it to save geometry."
        )
        cal_btn.clicked.connect(self._on_calibrate)
        cal_lay.addWidget(cal_btn)

        move_btn_btn = QPushButton("Move settings button…")
        move_btn_btn.setToolTip(
            "Drag the 'LV1-Digico Overlay' button to the desired position\n"
            "(e.g. over the LV1 logo). Click it again to save."
        )
        move_btn_btn.clicked.connect(self._on_move_settings_btn)
        cal_lay.addWidget(move_btn_btn)

        spill_cal_btn = QPushButton("Calibrate spill button…")
        spill_cal_btn.setToolTip(
            "Place the crosshair over LV1's Spill button while spill is OFF,\n"
            "then click ✓ Apply. The overlay samples that pixel to detect spill mode."
        )
        spill_cal_btn.clicked.connect(self._on_calibrate_spill)
        cal_lay.addWidget(spill_cal_btn)

        root.addWidget(cal_grp)


        # ── Diagnostics (bottom section) ──────────────────────────────────────
        dg = QGroupBox("Diagnostics")
        dl = QVBoxLayout(dg)
        self._verbose_logging_chk = QCheckBox("Verbose logging")
        self._verbose_logging_chk.setChecked(bool(settings.get("verbose_logging", False)))
        self._verbose_logging_chk.setToolTip(
            "Enable detailed debug logs in lv1_overlay.log.\n"
            "Use this for troubleshooting real-console issues."
        )
        dl.addWidget(self._verbose_logging_chk)
        root.addWidget(dg)

        # ── Bottom buttons ────────────────────────────────────────────────────
        _BTN_W = 120
        _BTN_BASE = "padding: 4px 10px; font-size: 11px;"

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._overlay_btn = QPushButton()
        self._overlay_btn.setFixedWidth(_BTN_W)
        self._update_overlay_btn()   # sets text + color styleSheet
        self._overlay_btn.clicked.connect(self._on_overlay_toggle)
        btn_row.addWidget(self._overlay_btn)

        btn_row.addSpacing(8)

        close_dlg_btn = QPushButton("Close")
        close_dlg_btn.setFixedWidth(_BTN_W)
        close_dlg_btn.setStyleSheet(_BTN_BASE)
        close_dlg_btn.clicked.connect(self._save_opacity_and_close)
        btn_row.addWidget(close_dlg_btn)

        btn_row.addSpacing(8)

        close_app_btn = QPushButton("Close App")
        close_app_btn.setFixedWidth(_BTN_W)
        close_app_btn.setStyleSheet(_BTN_BASE + " color: #c0392b; font-weight: bold;")
        close_app_btn.clicked.connect(QApplication.quit)
        btn_row.addWidget(close_app_btn)

        btn_row.addStretch()
        root.addLayout(btn_row)

    def _update_conn_btn(self) -> None:
        if self._connected:
            self._conn_btn.setText("Disconnect")
            self._conn_btn.setStyleSheet("color: #c0392b; font-weight: bold;")
        else:
            self._conn_btn.setText("Connect")
            self._conn_btn.setStyleSheet("color: #27ae60; font-weight: bold;")

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self._update_conn_btn()

    def _update_overlay_btn(self) -> None:
        _base = "padding: 4px 10px; font-size: 11px; font-weight: bold;"
        if self._overlay_visible:
            self._overlay_btn.setText("Overlay: ON")
            self._overlay_btn.setStyleSheet(_base + " color: #27ae60;")
        else:
            self._overlay_btn.setText("Overlay: OFF")
            self._overlay_btn.setStyleSheet(_base + " color: #e67e22;")

    def _on_overlay_toggle(self) -> None:
        self._overlay_visible = not self._overlay_visible
        self._update_overlay_btn()
        win = self.parent()
        if isinstance(win, OverlayWindow):
            win.set_overlay_visible(self._overlay_visible)

    def _on_conn_clicked(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
            # Update UI immediately — don't wait for async OSC thread confirmation
            self._connected = False
            self._update_conn_btn()
            self._stat_osc.setText("Disconnected")
            self._stat_osc.setStyleSheet("color: #c0392b;")
        else:
            self._apply()   # apply saves settings and triggers reconnect

    def _on_calibrate(self) -> None:
        win = self.parent()
        if isinstance(win, OverlayWindow):
            win.toggle_calibration()
        self.hide()

    def _on_calibrate_layers(self) -> None:
        win = self.parent()
        if isinstance(win, OverlayWindow):
            win.toggle_layer_calibration()
        self.hide()

    def _on_calibrate_spill(self) -> None:
        win = self.parent()
        if isinstance(win, OverlayWindow):
            win.toggle_spill_calibration()
        self.hide()

    def _on_move_settings_btn(self) -> None:
        win = self.parent()
        if isinstance(win, OverlayWindow):
            win.start_move_settings_btn()
        self.hide()

    @staticmethod
    def _parse_ip(raw: str) -> str:
        """Normalise an IP string from the input mask.

        The mask stores each octet as exactly 3 characters (e.g. '001', '  6').
        We strip spaces, then strip leading zeros from each octet so that
        Windows' getaddrinfo doesn't reject addresses like '192.168.001.006'.
        """
        cleaned = raw.replace(" ", "")
        try:
            parts = cleaned.split(".")
            if len(parts) == 4:
                return ".".join(str(int(p)) if p else "0" for p in parts)
        except ValueError:
            pass
        return cleaned

    def _save_opacity_and_close(self) -> None:
        """Save the entire panel state without reconnecting, then close."""
        self._persist_panel_state()
        self.close()

    def _persist_panel_state(self) -> None:
        """Persist full panel state without reconnecting OSC."""
        win = self.parent()
        if isinstance(win, OverlayWindow):
            win.save_panel_state(self._collect_settings_dict())

    def _collect_settings_dict(self) -> dict:
        """Return current panel values as a settings patch dict."""
        def _parse_int(text: str, default: int) -> int:
            try:
                return int(text)
            except (TypeError, ValueError):
                return default

        idx = self._nic.currentIndex()
        bind_ip = self._ifaces[idx][1] if 0 <= idx < len(self._ifaces) else "0.0.0.0"
        return {
            "console_ip":   self._ip.text().strip(),
            "console_port": _parse_int(self._send_port.text() or str(DEFAULT_CONSOLE_PORT),
                                       DEFAULT_CONSOLE_PORT),
            "listen_port":  _parse_int(self._recv_port.text() or str(DEFAULT_LISTEN_PORT),
                                       DEFAULT_LISTEN_PORT),
            "bind_ip":      bind_ip,
            "lv1_db":       self._lv1_db,
            "opacity":      self._opacity.value(),
            "sync_names":   self._sync_names_chk.isChecked(),
            "load_preamp_with_session": self._load_preamp_chk.isChecked(),
            "verbose_logging": self._verbose_logging_chk.isChecked(),
        }

    def _apply(self) -> None:
        d = self._collect_settings_dict()
        self.applied.emit(d)
        # save_settings is called by _on_settings_applied in OverlayWindow
        # with the full _settings dict (including geometry), so we must NOT
        # save a partial dict here — that would wipe the calibration geometry.

    def closeEvent(self, ev) -> None:
        # Persist settings even when panel is closed via the window "X".
        try:
            self._persist_panel_state()
        except Exception as exc:
            log.warning("settings close persist failed: %s", exc)
        super().closeEvent(ev)

    # ── Live status update methods (called from OverlayWindow) ────────────────

    def update_osc_status(self, ok: bool, detail: str, waiting: bool = False) -> None:
        confirmed = ok and not detail.startswith("Connecting:")
        if confirmed:
            self._stat_osc.setText("Connected")
            self._stat_osc.setStyleSheet("color: #27ae60;")
        elif ok:  # socket ready but not yet confirmed
            self._stat_osc.setText("Waiting for console…")
            self._stat_osc.setStyleSheet("color: #e67e22;")
        else:
            self._stat_osc.setText("Disconnected")
            self._stat_osc.setStyleSheet("color: #c0392b;")
        self.set_connected(confirmed)

    def update_lv1_status(self, text: str, lv1_ok: bool = False) -> None:
        self._stat_lv1.setText("Connected" if lv1_ok else "Disconnected")
        self._stat_lv1.setStyleSheet("color: #27ae60;" if lv1_ok else "color: #c0392b;")

    def update_lv1_session(self, session_name: str) -> None:
        self._lv1_session_name = str(session_name or "").strip()
        self._stat_lv1_session.setText(self._lv1_session_name or "—")

    def _refresh_local_ip(self, *_args) -> None:
        self._stat_ip.setText(_local_ip())

    def update_channels(self, text: str) -> None:
        pass  # channel count row removed


# ── StatusBar widget ──────────────────────────────────────────────────────────

class StatusBar(QWidget):
    """Thin status strip at the very bottom of the overlay (semi-transparent)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 1, 6, 1)
        lay.setSpacing(10)
        self._osc_lbl = QLabel("OSC: —")
        self._lv1_lbl = QLabel("LV1: —")
        self._ch_lbl  = QLabel("")
        for lbl in (self._osc_lbl, self._lv1_lbl, self._ch_lbl):
            lbl.setFont(QFont("Arial", 7))
            lbl.setStyleSheet("color: rgba(200,210,220,200);")
            lay.addWidget(lbl)
        lay.addStretch()
        self.setFixedHeight(18)
        self._osc_text = ""
        self._lv1_text = "—"
        self._ch_text  = "—"

    def set_osc(self, text: str) -> None:
        self._osc_text = text
        self._osc_lbl.setText(f"OSC: {text}")

    def set_lv1(self, text: str) -> None:
        self._lv1_text = text
        self._lv1_lbl.setText(f"LV1: {text}")

    def set_channels(self, text: str) -> None:
        self._ch_text = text
        self._ch_lbl.setText(text)

    def get_osc(self) -> str:      return self._osc_text
    def get_lv1(self) -> str:      return self._lv1_text
    def get_channels(self) -> str: return self._ch_text


# ── CalibrationOverlay ────────────────────────────────────────────────────────

class CalibrationOverlay(QWidget):
    """
    Semi-transparent orange rectangle the user drags and resizes to align
    the overlay with LV1's 16-channel preamp section.

    Covers exactly the combined width of 16 channel strips.

    Controls:
      • Drag anywhere in the centre to move
      • Drag any corner handle (orange squares) to resize
      • Live readout of x / y / cell_w / cell_h at the top
      • "Apply" button sets those values into the parent OverlayWindow settings
    """

    HANDLE = 14   # corner handle size in px

    def __init__(self, parent: "OverlayWindow") -> None:
        super().__init__(parent)
        s = parent._settings
        x = s.get("preamp_x0", 158)
        y = s.get("preamp_y0", 170)
        w = s.get("preamp_total_w", s.get("cell_w", 93) * 16)
        h = s.get("cell_h", 103)
        self.setGeometry(x, y, w, h)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

        self._drag   = False
        self._resize = False
        self._corner = ""
        self._offset = QPoint()

        # Apply button (always visible at top-right of the rect)
        self._apply_btn = QPushButton("✓ Apply", self)
        self._apply_btn.setFixedSize(68, 22)
        self._apply_btn.setStyleSheet(
            "QPushButton { background: #1a6e2a; color: white; "
            "border-radius: 3px; font-weight: bold; font-size: 9px; }"
            "QPushButton:hover { background: #25923b; }"
        )
        self._apply_btn.clicked.connect(self._apply)
        self._apply_btn.setCursor(Qt.CursorShape.ArrowCursor)
        self._place_button()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _place_button(self) -> None:
        self._apply_btn.move(self.width() - 72, 4)

    def _corner_rects(self) -> dict:
        H = self.HANDLE
        w, h = self.width(), self.height()
        return {
            "tl": QRect(0, 0, H, H),
            "tr": QRect(w - H, 0, H, H),
            "bl": QRect(0, h - H, H, H),
            "br": QRect(w - H, h - H, H, H),
        }

    def _hit_corner(self, pos: QPoint) -> str:
        for name, rect in self._corner_rects().items():
            if rect.contains(pos):
                return name
        return ""

    def get_settings(self) -> dict:
        w, h = self.width(), self.height()
        return {
            "preamp_x0":      self.x(),
            "preamp_y0":      self.y(),
            "preamp_total_w": w,           # exact pixel width → no integer-division gap
            "cell_w":         max(1, w // 16),   # kept for compat with manual spin-box
            "cell_h":         h,
        }

    # ── painting ─────────────────────────────────────────────────────────────

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        H = self.HANDLE

        # Tinted fill
        p.fillRect(0, 0, w, h, QColor(255, 140, 0, 25))

        # Vertical dividers for 16 slots
        cell_w = w / 16
        p.setPen(QPen(QColor(255, 140, 0, 80), 1, Qt.PenStyle.DashLine))
        for i in range(1, 16):
            x = round(i * cell_w)
            p.drawLine(x, 0, x, h)

        # Outer border
        p.setPen(QPen(QColor(255, 140, 0, 220), 2))
        p.drawRect(1, 1, w - 2, h - 2)

        # Corner handles
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 140, 0, 200))
        for rect in self._corner_rects().values():
            p.drawRect(rect)

        # Info readout
        cell_w_int = max(1, w // 16)
        info = (f"  x:{self.x()}  y:{self.y()}  "
                f"cell_w:{cell_w_int}  cell_h:{h}  "
                f"(total {w} × {h})")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QColor(255, 200, 80))
        p.drawText(0, 14, info)

    # ── mouse events ─────────────────────────────────────────────────────────

    def mousePressEvent(self, ev) -> None:
        pos = ev.position().toPoint()
        corner = self._hit_corner(pos)
        if corner:
            self._resize = True
            self._corner = corner
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self._drag = True
            # Store offset in parent coordinates for QCursor.pos() tracking
            gp = self.parent().mapFromGlobal(QCursor.pos())
            self._offset = QPoint(gp.x() - self.x(), gp.y() - self.y())
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.grabMouse()
        ev.accept()

    def mouseMoveEvent(self, ev) -> None:
        gp = self.parent().mapFromGlobal(QCursor.pos())
        pw = self.parent().width()
        ph = self.parent().height()
        if self._drag:
            new = QPoint(gp.x() - self._offset.x(), gp.y() - self._offset.y())
            new.setX(max(0, min(pw - self.width(),  new.x())))
            new.setY(max(0, min(ph - self.height(), new.y())))
            self.move(new)
            self.update()
        elif self._resize:
            ox, oy = self.x(), self.y()
            ow, oh = self.width(), self.height()
            c = self._corner
            if "r" in c:
                new_w = max(160, gp.x() - ox)
            else:  # l
                new_x = min(gp.x(), ox + ow - 160)
                new_w = ox + ow - new_x
                ox = new_x
            if "b" in c:
                new_h = max(60, gp.y() - oy)
            else:  # t
                new_y = min(gp.y(), oy + oh - 60)
                new_h = oy + oh - new_y
                oy = new_y
            self.setGeometry(ox, oy, new_w, new_h)
            self._place_button()
            self.update()
        else:
            # Hover: change cursor over corner handles
            corner = self._hit_corner(ev.pos())
            self.setCursor(
                Qt.CursorShape.SizeFDiagCursor if corner
                else Qt.CursorShape.SizeAllCursor
            )
        ev.accept()

    def mouseReleaseEvent(self, ev) -> None:
        self._drag = self._resize = False
        self.releaseMouse()
        ev.accept()

    # ── apply ─────────────────────────────────────────────────────────────────

    def _apply(self) -> None:
        log.info("CalibrationOverlay._apply: settings=%s", self.get_settings())
        win: OverlayWindow = self.parent()  # type: ignore[assignment]
        win._settings.update(self.get_settings())
        save_settings(win._settings)
        win._save_active_profile()
        win._reposition_widgets()
        self.hide()
        # CalibrationOverlay covers channel cells, not layer buttons — no-op here


# ── LayerCalibrationOverlay ───────────────────────────────────────────────────

class LayerCalibrationOverlay(QWidget):
    """
    Draggable / resizable blue rectangle that covers all 8 layer buttons.

    Shows 7 horizontal dividers.  "Apply" writes:
        layer_x, layer_y0, layer_w, layer_h, layer_gap
    into the parent OverlayWindow settings.
    """

    HANDLE = 14
    N_LAYERS = 8

    def __init__(self, parent: "OverlayWindow") -> None:
        super().__init__(parent)
        s = parent._settings
        x  = s.get("layer_x",   1773)
        y  = s.get("layer_y0",  422)
        w  = s.get("layer_w",   93)
        lh = s.get("layer_h",   81)
        lg = s.get("layer_gap", 0)
        h  = self.N_LAYERS * lh + (self.N_LAYERS - 1) * lg
        self.setGeometry(x, y, w, h)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

        self._drag   = False
        self._resize = False
        self._corner = ""
        self._offset = QPoint()

        self._apply_btn = QPushButton("✓ Apply", self)
        self._apply_btn.setFixedSize(68, 22)
        self._apply_btn.setStyleSheet(
            "QPushButton { background: #1a3e6e; color: white; "
            "border-radius: 3px; font-weight: bold; font-size: 9px; }"
            "QPushButton:hover { background: #2557a0; }"
        )
        self._apply_btn.clicked.connect(self._apply)
        self._apply_btn.setCursor(Qt.CursorShape.ArrowCursor)
        self._place_button()

    def _place_button(self) -> None:
        self._apply_btn.move(self.width() - 72, 4)

    def _corner_rects(self) -> dict:
        H = self.HANDLE
        w, h = self.width(), self.height()
        return {
            "tl": QRect(0, 0, H, H),
            "tr": QRect(w - H, 0, H, H),
            "bl": QRect(0, h - H, H, H),
            "br": QRect(w - H, h - H, H, H),
        }

    def _hit_corner(self, pos: QPoint) -> str:
        for name, rect in self._corner_rects().items():
            if rect.contains(pos):
                return name
        return ""

    def get_settings(self) -> dict:
        w, h = self.width(), self.height()
        btn_h = max(1, h // self.N_LAYERS)
        return {
            "layer_x":   self.x(),
            "layer_y0":  self.y(),
            "layer_w":   w,
            "layer_h":   btn_h,
            "layer_gap": 0,
        }

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        w, h = self.width(), self.height()
        H = self.HANDLE

        p.fillRect(0, 0, w, h, QColor(30, 100, 220, 25))

        row_h = h / self.N_LAYERS
        p.setPen(QPen(QColor(60, 140, 255, 100), 1, Qt.PenStyle.DashLine))
        for i in range(1, self.N_LAYERS):
            y = round(i * row_h)
            p.drawLine(0, y, w, y)

        p.setPen(QPen(QColor(60, 140, 255, 230), 2))
        p.drawRect(1, 1, w - 2, h - 2)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(60, 140, 255, 200))
        for rect in self._corner_rects().values():
            p.drawRect(rect)

        btn_h = max(1, h // self.N_LAYERS)
        info = f"  x:{self.x()}  y:{self.y()}  w:{w}  btn_h:{btn_h}"
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QColor(120, 200, 255))
        p.drawText(0, 14, info)

    def mousePressEvent(self, ev) -> None:
        pos = ev.position().toPoint()
        corner = self._hit_corner(pos)
        if corner:
            self._resize = True
            self._corner = corner
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self._drag = True
            self._offset = pos
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.grabMouse()
        ev.accept()

    def mouseMoveEvent(self, ev) -> None:
        # Use the true global cursor position — ev.position() can be clamped
        # to widget bounds on some Windows/Qt configurations even with grabMouse.
        gp = self.parent().mapFromGlobal(QCursor.pos())
        pw = self.parent().width()
        ph = self.parent().height()
        if self._drag:
            new = QPoint(gp.x() - self._offset.x(), gp.y() - self._offset.y())
            new.setX(max(0, min(pw - self.width(),  new.x())))
            new.setY(max(0, min(ph - self.height(), new.y())))
            self.move(new)
            self.update()
        elif self._resize:
            ox, oy = self.x(), self.y()
            ow, oh = self.width(), self.height()
            c = self._corner
            if "r" in c:
                new_w = max(40, min(pw - ox, gp.x() - ox))
            else:
                new_x = min(gp.x(), ox + ow - 40)
                new_w = ox + ow - new_x
                ox = new_x
            if "b" in c:
                new_h = max(self.N_LAYERS * 4, min(ph - oy, gp.y() - oy))
            else:
                new_y = min(gp.y(), oy + oh - self.N_LAYERS * 4)
                new_h = oy + oh - new_y
                oy = new_y
            self.setGeometry(ox, oy, new_w, new_h)
            self._place_button()
            self.update()
        else:
            corner = self._hit_corner(ev.position().toPoint())
            self.setCursor(
                Qt.CursorShape.SizeFDiagCursor if corner
                else Qt.CursorShape.SizeAllCursor
            )
        ev.accept()

    def mouseReleaseEvent(self, ev) -> None:
        self._drag = self._resize = False
        self.releaseMouse()
        ev.accept()

    def _apply(self) -> None:
        log.info("LayerCalibrationOverlay._apply: settings=%s", self.get_settings())
        win: OverlayWindow = self.parent()  # type: ignore[assignment]
        win._settings.update(self.get_settings())
        save_settings(win._settings)
        win._save_active_profile()
        win._reposition_widgets()
        self.hide()
        win._set_layer_buttons_calibrating(False)


# ── SpillCalibrationOverlay ───────────────────────────────────────────────────

class SpillCalibrationOverlay(QWidget):
    """Draggable crosshair bar — sample point is widget centre (spill button pixel)."""

    W = 200
    H = 44

    def __init__(self, parent: "OverlayWindow") -> None:
        super().__init__(parent)
        pos = parent._settings.get("spill_btn_pos") or [1818, 339]
        cx, cy = int(pos[0]), int(pos[1])
        self.setGeometry(cx - self.W // 2, cy - self.H // 2, self.W, self.H)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

        self._drag = False
        self._offset = QPoint()

        self._apply_btn = QPushButton("✓ Apply", self)
        self._apply_btn.setFixedSize(68, 22)
        self._apply_btn.setStyleSheet(
            "QPushButton { background: #6e3a1a; color: white; "
            "border-radius: 3px; font-weight: bold; font-size: 9px; }"
            "QPushButton:hover { background: #a05725; }"
        )
        self._apply_btn.clicked.connect(self._apply)
        self._apply_btn.setCursor(Qt.CursorShape.ArrowCursor)
        self._apply_btn.move(self.W - 72, 4)

    def _centre_global(self) -> tuple[int, int]:
        g = self.mapToGlobal(QPoint(self.width() // 2, self.height() // 2))
        return g.x(), g.y()

    def get_settings(self) -> dict:
        cx, cy = self._centre_global()
        off_color = _sample_screen_pixel(cx, cy)
        return {
            "spill_btn_pos": [cx, cy],
            "spill_off_color": off_color,
        }

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w // 2, h // 2

        p.fillRect(0, 0, w, h, QColor(255, 120, 30, 90))
        p.setPen(QPen(QColor(255, 160, 50, 240), 2))
        p.drawRect(1, 1, w - 2, h - 2)

        p.setPen(QPen(QColor(255, 220, 80, 255), 2))
        p.drawLine(cx - 24, cy, cx + 24, cy)
        p.drawLine(cx, cy - 16, cx, cy + 16)
        p.setBrush(QColor(255, 220, 80, 220))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(cx - 4, cy - 4, 8, 8)

        gx, gy = self._centre_global()
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QColor(255, 230, 180))
        p.drawText(6, 14, "Spill btn — drag crosshair (spill OFF)")
        p.drawText(6, h - 6, f"sample {gx},{gy}")

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton and not self._apply_btn.geometry().contains(ev.pos()):
            self._drag = True
            self._offset = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev) -> None:
        if self._drag:
            self.move(ev.globalPosition().toPoint() - self._offset)
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag = False
            self.update()

    def _apply(self) -> None:
        win: OverlayWindow = self.parent()  # type: ignore[assignment]
        cx, cy = self._centre_global()
        pos = [cx, cy]
        self.hide()
        win.update()

        def _finish() -> None:
            off_color = _sample_screen_pixel(pos[0], pos[1])
            patch = {
                "spill_btn_pos": pos,
                "spill_off_color": off_color,
            }
            win._settings.update(patch)
            save_settings(win._settings)
            win._save_active_profile()
            win._reset_spill_latch()
            log.info(
                "SpillCalibrationOverlay._apply: pos=%s off_color=%s (sampled after hide)",
                pos, off_color,
            )
            win._poll_tab()
            win._poll_db_layer()

        QTimer.singleShot(150, _finish)


# ── Layer detector (background thread) ───────────────────────────────────────

# ── OverlayWindow ─────────────────────────────────────────────────────────────

class OverlayWindow(QWidget):
    """
    Main overlay window — frameless, transparent, always-on-top.

    Layout (absolute pixel positions set from geometry settings):
      • 16 OverlayChannel cells across the preamp section
      • 8 LayerButton widgets on the right panel
      • 1 SettingsButton over LV1 logo
      • 1 StatusBar at the very bottom

    Mouse clicks on purely transparent areas fall through to LV1 via
    WM_NCHITTEST → HTTRANSPARENT in nativeEvent().
    """

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self._settings = settings
        self._profiles = load_profiles()
        self._active_session_path = ""
        self._active_session_name = ""
        self._preamp_load_pending = False    # True while waiting to send loaded values
        self._preamp_load_explicit = False   # True → session switch (prefer explicit save)
        self._emo_path: str = ""             # full path to the current .emo session file
        self._session_emo_mtime: float = 0.0 # .emo mtime when this session was loaded
        # Use saved gain range (or module defaults). Auto-expands/contracts when
        # console reports out-of-range values so we always match actual hardware.
        _dmc.GAIN_MIN = float(settings.get("gain_min", _dmc.GAIN_MIN))
        _dmc.GAIN_MAX = float(settings.get("gain_max", _dmc.GAIN_MAX))
        self._current_layer    = 0
        self._current_is_custom = False
        self._lv1_hwnd: Optional[int] = None
        self._session: Optional[Lv1SessionParser] = None
        self._n_ch = settings.get("n_channels", DEFAULT_N_CHANNELS)
        self._connected = False
        self._overlay_visible = True   # user toggle (settings panel on/off)
        # Start hidden to avoid startup flicker before tab/mode state is known.
        self._mode_visible = False     # True when LV1 is in Input mode + Mixer 1/2
        self._lv1_force_hidden = False
        self._overlay_tab_blocked = True  # pixel gate — fast hide when leaving mixer tab
        self._last_mode_log_key: Optional[tuple] = None
        self._spill_pixel_active = False
        self._spill_pending_active = False
        self._spill_pending_hits = 0
        self._last_spill_active = False
        self._latched_spill_link_group = 0
        self._link_focus_hint = 0
        self._selected_link_group = 0
        self._last_spill_prop10_raw = -1
        self._spill_prop10_pending_raw = -1
        self._spill_prop10_pending_hits = 0
        self._pre_spill_digicos: List[Optional[int]] = []
        self._db_last_layer = -1
        self._db_last_is_custom = False
        self._db_last_surface = 0
        self._last_completed_spill_link = 0
        self._spill_page = 0
        self._layer_channels_override: Optional[List[Optional[int]]] = None
        self._last_routing_digest: tuple = ()
        self._active_surface: Optional[int] = None  # set by _poll_tab; default Mixer 1
        self._last_input_tick = _read_last_input_tick()
        self._last_pixel_poll_ms = 0.0
        self._PIXEL_POLL_IDLE_MS = 500
        self._mouse_hook_id: Optional[int] = None
        self._forwarding_click = False        # guards WS_EX_TRANSPARENT toggle
        self._name_sync_queue: list[tuple[str, str]] = []
        self._last_synced_names: dict[int, str] = {}
        self._name_sync_timer = QTimer(self)
        self._name_sync_timer.setInterval(150)  # conservative pacing for real-console stability
        self._name_sync_timer.timeout.connect(self._flush_name_sync_tick)
        self._window_health_timer = QTimer(self)
        self._window_health_timer.setInterval(1000)
        self._window_health_timer.timeout.connect(self._check_window_health)
        # Single-shot timer: fires 3s after first .emo event and writes explicit save.
        # Cancelled by session switch so dialog-triggered .emo writes are ignored.
        self._explicit_save_timer = QTimer(self)
        self._explicit_save_timer.setSingleShot(True)
        self._explicit_save_timer.timeout.connect(self._do_explicit_preamp_save)

        self._setup_window()
        self._build_bridge_and_threads()
        self._build_ui()
        self._calibration: Optional[CalibrationOverlay] = None
        self._layer_calibration: Optional[LayerCalibrationOverlay] = None
        self._spill_calibration: Optional[SpillCalibrationOverlay] = None
        self._settings_panel: Optional[SettingsPanel] = None
        self._move_done_btn: Optional[QPushButton] = None
        self._load_lv1_session()
        self._refresh_emo_path()   # set up .emo watcher before first OSC connect

        # ── LV1 session polling ───────────────────────────────────────────────
        # Routing: debounced refresh when WAL/main DB token changes (25 ms poll).
        self._routing_debounce = QTimer()
        self._routing_debounce.setSingleShot(True)
        self._routing_debounce.setInterval(150)
        self._routing_debounce.timeout.connect(self._debounced_routing_refresh)

        # Debounced auto-save: fires 3 s after the last gain/phantom change.
        self._preamp_save_debounce = QTimer()
        self._preamp_save_debounce.setSingleShot(True)
        self._preamp_save_debounce.setInterval(1000)
        self._preamp_save_debounce.timeout.connect(self._auto_save_preamp)
        # Backup: periodic session/name poll.
        self._session_poll_timer = QTimer()
        self._session_poll_timer.setInterval(500)
        self._session_poll_timer.timeout.connect(self._poll_lv1_session)
        self._session_poll_timer.start()

        # Secondary: QFileSystemWatcher on both the file AND its parent directory.
        # Watching the directory is immune to atomic saves (write-new → rename-over),
        # which is how LV1 writes CurrentLV1.dat — the file watcher alone loses
        # track of the path after such a replace.
        self._watcher = QFileSystemWatcher()
        db = settings.get("lv1_db", "") or (find_autosave() or "")
        if db:
            db_path = Path(db)
            if db_path.exists():
                self._watcher.addPath(db)
            # Watch the WAL file — LV1 uses SQLite WAL mode; routing changes go
            # to <db>-wal and the main file mtime never changes between checkpoints.
            wal_path = db + "-wal"
            if Path(wal_path).exists():
                self._watcher.addPath(wal_path)
            # Always watch the parent directory — catches renames/atomic replaces.
            if db_path.parent.exists():
                self._watcher.addPath(str(db_path.parent))
        self._watcher.fileChanged.connect(self._on_lv1_file_changed)
        self._watcher.fileChanged.connect(self._on_emo_file_changed)
        self._watcher.directoryChanged.connect(self._on_lv1_dir_changed)
        # Debounce: LV1 may write several chunks in quick succession.
        self._reload_timer = QTimer()
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(200)
        self._reload_timer.timeout.connect(self._reload_lv1_session)

        # Poll for LV1 window position every 2 s
        self._lv1_track_timer = QTimer()
        self._lv1_track_timer.setInterval(2000)
        self._lv1_track_timer.timeout.connect(self._track_lv1)
        self._lv1_track_timer.start()
        self._track_lv1()

        # Tab/spill detection: cheap input watchdog (50 ms) triggers GetPixel
        # only after keyboard/mouse activity, plus a slow idle heartbeat.
        self._active_surface: Optional[int] = None  # last known stable surface (2 or 3)
        self._pending_surface: Optional[int] = None
        self._pending_surface_hits: int = 0
        self._tab_poll_timer = QTimer()
        self._tab_poll_timer.setInterval(50)
        self._tab_poll_timer.timeout.connect(self._tab_input_watchdog)

        # Lightweight DB poll every 25 ms (+ immediate poll on WAL writes).
        self._db_layer_timer = QTimer()
        self._db_layer_timer.setInterval(25)
        self._db_layer_timer.timeout.connect(self._poll_db_layer)


    # ── window setup ──────────────────────────────────────────────────────────

    def _setup_window(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool           # no taskbar entry
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Use the actual logical screen geometry — this is DPI-agnostic and
        # works correctly at 100 %, 125 %, 150 %, 200 % etc.
        screen_geo = QApplication.primaryScreen().geometry()
        dpr = QApplication.primaryScreen().devicePixelRatio()
        log.info("screen logical=%dx%d DPR=%.2f physical=%dx%d",
                 screen_geo.width(), screen_geo.height(), dpr,
                 round(screen_geo.width() * dpr), round(screen_geo.height() * dpr))
        self.setGeometry(screen_geo)
        self.setWindowTitle(f"DiGiCo LV1 Overlay v{__version__}")

    # ── OSC bridge / threads ──────────────────────────────────────────────────

    def _build_bridge_and_threads(self) -> None:
        self._bridge = OscBridge()
        self._store  = ChannelStateStore(self._n_ch)

        self._bridge.ch_name.connect(self._store.on_name)
        self._bridge.ch_gain.connect(self._store.on_gain)
        self._bridge.ch_gain.connect(self._on_gain_ceiling_check)
        self._bridge.ch_phantom.connect(self._store.on_phantom)
        self._bridge.ch_source.connect(self._store.on_source)
        self._store.channel_updated.connect(self._on_channel_updated)

        self._osc_thread  = QThread()
        self._osc_engine  = OscEngine(self._bridge)
        self._osc_engine.moveToThread(self._osc_thread)
        self._bridge.do_connect.connect(
            self._osc_engine.on_connect, Qt.ConnectionType.QueuedConnection)
        self._bridge.do_disconnect.connect(
            self._osc_engine.on_disconnect, Qt.ConnectionType.QueuedConnection)
        self._bridge.do_send.connect(
            self._osc_engine.on_send, Qt.ConnectionType.QueuedConnection)
        self._bridge.do_repoll.connect(
            self._osc_engine.on_repoll, Qt.ConnectionType.QueuedConnection)
        self._bridge.do_set_channels.connect(
            self._osc_engine.on_set_channels, Qt.ConnectionType.QueuedConnection)
        self._osc_thread.start()

        # Pre-warm the NIC description cache on a background thread so the
        # first Settings panel open is instant (avoids ~2 s PowerShell call).
        import threading as _threading
        _threading.Thread(
            target=_dmc._win_ip_descriptions, daemon=True, name="nic-cache-warm"
        ).start()

        self._hb_thread  = QThread()
        self._hb_worker  = HeartbeatWorker(self._bridge)
        self._hb_worker.moveToThread(self._hb_thread)
        self._bridge.osc_ready.connect(
            self._hb_worker.start, Qt.ConnectionType.QueuedConnection)
        self._bridge.status.connect(
            self._hb_worker.on_status, Qt.ConnectionType.QueuedConnection)
        self._hb_thread.start()

        self._bridge.status.connect(self._on_status)
        self._bridge.ch_count.connect(self._on_ch_count)
        self._bridge.console_name.connect(self._on_console_name)
        self._bridge.log.connect(lambda msg: log.debug("OSC: %s", msg))

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        s = self._settings

        # Channel cells
        self._cells: List[OverlayChannel] = []
        for slot_idx in range(16):
            cell = OverlayChannel(slot_idx, self)
            cell.want_gain.connect(self._send_gain)
            cell.want_phantom.connect(self._send_phantom)
            cell.resize(s["cell_w"], s["cell_h"])
            self._cells.append(cell)

        # Layer buttons
        self._layer_btns: List[LayerButton] = []
        for i in range(N_LAYERS):
            btn = LayerButton(i, LAYER_NAMES[i], lambda: self._lv1_hwnd, self)  # noqa: E731
            btn.layer_selected.connect(self._on_layer_selected)
            btn.resize(s["layer_w"], s["layer_h"])
            self._layer_btns.append(btn)

        # Settings button
        self._settings_btn = SettingsButton(self)
        self._settings_btn.resize(s["settings_w"], s["settings_h"])
        self._settings_btn.set_overlay_on(self._overlay_visible)
        self._settings_btn.clicked.connect(self._open_settings)
        self._settings_btn.position_changed.connect(self._on_settings_btn_moved)
        self._settings_btn.overlay_toggled.connect(self.set_overlay_visible)

        # Status bar — hidden from overlay; values forwarded to settings panel
        self._status_bar = StatusBar(self)
        self._status_bar.setGeometry(0, self.height() - 18, self.width(), 18)
        self._status_bar.hide()

        self._reposition_widgets()
        self._apply_layer(0)
        self._apply_mode_visibility()

    def _reset_spill_latch(self) -> None:
        """Clear spill detection state (e.g. after calibration)."""
        self._spill_pixel_active = False
        self._spill_pending_active = False
        self._spill_pending_hits = 0
        self._last_spill_active = False
        self._latched_spill_link_group = 0
        self._last_spill_prop10_raw = -1
        self._spill_prop10_pending_raw = -1
        self._spill_prop10_pending_hits = 0
        self._pre_spill_digicos = []
        self._layer_channels_override = None
        self._lv1_force_hidden = False

    def _clear_stale_spill_mapping(self) -> None:
        """Drop spill slot override after a DB/session reload outside active spill."""
        if self._spill_pixel_active:
            return
        self._layer_channels_override = None
        self._spill_page = 0
        self._latched_spill_link_group = 0
        self._last_spill_prop10_raw = -1
        self._spill_prop10_pending_raw = -1
        self._spill_prop10_pending_hits = 0

    def _refresh_selected_link(self, surface: int) -> None:
        """Track which link key is selected — prop10 only valid outside spill."""
        if not self._session or self._spill_pixel_active:
            return
        raw = self._session.read_track_link_index_raw(surface)
        link = self._session.prop10_raw_to_link_group(raw)
        if link > 0:
            self._selected_link_group = link

    def _poll_spill_link_group(self, surface: int) -> bool:
        """Read live prop10 while spill is active; LV1 uses 0-based index during spill."""
        if not self._session or not self._spill_pixel_active:
            return False
        raw = self._session.read_track_link_index_raw(surface)
        if raw == self._spill_prop10_pending_raw:
            self._spill_prop10_pending_hits += 1
        else:
            self._spill_prop10_pending_raw = raw
            self._spill_prop10_pending_hits = 1
        if self._spill_prop10_pending_hits < 2:
            return False
        if raw == self._last_spill_prop10_raw:
            return False
        self._last_spill_prop10_raw = raw
        link = self._session.prop10_spill_to_link_group(raw)
        if link <= 0:
            return False
        if link == self._latched_spill_link_group:
            return False
        log.info(
            "spill link live: %d -> %d (prop10=%d)",
            self._latched_spill_link_group, link, raw,
        )
        self._latched_spill_link_group = link
        self._apply_spill_page_mapping(force=True)
        return True

    def _begin_spill_link_capture(self) -> None:
        """Seed spill link from prop10 or last selected link key."""
        surface = self._active_surface or 2
        self._latched_spill_link_group = 0
        self._last_spill_prop10_raw = -1
        self._spill_prop10_pending_raw = -1
        self._spill_prop10_pending_hits = 0
        if not self._pre_spill_digicos:
            self._pre_spill_digicos = [
                c._ch for c in self._cells if getattr(c, "_ch", None)
            ]
        raw = self._session.read_track_link_index_raw(surface) if self._session else 0
        link = self._session.prop10_spill_to_link_group(raw) if self._session else 0
        if link <= 0 and self._selected_link_group > 0:
            link = self._selected_link_group
        if link > 0:
            self._latched_spill_link_group = link
            self._last_spill_prop10_raw = raw
            log.info(
                "spill link seed: %d (prop10=%d selected=%d)",
                link, raw, self._selected_link_group,
            )
        self._apply_spill_page_mapping(force=True)
        for delay_ms in (60, 180, 350, 500):
            QTimer.singleShot(delay_ms, self._sync_spill_page_from_lv1)

    def _sync_spill_page_from_lv1(self) -> None:
        """Re-read LV1 LayersPage (prop 14) and refresh spill slot mapping."""
        if self._spill_pixel_active:
            self._apply_spill_page_mapping(force=True)

    def _apply_spill_page_mapping(self, force: bool = False) -> bool:
        """Rebuild spill slots from live LV1 page (prop 14). Returns True if applied."""
        if not self._session or not self._spill_pixel_active:
            return False
        if self._latched_spill_link_group <= 0:
            return False
        surface = self._active_surface or 2
        spill_page = self._session.read_spill_page(surface)
        new_override = self._session.get_spill_slot_channels(
            self._latched_spill_link_group, surface, spill_page=spill_page,
        )
        page_changed = spill_page != self._spill_page
        override_changed = new_override != self._layer_channels_override
        if not force and not page_changed and not override_changed:
            return False
        self._spill_page = spill_page
        self._layer_channels_override = new_override
        log.info(
            "spill page: lv1_page=%d link=%d slots=%s",
            spill_page + 1,
            self._latched_spill_link_group,
            [c for c in new_override if c is not None],
        )
        self._apply_layer(spill_page)
        return True

    def _spill_calibration_active(self) -> bool:
        return bool(self._spill_calibration and self._spill_calibration.isVisible())

    def _any_calibration_visible(self) -> bool:
        return bool(
            (self._calibration       and self._calibration.isVisible()) or
            (self._layer_calibration and self._layer_calibration.isVisible()) or
            (self._spill_calibration and self._spill_calibration.isVisible())
        )

    def _reposition_widgets(self) -> None:
        log.info("_reposition_widgets: settings=%s", {k: v for k, v in self._settings.items()
                                                       if k not in ('console_ip',)})
        s = self._settings
        # Channel cells — use preamp_total_w if available for pixel-perfect fill
        x0   = s["preamp_x0"]
        y0   = s["preamp_y0"]
        ch   = s["cell_h"]
        tw   = s.get("preamp_total_w", s["cell_w"] * 16)
        for i, cell in enumerate(self._cells):
            cx = x0 + round(i * tw / 16)
            cw = round((i + 1) * tw / 16) - round(i * tw / 16)
            cell.setGeometry(cx, y0, cw, ch)

        # Layer buttons
        for i, btn in enumerate(self._layer_btns):
            y = s["layer_y0"] + i * (s["layer_h"] + s["layer_gap"])
            btn.setGeometry(s["layer_x"], y, s["layer_w"], s["layer_h"])

        # Settings button
        self._settings_btn.setGeometry(
            s["settings_x"], s["settings_y"], s["settings_w"], s["settings_h"]
        )

    def set_overlay_visible(self, visible: bool) -> None:
        self._overlay_visible = visible
        self._settings_btn.set_overlay_on(visible)
        # Cells are shown only when both the user toggle AND the mode check are True.
        self._apply_mode_visibility()
        for w in self._layer_btns:
            w.setVisible(visible)
        # Status bar stays hidden; settings button always visible

    # ── helpers ───────────────────────────────────────────────────────────────

    def _set_lv1_status(self, text: str) -> None:
        self._status_bar.set_lv1(text)
        if self._settings_panel:
            self._settings_panel.update_lv1_status(text, self._lv1_hwnd is not None)

    # ── LV1 window tracking ────────────────────────────────────────────────────

    def _track_lv1(self) -> None:
        hwnd = _find_lv1_hwnd()
        if hwnd != self._lv1_hwnd:
            self._lv1_hwnd = hwnd
            log.info("_track_lv1: hwnd=%s", hwnd)
        if self._lv1_hwnd:
            self._set_lv1_status("found ✓")
            # Re-assert always-on-top.  LV1 in borderless fullscreen sets
            # itself HWND_TOPMOST periodically, pushing us behind it.
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            HWND_TOPMOST = -1
            _user32.SetWindowPos(
                int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
            # Once found, slow down to 5 s — EnumWindows is relatively expensive
            self._lv1_track_timer.setInterval(5000)
        else:
            self._set_lv1_status("not found")
            # Poll quickly (2 s) until LV1 appears
            self._lv1_track_timer.setInterval(2000)

    # ── LV1 session parsing ────────────────────────────────────────────────────

    def _load_lv1_session(self) -> None:
        db = self._settings.get("lv1_db", "")
        if not db or not Path(db).exists():
            db = find_autosave() or ""
        if not db:
            self._set_lv1_status("no autosave")
            return
        try:
            self._session = Lv1SessionParser(db)
            self._session.refresh()   # populate maps before any _apply_layer call
            n = self._session._slot_count()
            self._set_lv1_status(f"session OK ({n} slots mapped)")
            self._on_session_identity_changed(self._session.db_path, self._session.get_session_name())
        except Exception as exc:
            self._set_lv1_status(f"session error: {exc}")
        self._apply_layer(self._current_layer, self._current_is_custom)

    def _on_lv1_file_changed(self, path: str) -> None:
        # On Windows an atomic save removes the path from the watcher.
        # Re-add it so future saves are still detected.
        if path and path not in self._watcher.files():
            if Path(path).exists():
                self._watcher.addPath(path)
        self._reload_timer.start()

    def _on_lv1_dir_changed(self, _dir: str) -> None:
        # Directory notification fires on atomic renames (the common LV1 save
        # pattern).  Re-register the file path in case it was replaced, then
        # let the debounce timer decide whether to reload.
        if self._session:
            db = self._session.db_path
            if db not in self._watcher.files() and Path(db).exists():
                self._watcher.addPath(db)
        self._reload_timer.start()

    def _debounced_routing_refresh(self) -> None:
        """Apply routing map after LV1 finishes a WAL write (debounced).

        Always applies the current layer when the timer fires.  This prevents
        a race where _poll_lv1_session consumes the DB token (with partial routes)
        before this handler runs, which would otherwise leave cells stale because
        changed=False causes an early return.  Name-sync is only sent when
        routing actually changed to avoid flooding the DiGiCo with redundant OSC.
        """
        if not self._session:
            return
        changed, _ = self._session.refresh_if_changed()
        if changed:
            self._clear_stale_spill_mapping()
            self._last_routing_digest = tuple(sorted(self._session._lv1_to_digico.items()))
            chs = self._session.get_layer_channels(
                self._current_layer, self._current_is_custom,
                surface_id=self._active_surface or 2,
            )
            log.info(
                "routing refresh: page=%d custom=%s strip1->D%s strip12->D%s",
                self._current_layer, self._current_is_custom,
                chs[0], chs[11],
            )
            self._send_channel_names()
        # Always re-apply regardless of changed — ensures cells reflect the
        # current routes even when _poll_lv1_session consumed the token first.
        self._apply_layer(self._current_layer, self._current_is_custom)

    def _poll_lv1_session(self) -> None:
        """Called every 500 ms — refreshes routes and always re-applies current page.

        Always calls _refresh_overlay_cells() regardless of whether routing
        changed, to recover from race conditions where a prior refresh consumed
        the DB token mid-session-switch and left cells showing stale data.
        """
        if not self._session:
            self._load_lv1_session()
            return
        changed, n = self._session.refresh_if_changed()
        if changed:
            self._clear_stale_spill_mapping()
        self._on_session_identity_changed(self._session.db_path, self._session.get_session_name())
        if changed:
            log.info("session updated: m1=factory=%d custom=%d m2=factory=%d custom=%d names=%d",
                     len(self._session._factory_maps[2]), len(self._session._custom_maps[2]),
                     len(self._session._factory_maps[3]), len(self._session._custom_maps[3]),
                     len(self._session._name_map))
            self._set_lv1_status(f"session updated ({n} slots)")
            # Apply immediately so cells reflect new routes right away.
            self._apply_layer(self._current_layer, self._current_is_custom)
            self._send_channel_names()
            # Settle-check 500 ms later for partial WAL writes.
            QTimer.singleShot(500, self._refresh_overlay_cells)


    def _reload_lv1_session(self) -> None:
        """Called via the file-watcher debounce — always force a full refresh."""
        try:
            self._clear_stale_spill_mapping()
            if self._session:
                self._session.refresh()
                n = self._session._slot_count()
                self._set_lv1_status(f"session reloaded ({n} slots)")
                self._on_session_identity_changed(
                    self._session.db_path, self._session.get_session_name()
                )
            else:
                self._load_lv1_session()
            self._refresh_overlay_cells()
            self._send_channel_names()
        except Exception as exc:
            log.error("_reload_lv1_session error: %s", exc, exc_info=True)

    def _send_channel_names(self) -> None:
        """Send LV1 channel names to DiGiCo via OSC (if enabled and connected)."""
        try:
            if not self._settings.get("sync_names", False):
                self._name_sync_queue.clear()
                if self._name_sync_timer.isActive():
                    self._name_sync_timer.stop()
                return
            if not self._connected or self._session is None:
                return
            name_map = self._session.get_digico_name_map()
            if not name_map:
                return
            q: list[tuple[str, str]] = []
            for digico_ch, name in sorted(name_map.items()):
                try:
                    ch_i = int(digico_ch)
                    path = f"/Input_Channels/{ch_i}/Channel_Input/name"
                    safe_name = self._sanitize_digico_name(name)
                    if not safe_name:
                        continue
                    # Send only changed names to reduce write pressure on console.
                    if self._last_synced_names.get(ch_i) == safe_name:
                        continue
                    q.append((path, safe_name))
                except Exception:
                    continue
            self._name_sync_queue = q
            if self._name_sync_queue and not self._name_sync_timer.isActive():
                self._name_sync_timer.start()
            if self._name_sync_queue:
                log.info("_send_channel_names: queued %d names to DiGiCo", len(self._name_sync_queue))
        except Exception as exc:
            # Never let name-sync crash the UI thread on real consoles.
            log.error("_send_channel_names failed: %s", exc)
            log.debug("traceback:\n%s", traceback.format_exc())

    @staticmethod
    def _sanitize_digico_name(name: object) -> str:
        """Return a console-safe ASCII name with bounded length."""
        s = str(name).encode("ascii", "ignore").decode("ascii").strip()
        if not s:
            return ""
        # Keep it conservative for real-console parsers.
        return s[:24]

    def _flush_name_sync_tick(self) -> None:
        """Send one queued name per tick to avoid burst-related console issues."""
        if not self._connected or not self._name_sync_queue:
            self._name_sync_queue.clear()
            if self._name_sync_timer.isActive():
                self._name_sync_timer.stop()
            return
        path, safe_name = self._name_sync_queue.pop(0)
        try:
            self._bridge.do_send.emit(path, safe_name)
            # Path format: /Input_Channels/{n}/Channel_Input/name
            parts = path.split("/")
            if len(parts) > 2:
                try:
                    ch_i = int(parts[2])
                    self._last_synced_names[ch_i] = safe_name
                except ValueError:
                    pass
        except Exception as exc:
            log.error("name-sync send failed (%s): %s", path, exc)
        if not self._name_sync_queue and self._name_sync_timer.isActive():
            self._name_sync_timer.stop()

    # ── Layer management ──────────────────────────────────────────────────────

    def _refresh_overlay_cells(self) -> None:
        """Re-sync cell visibility and channel assignment after layout/state changes."""
        self._apply_mode_visibility()
        self._apply_layer(self._current_layer, self._current_is_custom)

    def _apply_layer(self, layer_idx: int, is_custom: Optional[bool] = None) -> None:
        if is_custom is None:
            is_custom = self._current_is_custom
        log.debug("_apply_layer: %d  is_custom=%s  btns=%d cells=%d session=%s",
                  layer_idx, is_custom, len(self._layer_btns), len(self._cells),
                  self._session is not None)
        self._current_layer    = layer_idx
        self._current_is_custom = is_custom
        for btn in self._layer_btns:
            btn.set_selected(btn._idx == layer_idx)

        surface = self._active_surface or 2
        channels = self._layer_channels_override
        if channels is None:
            channels = (
                self._session.get_layer_channels(
                    layer_idx, is_custom=is_custom, surface_id=surface,
                )
                if self._session else [None] * 16
            )
        log.debug(
            "_apply_layer page=%d custom=%s surface=%d ch=%s",
            layer_idx, is_custom, surface,
            [(i + 1, c) for i, c in enumerate(channels) if c is not None],
        )
        for slot_idx, cell in enumerate(self._cells):
            ch = channels[slot_idx] if slot_idx < len(channels) else None
            if self._session and ch is not None:
                if self._layer_channels_override is not None:
                    ch2 = self._session.get_secondary_for_digico(ch)
                else:
                    ch2 = self._session.get_secondary_digico_channel(
                        layer_idx, slot_idx, is_custom, surface_id=surface,
                    )
            else:
                ch2 = None
            if ch is not None:
                # Pre-fill from store so the cell never shows a blank frame
                # between set_channel and the individual set_* calls.
                state = self._store.get(ch)
                cell._ch      = ch
                cell._ch2     = ch2
                cell._name    = state.name
                cell._gain    = state.gain_db
                cell._phantom = state.phantom
            else:
                cell._ch      = None
                cell._ch2     = None
                cell._name    = ""
                cell._gain    = 0.0
                cell._phantom = False
            # Explicitly schedule each cell to repaint.  Qt does not guarantee
            # that a parent repaint() cascades to children, so we trigger them
            # directly.  update() is async and Qt will batch these into one
            # composite frame, keeping GPU work to a single pass.
            cell.update()

    @pyqtSlot(int)
    def _on_layer_selected(self, idx: int) -> None:
        # During spill, LV1 owns page selection — always read prop 14, not local idx.
        if self._spill_pixel_active and self._session and self._latched_spill_link_group > 0:
            self._apply_spill_page_mapping(force=True)
            return
        self._apply_layer(idx)

    # ── Channel state updates ──────────────────────────────────────────────────

    @pyqtSlot(int)
    def _on_channel_updated(self, ch: int) -> None:
        state = self._store.get(ch)
        for cell in self._cells:
            if cell._ch == ch:
                cell.set_name(state.name)
                cell.set_gain(state.gain_db)
                cell.set_phantom(state.phantom)
        # Restart the save debounce; the actual write happens 3 s after the
        # last update so we don't hammer disk during initial poll or gain drags.
        if self._active_session_name and not self._preamp_load_pending:
            self._preamp_save_debounce.start()

    def _auto_save_preamp(self) -> None:
        """Write current gain+phantom to the autosave file (debounced, ~1 s)."""
        if self._active_session_name and not self._preamp_load_pending:
            try:
                save_preamp_autosave(self._active_session_name, self._store,
                                     emo_mtime=self._session_emo_mtime)
            except Exception as exc:
                log.warning("auto preamp save failed: %s", exc)

    def _apply_preamp_from_save(self) -> None:
        """Send loaded gain+phantom values to DiGiCo (called once on load)."""
        self._preamp_load_pending = False   # allow auto-save to resume
        if not self._connected or not self._active_session_name:
            return
        if not self._settings.get("load_preamp_with_session", False):
            return
        data = load_preamp_state(self._active_session_name,
                                 emo_path=self._emo_path,
                                 prefer_explicit=self._preamp_load_explicit)
        if not data:
            log.info("preamp load: no save file for '%s' — skipping", self._active_session_name)
            return
        log.info("applying preamp save: %d channels → DiGiCo", len(data))
        for ch, (gain, phantom) in sorted(data.items()):
            self._bridge.do_send.emit(_osc_gain(ch), gain)
            self._bridge.do_send.emit(_osc_phantom(ch), 1.0 if phantom else 0.0)
        # Suppress auto-save for 6 s after loading so the console's poll
        # responses (echoing back the values we just set) don't immediately
        # trigger _auto_save_preamp before the store settles.
        self._preamp_save_debounce.stop()
        QTimer.singleShot(6000, lambda: self._preamp_save_debounce.start() if self._active_session_name else None)

    # ── OSC send helpers ──────────────────────────────────────────────────────

    @pyqtSlot(int, object)
    def _on_gain_ceiling_check(self, _ch: int, v: object) -> None:
        """Auto-expand GAIN_MAX or contract GAIN_MIN if the console reports out-of-range values."""
        try:
            val = float(v)
        except (TypeError, ValueError):
            return
        changed = False
        if val > _dmc.GAIN_MAX:
            _dmc.GAIN_MAX = round(val * 2) / 2
            self._settings["gain_max"] = _dmc.GAIN_MAX
            log.info("GAIN_MAX auto-expanded to %.1f dB", _dmc.GAIN_MAX)
            changed = True
        if val < _dmc.GAIN_MIN:
            _dmc.GAIN_MIN = round(val * 2) / 2
            self._settings["gain_min"] = _dmc.GAIN_MIN
            log.info("GAIN_MIN auto-contracted to %.1f dB", _dmc.GAIN_MIN)
            changed = True
        if changed:
            save_settings(self._settings)

    def _send_gain(self, ch: int, val: float) -> None:
        self._bridge.do_send.emit(_osc_gain(ch), val)

    def _send_phantom(self, ch: int, on: bool) -> None:
        self._bridge.do_send.emit(_osc_phantom(ch), 1.0 if on else 0.0)

    # ── Bridge signal handlers ────────────────────────────────────────────────

    @pyqtSlot(bool, str)
    def _on_status(self, ok: bool, detail: str) -> None:
        self._connected = ok
        confirmed = ok and not detail.startswith("Connecting:")
        self._connected = confirmed
        self._status_bar.set_osc(detail)
        self._settings_btn.set_digico_connected(confirmed)
        if confirmed:
            log.info("OSC confirmed: %s", detail)
            QTimer.singleShot(2000, self._send_channel_names)  # give console extra settle time
            self._refresh_overlay_cells()
            if self._preamp_load_pending:
                # Keep _preamp_load_pending=True until _apply_preamp_from_save
                # actually runs — this prevents the poll results that arrive in
                # the next ~3 s from triggering _auto_save_preamp and overwriting
                # the good save file with the console's (reset) defaults.
                self._preamp_load_explicit = False  # startup → auto state b/c detection
                QTimer.singleShot(4000, self._apply_preamp_from_save)
        elif not ok:
            self._name_sync_queue.clear()
            self._last_synced_names.clear()
            if self._name_sync_timer.isActive():
                self._name_sync_timer.stop()
            log.warning("OSC disconnected/failed: %s", detail)
            # Auto-reconnect once after 5 s — covers the case where the console
            # temporarily went away (e.g. LV1 restart releasing the port) without
            # requiring the user to touch the Settings panel.
            QTimer.singleShot(5000, self._auto_reconnect_if_down)
        self._apply_mode_visibility()
        if self._settings_panel:
            self._settings_panel.update_osc_status(ok, detail)

    @pyqtSlot(int)
    def _on_ch_count(self, n: int) -> None:
        self._status_bar.set_channels(f"{n} ch")
        if self._settings_panel:
            self._settings_panel.update_channels(f"{n} channels")

    @pyqtSlot(str)
    def _on_console_name(self, name: str) -> None:
        if name:
            self._status_bar.set_osc(f"connected  ({name})")
            if self._settings_panel:
                self._settings_panel.update_osc_status(True, name)

    # ── Calibration ───────────────────────────────────────────────────────────

    def toggle_calibration(self) -> None:
        if self._calibration and self._calibration.isVisible():
            self._calibration.hide()
        else:
            if self._calibration is None:
                self._calibration = CalibrationOverlay(self)
            self._calibration.show()
            self._calibration.raise_()

    def toggle_layer_calibration(self) -> None:
        if self._layer_calibration and self._layer_calibration.isVisible():
            self._layer_calibration.hide()
            self._set_layer_buttons_calibrating(False)
        else:
            if self._layer_calibration is None:
                self._layer_calibration = LayerCalibrationOverlay(self)
            self._layer_calibration.show()
            self._layer_calibration.raise_()
            self._set_layer_buttons_calibrating(True)

    def toggle_spill_calibration(self) -> None:
        if self._spill_calibration and self._spill_calibration.isVisible():
            self._spill_calibration.hide()
        else:
            if self._spill_calibration is not None:
                self._spill_calibration.deleteLater()
                self._spill_calibration = None
            self._spill_calibration = SpillCalibrationOverlay(self)
            self._spill_calibration.show()
            self._spill_calibration.raise_()
            self.raise_()
            self.activateWindow()
            log.info("spill calibration shown at %s", self._spill_calibration.geometry())

    def _set_layer_buttons_calibrating(self, on: bool) -> None:
        """Show / hide layer buttons based on calibration mode."""
        for btn in self._layer_btns:
            btn.set_calibrating(on)

    # ── Settings-button move mode ──────────────────────────────────────────────

    def start_move_settings_btn(self) -> None:
        """Enter move mode so the user can drag the settings button."""
        self._settings_btn.set_move_mode(True)
        self._settings_btn.raise_()

        # Show a floating "Done" button so the user has an explicit exit point
        if self._move_done_btn is None:
            btn = QPushButton("✓ Done", self)
            btn.setStyleSheet(
                "QPushButton { background: #27ae60; color: white; font-weight: bold;"
                " font-size: 11px; padding: 3px 10px; border-radius: 4px; }"
                "QPushButton:hover { background: #2ecc71; }"
            )
            btn.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
            btn.clicked.connect(self._finish_move_settings_btn)
            self._move_done_btn = btn
        self._reposition_move_done_btn()
        self._move_done_btn.show()
        self._move_done_btn.raise_()

        # Reposition the done button whenever the settings button moves
        self._settings_btn.position_changed.connect(self._on_settings_btn_dragging)

    def _reposition_move_done_btn(self) -> None:
        if self._move_done_btn is None:
            return
        btn = self._settings_btn
        # Place "Done" just to the right of the settings button; fallback left if near edge
        bw = self._move_done_btn.sizeHint().width() + 4
        bh = self._move_done_btn.sizeHint().height() + 4
        x = btn.x() + btn.width() + 4
        y = btn.y()
        if x + bw > self.width():
            x = max(0, btn.x() - bw - 4)
        self._move_done_btn.setGeometry(x, y, bw, bh)

    @pyqtSlot(int, int, int, int)
    def _on_settings_btn_dragging(self, *_) -> None:
        """Keep Done button next to the settings button while dragging."""
        self._reposition_move_done_btn()

    def _finish_move_settings_btn(self) -> None:
        """Save position, exit move mode, and hide Done button."""
        btn = self._settings_btn
        self._settings.update({"settings_x": btn.x(), "settings_y": btn.y(),
                                "settings_w": btn.width(), "settings_h": btn.height()})
        save_settings(self._settings)
        self._settings_btn.set_move_mode(False)
        try:
            self._settings_btn.position_changed.disconnect(self._on_settings_btn_dragging)
        except RuntimeError:
            pass
        if self._move_done_btn:
            self._move_done_btn.hide()
        self._save_active_profile()
        log.info("Settings button saved at (%d,%d) %dx%d",
                 btn.x(), btn.y(), btn.width(), btn.height())

    @pyqtSlot(int, int, int, int)
    def _on_settings_btn_moved(self, x: int, y: int, w: int, h: int) -> None:
        """Called on mouse-release while dragging — just reposition the Done button."""
        self._reposition_move_done_btn()
        self._save_active_profile()

    # ── Settings panel ────────────────────────────────────────────────────────

    def _open_settings(self) -> None:
        if self._settings_panel and self._settings_panel.isVisible():
            self._settings_panel._refresh_local_ip()
            self._settings_panel.raise_()
            self._settings_panel.activateWindow()
            return

        panel = SettingsPanel(self._settings, connected=self._connected,
                              overlay_visible=self._overlay_visible, parent=self)
        self._settings_panel = panel
        panel.applied.connect(self._on_settings_applied)
        panel.disconnect_requested.connect(self._bridge.do_disconnect.emit)

        # Populate status immediately from cached values
        osc_detail = self._status_bar.get_osc() if self._connected else ""
        panel.update_osc_status(self._connected, osc_detail)
        panel.update_lv1_status(self._status_bar.get_lv1(), self._lv1_hwnd is not None)
        live_session = (
            self._active_session_name
            or (self._session.get_session_name() if self._session else "")
            or str(self._settings.get("lv1_session_name", ""))
        )
        panel.update_lv1_session(live_session)
        panel._refresh_local_ip()
        panel.update_channels(self._status_bar.get_channels())

        def _on_close() -> None:
            self._settings_panel = None
        panel.finished.connect(_on_close)

        panel.show()
        self._position_settings_panel(panel)

    def _position_settings_panel(self, panel: QWidget) -> None:
        """Place the settings panel adjacent to the settings button on screen."""
        btn        = self._settings_btn
        btn_global = btn.mapToGlobal(QPoint(0, btn.height()))  # bottom-left of button
        screen     = QApplication.primaryScreen().availableGeometry()

        pw = panel.width()
        ph = panel.height()
        x  = btn_global.x()
        y  = btn_global.y() + 4

        # Shift left if it clips the right edge
        if x + pw > screen.right():
            x = screen.right() - pw - 4

        # Flip above if it clips the bottom edge
        if y + ph > screen.bottom():
            top = btn.mapToGlobal(QPoint(0, 0)).y()
            y   = max(screen.top(), top - ph - 4)

        x = max(screen.left(), x)
        panel.move(x, y)

    @pyqtSlot(dict)
    def _on_settings_applied(self, d: dict) -> None:
        prev_load = bool(self._settings.get("load_preamp_with_session", False))
        prev_osc = self._osc_connection_params()
        self._settings.update(d)
        _apply_log_level(bool(self._settings.get("verbose_logging", False)))
        save_settings(self._settings)
        # If user just enabled "load preamp with session" while already connected,
        # apply the saved values immediately.
        now_load = bool(self._settings.get("load_preamp_with_session", False))
        if now_load and not prev_load and self._connected and self._active_session_name:
            QTimer.singleShot(200, self._apply_preamp_from_save)
        self._set_opacity(d.get("opacity", self._settings.get("opacity", 85)))
        self._reposition_widgets()
        params_changed = self._osc_connection_params() != prev_osc
        reconnect = params_changed or not self._connected
        if reconnect:
            ip = self._settings.get("console_ip", DEFAULT_CONSOLE_IP)
            reason = "params changed" if params_changed else "currently disconnected"
            log.info("_on_settings_applied: reconnecting to %s (%s)", ip, reason)
            self._bridge.do_disconnect.emit()
            QTimer.singleShot(300, self._connect_digico)
        else:
            log.info("_on_settings_applied: OSC settings unchanged — skip reconnect")
        # Reload LV1 session if path changed
        db = d.get("lv1_db", "")
        if db and self._session and self._session.db_path != db:
            self._settings["lv1_db"] = db
            self._load_lv1_session()
            # Update file watcher
            self._watcher.removePaths(self._watcher.files())
            if Path(db).exists():
                self._watcher.addPath(db)
        self._save_active_profile()
        self._refresh_overlay_cells()

    @staticmethod
    def _profile_patch_keys() -> tuple:
        return (
            "preamp_x0", "preamp_y0", "preamp_total_w", "cell_w", "cell_h",
            "layer_x", "layer_y0", "layer_w", "layer_h", "layer_gap",
            "settings_x", "settings_y", "settings_w", "settings_h",
            "mixer1_tab_pos", "mixer2_tab_pos",
            "spill_btn_pos", "spill_off_color", "spill_on_threshold",
        )

    def _save_active_profile(self) -> None:
        path_key = self._active_session_path.strip().lower()
        name_key = self._active_session_name.strip().lower()
        if not path_key and not name_key:
            return
        patch = {k: self._settings.get(k) for k in self._profile_patch_keys() if k in self._settings}
        if path_key:
            self._profiles.setdefault("by_path", {})[path_key] = {
                "session_name": self._active_session_name,
                "settings": patch,
            }
        if name_key:
            self._profiles.setdefault("by_name", {})[name_key] = {
                "session_path": self._active_session_path,
                "settings": patch,
            }
        save_profiles(self._profiles)

    def _refresh_emo_path(self) -> None:
        """Update _emo_path and _session_emo_mtime from the Windows registry."""
        try:
            emo = Lv1SessionParser.get_emo_path()
            if emo != self._emo_path:
                # Stop watching the old file, start watching the new one.
                if self._emo_path and self._emo_path in self._watcher.files():
                    self._watcher.removePath(self._emo_path)
                self._emo_path = emo
                if emo:
                    self._watcher.addPath(emo)
            if emo and Path(emo).exists():
                self._session_emo_mtime = Path(emo).stat().st_mtime
            else:
                self._session_emo_mtime = 0.0
        except Exception:
            pass

    def _on_emo_file_changed(self, path: str) -> None:
        """LV1 saved the .emo file → schedule an explicit preamp snapshot in 3 seconds.

        A single LV1 save fires multiple filesystem events (SQLite WAL + checkpoint)
        spread over several seconds.  Additionally LV1 writes to the .emo file when
        showing/dismissing the session-switch dialog — even when the user clicks
        "Don't Save".  We therefore SCHEDULE the snapshot write 3 seconds after the
        first event.  If a session switch arrives before the timer fires, the timer
        is cancelled so dialog-triggered writes never pollute the explicit save.
        """
        # Re-add: atomic saves on Windows remove the path from the watcher.
        if path and path not in self._watcher.files() and Path(path).exists():
            self._watcher.addPath(path)
        try:
            new_mtime = Path(path).stat().st_mtime if Path(path).exists() else 0.0
        except Exception:
            new_mtime = 0.0
        if new_mtime <= self._session_emo_mtime:
            return
        self._session_emo_mtime = new_mtime

        if not self._active_session_name or self._preamp_load_pending:
            return

        # Only start the timer if not already scheduled AND no post-fire cooldown.
        # The post-fire cooldown prevents subsequent .emo events (LV1 fires 2-3 events
        # per save, spread up to ~10s apart) from scheduling a second snapshot after
        # the user has already changed preamp values.
        now = time.monotonic()
        if self._explicit_save_timer.isActive():
            return  # first event's timer still pending — ignore duplicate
        if now < getattr(self, '_explicit_save_fired_until', 0.0):
            log.debug("emo: explicit save suppressed (post-fire cooldown active)")
            return
        log.info("LV1 session saved → explicit snapshot scheduled in 3s")
        self._explicit_save_timer.start(3000)

    def _do_explicit_preamp_save(self) -> None:
        """Called 3s after a .emo change — write the explicit preamp snapshot."""
        if not self._active_session_name or self._preamp_load_pending:
            return
        log.info("LV1 explicit save committed → writing preamp snapshot")
        try:
            save_preamp_explicit(self._active_session_name, self._store)
        except Exception as exc:
            log.warning("explicit preamp save failed: %s", exc)
            return
        # Block any further .emo events from triggering a re-save for 30 seconds.
        # LV1 writes the .emo file 2-3 times per save (WAL + checkpoint), spread up
        # to ~10s apart.  Without this cooldown the second event would overwrite the
        # correct snapshot with whatever values the user has changed to by then.
        self._explicit_save_fired_until = time.monotonic() + 30.0

    def _on_session_identity_changed(self, db_path: str, session_name: str) -> None:
        path_norm = str(db_path or "").strip()
        name_norm = str(session_name or "").strip()
        old_path = self._active_session_path
        old_name = self._active_session_name
        if old_path == path_norm and old_name == name_norm:
            return
        # Cancel any pending explicit save — it was triggered by the session-switch
        # dialog writing to .emo, not by a genuine user Ctrl+S.
        if self._explicit_save_timer.isActive():
            self._explicit_save_timer.stop()
            log.info("explicit save timer cancelled: session switch in progress")
        # ── Outgoing session: persist its preamp state ────────────────────────
        if old_name:
            try:
                # Always write autosave (state c continuity).
                save_preamp_autosave(old_name, self._store,
                                     emo_mtime=self._session_emo_mtime)
                # Explicit save: only if the .emo file was saved during this session.
                # _on_emo_file_changed keeps _session_emo_mtime current; if the .emo
                # file on disk is newer, the user saved after our last snapshot.
                emo_now = 0.0
                if self._emo_path and Path(self._emo_path).exists():
                    emo_now = Path(self._emo_path).stat().st_mtime
                if emo_now > self._session_emo_mtime:
                    log.info("session switch: .emo newer → writing explicit preamp snapshot")
                    save_preamp_explicit(old_name, self._store)
                else:
                    log.info("session switch: autosave written for '%s'", old_name)
            except Exception as exc:
                log.warning("preamp save on session switch failed: %s", exc)
        # Persist outgoing profile before switching identity.
        if old_path or old_name:
            self._save_active_profile()
        if name_norm != old_name:
            self._clear_stale_spill_mapping()
        log.info("session identity: '%s' -> '%s'", old_name, name_norm)
        # ── Incoming session: refresh .emo tracking ───────────────────────────
        self._refresh_emo_path()
        # Mark that we want to load preamp values for the incoming session.
        self._preamp_load_pending = bool(
            name_norm and self._settings.get("load_preamp_with_session", False)
        )
        self._active_session_path = path_norm
        self._active_session_name = name_norm
        self._settings["lv1_session_name"] = name_norm
        if path_norm:
            self._settings["lv1_db"] = path_norm
        if self._settings_panel:
            self._settings_panel.update_lv1_session(name_norm)
        # Read preference: exact path match -> name fallback.
        prof = None
        by_path = self._profiles.get("by_path", {}) if isinstance(self._profiles, dict) else {}
        by_name = self._profiles.get("by_name", {}) if isinstance(self._profiles, dict) else {}
        if path_norm:
            prof = by_path.get(path_norm.lower())
        if prof is None and name_norm:
            prof = by_name.get(name_norm.lower())
        if isinstance(prof, dict):
            patch = prof.get("settings")
            if isinstance(patch, dict) and patch:
                self._settings.update(patch)
                self._reposition_widgets()
                self._set_opacity(int(self._settings.get("opacity", 85)))
        save_settings(self._settings)
        self._refresh_overlay_cells()
        # If OSC is already up, _on_status won't fire again — schedule the load
        # directly so the session switch immediately sends saved preamp values.
        if self._preamp_load_pending and self._connected:
            log.info("preamp load scheduled: session switch while connected")
            self._preamp_load_explicit = True   # session switch → prefer explicit save
            QTimer.singleShot(500, self._apply_preamp_from_save)

    def _osc_connection_params(self) -> dict:
        return {
            "console_ip":   self._settings.get("console_ip", DEFAULT_CONSOLE_IP),
            "console_port": int(self._settings.get("console_port", DEFAULT_CONSOLE_PORT)),
            "listen_port":  int(self._settings.get("listen_port", DEFAULT_LISTEN_PORT)),
            "bind_ip":      self._settings.get("bind_ip", "0.0.0.0"),
            "n_channels":   int(self._settings.get("n_channels", DEFAULT_N_CHANNELS)),
        }

    def _connect_digico(self) -> None:
        s = self._settings
        self._bridge.do_connect.emit(
            s.get("console_ip",   DEFAULT_CONSOLE_IP),
            int(s.get("console_port", DEFAULT_CONSOLE_PORT)),
            int(s.get("listen_port",  DEFAULT_LISTEN_PORT)),
            int(s.get("n_channels",   DEFAULT_N_CHANNELS)),
            s.get("bind_ip",      "0.0.0.0"),
        )

    def _auto_reconnect_if_down(self) -> None:
        """Reconnect after an unexpected OSC drop, if still disconnected."""
        if not self._connected:
            log.info("auto-reconnect: still disconnected, retrying")
            self._bridge.do_disconnect.emit()
            QTimer.singleShot(300, self._connect_digico)

    # ── Click-through for transparent areas ───────────────────────────────────

    def _apply_click_through(self) -> None:
        """
        WA_TranslucentBackground already sets WS_EX_LAYERED on the HWND.
        We just apply the initial opacity here via Qt's own setWindowOpacity()
        which is the correct API when WA_TranslucentBackground is active.
        """
        self._set_opacity(self._settings.get("opacity", 85))

    def save_opacity(self, pct: int) -> None:
        """Persist opacity to settings and apply it without reconnecting."""
        self._settings["opacity"] = pct
        save_settings(self._settings)
        self._set_opacity(pct)
        self._save_active_profile()

    def set_verbose_logging(self, enabled: bool) -> None:
        """Persist verbose logging setting and apply log level live."""
        self._settings["verbose_logging"] = bool(enabled)
        _apply_log_level(bool(enabled))
        save_settings(self._settings)

    def save_panel_state(self, patch: dict) -> None:
        """Persist settings from panel close without reconnecting OSC."""
        prev_sync = bool(self._settings.get("sync_names", False))
        self._settings.update(patch)
        _apply_log_level(bool(self._settings.get("verbose_logging", False)))
        save_settings(self._settings)
        # Apply immediately-safe local effects.
        self._set_opacity(int(self._settings.get("opacity", 85)))
        self._reposition_widgets()
        now_sync = bool(self._settings.get("sync_names", False))
        if now_sync and not prev_sync and self._connected:
            log.info("sync_names enabled from settings close — scheduling name sync")
            QTimer.singleShot(500, self._send_channel_names)
        elif not now_sync and prev_sync:
            self._name_sync_queue.clear()
            self._last_synced_names.clear()
            if self._name_sync_timer.isActive():
                self._name_sync_timer.stop()
        self._save_active_profile()
        self._refresh_overlay_cells()

    def _set_opacity(self, pct: int) -> None:
        """
        Use Qt's setWindowOpacity() — the correct way for
        WA_TranslucentBackground + FramelessWindowHint windows.
        Do NOT call SetLayeredWindowAttributes(LWA_ALPHA): it switches the
        window out of per-pixel alpha mode and makes Qt's rendering invisible.
        """
        opacity = max(0.1, min(1.0, pct / 100.0))
        self.setWindowOpacity(opacity)

    # ── Click forwarding to LV1 ───────────────────────────────────────────────

    def _forward_click_to_lv1(self, screen_x: int, screen_y: int) -> None:
        """
        Forward a left-click to LV1 by temporarily applying WS_EX_TRANSPARENT
        to the overlay so the injected mouse_event passes straight through.

        Steps
        -----
        1. Guard with _forwarding_click to prevent re-entrancy.
        2. Add WS_EX_TRANSPARENT to the overlay's extended style.
        3. After 50 ms (enough for the style change to take effect) inject
           a physical mouse-down + mouse-up via mouse_event with
           MOUSEEVENTF_ABSOLUTE so LV1 receives it normally.
        4. After another 100 ms restore the original extended style.

        The WH_MOUSE_LL hook skips processing when _forwarding_click is True
        so the injected event does not cause a second overlay layer update.
        """
        if self._forwarding_click:
            return
        self._forwarding_click = True

        GWL_EXSTYLE       = -20
        WS_EX_TRANSPARENT = 0x00000020

        _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t

        hwnd      = int(self.winId())
        old_style = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                  old_style | WS_EX_TRANSPARENT)
        log.debug("_forward_click: WS_EX_TRANSPARENT set; injecting at (%d,%d)",
                  screen_x, screen_y)

        def _inject() -> None:
            # Find the exact child window at the target position now that
            # WS_EX_TRANSPARENT hides our overlay.
            pt = wt.POINT(screen_x, screen_y)
            _user32.WindowFromPoint.restype = wt.HWND
            target = _user32.WindowFromPoint(pt)
            target_int = int(target) if target else 0
            log.info("_forward_click: inject target=%d lv1=%d match=%s",
                     target_int, self._lv1_hwnd or 0,
                     target_int == (self._lv1_hwnd or 0))

            if target_int:
                # Compute coordinates relative to the target window's client area
                t_rect = wt.RECT()
                _user32.GetWindowRect(target, ctypes.byref(t_rect))
                cx = screen_x - t_rect.left
                cy = screen_y - t_rect.top
                lparam = (cy << 16) | (cx & 0xFFFF)
                log.debug("_forward_click: PostMsg to %d client(%d,%d)",
                          target_int, cx, cy)
                _user32.PostMessageW(target, WM_LBUTTONDOWN, 1, lparam)
                _user32.PostMessageW(target, WM_LBUTTONUP,   0, lparam)
            else:
                log.warning("_forward_click: no target window found at (%d,%d)",
                            screen_x, screen_y)
            log.debug("_forward_click: done; will restore style in 100 ms")
            QTimer.singleShot(100, _restore)

        def _restore() -> None:
            _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, old_style)
            self._forwarding_click = False
            log.debug("_forward_click: WS_EX_TRANSPARENT restored")

        QTimer.singleShot(50, _inject)

    # ── WM_NCHITTEST: transparent areas pass through to whatever is beneath ───

    def _install_wndproc(self) -> None:
        """
        Subclass the native HWND wndproc so WM_NCHITTEST is handled at the
        C level — no MSG pointer dereferencing, no ctypes.from_address crashes.
        lParam arrives as a proper C integer argument.
        """
        GWL_WNDPROC = -4
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            wt.HWND, ctypes.c_uint, wt.WPARAM, ctypes.c_ssize_t,
        )
        # GetWindowLongPtrW / SetWindowLongPtrW return pointer-sized values;
        # ctypes defaults to c_int (32-bit) which truncates on 64-bit Windows.
        _user32.GetWindowLongPtrW.restype  = ctypes.c_ssize_t
        _user32.SetWindowLongPtrW.restype  = ctypes.c_ssize_t
        _user32.CallWindowProcW.restype    = ctypes.c_ssize_t
        _user32.CallWindowProcW.argtypes   = [
            ctypes.c_ssize_t, wt.HWND, ctypes.c_uint, wt.WPARAM, ctypes.c_ssize_t,
        ]

        hwnd = int(self.winId())
        orig: int = _user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)

        @WNDPROC
        def _proc(h, msg, wp, lp):
            if msg == WM_HOTKEY and wp == _HOTKEY_EXIT_ID:
                QTimer.singleShot(0, self.close)
                return 0
            if msg == WM_NCHITTEST:
                # lParam carries physical screen pixels; Qt works in logical
                # pixels. Divide by devicePixelRatio so childAt() finds the
                # right widget on high-DPI displays (e.g. 150 %, 200 %).
                x = ctypes.c_short(lp & 0xFFFF).value
                y = ctypes.c_short((lp >> 16) & 0xFFFF).value
                dpr = self.devicePixelRatio()
                logical = QPoint(round(x / dpr), round(y / dpr))
                local = self.mapFromGlobal(logical)
                child = self.childAt(local)
                if child is None or child is self:
                    return HTTRANSPARENT
                # Non-calibrating LayerButtons are transparent hit-areas: let
                # the real physical click fall straight through to LV1.
                # The WH_MOUSE_LL hook (fires before delivery) keeps the overlay
                # in sync.  Synthesized input does not work on LV1's OpenGL
                # surface, so HTTRANSPARENT + real click is the only option.
                if isinstance(child, LayerButton) and not child._calibrating:
                    return HTTRANSPARENT
                # Unassigned or mode-blocked overlay cells are invisible and must
                # not block LV1's native gain/phantom controls beneath.
                if isinstance(child, OverlayChannel) and (child._ch is None or child._blocked):
                    return HTTRANSPARENT
                # Return HTCLIENT for all other interactive widgets.
                return HTCLIENT
            return _user32.CallWindowProcW(orig, h, msg, wp, lp)

        # Keep reference so the callback isn't garbage-collected
        self._wndproc_ref = _proc
        _user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, _proc)

        # Register Ctrl+Q as a global hotkey so it works even without focus
        _user32.RegisterHotKey(hwnd, _HOTKEY_EXIT_ID, MOD_CONTROL, ord('Q'))

    # ── Painting / show ───────────────────────────────────────────────────────

    def paintEvent(self, _) -> None:
        # Window background is fully transparent; children paint themselves.
        pass

    # ── Global mouse hook ─────────────────────────────────────────────────────

    def _install_mouse_hook(self) -> None:
        """
        Install a WH_MOUSE_LL (low-level mouse) hook that peeks at every
        left-click system-wide.  When a click lands inside a layer button's
        bounding rectangle the app updates its layer state.  The hook always
        calls CallNextHookEx so the click is never consumed — LV1 receives
        every click naturally without any forwarding.
        """
        _HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t,
        )

        # Ensure CallNextHookEx accepts a pointer-sized lParam on 64-bit Windows
        _user32.CallNextHookEx.restype  = ctypes.c_ssize_t
        _user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t,
        ]

        @_HOOKPROC
        def _hook(nCode, wParam, lParam):
            if nCode >= 0 and wParam == WM_LBUTTONDOWN:
                # Skip injected synthetic clicks so we don't double-process
                # our own _forward_click_to_lv1 mouse_event injection.
                if not self._forwarding_click:
                    try:
                        ms = ctypes.cast(lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                        dpr = self.devicePixelRatio()
                        lx = round(ms.pt.x / dpr)
                        ly = round(ms.pt.y / dpr)
                        log.debug("hook: LBUTTONDOWN phys=(%d,%d) logical=(%d,%d)",
                                  ms.pt.x, ms.pt.y, lx, ly)
                        for i, btn in enumerate(self._layer_btns):
                            g = btn.mapToGlobal(QPoint(0, 0))
                            if (g.x() <= lx < g.x() + btn.width()
                                    and g.y() <= ly < g.y() + btn.height()):
                                log.info("hook: matched layer btn %d", i)
                                self._on_layer_btn_click(i)
                                break
                    except Exception as exc:
                        log.exception("hook: exception: %s", exc)
            return _user32.CallNextHookEx(0, nCode, wParam, lParam)

        self._mouse_hook_cb = _hook  # prevent GC
        self._mouse_hook_id = _user32.SetWindowsHookExW(WH_MOUSE_LL, _hook, None, 0)
        if not self._mouse_hook_id:
            log.error("mouse hook install failed (err=%d)", ctypes.GetLastError())
        else:
            log.info("mouse hook installed: id=%s", self._mouse_hook_id)

    # Overlay cells are only relevant on Input Layer Mode (section=0).
    # Tab detection (pixel-based) already ensures we're on a Mixer tab, so
    # no page restriction is needed — the MGB filter handles empty pages.
    _VISIBLE_SECTION = 0   # Input Layer Mode

    # Screen positions of the Mixer 1 and Mixer 2 tab buttons (1920×1080).
    # When a tab is active its pixel is cyan/light-blue (~#1FD3F1 / #1DC7E4).
    # Inactive tabs show gray or black.  Both positions are stored in settings
    # so the user can recalibrate without touching the code.
    # Each mixer tab maps to a DB surface ID:
    #   Mixer 1 tab → surface=2,  Mixer 2 tab → surface=3
    _MIXER1_TAB_DEFAULT = (731, 39)
    _MIXER2_TAB_DEFAULT = (819, 23)

    def _lv1_is_foreground(self) -> bool:
        """True when LV1 (or this overlay) owns the foreground window."""
        fg = _user32.GetForegroundWindow()
        if not fg:
            return True
        if self._lv1_hwnd and fg == self._lv1_hwnd:
            return True
        try:
            if fg == int(self.winId()):
                return True
        except Exception:
            pass
        return False

    def _tab_input_watchdog(self) -> None:
        """Cheap 50 ms timer — only sample pixels after input or idle heartbeat."""
        if self._lv1_hwnd and not self._lv1_is_foreground():
            return
        tick = _read_last_input_tick()
        now_ms = time.monotonic() * 1000.0
        input_changed = tick != self._last_input_tick
        idle_due = (now_ms - self._last_pixel_poll_ms) >= self._PIXEL_POLL_IDLE_MS
        if input_changed or idle_due:
            if input_changed:
                self._last_input_tick = tick
            self._last_pixel_poll_ms = now_ms
            self._poll_tab()

    def _sample_surface_pixels(self) -> dict:
        """Sample mixer tab + spill button pixels in one GetDC pass."""
        mx1 = self._settings.get("mixer1_tab_pos", list(self._MIXER1_TAB_DEFAULT))
        mx2 = self._settings.get("mixer2_tab_pos", list(self._MIXER2_TAB_DEFAULT))
        points: list[tuple[int, int, str]] = [
            (int(mx1[0]), int(mx1[1]), "tab1"),
            (int(mx2[0]), int(mx2[1]), "tab2"),
        ]
        spill_pos = self._settings.get("spill_btn_pos")
        if spill_pos and len(spill_pos) >= 2 and not self._spill_calibration_active():
            points.append((int(spill_pos[0]), int(spill_pos[1]), "spill"))
        colors: dict = {}
        dc = _user32.GetDC(None)
        try:
            for x, y, key in points:
                colors[key] = int(_gdi32.GetPixel(dc, x, y))
        finally:
            _user32.ReleaseDC(None, dc)
        return colors

    def _surface_from_tab_pixels(self, colors: dict) -> Optional[int]:
        def _cyan(c: int) -> bool:
            return c >= 0 and (c & 0xFF) < 100 and ((c >> 8) & 0xFF) > 150 and ((c >> 16) & 0xFF) > 150

        c1 = colors.get("tab1", -1)
        c2 = colors.get("tab2", -1)
        if _cyan(c1):
            return 2
        if _cyan(c2):
            return 3
        return None

    def _spill_active_from_pixel(self, colors: dict) -> bool:
        if self._spill_calibration_active():
            return False
        if "spill" not in colors:
            return False
        c = colors["spill"]
        if c < 0:
            return False
        if _pixel_is_spill_green(c):
            return True
        off = self._settings.get("spill_off_color")
        if off is not None:
            thr_on = int(self._settings.get("spill_on_threshold", 45))
            thr_off = max(12, thr_on // 2)
            dist = _pixel_rgb_distance(c, int(off))
            if self._spill_pixel_active:
                return dist >= thr_off
            return dist >= thr_on
        return _pixel_is_highlight(c)

    def _update_spill_latch(self, active: bool) -> None:
        if active == self._spill_pending_active:
            self._spill_pending_hits += 1
        else:
            self._spill_pending_active = active
            self._spill_pending_hits = 1
        required = 2 if active else 2
        if self._spill_pending_hits >= required and active != self._spill_pixel_active:
            was_active = self._spill_pixel_active
            self._spill_pixel_active = active
            log.info("spill pixel: %s", "active" if active else "inactive")
            if active and not was_active:
                self._last_spill_active = True    # keep in sync with pixel state
                surface = self._active_surface or 2
                self._refresh_selected_link(surface)
                self._latched_spill_link_group = 0
                self._last_spill_prop10_raw = -1
                self._spill_prop10_pending_raw = -1
                self._spill_prop10_pending_hits = 0
                self._link_focus_hint = 0
                self._begin_spill_link_capture()
            elif not active and was_active:
                # Clear the override immediately here so _poll_db_layer's early
                # return (surface=None) never leaves stale spill cells on screen.
                self._layer_channels_override = None
                self._spill_page = 0
                self._last_spill_active = False   # keep in sync so poll sees no change
                if self._latched_spill_link_group > 0:
                    self._last_completed_spill_link = self._latched_spill_link_group
                self._latched_spill_link_group = 0
                self._last_spill_prop10_raw = -1
                self._spill_prop10_pending_raw = -1
                self._spill_prop10_pending_hits = 0
                self._pre_spill_digicos = []
                # Re-apply current layer so cells immediately show normal routing.
                QTimer.singleShot(0, lambda: self._apply_layer(
                    self._current_layer, self._current_is_custom
                ))

    def _schedule_tab_resample(self) -> None:
        """One-shot faster re-sample during tab transitions only."""
        QTimer.singleShot(40, self._poll_tab)

    def _on_mixer_tab_unblocked(self) -> None:
        """Restore overlay state immediately after returning to a mixer tab."""
        self._overlay_tab_blocked = False
        self._apply_mode_visibility()
        self._poll_db_layer()

    def _is_lv1_input_mode(self, layer: int, section: int, is_custom: bool, section_page: int = -1) -> bool:
        # Tab detection (pixel) already gates which mixer is active.
        # Here we only need to check that LV1 is in Input Layer Mode.
        return (section == self._VISIBLE_SECTION) or (section_page == 1)

    def _poll_tab(self) -> None:
        """Pixel-sample mixer tabs (+ spill button) on input or idle heartbeat."""
        if self._lv1_hwnd and not self._lv1_is_foreground():
            return

        try:
            colors = self._sample_surface_pixels()
            surface = self._surface_from_tab_pixels(colors)
            spill_raw = self._spill_active_from_pixel(colors)
        except Exception:
            surface = self._active_mixer_surface_fallback()
            spill_raw = False

        self._update_spill_latch(spill_raw)
        if surface is not None and not self._spill_pixel_active:
            self._refresh_selected_link(surface)
        elif surface is not None and self._spill_pixel_active:
            if self._poll_spill_link_group(surface):
                self._poll_db_layer()
        if self._spill_pixel_active != self._last_spill_active:
            self._poll_db_layer()

        if surface == self._pending_surface:
            self._pending_surface_hits += 1
        else:
            prev_pending = self._pending_surface
            leaving_mixer = (
                self._active_surface is not None
                and surface is None
                and prev_pending is not None
            )
            entering_mixer = surface is not None and (
                prev_pending is None or self._overlay_tab_blocked
            )
            self._pending_surface = surface
            self._pending_surface_hits = 1
            if leaving_mixer or entering_mixer:
                self._schedule_tab_resample()
            return

        hits = self._pending_surface_hits

        if self._active_surface is not None and surface is None and hits >= 2:
            if not self._overlay_tab_blocked:
                self._overlay_tab_blocked = True
                if self._mode_visible:
                    self._mode_visible = False
                self._apply_mode_visibility()

        if surface is not None and hits >= 2 and self._overlay_tab_blocked:
            self._on_mixer_tab_unblocked()

        if self._active_surface is not None and surface is None:
            required_hits = 2
        elif self._active_surface is None and surface is not None:
            required_hits = 3
        else:
            required_hits = 2

        if hits < required_hits:
            return

        if surface != self._active_surface:
            log.info("tab changed: surface %s -> %s", self._active_surface, surface)
            self._active_surface = surface
            if surface is None:
                self._overlay_tab_blocked = True
                if self._mode_visible:
                    self._mode_visible = False
                self._apply_mode_visibility()
            elif self._overlay_tab_blocked:
                self._on_mixer_tab_unblocked()
            else:
                self._poll_db_layer()

    def _active_mixer_surface_fallback(self) -> Optional[int]:
        """Legacy single-sample path used only on GDI errors."""
        try:
            colors = self._sample_surface_pixels()
            return self._surface_from_tab_pixels(colors)
        except Exception:
            return 2

    def _poll_db_layer(self) -> None:
        """Read active layer/mode from LV1 SQLite every 150 ms.

        Uses the cached _active_surface (updated by _poll_tab every 150 ms)
        so this tight loop does only one SQLite open/close per call — no GDI.
        """
        if not self._session:
            return

        if self._session._db_token_changed():
            # Only start if not already pending — prevents continuous WAL
            # writes from perpetually resetting the timer and delaying refresh.
            if not self._routing_debounce.isActive():
                self._routing_debounce.start()

        if self._overlay_tab_blocked:
            if self._mode_visible:
                self._mode_visible = False
                self._apply_mode_visibility()
            return

        surface = self._active_surface   # set by _poll_tab
        if surface is None:
            # Not on a Mixer tab — ensure cells are hidden
            if self._mode_visible or self._lv1_force_hidden:
                self._mode_visible = False
                self._lv1_force_hidden = False
                log.info("db_layer: cells hidden  (tab=other)")
                self._apply_mode_visibility()
            return

        try:
            mode_ctx = self._session.get_mode_context(surface)
            db_page = int(mode_ctx.get("layer", 0))   # prop 14 — active page (factory/custom or spill sub-page)
            is_custom = bool(mode_ctx.get("is_custom", False))
            section = int(mode_ctx.get("section", -1))
            section_page = int(mode_ctx.get("section_page", -1))
        except Exception:
            return

        spill_active = self._spill_pixel_active
        # Page selection is driven entirely by the LV1 DB (prop 14,
        # eProp_LayersPage).  In normal mode it is the active Factory/Custom
        # page index; during spill it is the spill sub-page.  We must NOT use
        # the local _current_layer here — the layer-button mouse hook is
        # disabled in stability mode, so _current_layer never advances past 0
        # and would freeze the overlay on Page 1.
        page_layer = db_page

        if spill_active and self._session:
            self._poll_spill_link_group(surface)

        if not spill_active and self._session:
            self._refresh_selected_link(surface)

        if not spill_active and self._session:
            page_channels = self._session.get_layer_channels(
                page_layer, is_custom, surface_id=surface,
            )
            if any(c for c in page_channels if c):
                self._pre_spill_digicos = page_channels

        if (
            self._db_last_layer >= 0
            and page_layer == 0
            and self._db_last_layer != 0
            and self._session
            and not spill_active
        ):
            page_channels = self._session.get_layer_channels(
                self._db_last_layer, self._db_last_is_custom, surface_id=surface,
            )
            if any(c for c in page_channels if c):
                self._pre_spill_digicos = page_channels

        self._db_last_layer = page_layer
        self._db_last_is_custom = is_custom
        surface_changed = surface != self._db_last_surface
        self._db_last_surface = surface

        tab_name = "Mixer1" if surface == 2 else "Mixer2"

        link_group = int(mode_ctx.get("link_group", 0))
        if spill_active:
            link_group = self._latched_spill_link_group

        force_hidden = bool(mode_ctx.get("is_all", False))
        force_hidden_changed = force_hidden != self._lv1_force_hidden
        self._lv1_force_hidden = force_hidden

        spill_changed = spill_active != self._last_spill_active
        if spill_changed:
            self._last_spill_active = spill_active
            if not spill_active:
                self._spill_page = 0
                self._layer_channels_override = None
                log.info("db_layer: spill off link_group=%d", link_group)
            else:
                log.info("db_layer: spill on link_group=%d", link_group)

        mode_log_key = (
            bool(mode_ctx.get("is_all", False)),
            spill_active,
            link_group,
            int(mode_ctx.get("track_mode", -1)) if isinstance(mode_ctx.get("track_mode"), int)
            else str(mode_ctx.get("track_mode")),
        )
        if mode_log_key != self._last_mode_log_key:
            self._last_mode_log_key = mode_log_key
            log.info(
                "db_layer: mode track=%s all=%s spill=%s link=%s hidden=%s",
                mode_ctx.get("track_mode"),
                mode_ctx.get("is_all", False),
                spill_active,
                link_group,
                self._lv1_force_hidden,
            )

        in_input_mode = (
            self._is_lv1_input_mode(page_layer, section, is_custom, section_page)
            and bool(mode_ctx.get("is_input_layer", True))
        )
        should_show = in_input_mode and not self._lv1_force_hidden
        if should_show != self._mode_visible or force_hidden_changed:
            self._mode_visible = should_show
            mode_bits = []
            if mode_ctx.get("is_all"):
                mode_bits.append("ALL")
            elif spill_active:
                mode_bits.append("SPILL+MAP")
            else:
                mode_bits.append("NORMAL")
            log.info("db_layer: cells %s  (tab=%s surface=%d section=%d section_page=%d mode=%s layer_type=%s)",
                     "shown" if should_show else "hidden",
                     tab_name, surface, section, section_page, "/".join(mode_bits),
                     mode_ctx.get("layer_type", "?"))
            self._apply_mode_visibility()

        if spill_active and link_group > 0:
            self._apply_spill_page_mapping()
        elif spill_changed and not spill_active:
            self._apply_layer(page_layer, is_custom)
        elif not spill_active and (
            surface_changed
            or page_layer != self._current_layer
            or is_custom != self._current_is_custom
        ):
            if surface_changed:
                log.info("db_layer: mixer surface -> %s (tab=%s page=%d custom=%s)",
                         surface, tab_name, page_layer, is_custom)
            elif is_custom != self._current_is_custom:
                log.info("db_layer: mode changed to %s (surface=%d page=%d)",
                         'Custom' if is_custom else 'Factory', surface, page_layer)
            else:
                log.info("db_layer: page %d -> %d", self._current_layer, page_layer)
            self._apply_layer(page_layer, is_custom)

        # Reactive stale-cell guard: compare what's in the cells against what
        # the current routes say should be there.  Fires within one 25ms tick
        # of any mismatch — catches session-switch races that the debounce
        # missed regardless of token-consumption ordering.
        if (self._session
                and not spill_active
                and self._layer_channels_override is None
                and self._mode_visible):
            expected = self._session.get_layer_channels(
                page_layer, is_custom, surface_id=surface,
            )
            if [c._ch for c in self._cells] != expected:
                self._apply_layer(page_layer, is_custom)

    def _apply_mode_visibility(self) -> None:
        """Block or unblock cell painting based on the current mode.

        Uses _blocked flag + update() rather than setVisible(), which can
        leave ghost pixels on transparent frameless windows.  The WNDPROC
        also returns HTTRANSPARENT for blocked cells so clicks pass through.
        """
        # Keep cells hidden until OSC is confirmed; this avoids startup flicker
        # while the overlay has geometry but no live DiGiCo state yet.
        blocked = not (
            self._mode_visible
            and self._overlay_visible
            and self._connected
            and not self._lv1_force_hidden
            and not self._overlay_tab_blocked
        )
        for cell in self._cells:
            cell._blocked = blocked
            cell.update()   # Qt does not auto-repaint children when parent updates

    def _on_layer_btn_click(self, idx: int) -> None:
        if 0 <= idx < len(self._layer_btns):
            self._layer_btns[idx].layer_selected.emit(idx)

    def _remove_mouse_hook(self) -> None:
        if self._mouse_hook_id:
            _user32.UnhookWindowsHookEx(self._mouse_hook_id)
            self._mouse_hook_id = None

    def showEvent(self, ev) -> None:
        log.info("showEvent: hwnd=%s dpr=%.2f screen=%s",
                 int(self.winId()) if self.testAttribute(Qt.WidgetAttribute.WA_WState_Created) else "?",
                 self.devicePixelRatio(),
                 QApplication.primaryScreen().geometry())
        super().showEvent(ev)
        QTimer.singleShot(0, self._apply_click_through)
        QTimer.singleShot(0, self._install_wndproc)
        # Stability mode: skip low-level global mouse hook.
        # Layer following is driven by DB polling, so hook is optional and can
        # trigger native crashes on some real-console/LV1 systems.
        # Recompute physical positions now that the window is on screen so
        # mapToGlobal returns real screen coordinates.
        QTimer.singleShot(0, self._reposition_widgets)
        QTimer.singleShot(0, self._refresh_overlay_cells)
        if not self._db_layer_timer.isActive():
            self._db_layer_timer.start()
        if not self._tab_poll_timer.isActive():
            # Sample immediately so the first DB poll has a valid surface.
            self._poll_tab()
            self._tab_poll_timer.start()
        if not self._window_health_timer.isActive():
            self._window_health_timer.start()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.activateWindow()

    def _check_window_health(self) -> None:
        """Fail-safe: if the main overlay window vanishes, terminate process."""
        try:
            hwnd = int(self.winId())
            if hwnd and _user32.IsWindow(hwnd) and _user32.IsWindowVisible(hwnd):
                return
            log.error("window health check failed (hwnd=%s) — forcing app exit", hwnd)
        except Exception as exc:
            log.error("window health check exception: %s", exc)
        QApplication.quit()

    # ── Cleanup ───────────────────────────────────────────────────────────────


    def closeEvent(self, ev) -> None:
        log.info("closeEvent")
        # Persist preamp state immediately — don't rely on the debounce having fired.
        if self._active_session_name and not self._preamp_load_pending:
            try:
                save_preamp_autosave(self._active_session_name, self._store,
                                     emo_mtime=self._session_emo_mtime)
                log.info("preamp autosave on close: %s", self._active_session_name)
                emo_now = 0.0
                if self._emo_path and Path(self._emo_path).exists():
                    emo_now = Path(self._emo_path).stat().st_mtime
                if emo_now > self._session_emo_mtime:
                    save_preamp_explicit(self._active_session_name, self._store)
                    log.info("preamp explicit save on close")
            except Exception as exc:
                log.warning("preamp save on close failed: %s", exc)
        self._save_active_profile()
        save_settings(self._settings)
        try:
            _user32.UnregisterHotKey(int(self.winId()), _HOTKEY_EXIT_ID)
        except Exception:
            pass
        self._remove_mouse_hook()
        # If an explicit save was scheduled (user saved right before closing),
        # execute it immediately rather than discarding it.
        if self._explicit_save_timer.isActive():
            self._explicit_save_timer.stop()
            self._do_explicit_preamp_save()
        self._window_health_timer.stop()
        self._db_layer_timer.stop()
        self._tab_poll_timer.stop()
        self._bridge.do_disconnect.emit()
        for thread in (self._osc_thread, self._hb_thread):
            thread.quit()
            thread.wait(2000)
        super().closeEvent(ev)
        # os._exit bypasses any remaining Qt or Python cleanup that could keep
        # the process alive — ensures no zombie process is ever left behind.
        os._exit(0)


# ── Entry point ────────────────────────────────────────────────────────────────


def _hide_attached_console() -> None:
    """Hide the console window when launched via python.exe on Windows."""
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def main() -> int:
    _hide_attached_console()

    app = QApplication(sys.argv)
    app.setApplicationName(f"DiGiCo LV1 Overlay v{__version__}")

    settings = load_settings()
    _apply_log_level(bool(settings.get("verbose_logging", False)))

    # Auto-detect LV1 autosave if not configured
    if not Path(settings.get("lv1_db", "")).exists():
        found = find_autosave()
        if found:
            settings["lv1_db"] = found

    win = OverlayWindow(settings)
    win.show()

    # Defer the auto-connect until the main event loop is running.
    # In PyQt6 ≥6.9 queued cross-thread signals are only delivered once
    # QCoreApplication.exec() has started, so emitting do_connect before
    # app.exec() would silently stall on newer builds.
    QTimer.singleShot(0, win._connect_digico)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
