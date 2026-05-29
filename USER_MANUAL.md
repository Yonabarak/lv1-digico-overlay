# DiGiCo Preamp Overlay — User Manual

**Version 0.1.1**

---

## Overview

The DiGiCo Preamp Overlay is a transparent window that floats over Waves eMotion LV1. It shows the gain level and phantom power state for every DiGiCo preamp that is routed to the current LV1 page, and lets you adjust them without switching away from LV1.

The overlay follows LV1 automatically — when you change pages, switch layers, or enter spill mode, the overlay updates to show the correct channels.

---

## System Requirements

- Windows 10 or 11 (64-bit)
- Waves eMotion LV1 installed and running
- DiGiCo console connected to the same network, with OSC enabled
- The overlay application running (`lv1_overlay.py` or the built `.exe`)

---

## First-Time Setup

### 1. Enable OSC on the DiGiCo console

On your DiGiCo console:

1. Go to **Setup → External Control** (or equivalent for your model)
2. Enable **OSC Remote Control**
3. Set the **receive port** to `8000`
4. Note the console's **IP address** — you'll need it in the next step

### 2. Start the overlay

Run `lv1_overlay.py` (or the built `.exe`). The overlay window appears on top of LV1. It will be empty until you connect to the console.

### 3. Open Settings

Click the **⚙ Settings** button in the top-right corner of the overlay.

Enter:

| Field | What to enter |
|---|---|
| **Console IP** | The IP address of your DiGiCo console |
| **Console port** | `8000` (default — change only if your console uses a different port) |
| **Listen port** | `8001` (default) |
| **Bind IP** | The IP of your local network interface connected to the console. Use `0.0.0.0` to bind to all interfaces. |
| **Channels** | The number of input channels in your session (e.g. 64 or 96) |

Click **Connect**. The overlay cells will populate with live gain and phantom values from the console within a few seconds.

---

## The Overlay Interface

Each cell in the overlay represents one DiGiCo preamp mapped to the current LV1 page:

```
┌─────────────────┐
│   CH 1          │  ← LV1 channel name (synced from LV1)
│                 │
│    +24.0        │  ← Gain in dB (click and drag to adjust)
│                 │
│     48V         │  ← Phantom power button (click to toggle)
└─────────────────┘
```

- **Click and drag up/down** on the gain area to adjust gain
- **Click the 48V button** to toggle phantom power on/off
- Active phantom power is highlighted in red
- Cells that have no routing on the current page are hidden automatically

---

## Following LV1

The overlay tracks LV1's state automatically. You do not need to do anything — just use LV1 normally.

| LV1 action | Overlay response |
|---|---|
| Change page | Shows the DiGiCo preamps for the new page |
| Switch to Factory layer | Shows the standard 16-channel layout for that page |
| Switch to Custom layer | Shows the custom channel assignments |
| Enter Spill mode | Shows only the channels in the active link group |
| Exit Spill mode | Returns to the full page view |
| Switch to a Mix/Group/Aux layer | Overlay hides (no preamp control needed) |
| Switch back to an Input layer | Overlay reappears |

---

## Saving and Restoring Preamp State

The overlay saves your preamp settings automatically and restores them when you reload a session.

### How it works

- **When you save in LV1** (Ctrl+S or File → Save): the overlay captures a snapshot of the current gain and phantom values and associates it with that session file. This is the "saved" state.

- **As you work**: every time you adjust a gain or phantom value, the overlay also saves an "autosave" snapshot in the background. This represents your last working state, whether or not you explicitly saved in LV1.

- **When you open a session**: the overlay loads the correct snapshot:
  - If LV1 opened the session from its last explicit save → loads your saved state
  - If LV1 opened the session from its last working state (auto-recovery) → loads your last working state

### The "Don't Save" scenario

If you make changes to preamp values and then switch to a different session in LV1 and choose **Don't Save** when prompted:

- When you switch back, the overlay will restore the **last explicitly saved** state — not the changes you discarded.

This matches LV1's own behavior: "Don't Save" means "go back to the last saved version."

### Enabling / disabling

You can turn preamp save/restore on or off in Settings with the **"Load preamp values with session"** checkbox.

---

## Channel Name Sync

When **Sync names** is enabled in Settings, the overlay reads LV1 channel names and pushes them to the DiGiCo console via OSC. This keeps the console's channel names in sync with LV1.

If multiple LV1 channels route to the same DiGiCo preamp (double-patching), the lowest-indexed LV1 channel's name is used.

---

## Connection and Reconnection

The overlay connects to the DiGiCo console at startup or when you click **Connect** in Settings.

If the connection is lost (e.g. console restarted or network interrupted), the overlay detects this and attempts to reconnect automatically. You can also force a reconnect by clicking **Connect** in Settings.

When connection is re-established, the overlay re-polls all channel values from the console.

---

## Emergency Exit

Press **Ctrl+Q** at any time to close the overlay. This works even when the overlay does not have keyboard focus.

> **Note:** Do not use the Escape key — it is disabled to prevent accidental closure during a live show.

---

## Settings Reference

| Setting | Description |
|---|---|
| **Console IP** | IP address of the DiGiCo console |
| **Console port** | OSC port on the console (default: 8000) |
| **Listen port** | UDP port the overlay listens on (default: 8001) |
| **Bind IP** | Local network interface IP (use 0.0.0.0 for all) |
| **Channels** | Number of input channels (must match your LV1 session) |
| **Gain min / max** | Display range in dB. The overlay will auto-expand this if the console reports values outside the range. |
| **Sync names** | Push LV1 channel names to DiGiCo automatically |
| **Load preamp values with session** | Restore saved preamp state when switching sessions |
| **Verbose logging** | Enable detailed log output for troubleshooting |

---

## Troubleshooting

### Overlay does not connect to the console

- Verify the console IP address is correct
- Check that OSC is enabled on the console (Setup → External Control)
- Make sure the PC and console are on the same network segment
- Check that no firewall is blocking UDP port 8000/8001
- Try setting Bind IP to `0.0.0.0`

### Overlay shows wrong channels for the current page

- The overlay reads LV1's routing from the session database. If routing looks wrong, try switching to a different page and back — this forces a refresh.
- Make sure the **Channels** setting matches your actual session size.

### Overlay disappears unexpectedly

- You are probably on a Mix, Group, or Aux layer — the overlay only shows on Input layers.
- Switch to an Input layer and the overlay will reappear.

### Preamp values not restored after session switch

- Make sure **"Load preamp values with session"** is enabled in Settings.
- You must save in LV1 at least once to create the saved snapshot. Adjusting preamp values alone is not enough — you need to also save the LV1 session.

### Multiple overlay instances / app won't start

- Only one instance can run at a time. If the app crashes without cleaning up, use Task Manager to kill any remaining `pythonw.exe` or `LV1-DiGiCo-Overlay.exe` processes, then restart.

---

## Version History

| Version | Date | Notes |
|---|---|---|
| 0.1.1 | May 2026 | Initial release |

---

*For technical documentation and source code, see [README.md](README.md).*
