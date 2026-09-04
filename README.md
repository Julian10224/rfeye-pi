# RF Eye 0.7.32 for Raspberry Pi

This repository contains the complete **RF Eye 0.7.32 reference appliance** for the MHS35/CUQI-style 3.5-inch SPI touchscreen.

`main` is the only supported firmware/update branch. It contains the application, exact display/touch overlay, boot splash, systemd units, Labwc/Kanshi session, boot optimizations, NetworkManager policy and OTA package required to reproduce the working reference Raspberry Pi on a fresh Raspberry Pi OS installation.

## Supported hardware

Reference setup:

- Raspberry Pi with Raspberry Pi OS, systemd, LightDM and Labwc
- 3.5-inch MHS35/CUQI-style 480x320 SPI display
- ILI9486/piscreen-compatible DRM display path
- XPT2046 resistive touch exposed by Linux as `ADS7846 Touchscreen`
- RTL-SDR compatible receiver
- TMB12A03 active buzzer on BCM GPIO26 / physical pin 37
- Wi-Fi through NetworkManager

RF Eye renders a native **320x480 portrait UI** and rotates it once onto the physical **480x320** SPI framebuffer.

## Install a new Raspberry Pi

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/install-cuqi35.sh | sudo bash
sudo reboot
```

If the complete picture is upside-down:

```bash
curl -fsSL https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/install-cuqi35.sh | sudo env RFEYE_ROTATION=ccw bash
sudo reboot
```

Do not install a separate LCD-show/GoodTFT stack on top of this setup. RF Eye ships its own tested Device Tree overlay and startup configuration.

## What a fresh install reproduces

The installer reproduces the working 0.7.32 appliance path:

- `/opt/rfeye/rfeye/` receives the final runtime from this repository
- `/opt/rfeye/start-rfeye.sh` is installed from `scripts/start-rfeye.sh`
- `/opt/rfeye/.venv` is created for the application
- the committed `rfeye-mhs35.dts` is compiled into the exact reference `rfeye-mhs35.dtbo`
- boot selects `rfeye-mhs35` at 18 MHz SPI with DRM/XPT2046 settings
- the reference LightDM and `systemd-user-sessions` units remove measured startup waits
- `rfeye-user.service` starts RF Eye directly through `/opt/rfeye/start-rfeye.sh`
- Labwc starts without Raspberry Pi desktop chrome
- Kanshi leaves the SPI panel at its native DRM mode
- unused desktop audio, NFS/RPC, cloud-init, printing and other appliance-unneeded services are disabled or masked
- Plymouth and `Made by: Julian` startup artwork are installed into initramfs
- NetworkManager remains enabled and associates in parallel with local display startup
- OTA updates permanently use the `main` manifest

A fresh installation also receives `config/reference-config-cuqi35.json` as its initial **non-secret** RF Eye settings. It captures the working display/touch profile plus the automatic detector-v3 timing and baseline parameters. Detection sensitivity is automatic; there is no user dB threshold. Existing installations keep their own unrelated user settings during reinstall/update while obsolete sensitivity keys are migrated away.

Wi-Fi credentials and other account/machine secrets are **not** stored in this repository.

## Reference startup path

```text
boot firmware
  -> RF Eye Plymouth splash
  -> systemd-user-sessions.service without network.target wait
  -> LightDM without renderD128 wait
  -> Labwc user session
  -> rfeye-user.service
  -> /opt/rfeye/start-rfeye.sh
  -> /opt/rfeye/.venv/bin/python /opt/rfeye/rfeye/app.py
```

The app waits for the Wayland socket before display initialization, so the user service does not need another shell polling loop. The service restarts automatically with `Restart=always` and `RestartSec=0.5`.

## Captured 0.7.28 startup files

- `config/systemd/lightdm.service` — card0 dependency without nonexistent renderD128
- `config/systemd/systemd-user-sessions.service` — no network.target wait
- `config/systemd/rfeye-user.service.in` — direct RF Eye user-service template
- `config/systemd/rfeye-user-fast-ui.conf` — reference SDL/GTK startup environment
- `config/labwc-autostart` — removes panel/file-manager/on-screen-keyboard processes
- `config/labwc/rc.xml` — maps ADS7846/XPT2046 touch to the SPI output
- `config/kanshi-config` — native SPI display profile without HDMI override
- `config/overlays/rfeye-mhs35.dts` — tested display/touch source that recompiles byte-for-byte to the reference overlay

The installer compiles the committed DTS and verifies SHA-256 `1727ca3c3161bd90db1cbc7a076dad692d34ee67c7acf70afab28fbf16fdec34`. If the result is not byte-for-byte identical to the reference overlay, installation stops instead of silently using a different display definition.

## User interface in 0.7.32

The compact profile contains:

- 320x480 portrait home screen
- three RF activity meters with retained MHz labels
- settings gear and large touch targets
- Sound/Mute and Spectrum controls on the home screen
- Settings with seven rows
- automatic soft RF sensitivity with no dB slider
- brightness slider
- Wi-Fi scan/connection UI
- software update action
- Debug performance page
- touch calibration available only from Debug
- RF recording with an explicit large **NEE / JA** confirmation page
- `Made by: Julian` in Settings/startup artwork

Audio mode stays on the adaptive RF Eye behavior. The active TMB12A03 buzzer uses rhythm changes rather than pitch changes.

## Touch input

RF Eye reads the XPT2046 controller directly through Linux evdev. SDL/Wayland duplicate pointer events are filtered so one physical press cannot activate two controls.

The reference calibration is included for a new install. If a replacement panel differs, use:

```text
Settings -> Debug -> Touch calibration
```

The five-point affine calibration is saved immediately in the local RF Eye config.

## RF activity scanner

The SDR backend keeps a persistent `librtlsdr` handle open where possible and uses vectorized FFT-based scanning over the configured bands. Detector profile v5 keeps the mobile/uplink band on an approximately one-second revisit cadence on the reference Pi, refreshes downlink context periodically, tracks confirmation per 25 kHz carrier and uses soft burst/SNR scoring instead of a user dB cutoff. A per-channel slow temporal reference is maintained for every observed raster channel, with common-mode AGC motion removed before calculating local change.

On the first calibration for a detector profile, five normal sweeps observe the local Pi/RTL-SDR RF environment. A frequency is added to the stationary clutter map only when it is present in enough sweeps and remains stable in relative RF-SNR, burst duty and burst span. A carrier whose metrics become variable during those sweeps is marked transient and may escape the startup filter immediately instead of being learned as background.

The resulting hardware baseline is stored locally under the user's RF Eye state directory and reused on later restarts (subject to profile/band/sample-rate validation and a maximum age). That removes the repeated five-sweep blind window on normal restarts. A loaded baseline still uses the same slow EMA drift tracking, while new or materially changed carriers pass through to normal duplex/confidence/hysteresis processing.

When a sweep is unusually busy, RF Eye 0.7.32 no longer deletes the entire candidate set. The broadband guard instead keeps only carriers whose RF-SNR, duty or burst span changed materially relative to their own slow temporal reference. A median common-mode correction prevents RTL-SDR AGC movement across the whole band from looking like a local transient.

The display reports RF activity/status only; it does not identify a transmitter or determine an exact physical distance.

## RF recording

**Record RF** stores a short time series for later analysis. Recording starts only after a deliberate press on the lower **JA** button; the upper **NEE** area cancels and returns to Settings.

Captured JSON files are stored locally under:

```text
~/.local/share/rfeye/captures/
```

They are not committed to GitHub automatically. Recording schema v5 also stores raw/post-artifact candidate counts, how many candidates the broadband guard kept, and a compact pre-pair candidate debug set including temporal-departure information. This makes a future field recording sufficient to identify exactly which detector stage accepted or rejected a transient.

## Buzzer wiring

RF Eye 0.7.32 uses a **TMB12A03 active buzzer**:

```text
TMB12A03 signal -> physical pin 37 (BCM GPIO26)
GND             -> physical pin 39 (GND)
```

Do not put 5 V onto GPIO26. A normal low-impedance speaker must not be connected directly to the GPIO. See `docs/SPEAKER_WIRING_RPI3BPLUS.md` for the wiring notes.

## Software updates

RF Eye checks only the `main` manifest:

```text
https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/update/manifest.json
```

That manifest points to the deterministic OTA package on `main`:

```text
https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/update/rfeye-update.zip
```

The updater downloads the ZIP, verifies its SHA-256, makes a backup and replaces the RF Eye application files. Starting with 0.7.30, a successful OTA install automatically exits the running app after showing `RESTARTING`; the `rfeye-user.service` (`Restart=always`) then relaunches RF Eye from the newly installed files. The visible `RESTART` action is also a real manual restart fallback.

Application-only OTA updates update `/opt/rfeye/rfeye`. Device Tree, systemd, Plymouth and boot-service changes are applied by `install-cuqi35.sh` and therefore require root.

## Release build

`VERSION` and `rfeye/config.py` identify this release as **0.7.32**.

Build the OTA package with:

```bash
./scripts/build-release.sh
```

The build normalizes archive metadata so unchanged source produces the same ZIP SHA-256. GitHub Actions rebuilds the `main` manifest/ZIP after a release commit and runs syntax, compact-UI, startup-snapshot and deterministic-release checks.

## Diagnostics

```bash
sudo rfeye-cuqi35-status
```

Useful checks:

```bash
grep -E 'rfeye-mhs35|spi|disable_splash|auto_initramfs' /boot/firmware/config.txt
grep -B1 -A6 -Ei 'ADS7846|XPT2046|Touchscreen' /proc/bus/input/devices
systemctl show lightdm.service -p Wants -p After
systemctl show systemd-user-sessions.service -p After
systemctl --user cat rfeye-user.service
systemd-analyze
```

The installed overlay can be compared with the repository using:

```bash
sha256sum /boot/firmware/overlays/rfeye-mhs35.dtbo
```

## Repository layout

```text
rfeye/                         final runtime copied to /opt/rfeye/rfeye
scripts/start-rfeye.sh         direct application launcher
scripts/apply-cuqi35-system-fixes.sh
scripts/optimize-rpi-appliance.sh
config/overlays/               exact MHS35 Device Tree source
config/systemd/                reference startup units/templates
config/labwc-autostart         reference appliance session
config/labwc/rc.xml            touch/output mapping
config/kanshi-config           native SPI profile
config/reference-config-cuqi35.json
config/plymouth/rfeye/         boot splash theme
update/manifest.json           OTA metadata for main
update/rfeye-update.zip        deterministic OTA package
```

## Source-of-truth rule

For 0.7.28 and later, do not add install-time Python patch chains that mutate the application after checkout. The files under `rfeye/` are the final tested runtime. A fresh install and an OTA build must receive the same application files.

When publishing a later release:

1. change and test the runtime under `rfeye/`;
2. update `VERSION` and `rfeye/config.py` together;
3. update any required reference startup/config files explicitly;
4. run the repository checks and `scripts/build-release.sh`;
5. commit the tested snapshot to `main`.

That keeps GitHub `main` installable as a complete RF Eye appliance rather than as a partial code dump.
