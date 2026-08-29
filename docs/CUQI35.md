# RF Eye — MHS35 / CUQI 3.5-inch 480x320 main firmware

The RF Eye `main` branch targets the MHS35/CUQI-style 3.5-inch 480x320 Raspberry Pi SPI touchscreen. The tested touch controller is **XPT2046**, exposed by Linux through the compatible `ads7846` driver.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/install-cuqi35.sh | sudo bash
sudo reboot
```

The LCD remains in native 480x320 landscape orientation. RF Eye renders a native 320x480 portrait canvas and rotates it once into the physical framebuffer.

## Display and touch stack

RF Eye intentionally does **not** run GoodTFT/LCD-show as a full legacy installer. The current Raspberry Pi kernel already provides a working `piscreen` DRM display driver and the XPT2046-compatible `ads7846` touch driver. Running the complete legacy LCD-show script would replace modern graphics configuration with X11/fbcp/fbturbo pieces that are not required by this appliance.

The firmware instead keeps the modern DRM stack and applies only the MHS35-specific values that were verified against GoodTFT:

- SPI display: ILI9486/piscreen-compatible, 480x320
- touch: XPT2046 through Linux `ADS7846 Touchscreen`
- touch chip select: SPI0.1
- PENIRQ: GPIO17
- X-plate resistance: 60 ohm
- direct RF Eye evdev reader for `/dev/input/event*`
- MHS35 calibration values
- kernel `touchscreen-swapped-x-y` is respected exactly once
- final touch orientation is calibrated to the portrait RF Eye UI
- duplicate SDL/Wayland touch and mouse events are ignored

The installer builds a local `rfeye-mhs35.dtbo` from the OS-supplied `piscreen.dtbo` and raises `ti,pressure-max` from 255 to 1024. This keeps the same display/GPIO definitions as the installed kernel while accepting lighter resistive touches.

## UI changes for 320x480

The compact firmware has dedicated layouts for Home, Settings, Wi-Fi, Debug and Spectrum. Home uses:

- a sharp vector settings gear on the left
- three taller radar meters
- no Noise dB line on Home
- default frequency labels `381.000`, `382.500` and `384.000 MHz`
- each frequency label changes only when that column receives a real detection and then keeps the last detected frequency
- larger touch hit zones for Settings, Mute and Spectrum

## Boot fixes

Several boot problems were measured on the live MHS35 Raspberry Pi.

### 1. About 89 seconds waiting for renderD128

The SPI DRM driver creates `/dev/dri/card0` but not `/dev/dri/renderD128`. Raspberry Pi OS LightDM still requested both devices, so systemd waited for the missing render node until its device timeout expired.

The installer creates a full LightDM unit override in `/etc/systemd/system/lightdm.service` based on the OS vendor unit and removes only `dev-dri-renderD128.device`. The valid `dev-dri-card0.device` dependency remains.

### 2. Network ordering delayed the graphical session

The stock `systemd-user-sessions.service` ordered itself after `network.target`, putting NetworkManager on the local display critical path even though RF Eye does not require networking to show the radar screen.

The MHS35 appliance override removes only that network ordering. Wi-Fi still starts normally in parallel.

### 3. Plymouth loading screen moved during framebuffer handoff

Early boot exposes a firmware framebuffer and later the real 480x320 SPI framebuffer. Plymouth must therefore center against the current window dimensions without carrying an output-origin offset from the earlier framebuffer.

The RF Eye Plymouth script recalculates its position every refresh and uses only the active window width and height. The progress bar is capped below 100% until handoff.

### 4. Application startup

RF Eye starts Pygame as early as possible while Labwc is still becoming ready. The SDR backend import is deferred until the display path is available, so graphical startup and Python import work overlap instead of running serially.

Unused PipeWire/WirePlumber desktop audio services and unused NFS/RPC client services are removed from the appliance startup path.

## Buzzer / speaker pin

The firmware uses a **TMB12A03 active buzzer on BCM GPIO26 / physical pin 37**. Ground can use physical pin 39.

```text
TMB12A03 / compatible driver signal -> physical pin 37 (BCM GPIO26)
GND                                  -> physical pin 39 (GND)
```

GPIO26 is outside the first 26 physical header pins used by this display.

## Rotation

Default RF Eye portrait direction is clockwise. If the complete image is mounted upside-down:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/install-cuqi35.sh | sudo env RFEYE_ROTATION=ccw bash
sudo reboot
```

Do not add another `swapxy` overlay option on top of this profile; the kernel piscreen overlay already swaps the controller axes and RF Eye accounts for the final portrait mapping.

## Diagnostics

```bash
sudo rfeye-cuqi35-status
```

Useful manual checks:

```bash
grep -E 'spi|piscreen|rfeye-mhs35|rfeye-cuqi35' /boot/firmware/config.txt
ls -l /dev/fb* /dev/dri/* 2>/dev/null
grep -B1 -A6 -Ei 'ADS7846|XPT2046|Touchscreen' /proc/bus/input/devices
systemctl show lightdm.service -p Wants -p After
systemctl show systemd-user-sessions.service -p After
systemd-analyze
evtest /dev/input/event0
```

A healthy MHS35 touch device normally appears as `ADS7846 Touchscreen`; that is expected for XPT2046-compatible hardware.

## OTA updates

From RF Eye 0.7.24 onward the firmware reads its OTA manifest from:

```text
https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/update/manifest.json
```

The main GitHub Actions release workflow builds the OTA ZIP and writes a SHA-256-protected manifest after version changes.

The former `display-cuqi-35-portrait` branch name can remain temporarily as a compatibility alias to `main` so already installed 0.7.23 units can receive the migration update. It is not maintained as a separate firmware branch.

Application OTA updates can replace RF Eye files without root privileges. Bootloader, Device Tree, Plymouth and systemd changes require root and are applied by rerunning `install-cuqi35.sh`.
