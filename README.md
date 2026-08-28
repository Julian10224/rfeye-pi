# RF Eye for Raspberry Pi

RF Eye is a portrait RF-activity display for a Raspberry Pi, Elecrow 5-inch 800x480 HDMI display, RTL-SDR and optional GPIO buzzer.

## Main features

- portrait 480x800 interface on an 800x480 HDMI display
- RTL-SDR status: CONNECTED / NOT CONNECTED
- CLEAR / FAR / MID / NEAR indication
- three large centered signal meters with fallback MHz labels
- blue settings gear with transparent background
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
- Elecrow 5-inch 800x480 HDMI display
- RTL-SDR compatible receiver
- Internet connection during installation

## One-click installation

On a fresh Raspberry Pi OS Desktop installation, run:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/install.sh | sudo bash
sudo reboot
```

The installer configures the display, RTL-SDR permissions, graphical auto-login, the RF Eye user service, the custom Plymouth theme and the kiosk/appliance session. On the next boot the intended flow is:

`power on -> RF EYE boot splash -> RF EYE application loading screen -> RF EYE fullscreen`

The Plymouth PNG assets are generated locally during installation by `scripts/generate-plymouth-assets.py`, so the repository contains the complete reproducible boot configuration without storing board-specific initramfs images.

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

Open **Settings > Software update**. RF Eye downloads `update/manifest.json`, compares versions, downloads the update ZIP, verifies its SHA-256 checksum, creates a backup and installs the new application files.

Without an RTL-SDR the app shows `SDR: NOT CONNECTED` and stays idle; demo mode is only enabled manually.

## Publishing a new update

Update `VERSION` and `rfeye/config.py`, then run:

```bash
./scripts/build-release.sh
```

Commit and push the updated source, `update/manifest.json` and `update/rfeye-update.zip` together so installed units never receive a partial release.
