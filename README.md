# DiGiCo Preamp Overlay for Waves eMotion LV1

A transparent Windows overlay that sits on top of Waves eMotion LV1 and gives you real-time gain and phantom power control for every DiGiCo preamp — without leaving the LV1 window.

---

## What it does

- Displays a floating, click-through overlay on top of LV1 showing gain (dB) and 48V phantom for each input channel
- Follows LV1's current page, layer, and spill mode automatically
- Sends OSC commands directly to the DiGiCo console when you adjust a cell
- Reads back the console's actual values on connect and after reconnects
- Saves and restores preamp state per session — tied to LV1's native save workflow

---

## Requirements

| Requirement | Version |
|---|---|
| Windows | 10 / 11 (64-bit) |
| Python | 3.11+ |
| PyQt6 | ≥ 6.6 |
| python-osc | ≥ 1.8 |
| psutil | ≥ 5.9 |
| Waves eMotion LV1 | Running on the same machine |
| DiGiCo console | Any model with OSC enabled on port 8000/8001 |

Install dependencies:

```
pip install -r requirements.txt
```

---

## Quick start

1. Open Waves eMotion LV1 with a session loaded
2. Run:
   ```
   pythonw lv1_overlay.py
   ```
3. Click the **gear icon** in the overlay to open Settings
4. Enter the DiGiCo console IP and click **Connect**

The overlay will appear transparently over LV1 and populate with live preamp values.

---

## Architecture

```
lv1_overlay.py          Main application — PyQt6 overlay window
lv1_session.py          LV1 database parser (CurrentLV1.dat / .emo SQLite files)
digico_multichannel.py  OSC engine + DiGiCo channel state store
```

### Threading model

| Thread | Purpose |
|---|---|
| Qt main thread | UI, all state mutations, QTimer callbacks |
| `OscThread` (QThread) | OSC send/receive loop (python-osc) |
| `HeartbeatThread` (QThread) | 30-second re-poll of all DiGiCo channels |

Cross-thread communication uses `OscBridge` (QObject with signals), all connected with `Qt.ConnectionType.QueuedConnection`.

### LV1 session tracking

The overlay reads `CurrentLV1.dat` (LV1's live SQLite autosave) every 25 ms to track:
- Current page / layer (`surface_property_int` prop 14)
- Custom vs Factory mode
- Spill mode and active link group
- Channel routing (`ovv_layer_track` + `routes` tables)

### Preamp save / load

Two-tier save system per session name:

| File | When written |
|---|---|
| `preamp_saves/<session>.json` | When user saves in LV1 (`.emo` file change detected, 3-second deferred write, 30-second post-fire cooldown to deduplicate LV1's multi-event save sequence) |
| `preamp_saves/<session>.autosave.json` | Every time a gain/phantom value changes (1-second debounce) and on session switch / app close |

On load, the system auto-detects whether LV1 opened from its last explicit save (state b) or its last working state (state c) by comparing the `.emo` file mtime against the mtime recorded in the autosave.

### Overlay visibility rules

The overlay shows on **Input** layers only (`surface_property_int` section=0 or section_page=1). It hides automatically on Mix, Group, Aux, FX, and Matrix layers.

In **Spill** mode the overlay shows only the channels belonging to the active link group.

---

## Settings reference

| Setting | Default | Description |
|---|---|---|
| Console IP | `0.0.0.0` | DiGiCo console IP address |
| Console port | 8000 | OSC receive port on the console |
| Listen port | 8001 | UDP port the overlay listens on |
| Bind IP | auto | Local NIC to bind the OSC socket |
| Channels | 96 | Number of input channels to control |
| Gain range | −8 … +60 dB | Display range (auto-expands to match console) |
| Sync names | on | Push LV1 channel names to DiGiCo via OSC |
| Load preamp with session | on | Restore saved preamp state on session switch |

---

## Versioning

Format: `MAJOR.MINOR.PATCH`

- **MAJOR** — breaking architectural changes
- **MINOR** — significant new features
- **PATCH** — bug fixes and small improvements

Current version: **0.1.1**

---

## Building a standalone executable

```powershell
.\build.ps1
```

Produces `dist\LV1-DiGiCo-Overlay.exe` via PyInstaller.

---

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+Q` | Emergency exit (works even without overlay focus) |

---

## License

MIT
