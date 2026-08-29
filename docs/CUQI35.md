# RF Eye — CUQI 3.5-inch 480x320 portrait fork

This branch targets the CUQI 3.5-inch 480x320 Raspberry Pi touchscreen sold as a 26-pin SPI display.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/install-cuqi35.sh | sudo bash
sudo reboot
```

The LCD controller stays in its native 480x320 landscape orientation. RF Eye itself uses a native 320x480 portrait canvas and rotates that once into the physical framebuffer. There is no aspect-ratio scaling: the main screen, settings, Wi-Fi keyboard, connected-network details, Debug and spectrum page all have dedicated 320x480 layouts and touch regions.

## What the installer changes

- Reuses the normal RF Eye installer for RTL-SDR, NetworkManager, buzzer permissions and appliance startup.
- Enables SPI and makes the SPI DRM panel the primary graphics device.
- Uses the kernel `piscreen` DRM overlay instead of legacy fbturbo/fbdev LCD-show graphics drivers.
- Sets `dtoverlay=piscreen,speed=18000000,drm,rotate=0` by default.
- Removes conflicting old `mhs35`, `tft35a`, `ads7846`, `99-fbturbo.conf` and `99-fbdev.conf` configuration where applicable.
- Configures RF Eye for a 320x480 logical portrait UI and 480x320 physical framebuffer.
- Uses a 20 FPS UI target to balance responsiveness and SPI/CPU load.
- Regenerates the RF Eye Plymouth boot artwork at native 480x320.
- Installs `rfeye-cuqi35-status` for DRM, framebuffer, SPI and touchscreen diagnostics.

## Buzzer / speaker pin

This fork uses the same current RF Eye buzzer profile as `main`: **TMB12A03 active buzzer on BCM GPIO26 / physical pin 37**. A nearby ground is **physical pin 39**.

GPIO26 is outside the first 26 physical header pins occupied by the CUQI SPI display, so the buzzer signal does not consume one of the display's 26-pin connections.

```text
TMB12A03 / compatible driver signal -> physical pin 37 (BCM GPIO26)
GND                                  -> physical pin 39 (GND)
```

Do not put 5 V onto GPIO26. If the buzzer current is not explicitly safe for a GPIO signal, use a transistor/driver. See `docs/SPEAKER_WIRING_RPI3BPLUS.md` for the full wiring guide.

## Rotation

Default portrait direction is clockwise. If the mounted screen is upside down, run:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/install-cuqi35.sh | sudo env RFEYE_ROTATION=ccw bash
sudo reboot
```

## Diagnostics

```bash
sudo rfeye-cuqi35-status
```

Useful manual checks:

```bash
cat /boot/firmware/config.txt | grep -E 'spi|piscreen|rfeye-cuqi35'
ls -l /dev/fb* /dev/dri/* /dev/spidev* 2>/dev/null
grep -B1 -A4 -Ei 'ADS7846|XPT2046|Touchscreen' /proc/bus/input/devices
```

## Touch compatibility options

The Raspberry Pi `piscreen` overlay supports axis options on compatible panels. If the touch controller is inverted, the installer can append an option, for example:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/install-cuqi35.sh | sudo env RFEYE_CUQI_TOUCH_OPTS=invx bash
```

Only use `invx`, `invy` or `swapxy` after checking the actual touch behaviour; different 3.5-inch clones wire the resistive controller differently. RF Eye itself already performs the 90-degree application rotation and inverse touch mapping, so kernel rotation stays at zero.

## Hardware note

The Amazon listing confirms the 3.5-inch 480x320, 26-pin SPI format, but does not publish the LCD controller IC. This fork targets the common ILI9486/piscreen-compatible implementation used by this display family. If `rfeye-cuqi35-status` shows no ILI9486/piscreen DRM device after reboot, collect its output before trying legacy vendor drivers.
