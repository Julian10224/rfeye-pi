# RF Eye — CUQI / MHS35 3.5-inch 480x320 portrait fork

This branch targets the CUQI/MHS35-style 3.5-inch 480x320 Raspberry Pi SPI touchscreen. The tested touch controller is **XPT2046**, exposed by Linux through the compatible `ads7846` driver.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/install-cuqi35.sh | sudo bash
sudo reboot
```

The LCD remains in native 480x320 landscape orientation. RF Eye renders a native 320x480 portrait canvas and rotates it once into the physical framebuffer.

## Display and touch stack

RF Eye intentionally does **not** run GoodTFT/LCD-show as a full legacy installer. The current Raspberry Pi kernel already provides a working `piscreen` DRM display driver and the XPT2046-compatible `ads7846` touch driver. Running the complete legacy LCD-show script would replace modern graphics configuration with X11/fbcp/fbturbo pieces that are not required by this appliance.

The fork instead keeps the modern DRM stack and applies only the MHS35-specific values that were verified against GoodTFT:

- SPI display: ILI9486/piscreen-compatible, 480x320
- touch: XPT2046 through Linux `ADS7846 Touchscreen`
- touch chip select: SPI0.1
- PENIRQ: GPIO17
- X-plate resistance: 60 ohm
- direct RF Eye evdev reader for `/dev/input/event*`
- GoodTFT MHS35 calibration values
- kernel `touchscreen-swapped-x-y` is respected exactly once
- portrait Y is inverted once so physical top maps to RF Eye top
- duplicate SDL/Wayland touch and mouse events are ignored

The installer builds a local `rfeye-mhs35.dtbo` from the OS-supplied `piscreen.dtbo` and raises `ti,pressure-max` from 255 to 1024. This keeps the same display/GPIO definitions as the installed kernel while accepting lighter resistive touches.

## UI changes for 320x480

The compact build has dedicated layouts for Home, Settings, Wi-Fi, Debug and Spectrum. Home uses:

- a sharp vector settings gear on the left
- three taller radar meters
- no Noise dB line on Home
- default frequency labels `381.000`, `382.500` and `384.000 MHz`
- each frequency label changes only when that column receives a real detection and then keeps the last detected frequency
- larger touch hit zones for Settings, Mute and Spectrum

## Boot fixes

Two separate boot problems were measured on the live MHS35 Raspberry Pi.

### 1. About 89 seconds waiting for renderD128

The SPI DRM driver creates `/dev/dri/card0` but not `/dev/dri/renderD128`. Raspberry Pi OS LightDM still requested both devices, so systemd waited for the missing render node until its device timeout expired.

The installer creates a full LightDM unit override in `/etc/systemd/system/lightdm.service` based on the OS vendor unit and removes only `dev-dri-renderD128.device`. The valid `dev-dri-card0.device` dependency remains.

### 2. Plymouth loading screen jumps down/right

Early boot first exposes a 720x480 firmware framebuffer and later the real 480x320 SPI framebuffer. A one-time Plymouth position calculated on 720x480 therefore became 120 pixels too far right and 80 pixels too low after the framebuffer switch.

The RF Eye Plymouth script now recalculates all positions during every refresh using the current framebuffer dimensions. The progress bar is also capped below 100% until handoff so it no longer appears completely finished while systemd is still waiting.

## Buzzer / speaker pin

The fork uses a **TMB12A03 active buzzer on BCM GPIO26 / physical pin 37**. Ground can use physical pin 39.

```text
TMB12A03 / compatible driver signal -> physical pin 37 (BCM GPIO26)
GND                                  -> physical pin 39 (GND)
```

GPIO26 is outside the first 26 physical header pins used by this display.

## Rotation

Default RF Eye portrait direction is clockwise. If the complete image is mounted upside-down:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/install-cuqi35.sh | sudo env RFEYE_ROTATION=ccw bash
sudo reboot
```

The direct XPT2046 mapping follows the RF Eye rotation setting. Do not add another `swapxy` overlay option on top of this profile; the kernel piscreen overlay already swaps the controller axes.

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
evtest /dev/input/event0
```

A healthy MHS35 touch device normally appears as `ADS7846 Touchscreen`; that is expected for XPT2046-compatible hardware.

## Existing installation: system fixes

Application OTA updates can replace RF Eye files without root privileges, but bootloader, Device Tree, Plymouth and LightDM changes require root. On an already installed unit, run the branch installer again to apply all system-level 0.7.21 fixes, then reboot.
