# RF Eye for Raspberry Pi

RF Eye is a portrait RF-activity display for Raspberry Pi with RTL-SDR support, Wi-Fi configuration, spectrum view, software updates and a GPIO alert buzzer.

RF Eye currently has two display variants:

| Variant | Display | Connection | Portrait UI | Installer |
| --- | --- | --- | --- | --- |
| **Standard** | Elecrow 5-inch 800x480 | HDMI + touch | 480x800 | `main/install.sh` |
| **CUQI / MHS35 3.5** | 3.5-inch 480x320 | 26-pin SPI + XPT2046 touch | 320x480 | `display-cuqi-35-portrait/install-cuqi35.sh` |

## Main features

- portrait interface optimized for the selected display
- RTL-SDR status: CONNECTED / NOT CONNECTED
- CLEAR / FAR / MID / NEAR indication
- three large centered signal meters with MHz labels
- blue settings interface
- TMB12A03 active buzzer on **BCM GPIO26 / physical pin 37** with mute support
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

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/install.sh | sudo bash
sudo reboot
```

The installer configures the HDMI display, RTL-SDR permissions, graphical auto-login, the RF Eye user service, the custom Plymouth theme and the kiosk/appliance session.

After reboot the intended flow is:

`power on -> RF EYE boot splash -> RF EYE application loading screen -> RF EYE fullscreen`

## Display variant 2 — CUQI / MHS35 3.5-inch 480x320 SPI touchscreen

This branch targets the 3.5-inch **480x320 26-pin MHS35-style SPI display**. The tested touch controller is **XPT2046**, which Linux reports as `ADS7846 Touchscreen` because it uses the compatible `ads7846` driver.

Branch:

`display-cuqi-35-portrait`

The compact build uses a native **320x480 portrait UI** and dedicated Home, Settings, Wi-Fi, Debug and Spectrum layouts.

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/install-cuqi35.sh | sudo bash
sudo reboot
```

The MHS35/CUQI installer:

- installs the normal RF Eye SDR, Wi-Fi, buzzer, updater and appliance components
- enables Raspberry Pi SPI
- uses the kernel `piscreen` DRM path instead of legacy fbturbo/fbdev graphics
- uses native 480x320 physical and 320x480 logical portrait geometry
- keeps the XPT2046 on SPI0.1 with GPIO17 PENIRQ
- uses the GoodTFT MHS35 X-plate value of 60 ohm
- builds an OS-matched `rfeye-mhs35.dtbo` with touch `pressure-max=1024` for lighter taps
- reads XPT2046 touch directly from evdev with MHS35 calibration
- suppresses duplicate Wayland/SDL pointer events from the same physical tap
- removes the nonexistent LightDM `renderD128` dependency that was measured to add about 89 seconds to boot
- dynamically re-centers Plymouth when boot switches from the firmware framebuffer to the 480x320 SPI framebuffer
- generates a 480x320 RF Eye boot splash
- installs `rfeye-cuqi35-status` diagnostics

The full legacy `goodtft/LCD-show` installer is intentionally not run. Its MHS35 touch values were used as reference, but its X11/fbcp/fbturbo graphics stack is unnecessary on the current DRM-based Raspberry Pi OS setup and could overwrite the working display configuration.

### Compact Home layout

The 320x480 Home screen includes:

- a sharp vector settings gear on the left
- taller radar bars
- no Noise dB line on Home
- default labels `381.000`, `382.500` and `384.000 MHz`
- sticky frequency labels that retain the last real detection for each column
- larger hit regions for Settings, Mute and Spectrum

### MHS35 diagnostics

```bash
sudo rfeye-cuqi35-status
```

Useful checks include:

```bash
grep -E 'spi|piscreen|rfeye-mhs35' /boot/firmware/config.txt
grep -B1 -A6 -Ei 'ADS7846|XPT2046|Touchscreen' /proc/bus/input/devices
systemctl show lightdm.service -p Wants -p After
evtest /dev/input/event0
```

If the complete image is upside-down, reinstall with:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/install-cuqi35.sh | sudo env RFEYE_ROTATION=ccw bash
sudo reboot
```

Do not add a second `swapxy` setting to the default MHS35 profile: the kernel `piscreen` overlay already swaps the XPT2046 axes and RF Eye accounts for that exactly once.

Full documentation:

**[CUQI / MHS35 3.5-inch setup and diagnostics](https://github.com/Julian10224/rfeye-pi/blob/display-cuqi-35-portrait/docs/CUQI35.md)**

## Buzzer / speaker wiring on Raspberry Pi 3B+

RF Eye uses a **TMB12A03 active buzzer** on **BCM GPIO26**, which is **physical pin 37** on the Raspberry Pi 3B+ 40-pin header. GPIO26 is outside the first 26 header pins used by the 3.5-inch SPI display.

Recommended signal wiring:

```text
TMB12A03 active buzzer / driver input
SIG or +  -> Raspberry Pi physical pin 37 (BCM GPIO26)
GND or -  -> Raspberry Pi physical pin 39 (GND)
```

The exact power connection depends on the buzzer/module version. Do not put 5 V onto GPIO26. RF Eye treats the TMB12A03 as an active buzzer and switches it on/off in rhythm; it does not use GPIO PWM to change pitch.

Do **not** connect a normal 4 ohm or 8 ohm loudspeaker directly to GPIO26. Use an audio amplifier for a real loudspeaker, and use a transistor/driver if the buzzer current exceeds what a GPIO signal can safely supply.

See `docs/SPEAKER_WIRING_RPI3BPLUS.md` for the complete wiring and hardware-test guide.

## Wi-Fi setup

Open **Settings > Wi-Fi**. The screen starts a fresh scan in the background. Tap **RESCAN** to repeat the scan. Tap the connected network for IP, gateway, DNS, signal and security details, or choose another network to enter its password.

## Spectrum threshold

The **Sensitivity** dB value in Settings is also drawn in Spectrum as a yellow threshold line at the current noise floor plus the configured threshold.

## Software updates over Wi-Fi

Open **Settings > Software update**. RF Eye downloads its manifest, compares versions, downloads the update ZIP, verifies SHA-256, creates a backup and replaces application files.

Application OTA updates run as the desktop user. System-level MHS35 changes such as Device Tree, Plymouth and LightDM require root, so an existing 0.7.20 or older MHS35 installation should run `install-cuqi35.sh` again once for the 0.7.21 system fixes.

Without an RTL-SDR the app shows `SDR: NOT CONNECTED` and stays idle; demo mode is only enabled manually.

## Publishing a new update

For the standard `main` build, update `VERSION` and `rfeye/config.py`, then run:

```bash
./scripts/build-release.sh
```

The MHS35/CUQI display fork is maintained separately on `display-cuqi-35-portrait` so display-specific driver, boot and UI changes do not break the standard Elecrow installation.
