# DiGiCo-LV1-Overlay — User Manual

**Version 0.1.1**

---

## Overview

The DiGiCo-LV1-Overlay is a semi transparent window that floats over Waves eMotion LV1. 
It shows the gain level and phantom power state for every DiGiCo preamp that is routed to the current LV1 page using an MGB, and lets you adjust them from within LV1. 
ONLY CHANNELS THAT ARE ROUTED TO AN MGB WILL HAVE AN OVERLAY.
64 and 80 channels configurations are supported with seemless switching between them (No need for overlay restart).

The overlay follows LV1 automatically — when you change pages, switch layers, or enter spill mode, the overlay updates to show the correct channels.

---

## System Requirements

- Windows 10 or 11 (64-bit)
- Waves eMotion LV1 V16 (V17 coming soon) installed and running
- DiGiCo console connected to the same network, with OSC enabled
- The overlay application. Extract the zip and leave all the files in the same directory. This directory will include your settings and session save files.

---

## First-Time Setup

### 1. Enable OSC on the DiGiCo console

On the DiGiCo console:

1. Go to **Setup → External Control** 
2. Enable **External Control**
3. Add device - **Digico Pad**
4. Name the device and enter the IP address of the LV1 computer.
5. Set the send and receive ports to whatever you want. Preferebaly 8XXX.
6. Note the console's **IP address** — you'll need it in the next step.

### 2. Start the overlay

The overlay window appears on top of LV1. It will be empty until you connect to the console.
Click the **Settings** button in the top-right corner of the overlay.

Alignment - Allows you to allign the overlay size and position of the preamp section, the settings button and the location of the spill button.
Once aligned these settings will be saved with the program.

### 3. Connect to Digico

Enter:

| **Console IP** | The IP address of your DiGiCo console (Can be seen in the Digico External Control window at the bottom).
| **Console ports** | Enter the Receive port from Digico in the Send port, and the Send port from the Digico in the Receive port.
| **Network Ports** | Make sure the correct ethernet port is selected (The control port and not the SG port)

Click **Connect**. The overlay cells will populate with live gain and phantom values from the console within a few seconds.

Other options in the Settings menu:

| **Sync LV1 channel names to Digico** | Will transfer the names of the LV1 channels to the corresponding Digico channels.
| **Load preamp values with session** | Will load the saved value (If there are any) from the session to the Digico preamps. If multiple LV1 channels route to the same DiGiCo preamp (double-patching), the lowest-indexed LV1 channel's name is used.
---

## The Overlay Interface

Each cell in the overlay represents one DiGiCo preamp mapped to the current LV1 page:

```
┌─────────────────┐
│      48V        │   ← Phantom power button (click to toggle) 
│                 │
│      Name       │   ← Digico channel name (synced from LV1), with double patched channels the displayes name correspond to the first LV1 channel connected to the preamp.
│                 │
│     Value       │   ← Gain in dB (click and drag to adjust), half dB increments.
└─────────────────┘
```

- **Click and drag up/down** on the gain area to adjust gain
- **Click the 48V button** to toggle phantom power on/off
- Active phantom power is highlighted in red
- Cells that have no routing on the current page are hidden automatically

---

## Saving and Restoring Preamp State

The overlay saves your preamp settings automatically and restores them when you reload a session.

### How it works

- **When you save in LV1** (Ctrl+S or File → Save): the overlay captures a snapshot of the current gain and phantom values and associates it with that session file. This is the "saved" state.

- **As you work**: every time you adjust a gain or phantom value, the overlay also saves an "autosave" snapshot in the background. This represents your last working state, whether or not you explicitly saved in LV1.

- **When you open a session**: the overlay loads the correct snapshot:
  - If LV1 opened the session from its last explicit save → loads your saved state
  - If LV1 opened the session from its last working state → loads your last working state

---

## Connection and Reconnection

The overlay connects to the DiGiCo console at startup (If settings remained the same from last session) or when you click **Connect** in Settings.

If the connection is lost (e.g. console restarted or network interrupted), the overlay detects this and attempts to reconnect automatically. You can also force a reconnect by clicking **Connect** in Settings.

When connection is re-established, the overlay re-polls all channel values from the console.

---

## Emergency Exit

Press **Ctrl+Q** at any time to close the overlay. 

---

## Troubleshooting

### Overlay does not connect to the console

- Verify the console IP address is correct
- Check that OSC is enabled on the console (Setup → External Control)
- Make sure the PC and console are on the same network
- Check that no firewall is blocking UDP port 8000/8001

### Overlay shows wrong channels for the current page

- The overlay reads LV1's routing from the session database. If routing looks wrong, try switching to a different page and back — this forces a refresh.
- Make sure the **Channels** setting matches your actual session size.

### Preamp values not restored after session switch

- Make sure **"Load preamp values with session"** is enabled in Settings.
- You must save in LV1 at least once to create the saved snapshot. Adjusting preamp values alone is not enough — you need to also save the LV1 session.

---

## Version History

| Version | Date | Notes |
|---|---|---|
| 0.1.1 | May 2026 | Initial release |

---

*For technical documentation and source code, see [README.md](README.md).*
