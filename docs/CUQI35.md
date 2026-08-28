# RF Eye — CUQI 3.5-inch 480x320 portrait fork

This branch targets the CUQI 3.5-inch 480x320 Raspberry Pi touchscreen sold as a 26-pin SPI display.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/install-cuqi35.sh | sudo bash
sudo reboot
```

The installer keeps the LCD controller at its native 480x320 orientation and renders RF Eye vertically in software. RF Eye retains its 480x800 logical portrait canvas and scales the final rotated frame to the physical 480x320 display. Touch is mapped through the inverse transform so the existing Wi-Fi keyboard, settings, spectrum and main screen keep their original hit regions.

## What the installer changes

- Reuses the normal RF Eye installer for RTL-SDR, NetworkManager, boot splash, buzzer permissions and appliance startup.
- Enables SPI.
- Uses the kernel `piscreen` DRM overlay instead of legacy fbturbo/fbdev LCD-show graphics drivers.
- Sets `dtoverlay=piscreen,speed=18000000,drm,rotate=0` by default.
- Removes conflicting old `mhs35`, `tft35a`, `ads7846`, `99-fbturbo.conf` and `99-fbdev.conf` configuration where applicable.
- Configures RF Eye for a 480x320 physical framebuffer with the `cuqi35` display profile.
- Limits UI rendering to 18 FPS to reduce SPI transfer/CPU load on the small display.
- Installs `rfeye-cuqi35-status` for display, SPI and touchscreen diagnostics.

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
ls -l /dev/spidev* /dev/dri/* 2>/dev/null
grep -B1 -A4 -Ei 'ADS7846|XPT2046|Touchscreen' /proc/bus/input/devices
```

## Touch compatibility options

The Raspberry Pi `piscreen` overlay supports touchscreen axis options on compatible panels. If a clone reports inverted axes, the installer can append an overlay option, for example:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/install-cuqi35.sh | sudo env RFEYE_CUQI_TOUCH_OPTS=invx bash
```

Only use `invx`, `invy` or `swapxy` after checking the actual touch behaviour; different 3.5-inch clones wire the resistive controller differently.

## Hardware note

The Amazon listing confirms the 3.5-inch 480x320, 26-pin SPI format, but does not publish the LCD controller IC. This fork targets the common ILI9486/piscreen-compatible implementation used by this display family. If `rfeye-cuqi35-status` shows no ILI9486/piscreen DRM device after reboot, collect its output before trying legacy vendor drivers.
