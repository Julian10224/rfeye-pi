# RF Eye for Raspberry Pi

RF Eye is a portrait RF-activity display for Raspberry Pi with RTL-SDR support, Wi-Fi configuration, spectrum view, OTA updates and a GPIO alert buzzer.

## Current main firmware

`main` now targets the **MHS35 / CUQI-style 3.5-inch 480x320 SPI touchscreen** with an **XPT2046** touch controller. Linux exposes the controller through the compatible `ADS7846 Touchscreen` driver.

The firmware renders a native **320x480 portrait UI** and rotates it once onto the physical 480x320 panel.

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/install-cuqi35.sh | sudo bash
sudo reboot
```

If the complete image is upside-down:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/install-cuqi35.sh | sudo env RFEYE_ROTATION=ccw bash
sudo reboot
```

## Main features

- native 320x480 portrait interface for MHS35/CUQI 3.5-inch displays
- XPT2046 resistive touch with direct calibrated evdev input
- RTL-SDR status and RF activity monitoring
- CLEAR / FAR / MID / NEAR indication
- three large radar meters with sticky MHz labels
- default labels `381.000`, `382.500` and `384.000 MHz` before the first detection
- sharp vector settings gear on the left
- Settings, Mute and Spectrum touch controls
- Wi-Fi setup and rescanning
- spectrum analyzer with threshold line
- TMB12A03 active buzzer on BCM GPIO26 / physical pin 37
- OTA software updates from the `main` branch
- appliance-style boot and RF Eye Plymouth splash
- persistent RTL-SDR session, ctypes SDR path and optimized FFT/scanning code
- startup optimizations for the SPI-only DRM display

## MHS35 display and touch stack

RF Eye intentionally uses the Raspberry Pi kernel `piscreen` DRM path rather than installing the complete legacy GoodTFT/LCD-show graphics stack.

The MHS35 profile uses:

- 480x320 ILI9486/piscreen-compatible SPI display
- XPT2046 through Linux `ADS7846 Touchscreen`
- SPI0.1 touch chip select
- GPIO17 PENIRQ
- X-plate value `xohms=60`
- RF Eye direct evdev touch reader
- a local `rfeye-mhs35.dtbo` derived from the OS `piscreen.dtbo`
- `pressure-max=1024` for lighter taps

The application suppresses the duplicate SDL/Wayland pointer events so a single physical tap is processed only once.

## Boot optimizations

The MHS35 firmware contains the boot fixes measured on the live Raspberry Pi:

- removes LightDM's invalid `/dev/dri/renderD128` dependency while retaining `/dev/dri/card0`
- removes the network ordering dependency from the local graphical-session critical path
- disables unused NFS/RPC boot services for the RF Eye appliance
- disables unused PipeWire/WirePlumber desktop audio services in the RF Eye user session
- starts RF Eye/Pygame as early as possible while Labwc is coming up
- defers the SDR backend import until the display path is ready
- keeps Plymouth centered across the firmware-framebuffer to SPI-framebuffer handoff

The previous `renderD128` bug caused roughly 89 seconds of unnecessary waiting on the tested Pi.

## Diagnostics

```bash
sudo rfeye-cuqi35-status
```

Useful manual checks:

```bash
grep -E 'spi|piscreen|rfeye-mhs35' /boot/firmware/config.txt
grep -B1 -A6 -Ei 'ADS7846|XPT2046|Touchscreen' /proc/bus/input/devices
systemctl show lightdm.service -p Wants -p After
systemd-analyze
evtest /dev/input/event0
```

## Buzzer wiring

RF Eye uses a **TMB12A03 active buzzer** on **BCM GPIO26**, physical pin **37**. Ground can use physical pin **39**.

```text
TMB12A03 signal -> physical pin 37 (BCM GPIO26)
GND             -> physical pin 39 (GND)
```

Do not put 5 V onto GPIO26. A normal 4-ohm or 8-ohm loudspeaker requires an amplifier and must not be connected directly to the GPIO.

See `docs/SPEAKER_WIRING_RPI3BPLUS.md` for the full wiring guide.

## Software updates

Open **Settings > Software update**. RF Eye reads:

```text
https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/update/manifest.json
```

The updater compares versions, downloads the main OTA ZIP, verifies its SHA-256 checksum, creates a backup and replaces the RF Eye application files.

From version **0.7.24** onward the installed firmware permanently uses `main` for OTA updates.

For compatibility with already installed 0.7.23 units, the former `display-cuqi-35-portrait` branch name may remain temporarily as an alias to the same main firmware. This prevents old units from losing access to their migration manifest. It is not a separate firmware line.

System-level changes such as Device Tree, Plymouth and systemd overrides require root and therefore are applied by `install-cuqi35.sh`. Application-only OTA updates do not need root.

## Publishing a new update

1. Increase `VERSION`.
2. Set the same version in `rfeye/config.py`.
3. Commit and push to `main`.

GitHub Actions automatically runs the MHS35 checks and rebuilds:

```text
update/manifest.json
update/rfeye-update.zip
```

The generated manifest and ZIP URL both point to `main`, so subsequent updates continue from the same firmware branch.

## Documentation

See [docs/CUQI35.md](docs/CUQI35.md) for MHS35 display, touch and boot details.
