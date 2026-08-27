# RF Eye for Raspberry Pi

RF Eye is a portrait RF-activity display for a Raspberry Pi, Elecrow 5-inch 800x480 HDMI display, RTL-SDR and optional GPIO buzzer.

## Main features

- portrait 480x800 interface on an 800x480 HDMI display
- RTL-SDR status: CONNECTED / NOT CONNECTED
- CLEAR / FAR / MID / NEAR indication
- three large centered signal meters
- frequency labels underneath each meter
- GPIO18 buzzer with mute support
- Settings menu with demo, sensitivity, brightness and update controls
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

The installer automatically:

- installs Python, Pygame, RTL-SDR and GPIO dependencies
- downloads the latest RF Eye code from GitHub
- configures RTL-SDR driver access
- applies the Elecrow 800x480 HDMI settings
- detects the connected HDMI output for the appliance display
- disables the Raspberry Pi top panel and on-screen keyboard in the RF Eye session
- configures desktop auto-login
- installs RF Eye under `/opt/rfeye`
- configures the GitHub update manifest URL

After reboot RF Eye starts automatically.

## Software updates over Wi-Fi

RF Eye checks the GitHub update manifest configured during installation.

Open **Settings** and select **Software update**. RF Eye then:

1. downloads `update/manifest.json` from GitHub;
2. compares the published version with the installed version;
3. downloads the update ZIP when a newer version is available;
4. verifies the SHA-256 checksum;
5. creates a backup of the current application;
6. installs the new application files.

The update mechanism does not use demo mode if the RTL-SDR is missing. Without an SDR, the main screen shows `SDR: NOT CONNECTED` and remains idle.

## Publishing a new update

After changing the code, update the version in `VERSION` and `rfeye/config.py`, then run:

```bash
./scripts/build-release.sh
```

This creates:

- `update/rfeye-update.zip`
- `update/manifest.json`
- a SHA-256 checksum for the release

Commit and push those files to GitHub. Installed units can then discover the new version through **Settings > Software update**.
