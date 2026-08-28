# RF Eye for Raspberry Pi

RF Eye is a portrait RF-activity display for Raspberry Pi with RTL-SDR support, Wi-Fi configuration, spectrum view, software updates and an optional GPIO buzzer.

RF Eye currently has two display variants:

| Variant | Display | Connection | Portrait UI | Installer |
| --- | --- | --- | --- | --- |
| **Standard** | Elecrow 5-inch 800x480 | HDMI + touch | 480x800 | `main/install.sh` |
| **CUQI 3.5** | CUQI 3.5-inch 480x320 | 26-pin SPI + touch | 320x480 | `display-cuqi-35-portrait/install-cuqi35.sh` |

## Main features

- portrait interface optimized for the selected display
- RTL-SDR status: CONNECTED / NOT CONNECTED
- CLEAR / FAR / MID / NEAR indication
- three large centered signal meters with MHz labels
- blue settings interface
- GPIO18 buzzer with mute support
- Settings menu with demo, sensitivity, brightness, Wi-Fi and update controls
- spectrum view with a visible threshold line derived from the configured dB threshold
- Wi-Fi setup with asynchronous rescanning, connected-network details and on-screen password keyboard
- mouse cursor appears when the mouse is used and hides again automatically
- Wi-Fi software updates from GitHub
- automatic appliance-style startup
- RF Eye Plymouth boot splash with progress bar
- Raspberry Pi firmware logo and verbose boot status hidden
- Raspberry Pi desktop panel/file-manager suppressed before RF Eye starts fullscreen

## Requirements

Recommended starting point:

- Raspberry Pi OS Desktop 64-bit
- Raspberry Pi 3B+ or newer
- one of the supported displays listed below
- RTL-SDR compatible receiver
- Internet connection during installation

## Display variant 1 — Elecrow 5-inch 800x480 HDMI

This is the standard RF Eye build. It uses the Elecrow 5-inch 800x480 HDMI touchscreen and renders RF Eye as a **480x800 portrait interface** rotated onto the physical 800x480 framebuffer.

### Install

On a fresh Raspberry Pi OS Desktop installation, run:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/install.sh | sudo bash
sudo reboot
```

The installer configures the HDMI display, RTL-SDR permissions, graphical auto-login, the RF Eye user service, the custom Plymouth theme and the kiosk/appliance session.

After reboot the intended flow is:

`power on -> RF EYE boot splash -> RF EYE application loading screen -> RF EYE fullscreen`

The Plymouth PNG assets are generated locally during installation by `scripts/generate-plymouth-assets.py`, so the repository contains the complete reproducible boot configuration without storing board-specific initramfs images.

## Display variant 2 — CUQI 3.5-inch 480x320 SPI touchscreen

A separate RF Eye display fork is available for the CUQI 3.5-inch **480x320 26-pin SPI touchscreen**.

Branch:

`display-cuqi-35-portrait`

This build uses a native **320x480 portrait UI**. After software rotation, those pixels map directly to the physical 480x320 panel. The compact build contains dedicated layouts for the main screen, Settings, Wi-Fi setup, on-screen keyboard, connected-network details and Spectrum.

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/install-cuqi35.sh | sudo bash
sudo reboot
```

The CUQI installer:

- installs the normal RF Eye SDR, Wi-Fi, buzzer, updater and appliance components
- enables Raspberry Pi SPI
- configures the panel through the kernel `piscreen` DRM path instead of legacy `fbturbo`/`fbdev` display drivers
- uses `dtoverlay=piscreen,speed=18000000,drm,rotate=0`
- removes conflicting legacy `mhs35`, `tft35a`, `ads7846`, `99-fbturbo.conf` and `99-fbdev.conf` configuration where applicable
- configures RF Eye for a 480x320 physical framebuffer and 320x480 portrait UI
- generates a 480x320-specific Plymouth boot splash
- installs a display diagnostics command

### CUQI diagnostics

After reboot:

```bash
sudo rfeye-cuqi35-status
```

This reports DRM devices, SPI devices, touchscreen detection and relevant kernel display messages.

If the image is upside-down, reinstall with counter-clockwise RF Eye rotation:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/install-cuqi35.sh | sudo env RFEYE_ROTATION=ccw bash
sudo reboot
```

Different 3.5-inch SPI clones can wire the resistive touch controller differently. Touch-axis compatibility options such as `invx`, `invy` and `swapxy` are therefore configurable rather than hardcoded.

Full CUQI-specific documentation is available on the display branch:

**[CUQI 3.5-inch setup and diagnostics](https://github.com/Julian10224/rfeye-pi/blob/display-cuqi-35-portrait/docs/CUQI35.md)**

> The Amazon listing identifies the CUQI panel as a 3.5-inch 480x320 26-pin SPI touchscreen but does not publish the LCD controller IC. The RF Eye fork targets the common ILI9486/piscreen-compatible implementation used by this display family. Run `sudo rfeye-cuqi35-status` after the first boot if the panel does not initialize correctly.

## Buzzer / speaker wiring on Raspberry Pi 3B+

RF Eye uses **BCM GPIO18**, which is **physical pin 12** on the Raspberry Pi 3B+ 40-pin header. The default configuration is for a passive piezo buzzer.

Recommended basic connection:

```text
Passive 3.3 V piezo buzzer
+  -> Raspberry Pi physical pin 12 (BCM GPIO18)
-  -> Raspberry Pi physical pin 14 (GND)
```

Do **not** connect a normal 4 ohm or 8 ohm loudspeaker directly to GPIO18. Use a transistor driver for a higher-current buzzer or an audio amplifier for a real loudspeaker.

See the complete installation, pinout, transistor wiring, active/passive buzzer explanation, loudspeaker option and hardware test in:

**[docs/SPEAKER_WIRING_RPI3BPLUS.md](docs/SPEAKER_WIRING_RPI3BPLUS.md)**

## Wi-Fi setup

Open **Settings > Wi-Fi**. The screen starts a fresh scan in the background. While scanning, the button shows `SCANNING...`; when complete it shows the number of networks found. Tap **RESCAN** to repeat the scan.

Tap the currently connected network to view its IP address, gateway, DNS, signal level and security. Tap another network to open the password page; the on-screen keyboard is shown only while entering a Wi-Fi password.

## Spectrum threshold

The **Sensitivity** dB value in Settings is also drawn in Spectrum as a yellow threshold line. The line is positioned at the current measured noise floor plus the configured threshold value.

## Software updates over Wi-Fi

Open **Settings > Software update**. RF Eye downloads its update manifest, compares versions, downloads the update ZIP, verifies its SHA-256 checksum, creates a backup and installs the new application files.

Without an RTL-SDR the app shows `SDR: NOT CONNECTED` and stays idle; demo mode is only enabled manually.

## Publishing a new update

For the standard `main` build, update `VERSION` and `rfeye/config.py`, then run:

```bash
./scripts/build-release.sh
```

Commit and push the updated source, `update/manifest.json` and `update/rfeye-update.zip` together so installed units never receive a partial release.

The CUQI display fork is maintained separately on `display-cuqi-35-portrait` so display-specific driver, boot and UI changes do not break the standard Elecrow installation.
