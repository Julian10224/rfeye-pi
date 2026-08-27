# RF Eye for Raspberry Pi

RF Eye is a portrait RF-activity display for a Raspberry Pi, Elecrow 5-inch 800x480 HDMI display, RTL-SDR and optional GPIO buzzer.

## Main features

- portrait 480x800 interface on an 800x480 HDMI display
- RTL-SDR status: CONNECTED / NOT CONNECTED
- CLEAR / FAR / MID / NEAR indication
- three large centered signal meters
- always-visible frequency labels underneath each meter
- GPIO18 buzzer with mute support
- polished Settings menu with demo, sensitivity, brightness and update controls
- built-in Wi-Fi setup: scan, select and connect to another network
- mouse cursor appears when the mouse is used and hides again automatically
- Wi-Fi software updates from GitHub
- technician spectrum page
- automatic appliance-style startup

## Requirements

Recommended starting point:

- Raspberry Pi OS Desktop 64-bit
- Raspberry Pi 3B+ or newer
- Elecrow 5-inch 800x480 HDMI display
- RTL-SDR compatible receiver
- Internet connection during installation

## One-click installation

On a fresh Raspberry Pi OS Desktop installation, open a terminal or connect with SSH and run:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/install.sh | sudo bash
```

When the installer finishes, reboot:

```bash
sudo reboot
```

The installer automatically installs the required Python, Pygame, NetworkManager, RTL-SDR and GPIO packages, downloads RF Eye, configures the Elecrow 800x480 display, detects the connected HDMI output, configures autologin/appliance startup and installs RF Eye under `/opt/rfeye`.

After reboot RF Eye starts automatically.

## Wi-Fi setup

Open **Settings > Wi-Fi**. RF Eye scans nearby networks with NetworkManager. Select an SSID, type its password with a connected keyboard and press **Enter** or choose **CONNECT**. The **RESCAN** button refreshes the list. The currently connected network is shown in green.

This is useful when moving the RF Eye unit to another Wi-Fi network without leaving the application.

## Software updates over Wi-Fi

Open **Settings > Software update**. RF Eye:

1. downloads `update/manifest.json` from this repository;
2. compares the published version with the installed version;
3. downloads `update/rfeye-update.zip` when a newer version exists;
4. verifies its SHA-256 checksum;
5. creates a backup of the current application;
6. installs the new application files.

The RTL-SDR does not trigger demo mode when disconnected. The main screen shows `SDR: NOT CONNECTED` and remains idle until the receiver is available again.

## Publishing a new update

Update `VERSION` and `rfeye/config.py`, then run:

```bash
./scripts/build-release.sh
```

This regenerates `update/rfeye-update.zip`, `update/manifest.json` and its SHA-256 checksum. Commit and push those files. Installed RF Eye units can then discover the new version through **Settings > Software update**.
