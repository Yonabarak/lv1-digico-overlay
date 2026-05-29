"""
DiGiCo multi-channel preamp controller — interim GUI (no LV1 overlay).

Disguises itself as the DiGiCo iPad app using the iPad-native OSC paths:
  /Input_Channels/{n}/Channel_Input/name
  /Input_Channels/{n}/Channel_Input/analog_gain
  /Input_Channels/{n}/Channel_Input/phantom

Routing (rack name / input number) is read from the console's session file
via UNC admin share (\\\\<console_ip>\\C$\\...) — see _try_parse_session().

Architecture (thread model):
  Main thread    — PyQt6 GUI (MainWindow, ChannelTable, SettingsBar)
  OscThread      — OscEngine (UDP server + client, staggered poll queue)
  HbThread       — HeartbeatWorker (periodic full re-poll)
  OscBridge      — pyqtSignal bus; all cross-thread communication goes here
  ChannelStateStore — in-memory state per channel; lives on main thread
"""
from __future__ import annotations

import logging
import re
import select
import socket
import struct
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

# Suppress pythonosc's "Unhandled parameter type" root-logger warnings that
# fire for every DiGiCo-proprietary OSC type tag (@, C, etc.).
logging.getLogger().setLevel(logging.ERROR)

from PyQt6.QtCore import QObject, QThread, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QIntValidator
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDial,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient


class _RobustOSCServer(BlockingOSCUDPServer):
    """Suppress stderr noise from non-OSC UDP packets on the shared port.

    Malformed datagrams (UnicodeDecodeError, ValueError, struct.error) raised
    inside the OSC parser are swallowed in process_request — the exception
    never reaches BaseServer's default error handler.  handle_error is also
    overridden as a safety net.

    allow_reuse_address=True so the port can be reclaimed immediately after
    a previous instance exits (avoids WinError 10048 on quick restarts).
    """
    allow_reuse_address = True
    _SILENT_ERRORS = (UnicodeDecodeError, ValueError, struct.error)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Catch malformed-datagram parse errors HERE — before they reach
        # _handle_request_noblock's except clause, which would otherwise
        # dispatch to BaseServer.handle_error and print a traceback.
        try:
            super().process_request(request, client_address)
        except self._SILENT_ERRORS:
            try:
                self.shutdown_request(request)
            except Exception:
                pass

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], self._SILENT_ERRORS):
            return
        super().handle_error(request, client_address)

# ─── Network interface enumeration ────────────────────────────────────────────

_WIN_IP_DESC_CACHE: dict[str, str] | None = None


def _win_ip_descriptions() -> dict[str, str]:
    """Return {ipv4: InterfaceDescription} on Windows, else {}.

    Result is cached after the first call — the PowerShell subprocess takes
    ~1–2 s and the NIC list rarely changes during a session.
    """
    global _WIN_IP_DESC_CACHE
    if _WIN_IP_DESC_CACHE is not None:
        return _WIN_IP_DESC_CACHE
    if sys.platform != "win32":
        _WIN_IP_DESC_CACHE = {}
        return _WIN_IP_DESC_CACHE
    import json
    import subprocess
    script = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object { $_.IPAddress -notlike '127.*' } | "
        "ForEach-Object { "
        "$a = Get-NetAdapter -InterfaceIndex $_.InterfaceIndex "
        "-ErrorAction SilentlyContinue; "
        "[PSCustomObject]@{ IP = $_.IPAddress; "
        "Description = $a.InterfaceDescription } "
        "} | ConvertTo-Json -Compress"
    )
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=8,
            creationflags=flags,
        )
        if ps.returncode != 0 or not ps.stdout.strip():
            _WIN_IP_DESC_CACHE = {}
            return _WIN_IP_DESC_CACHE
        data = json.loads(ps.stdout.strip())
        if isinstance(data, dict):
            data = [data]
        _WIN_IP_DESC_CACHE = {
            item["IP"]: item["Description"]
            for item in data
            if item.get("IP") and item.get("Description")
        }
    except Exception:
        _WIN_IP_DESC_CACHE = {}
    return _WIN_IP_DESC_CACHE


def get_interfaces() -> list[tuple[str, str]]:
    """
    Return [(label, ip), ...] for all active IPv4 interfaces.
    The first entry is always ("All interfaces", "0.0.0.0").
    Skips loopback (127.x) addresses.
    On Windows, label uses the adapter device description (e.g. "Realtek …").
    Uses psutil when available; falls back to socket for hostname-bound IPs.
    """
    def _skip(ip: str) -> bool:
        return ip.startswith("127.")

    descriptions = _win_ip_descriptions()
    result: list[tuple[str, str]] = [("All interfaces (0.0.0.0)", "0.0.0.0")]
    seen: set[str] = {"0.0.0.0"}
    try:
        import psutil  # type: ignore[import]
        for name, addrs in sorted(psutil.net_if_addrs().items()):
            for addr in addrs:
                if addr.family == socket.AF_INET and not _skip(addr.address):
                    ip = addr.address
                    if ip in seen:
                        continue
                    seen.add(ip)
                    desc = descriptions.get(ip, name)
                    result.append((f"{desc}  ({ip})", ip))
    except ImportError:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip: str = info[4][0]
                if not _skip(ip) and ip not in seen:
                    seen.add(ip)
                    desc = descriptions.get(ip, ip)
                    result.append((f"{desc}  ({ip})", ip))
        except OSError:
            pass
    # Last-resort fallback for systems where psutil and getaddrinfo both fail
    # to enumerate NICs (seen on some hardened/stripped Windows images).
    if len(result) == 1:
        try:
            host_ip = socket.gethostbyname(socket.gethostname())
            if host_ip and not _skip(host_ip) and host_ip not in seen:
                seen.add(host_ip)
                desc = descriptions.get(host_ip, "Host")
                result.append((f"{desc}  ({host_ip})", host_ip))
        except OSError:
            pass
    return result


# ─── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_CONSOLE_IP = "192.168.1.6"
DEFAULT_CONSOLE_PORT = 8000
DEFAULT_LISTEN_PORT = 8001
DEFAULT_N_CHANNELS = 96

POLL_STEP_MS = 10        # ms between ticks; each tick drains 4 queries (one full channel)
HEARTBEAT_MS = 30_000    # full re-poll interval

GAIN_MIN  = 0.0    # auto-contracts if console reports negative values (some racks go to -10)
GAIN_MAX  = 60.0   # auto-expands if console reports higher values (some racks go to 70)
GAIN_STEP = 0.5

# ─── OSC path helpers ──────────────────────────────────────────────────────────

def _osc_name(ch: int) -> str:    return f"/Input_Channels/{ch}/Channel_Input/name"
def _osc_gain(ch: int) -> str:    return f"/Input_Channels/{ch}/Channel_Input/analog_gain"
def _osc_phantom(ch: int) -> str: return f"/Input_Channels/{ch}/Channel_Input/phantom"
def _query(path: str) -> str:     return path if path.endswith("/?") else f"{path}/?"

# Matches push and query-response paths for name, gain and phantom.
_RE_FIELD = re.compile(
    r"/Input_Channels/(\d+)/Channel_Input/(name|analog_gain|phantom)"
)

# ─── Routing parser ────────────────────────────────────────────────────────────

_RACK_PATTERNS = [
    # "SD Rack A - CH 3", "Rack A - 3", "SD Mini - 12"  (dash/en-dash/em-dash separator)
    re.compile(r"^(.+?)\s*[-\u2013\u2014]\s*(?:CH\s*)?(\d+)$", re.IGNORECASE),
    # "SD Rack1-3" or "Stagebox1-1"  (hyphen with no surrounding spaces)
    re.compile(r"^(.+?)[-_](\d+)$"),
    # "Local 1", "MADI 12", "Dante 5", "Rio 1"
    re.compile(r"^(.+?)\s+(\d+)$"),
]


def parse_source(raw: str) -> tuple[str, int]:
    """
    Parse console /source string → (rack_label, channel_number).
    Falls back to (raw, 0) so the raw string is still visible in the UI.
    """
    s = raw.strip()
    if not s:
        return "", 0
    # Bare integer → no rack name, just a channel number
    if s.isdigit():
        return "", int(s)
    for pat in _RACK_PATTERNS:
        m = pat.match(s)
        if m:
            try:
                return m.group(1).strip(), int(m.group(2))
            except (ValueError, IndexError):
                pass
    return s, 0


# ─── Type coercers ─────────────────────────────────────────────────────────────

def _to_bool(v: Any) -> bool:
    if isinstance(v, bool): return v
    if isinstance(v, (int, float)): return bool(int(v))
    if isinstance(v, str): return v.strip().lower() in ("1", "true", "on", "yes")
    return False


def _to_float(v: Any, default: float = 0.0) -> float:
    try: return float(v)
    except (TypeError, ValueError): return default


def _to_str(v: Any) -> str:
    if v is None: return ""
    if isinstance(v, bytes): return v.decode("utf-8", errors="replace")
    return str(v)


# ─── ChannelState ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChannelState:
    ch:         int
    name:       str   = ""
    rack_name:  str   = ""
    rack_ch:    int   = 0
    source_raw: str   = ""
    gain_db:    float = 0.0
    phantom:    bool  = False


# ─── ChannelStateStore ─────────────────────────────────────────────────────────

class ChannelStateStore(QObject):
    """
    Lives on the main thread.  OscEngine signals are auto-queued by Qt when
    they cross thread boundaries, so all slot calls here happen on the main thread.
    """
    channel_updated = pyqtSignal(int)

    def __init__(self, n: int) -> None:
        super().__init__()
        self._data: dict[int, ChannelState] = {
            ch: ChannelState(ch=ch) for ch in range(1, n + 1)
        }

    def get(self, ch: int) -> ChannelState:
        return self._data.get(ch, ChannelState(ch=ch))

    def _apply(self, ch: int, **kw: Any) -> None:
        old = self._data.get(ch, ChannelState(ch=ch))
        new = replace(old, **kw)
        if new != old:
            self._data[ch] = new
            self.channel_updated.emit(ch)

    @pyqtSlot(int, object)
    def on_name(self, ch: int, v: object) -> None:
        self._apply(ch, name=_to_str(v))

    @pyqtSlot(int, object)
    def on_gain(self, ch: int, v: object) -> None:
        self._apply(ch, gain_db=_to_float(v))

    @pyqtSlot(int, object)
    def on_phantom(self, ch: int, v: object) -> None:
        self._apply(ch, phantom=_to_bool(v))

    @pyqtSlot(int, object)
    def on_source(self, ch: int, v: object) -> None:
        raw = _to_str(v)
        rack_name, rack_ch = parse_source(raw)
        self._apply(ch, source_raw=raw, rack_name=rack_name, rack_ch=rack_ch)


# ─── OscBridge ────────────────────────────────────────────────────────────────

class OscBridge(QObject):
    """All inter-thread signals pass through here.  Lives on main thread."""

    # GUI → OscEngine
    do_connect    = pyqtSignal(str, int, int, int, str)  # console_ip, port, listen_port, n_ch, bind_ip
    do_disconnect = pyqtSignal()
    do_send       = pyqtSignal(str, object)          # osc_address, value
    do_repoll     = pyqtSignal()

    # OscEngine → GUI
    log          = pyqtSignal(str)
    status       = pyqtSignal(bool, str)             # connected, detail text
    console_name = pyqtSignal(str)
    osc_ready    = pyqtSignal()

    # OscEngine → ChannelStateStore  (auto-queued cross-thread)
    ch_name    = pyqtSignal(int, object)
    ch_gain    = pyqtSignal(int, object)
    ch_phantom = pyqtSignal(int, object)
    ch_source  = pyqtSignal(int, object)

    # Emitted when a received gain value exceeds the known GAIN_MAX — lets the
    # UI auto-expand the knob range to match the actual hardware ceiling.
    gain_ceiling = pyqtSignal(float)   # new observed max (dB)

    # OscEngine → MainWindow: console-reported channel count
    ch_count   = pyqtSignal(int)

    # MainWindow → OscEngine: apply a new channel count and restart poll
    do_set_channels = pyqtSignal(int)

    # OscEngine → MainWindow: session file path reported by the console
    session_filename = pyqtSignal(str)


# ─── OscEngine ────────────────────────────────────────────────────────────────

class OscEngine(QObject):
    """
    Runs on a dedicated QThread.  Owns the UDP socket (shared for send + receive).
    Drives a staggered poll queue and dispatches all inbound OSC messages to bridge signals.
    """

    def __init__(self, bridge: OscBridge) -> None:
        super().__init__()
        self._b = bridge
        self._client: SimpleUDPClient | None = None
        self._server: "_RobustOSCServer | None" = None
        self._running = False
        self._n_ch = DEFAULT_N_CHANNELS
        self._console_ip = ""      # expected source IP
        self._confirmed  = False   # True once a packet from console_ip is received

        self._recv_timer: QTimer | None = None
        self._poll_timer: QTimer | None = None
        self._poll_q: deque[tuple[str, object]] = deque()

    # ── lifecycle ──────────────────────────────────────────────────────────────

    @pyqtSlot(str, int, int, int, str)
    def on_connect(self, ip: str, port: int, listen: int, n_ch: int, bind_ip: str) -> None:
        if self._running:
            return
        self._n_ch = n_ch
        self._console_ip = ip
        self._confirmed  = False
        bind_addr = bind_ip if bind_ip else "0.0.0.0"
        try:
            disp = Dispatcher()
            disp.set_default_handler(self._handle, needs_reply_address=True)

            self._server = _RobustOSCServer((bind_addr, listen), disp)
            # Non-blocking socket: handle_request() returns immediately when no
            # datagram is ready, instead of burning 1ms per empty call.
            # The drain loop in _recv_tick uses select() to break out early.
            self._server.socket.setblocking(False)
            self._client = SimpleUDPClient(ip, port)
            self._client._sock = self._server.socket
            self._running = True

            display_bind = f"{bind_addr}:{listen}"
            # Emit ok=True so the heartbeat starts, but use a "Connecting" prefix so
            # the UI shows amber "Waiting for console…" until the first packet arrives.
            self._b.status.emit(True, f"Connecting:{ip}")
            self._b.log.emit(
                f"OSC ready -- console {ip}:{port}, "
                f"bound {display_bind}, channels 1-{n_ch}"
            )
            self._recv_timer = QTimer()
            self._recv_timer.setInterval(10)
            self._recv_timer.timeout.connect(self._recv_tick)
            self._recv_timer.start()

            self._start_poll()
            self._b.osc_ready.emit()

        except OSError as exc:
            self._running = False
            self._b.status.emit(False, str(exc))
            self._b.log.emit(f"OSC start failed: {exc}")

    @pyqtSlot()
    def on_disconnect(self) -> None:
        self._running = False
        for t in (self._recv_timer, self._poll_timer):
            if t:
                t.stop()
        self._recv_timer = self._poll_timer = None
        if self._server:
            try:
                # Close the socket directly — server.shutdown() is only for
                # serve_forever() loops and deadlocks when that loop isn't running.
                self._server.socket.close()
            except Exception:
                pass
            self._server = None
        self._client = None
        self._poll_q.clear()
        self._confirmed  = False
        self._console_ip = ""
        self._b.status.emit(False, "Disconnected")
        self._b.log.emit("OSC disconnected")

    @pyqtSlot(str, object)
    def on_send(self, address: str, value: object) -> None:
        if not self._running or not self._client:
            return
        try:
            self._client.send_message(address, value)
        except Exception as exc:
            self._b.log.emit(f"Send error: {exc}")

    @pyqtSlot()
    def on_repoll(self) -> None:
        if self._running:
            self._b.log.emit("Re-poll requested")
            self._start_poll()

    @pyqtSlot(int)
    def on_set_channels(self, n: int) -> None:
        """Update channel count (from console auto-detect) and restart the poll."""
        if n != self._n_ch:
            self._b.log.emit(f"Channel count updated to {n} — restarting poll")
            self._n_ch = n
        if self._running:
            self._start_poll()

    # ── timers ─────────────────────────────────────────────────────────────────

    def _recv_tick(self) -> None:
        if not self._running or not self._server:
            return
        sock = self._server.socket
        # Drain only as many datagrams as are actually queued. Non-blocking
        # select() returns immediately if no packet is ready, so an idle tick
        # costs ~microseconds instead of ~64ms (was: 1 ms timeout * 64 loops).
        for _ in range(64):
            try:
                ready, _w, _e = select.select((sock,), (), (), 0)
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                self._server.handle_request()
            except OSError:
                break
            except Exception as exc:
                self._b.log.emit(f"OSC parse error: {exc}")

    def _poll_tick(self) -> None:
        # Drain up to 4 queries per tick (one full channel: name+gain+phantom+source).
        for _ in range(4):
            if not self._poll_q:
                if self._poll_timer:
                    self._poll_timer.stop()
                self._b.log.emit("Poll complete")
                return
            addr, val = self._poll_q.popleft()
            self.on_send(addr, val)

    # ── polling ────────────────────────────────────────────────────────────────

    def _start_poll(self) -> None:
        q: deque[tuple[str, object]] = deque()
        q.append((_query("/Console/name"), None))
        q.append((_query("/Console/Session/Filename"), None))
        # /Console/channels/? causes the desk to broadcast all Console/*
        # parameters (Name, Input_Channels, Aux_Outputs, …) in one burst.
        q.append((_query("/Console/channels"), None))
        for ch in range(1, self._n_ch + 1):
            q.append((_query(_osc_name(ch)),    None))
            q.append((_query(_osc_gain(ch)),    None))
            q.append((_query(_osc_phantom(ch)), None))
        self._poll_q = q

        if self._poll_timer is None:
            self._poll_timer = QTimer()
            self._poll_timer.setInterval(POLL_STEP_MS)
            self._poll_timer.timeout.connect(self._poll_tick)
        self._poll_timer.start()
        n_ticks = (len(q) + 3) // 4          # 4 queries drained per tick
        total_ms = n_ticks * POLL_STEP_MS
        self._b.log.emit(
            f"Polling {self._n_ch} channels "
            f"({len(q)} queries, ~{total_ms}ms)…"
        )

    # ── inbound dispatch ───────────────────────────────────────────────────────

    def _handle(self, client_address: tuple, address: str, *args: Any) -> None:
        if not args:
            return
        val = args[0] if len(args) == 1 else list(args)

        # Confirm connection on first packet from the configured console IP.
        if not self._confirmed and client_address[0] == self._console_ip:
            self._confirmed = True
            self._b.status.emit(True, self._console_ip)

        # ── Console-level messages ──────────────────────────────────────────
        if "Console/name" in address.lower():
            self._b.console_name.emit(_to_str(val))
            return

        if "/Console/" in address:
            # Session filename → relay for file-based routing lookup
            if "session/filename" in address.lower():
                self._b.session_filename.emit(_to_str(val))
                return
            # Only log top-level Console/* parameters (depth ≤ 2).
            bare = address.split("?")[0].rstrip("/")
            if bare.count("/") <= 2:
                self._b.log.emit(f"CON  {address}  =  {val!r}")
            # Exact /Console/Input_Channels carries the channel count.
            if bare in ("/Console/Input_Channels", "/Console/input_channels",
                        "/Console/no_of_input_channels"):
                try:
                    n = int(float(val))
                    if 1 <= n <= 512:
                        self._b.ch_count.emit(n)
                except (TypeError, ValueError):
                    pass
            return

        # ── Channel field messages (name, gain, phantom) ─────────────────────
        m = _RE_FIELD.search(address)
        if m:
            ch, field = int(m.group(1)), m.group(2)
            if 1 <= ch <= self._n_ch:
                if field == "name":
                    self._b.ch_name.emit(ch, val)
                elif field == "analog_gain":
                    self._b.ch_gain.emit(ch, val)
                elif field == "phantom":
                    self._b.ch_phantom.emit(ch, val)
            return

        # Unrecognised paths (EQ, dynamics, aux sends, etc.) are not logged —
        # the console floods these on connect.


# ─── HeartbeatWorker ──────────────────────────────────────────────────────────

class HeartbeatWorker(QObject):
    """Runs on its own QThread. Triggers a full re-poll on a fixed interval."""

    def __init__(self, bridge: OscBridge) -> None:
        super().__init__()
        self._b = bridge
        self._timer: QTimer | None = None

    @pyqtSlot()
    def start(self) -> None:
        if self._timer and self._timer.isActive():
            return
        self._timer = QTimer()
        self._timer.setInterval(HEARTBEAT_MS)
        self._timer.timeout.connect(lambda: self._b.do_repoll.emit())
        self._timer.start()
        self._b.log.emit(f"Heartbeat started ({HEARTBEAT_MS // 1000}s interval)")

    @pyqtSlot(bool, str)
    def on_status(self, ok: bool, _: str) -> None:
        if not ok and self._timer:
            self._timer.stop()
            self._timer = None


# ─── Table column indices ─────────────────────────────────────────────────────

C_CH, C_RACK, C_IN, C_NAME, C_GAIN, C_48V = range(6)
_PHANTOM_COLOR = QColor(80, 20, 20)


# ─── GainKnob ─────────────────────────────────────────────────────────────────

class GainKnob(QWidget):
    """
    Compact cell widget: small QDial (rotary knob) + monospace numeric readout.
    Emits value_committed only on mouse-release so OSC is not flooded while dragging.
    """
    value_committed = pyqtSignal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(4)

        self._dial = QDial()
        self._dial.setRange(0, int(GAIN_MAX * 10))   # 0..700 → 0.0..70.0 dB
        self._dial.setSingleStep(int(GAIN_STEP * 10))
        self._dial.setPageStep(10)                   # 1 dB per page-step
        self._dial.setNotchesVisible(True)
        self._dial.setWrapping(False)
        self._dial.setFixedSize(30, 30)
        lay.addWidget(self._dial)

        self._lbl = QLabel("0.0 dB")
        self._lbl.setMinimumWidth(60)
        self._lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._lbl.setStyleSheet("font-family: monospace; font-size: 11px;")
        lay.addWidget(self._lbl)

        self._editing = False
        self._dial.sliderPressed.connect(self._on_press)
        self._dial.sliderReleased.connect(self._on_release)
        self._dial.valueChanged.connect(self._on_changed)

    def _on_press(self) -> None:
        self._editing = True

    def _on_release(self) -> None:
        self._editing = False
        self.value_committed.emit(self._dial.value() / 10.0)

    def _on_changed(self, v: int) -> None:
        self._lbl.setText(f"{v / 10.0:.1f} dB")

    def setValue(self, v: float) -> None:
        """Update knob from external data; ignored while user is dragging."""
        if self._editing:
            return
        self._dial.blockSignals(True)
        self._dial.setValue(round(v * 10))
        self._lbl.setText(f"{v:.1f} dB")
        self._dial.blockSignals(False)

    def value(self) -> float:
        return self._dial.value() / 10.0


# ─── ChannelTable ─────────────────────────────────────────────────────────────

class ChannelTable(QTableWidget):
    """
    One row per input channel.  Gain column uses GainKnob (QDial + label);
    phantom column uses a centered QCheckBox.  All state changes from the
    console flow through ChannelStateStore.channel_updated → _on_update.
    User edits emit want_gain / want_phantom which the parent connects to OSC send.
    """

    want_gain    = pyqtSignal(int, float)   # ch, gain_db
    want_phantom = pyqtSignal(int, bool)    # ch, on

    def __init__(self, n: int, store: ChannelStateStore) -> None:
        super().__init__(n, 6)
        self._n = n
        self._store = store
        self._updating = False
        self._knobs:  dict[int, GainKnob] = {}
        self._checks: dict[int, QCheckBox] = {}

        self.setHorizontalHeaderLabels(["Ch", "Rack", "In", "Name", "Gain", "48V"])
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(C_CH,   QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(C_RACK, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_IN,   QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(C_NAME, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_GAIN, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(C_48V,  QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(C_CH,   36)
        self.setColumnWidth(C_RACK, 140)
        self.setColumnWidth(C_IN,   32)
        self.setColumnWidth(C_GAIN, 116)
        self.setColumnWidth(C_48V,  50)

        vh = self.verticalHeader()
        vh.setDefaultSectionSize(36)
        vh.hide()
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        for row in range(n):
            self._init_row(row, row + 1)

        store.channel_updated.connect(self._on_update)

    def _ro_item(
        self,
        text: str = "",
        align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
    ) -> QTableWidgetItem:
        it = QTableWidgetItem(text)
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        it.setTextAlignment(align)
        return it

    def _init_row(self, row: int, ch: int) -> None:
        center = Qt.AlignmentFlag.AlignCenter
        self.setItem(row, C_CH,   self._ro_item(str(ch), center))
        self.setItem(row, C_RACK, self._ro_item(""))
        self.setItem(row, C_IN,   self._ro_item("", center))
        self.setItem(row, C_NAME, self._ro_item(""))

        knob = GainKnob()
        knob.value_committed.connect(
            lambda val, _ch=ch: self._gain_committed(_ch, val)
        )
        self.setCellWidget(row, C_GAIN, knob)
        self._knobs[ch] = knob

        wrapper = QWidget()
        lay = QHBoxLayout(wrapper)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chk = QCheckBox()
        chk.toggled.connect(lambda checked, _ch=ch: self._phantom_toggled(_ch, checked))
        lay.addWidget(chk)
        self.setCellWidget(row, C_48V, wrapper)
        self._checks[ch] = chk

    def _gain_committed(self, ch: int, val: float) -> None:
        if not self._updating:
            self.want_gain.emit(ch, val)

    def _phantom_toggled(self, ch: int, checked: bool) -> None:
        if not self._updating:
            self.want_phantom.emit(ch, checked)
            self._tint(ch - 1, checked)

    @pyqtSlot(int)
    def _on_update(self, ch: int) -> None:
        row = ch - 1
        if not (0 <= row < self._n):
            return
        s = self._store.get(ch)
        self._updating = True
        try:
            # Show parsed rack name; fall back to raw source so user can see
            # what the console is actually sending if the regex doesn't match.
            rack_txt = s.rack_name if s.rack_name else s.source_raw
            self.item(row, C_RACK).setText(rack_txt)
            self.item(row, C_IN).setText(str(s.rack_ch) if s.rack_ch else "")
            self.item(row, C_NAME).setText(s.name)

            kn = self._knobs.get(ch)
            if kn and abs(kn.value() - s.gain_db) > 0.01:
                kn.setValue(s.gain_db)

            ck = self._checks.get(ch)
            if ck and ck.isChecked() != s.phantom:
                ck.setChecked(s.phantom)
        finally:
            self._updating = False
        self._tint(row, s.phantom)

    def _tint(self, row: int, phantom: bool) -> None:
        for col in (C_CH, C_RACK, C_IN, C_NAME):
            it = self.item(row, col)
            if it:
                if phantom:
                    it.setBackground(_PHANTOM_COLOR)
                else:
                    it.setData(Qt.ItemDataRole.BackgroundRole, None)


# ─── SettingsBar ──────────────────────────────────────────────────────────────

class SettingsBar(QWidget):
    # console_ip, console_port, listen_port, n_channels, bind_ip
    connect_requested    = pyqtSignal(str, int, int, int, str)
    disconnect_requested = pyqtSignal()
    refresh_requested    = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 2)
        root.setSpacing(3)

        # ── Row 1: console target + connection controls ────────────────────────
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        row1.addWidget(QLabel("Console IP:"))
        self._ip = QLineEdit(DEFAULT_CONSOLE_IP)
        self._ip.setFixedWidth(130)
        row1.addWidget(self._ip)

        _port_v = QIntValidator(1, 65535)

        row1.addWidget(QLabel("Send port:"))
        self._port = QLineEdit(str(DEFAULT_CONSOLE_PORT))
        self._port.setValidator(_port_v)
        self._port.setFixedWidth(60)
        self._port.setToolTip("UDP port the DiGiCo console listens on (default 8000)")
        row1.addWidget(self._port)

        row1.addWidget(QLabel("Receive port:"))
        self._listen = QLineEdit(str(DEFAULT_LISTEN_PORT))
        self._listen.setValidator(_port_v)
        self._listen.setFixedWidth(60)
        self._listen.setToolTip("UDP port this PC listens on — enter this in DiGiCo External Control")
        self._listen.textChanged.connect(lambda _: self._update_hint())
        row1.addWidget(self._listen)

        row1.addWidget(QLabel("Channels:"))
        self._n_ch = QSpinBox()
        self._n_ch.setRange(1, 256)
        self._n_ch.setValue(DEFAULT_N_CHANNELS)
        self._n_ch.setFixedWidth(52)
        self._n_ch.setToolTip("Set by console automatically on connect")
        row1.addWidget(self._n_ch)

        row1.addSpacing(8)
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setFixedWidth(90)
        self._connect_btn.clicked.connect(self._toggle)
        row1.addWidget(self._connect_btn)

        self._repoll_btn = QPushButton("Re-poll")
        self._repoll_btn.setFixedWidth(68)
        self._repoll_btn.setEnabled(False)
        self._repoll_btn.clicked.connect(self.refresh_requested)
        row1.addWidget(self._repoll_btn)

        row1.addStretch()

        self._status_lbl = QLabel("Disconnected")
        self._status_lbl.setStyleSheet("color: #c0392b; font-weight: bold;")
        row1.addWidget(self._status_lbl)

        self._console_lbl = QLabel("")
        self._console_lbl.setStyleSheet("color: #888; font-style: italic;")
        row1.addWidget(self._console_lbl)

        root.addLayout(row1)

        # ── Row 2: NIC selection + hint for DiGiCo External Control ───────────
        row2 = QHBoxLayout()
        row2.setSpacing(6)

        row2.addWidget(QLabel("This PC NIC:"))
        self._nic_combo = QComboBox()
        self._nic_combo.setMinimumWidth(260)
        self._nic_combo.currentIndexChanged.connect(self._on_nic_changed)
        row2.addWidget(self._nic_combo)

        refresh_nic_btn = QPushButton("Refresh")
        refresh_nic_btn.setFixedWidth(60)
        refresh_nic_btn.setToolTip("Re-scan network interfaces")
        refresh_nic_btn.clicked.connect(self._populate_nics)
        row2.addWidget(refresh_nic_btn)

        row2.addSpacing(16)
        row2.addWidget(QLabel("Enter in DiGiCo External Control:"))
        self._hint_lbl = QLabel("")
        self._hint_lbl.setStyleSheet("font-weight: bold; font-family: monospace;")
        row2.addWidget(self._hint_lbl)

        row2.addStretch()
        root.addLayout(row2)

        self._connected = False
        self._ifaces: list[tuple[str, str]] = []
        self._populate_nics()

    # ── NIC helpers ────────────────────────────────────────────────────────────

    def _populate_nics(self) -> None:
        self._ifaces = get_interfaces()
        prev_ip = self._selected_bind_ip()
        self._nic_combo.blockSignals(True)
        self._nic_combo.clear()
        # Default: first specific NIC (index 1) rather than the 0.0.0.0 wildcard.
        restore_idx = 1 if len(self._ifaces) > 1 else 0
        for i, (label, ip) in enumerate(self._ifaces):
            self._nic_combo.addItem(label, userData=ip)
            if ip == prev_ip and prev_ip != "0.0.0.0":
                restore_idx = i
        self._nic_combo.setCurrentIndex(restore_idx)
        self._nic_combo.blockSignals(False)
        self._update_hint()

    def _selected_bind_ip(self) -> str:
        idx = self._nic_combo.currentIndex()
        if 0 <= idx < len(self._ifaces):
            return self._ifaces[idx][1]
        return "0.0.0.0"

    def _on_nic_changed(self, _: int) -> None:
        self._update_hint()

    def _update_hint(self) -> None:
        ip = self._selected_bind_ip()
        try:
            port = int(self._listen.text())
        except ValueError:
            port = DEFAULT_LISTEN_PORT
        if ip == "0.0.0.0":
            self._hint_lbl.setText("(pick a specific NIC)")
            self._hint_lbl.setStyleSheet("color: #888; font-style: italic;")
        else:
            self._hint_lbl.setText(f"{ip} : {port}")
            self._hint_lbl.setStyleSheet(
                "font-weight: bold; font-family: monospace; color: #2980b9;"
            )

    # ── connection toggle ──────────────────────────────────────────────────────

    def _toggle(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
        else:
            self._update_hint()
            try:
                send_port    = int(self._port.text())
                receive_port = int(self._listen.text())
            except ValueError:
                return
            self.connect_requested.emit(
                self._ip.text().strip(),
                send_port,
                receive_port,
                self._n_ch.value(),
                self._selected_bind_ip(),
            )

    # ── inbound state slots ────────────────────────────────────────────────────

    @pyqtSlot(bool, str)
    def on_status(self, ok: bool, detail: str) -> None:
        self._connected = ok
        self._connect_btn.setText("Disconnect" if ok else "Connect")
        self._repoll_btn.setEnabled(ok)
        self._nic_combo.setEnabled(not ok)
        if ok:
            self._status_lbl.setText(f"Connected  {detail}")
            self._status_lbl.setStyleSheet("color: #27ae60; font-weight: bold;")
        else:
            self._status_lbl.setText(detail)
            self._status_lbl.setStyleSheet("color: #c0392b; font-weight: bold;")
            self._console_lbl.setText("")

    @pyqtSlot(str)
    def on_console_name(self, name: str) -> None:
        self._console_lbl.setText(f"   {name}" if name else "")

    def set_n_ch(self, n: int) -> None:
        """Called when the console auto-reports its channel count."""
        self._n_ch.setMaximum(max(n, 256))
        self._n_ch.setValue(n)


# ─── MainWindow ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DiGiCo Preamp Controller")
        self.resize(860, 720)

        self._bridge = OscBridge()
        self._n_ch = DEFAULT_N_CHANNELS
        self._connected = False
        self._pending_ch_count = 0
        self._console_ip = ""

        # Debounce channel-count changes: wait 800 ms after the last signal
        # before acting.  This absorbs the rapid burst of Console/* messages
        # on connect, AND picks up live restructuring with no loop risk.
        self._ch_count_timer = QTimer()
        self._ch_count_timer.setSingleShot(True)
        self._ch_count_timer.setInterval(800)
        self._ch_count_timer.timeout.connect(self._apply_ch_count)

        # ── channel state store (main thread) ──────────────────────────────────
        self._store = ChannelStateStore(self._n_ch)
        self._wire_store(self._store)

        # ── OSC thread ─────────────────────────────────────────────────────────
        self._osc_thread = QThread()
        self._osc_engine = OscEngine(self._bridge)
        self._osc_engine.moveToThread(self._osc_thread)
        self._bridge.do_connect.connect(  # (console_ip, port, listen, n_ch, bind_ip)
            self._osc_engine.on_connect, Qt.ConnectionType.QueuedConnection
        )
        self._bridge.do_disconnect.connect(
            self._osc_engine.on_disconnect, Qt.ConnectionType.QueuedConnection
        )
        self._bridge.do_send.connect(
            self._osc_engine.on_send, Qt.ConnectionType.QueuedConnection
        )
        self._bridge.do_repoll.connect(
            self._osc_engine.on_repoll, Qt.ConnectionType.QueuedConnection
        )
        self._bridge.do_set_channels.connect(
            self._osc_engine.on_set_channels, Qt.ConnectionType.QueuedConnection
        )
        self._osc_thread.start()

        # ── heartbeat thread ───────────────────────────────────────────────────
        self._hb_thread = QThread()
        self._hb_worker = HeartbeatWorker(self._bridge)
        self._hb_worker.moveToThread(self._hb_thread)
        self._bridge.osc_ready.connect(
            self._hb_worker.start, Qt.ConnectionType.QueuedConnection
        )
        self._bridge.status.connect(
            self._hb_worker.on_status, Qt.ConnectionType.QueuedConnection
        )
        self._hb_thread.start()

        # ── GUI layout ─────────────────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        self._settings_bar = SettingsBar()
        self._settings_bar.connect_requested.connect(self._on_connect)
        self._settings_bar.disconnect_requested.connect(self._on_disconnect)
        self._settings_bar.refresh_requested.connect(
            lambda: self._bridge.do_repoll.emit()
        )
        root.addWidget(self._settings_bar)

        self._splitter = QSplitter(Qt.Orientation.Vertical)

        self._table = ChannelTable(self._n_ch, self._store)
        self._table.want_gain.connect(self._send_gain)
        self._table.want_phantom.connect(self._send_phantom)
        self._splitter.addWidget(self._table)

        log_widget = QWidget()
        log_lay = QVBoxLayout(log_widget)
        log_lay.setContentsMargins(0, 0, 0, 0)
        log_lay.setSpacing(2)
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("OSC log"))
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(52)
        clear_btn.clicked.connect(lambda: self._log.clear())
        log_header.addWidget(clear_btn)
        log_header.addStretch()
        log_lay.addLayout(log_header)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(10000)
        log_lay.addWidget(self._log)
        self._splitter.addWidget(log_widget)

        self._splitter.setSizes([520, 160])
        root.addWidget(self._splitter, stretch=1)

        self._bridge.log.connect(self._append_log)
        self._bridge.status.connect(self._settings_bar.on_status)
        self._bridge.console_name.connect(self._settings_bar.on_console_name)
        self._bridge.ch_count.connect(self._on_ch_count_detected)
        self._bridge.session_filename.connect(self._on_session_filename)

    # ── channel-count auto-detect ──────────────────────────────────────────────

    @pyqtSlot(int)
    def _on_ch_count_detected(self, n: int) -> None:
        """Debounce incoming channel-count signals; apply after 800 ms of silence."""
        self._pending_ch_count = n
        self._ch_count_timer.start()   # restart timer on every signal

    def _apply_ch_count(self) -> None:
        """Called by the debounce timer — safe to rebuild here."""
        n = self._pending_ch_count
        if n == self._n_ch:
            self._append_log(f"Console: {n} input channels (no change)")
            return
        self._append_log(
            f"Console restructured → {n} input channels — rebuilding table"
        )
        self._settings_bar.set_n_ch(n)
        self._rebuild_for_channels(n)
        self._bridge.do_set_channels.emit(n)

    # ── store wiring ───────────────────────────────────────────────────────────

    def _wire_store(self, store: ChannelStateStore) -> None:
        self._bridge.ch_name.connect(store.on_name)
        self._bridge.ch_gain.connect(store.on_gain)
        self._bridge.ch_phantom.connect(store.on_phantom)
        self._bridge.ch_source.connect(store.on_source)

    def _unwire_store(self, store: ChannelStateStore) -> None:
        try: self._bridge.ch_name.disconnect(store.on_name)
        except TypeError: pass
        try: self._bridge.ch_gain.disconnect(store.on_gain)
        except TypeError: pass
        try: self._bridge.ch_phantom.disconnect(store.on_phantom)
        except TypeError: pass
        try: self._bridge.ch_source.disconnect(store.on_source)
        except TypeError: pass

    # ── rebuild on channel-count change ───────────────────────────────────────

    def _rebuild_for_channels(self, n_ch: int) -> None:
        """Swap out the store + table when the channel count changes."""
        self._unwire_store(self._store)
        self._store = ChannelStateStore(n_ch)
        self._wire_store(self._store)

        new_table = ChannelTable(n_ch, self._store)
        new_table.want_gain.connect(self._send_gain)
        new_table.want_phantom.connect(self._send_phantom)
        idx = self._splitter.indexOf(self._table)
        self._splitter.replaceWidget(idx, new_table)
        self._table.deleteLater()
        self._table = new_table
        self._n_ch = n_ch

    # ── slots ──────────────────────────────────────────────────────────────────

    def _on_connect(self, ip: str, port: int, listen: int, n_ch: int, bind_ip: str) -> None:
        if n_ch != self._n_ch:
            self._rebuild_for_channels(n_ch)
        self._connected = True
        self._console_ip = ip
        self._bridge.do_connect.emit(ip, port, listen, n_ch, bind_ip)

    # ── session-file routing ───────────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_session_filename(self, path: str) -> None:
        """Console reported its current session file path — try to read routing from it."""
        self._append_log(f"Session file: {path}")
        if path:
            self._try_parse_session(path)

    # Known DiGiCo session directories on the console's Windows PC (SD-series).
    # Tried in order when the console returns a bare filename with no directory component.
    _DIGICO_SESSION_DIRS = [
        "C:\\Projects",              # DiGiCo Offline Editor default
        "C:\\DiGiCo\\Sessions",
        "C:\\DiGiCo\\Quantum\\Sessions",
        "C:\\DiGiCo\\SD7\\Sessions",
        "C:\\DiGiCo\\SD9\\Sessions",
        "C:\\DiGiCo\\SD11\\Sessions",
        "C:\\DiGiCo\\SD12\\Sessions",
        "C:\\DiGiCo\\SD5\\Sessions",
        "C:\\DiGiCo\\SD10\\Sessions",
        "C:\\Users\\DiGiCo\\Documents\\DiGiCo Sessions",
        "C:\\Sessions",
        "C:\\DiGiCo",
    ]

    def _try_parse_session(self, path: str) -> None:
        """Attempt to read the DiGiCo session file and extract routing data.

        Tries:
          1. The path as-is (full path or UNC — works if already absolute).
          2. A UNC admin-share path:  \\<console_ip>\\<drive>$\\<rest>
             (works on Windows LAN when the console PC has default admin shares enabled).
          3. If the console returned only a bare filename (no directory), brute-force
             all known DiGiCo session directories via UNC admin share.
        """
        p = Path(path)
        candidates: list[Path] = [p]

        if self._console_ip:
            if p.is_absolute() and len(path) >= 3 and path[1] == ":":
                # Full Windows path → convert to UNC admin share
                drive = path[0].upper()
                rest = path[2:].lstrip("\\/").replace("/", "\\")
                candidates.append(Path(f"\\\\{self._console_ip}\\{drive}$\\{rest}"))
            elif not p.parent.parts or p.parent == Path("."):
                # Bare filename only — probe all known DiGiCo session directories
                self._append_log(
                    f"Bare filename received; probing known DiGiCo session paths "
                    f"on {self._console_ip}…"
                )
                for d in self._DIGICO_SESSION_DIRS:
                    drive = d[0].upper()
                    rest = d[2:].lstrip("\\").replace("/", "\\")
                    unc_dir = f"\\\\{self._console_ip}\\{drive}$\\{rest}"
                    candidates.append(Path(unc_dir) / p.name)

        for candidate in candidates:
            try:
                tree = ET.parse(candidate)
                self._append_log(f"Session file opened: {candidate}")
                count = self._parse_session_xml(tree)
                self._append_log(
                    f"Routing loaded from session: {count} channel(s) patched"
                    if count else "Session XML parsed — no routing elements found"
                )
                return
            except FileNotFoundError:
                self._append_log(f"Not found: {candidate}")
            except PermissionError:
                self._append_log(
                    f"Access denied: {candidate}  "
                    "(enable admin shares or map a network drive on the console PC)"
                )
            except ET.ParseError as exc:
                self._append_log(f"XML parse error in {candidate}: {exc}")
            except Exception as exc:
                self._append_log(f"Cannot read {candidate}: {exc}")

    def _parse_session_xml(self, tree: ET.ElementTree) -> int:
        """Walk the DiGiCo session XML tree and emit ch_source signals for any
        patching data found.  Returns the number of channels successfully patched.

        DiGiCo .show files are XML but the schema varies by firmware generation.
        We try several known structures; unknown structures are skipped gracefully.
        """
        root = tree.getroot()
        count = 0

        # ── Strategy 1: <Channel index="N"> with nested <Patching rack="…" port="N">
        for ch_el in root.iter("Channel"):
            idx = ch_el.get("index") or ch_el.get("number") or ch_el.get("id")
            if not idx:
                continue
            try:
                ch = int(idx)
            except ValueError:
                continue
            for tag in ("Patching", "Input", "Route", "Source"):
                patch_el = ch_el.find(f".//{tag}")
                if patch_el is None:
                    continue
                rack  = (patch_el.get("rack") or patch_el.get("device") or
                         patch_el.get("name") or "")
                port  = (patch_el.get("port") or patch_el.get("channel") or
                         patch_el.get("input") or "")
                if rack or port:
                    src = f"{rack} - {port}" if rack and port else (rack or port)
                    self._bridge.ch_source.emit(ch, src)
                    count += 1
                    break

        # ── Strategy 2: <input_channel number="N"><source>TEXT</source>
        if count == 0:
            for ch_el in root.iter("input_channel"):
                num = ch_el.get("number") or ch_el.get("index")
                if not num:
                    continue
                try:
                    ch = int(num)
                except ValueError:
                    continue
                src_el = ch_el.find("source") or ch_el.find("patch") or ch_el.find("input")
                if src_el is not None and src_el.text:
                    self._bridge.ch_source.emit(ch, src_el.text.strip())
                    count += 1

        return count

    def _on_disconnect(self) -> None:
        self._connected = False
        self._ch_count_timer.stop()
        self._bridge.do_disconnect.emit()

    def _send_gain(self, ch: int, val: float) -> None:
        self._bridge.do_send.emit(_osc_gain(ch), val)

    def _send_phantom(self, ch: int, on: bool) -> None:
        self._bridge.do_send.emit(_osc_phantom(ch), 1.0 if on else 0.0)

    def _append_log(self, msg: str) -> None:
        self._log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def closeEvent(self, event: Any) -> None:
        self._bridge.do_disconnect.emit()
        for thread in (self._osc_thread, self._hb_thread):
            thread.quit()
            thread.wait(2000)
        super().closeEvent(event)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("DiGiCo Preamp Controller")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
