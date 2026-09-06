# RF Eye 0.8.0 for Raspberry Pi

This repository contains the complete **RF Eye 0.7.37 reference appliance** for the MHS35/CUQI-style 3.5-inch SPI touchscreen.

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

The installer reproduces the working 0.7.37 appliance path:

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

A fresh installation also receives `config/reference-config-cuqi35.json` as its initial **non-secret** RF Eye settings. It captures the working display/touch profile plus the detector profile v8 acceptance limits. Detection sensitivity is automatic; there is no user dB threshold. Upgrading to v8 resets every detector key, because each v6/v7 tuning value controlled a gate that no longer exists; unrelated user settings are preserved.

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

## User interface in 0.8.0

The compact profile contains:

- 320x480 portrait home screen
- three RF activity meters with retained MHz labels
- settings gear and large touch targets
- Sound/Mute and Spectrum controls on the home screen
- Settings with eight rows, including a dedicated Recordings browser
- automatic soft RF sensitivity with no dB slider
- brightness slider
- Wi-Fi scan/connection UI
- software update action
- Debug performance page
- touch calibration available only from Debug
- RF recording with an explicit large **NEE / JA** confirmation page
- Recordings browser with replay, replay alert sound and guarded delete confirmation
- `Made by: Julian` in Settings/startup artwork

Audio mode stays on the adaptive RF Eye behavior. The active TMB12A03 buzzer uses rhythm changes rather than pitch changes.

## Touch input

RF Eye reads the XPT2046 controller directly through Linux evdev. SDL/Wayland duplicate pointer events are filtered so one physical press cannot activate two controls.

The reference calibration is included for a new install. If a replacement panel differs, use:

```text
Settings -> Debug -> Touch calibration
```

The five-point affine calibration is saved immediately in the local RF Eye config.

## C2000 detection (detector profile v8)

RF Eye detects that an emergency-services radio is transmitting near the
receiver. It does this by verifying the actual ETSI EN 300 392-2 TETRA
waveform, not by watching for energy in a frequency range.

### Why this was rebuilt

Detector profiles up to v7 decided from wideband energy statistics: channel
power, duty cycle, burst span, and how much those had moved since the previous
sweep. None of that is specific to TETRA, so no threshold on those features
could separate a police handset from any other bursty RF. Field recordings
made the failure explicit:

- the duplex-pair gate passed **100%** of the 390-395 MHz channels it scored,
  so "paired with a downlink" meant nothing;
- the temporal novelty gate rejected **zero** candidates during busy sweeps;
- alerts landed on three neighbouring 25 kHz raster points at once, which a
  single TETRA carrier physically cannot do.

Each release added another subtractive workaround -- a clutter baseline, a
coherent-comb rejector, a broadband guard -- and each one could also suppress
a real detection. v8 removes all of them and tests the signal instead.

### What is actually verified

Three independent physical-layer properties, in `rfeye/tetra_phy.py`:

| Test | What it proves | What it defeats |
| --- | --- | --- |
| pi/4-DQPSK at exactly 18000 baud | the modulation is TETRA | nearly everything else |
| Four-phase differential spread | four real constellation points, not one | CW spurs, narrowband FSK |
| Symbol-rate selectivity vs decoy rates | 18 kbaud specifically, not "some digital signal" | other DQPSK systems |
| 25 kHz RRC(0.35) shape, channel-edge valley | one 25 kHz carrier, not a wide hump | broadband clutter |
| 14.1667 ms TDMA slots, 17.647 Hz frame line | TETRA burst structure | arbitrary gated signals |

The fourth-moment test is frequency-offset invariant, so it works straight off
an uncalibrated RTL-SDR with tens of ppm of crystal error. Each score is
measured against deliberately mismatched decoy hypotheses -- a wrong symbol
rate, a wrong frame rate -- so the headline numbers are ratios rather than
absolute levels, and need no recalibration per unit, antenna or location.

### Two stages: lock the network, then watch it

**Stage 1 -- network lock.** C2000 base stations transmit continuously in
390-395 MHz, so they can be verified thoroughly and repeatedly. A carrier must
pass the full waveform test on several separate dwells before it counts. The
result is stored in `~/.local/state/rfeye/c2000-sites.json` and survives
restarts.

**If no base station verifies, the device stays silent and says so.** Without
C2000 coverage there is nothing to be near, so any alert would be wrong. The
main screen shows `SEARCHING FOR C2000 NETWORK` rather than an implied
all-clear.

**Stage 2 -- uplink watch.** TETRA duplex spacing in this band is 10 MHz, so
each verified downlink names exactly one uplink channel where handsets on that
site transmit: a verified downlink at 391.2375 MHz means handsets transmit at
381.2375 MHz. The uplink search is therefore a short watch list rather than a
blind sweep of 200 channels, and one 288 kS/s dwell covers a whole site's
worth of channels at once.

An alert means: a handset physically near this receiver is transmitting on a
carrier belonging to a base station this device independently verified.

### What it cannot tell you

C2000 carries police, ambulance, fire and the KMar on one shared network,
encrypted with TEA2. Nothing in the RF layer identifies the service, the unit
or the user, and RF Eye makes no attempt to decode traffic -- it measures
modulation structure only. A confirmed alert means *an emergency services
radio is transmitting nearby*, not specifically *police*. The display reports
RF activity and status only; it does not identify a transmitter or determine
a physical distance.

### Sampling and timing

The verification dwell runs at 288 kS/s: exactly 28.8 MHz / 100, and exactly
16x the 18000 baud symbol rate, so channel decimation and the symbol clock are
both exact with no resampling. Each dwell is 2^18 samples (0.910 s, about 16
TDMA frames). The tuner is deliberately offset from every channel under test
so the RTL-SDR DC spike never lands on a carrier being measured.

On the reference Pi 3 B+ a quiet cycle costs about 1.2 s and a cycle carrying
a real transmission about 1.7 s. Tests run cheapest first and stop at the
first hard failure, so eight empty channels cost the same as one.

### Verifying the detector yourself

Both suites run without an SDR, an antenna or a real transmission:

```bash
python3 scripts/tetra-phy-selftest.py --table
```

```bash
python3 scripts/detector-selftest.py --verbose
```

`tetra-phy-selftest.py` generates known-truth TETRA at several SNRs plus every
interferer shape that has caused a false alarm, then asserts the verdicts.
`--table` prints every measured score; that table is how the acceptance limits
in `tetra_phy.LIMITS` were chosen. `detector-selftest.py` drives the whole
backend against a simulated air interface, including the two cases that
mattered most in the field: a TETRA-shaped burst with no network behind it,
and interference sitting on exactly the uplink channel being watched. Both
must stay silent.

## RF recording

**Record RF** stores a short time series for later analysis. Recording starts only after a deliberate press on the lower **JA** button; the upper **NEE** area cancels and returns to Settings.

Captured JSON files are stored locally under:

```text
~/.local/share/rfeye/captures/
```

They are not committed to GitHub automatically. New files use the human-readable local start time as their filename, for example `2026-09-04_20-26-35.json`. Opening the Recordings browser also migrates older `rf-series-...` names from the embedded `recorded_from` timestamp without changing recording contents.

Recording schema v8 stores the physical-layer verdict for every channel that
was examined -- the pi/4-DQPSK score, the symbol-rate selectivity, the TDMA
timing and which individual check failed -- so a replay shows the real
reasoning rather than a re-derived score.

**When a recording captures an alert, the raw dwell is written beside it** as
an `.iq8` sidecar (plain unsigned 8-bit interleaved I/Q, the same format
`rtl_sdr` writes) with a small JSON header. This is the evidence trail that
recordings of derived statistics could never provide: any alert can be settled
from the signal itself.

```bash
python3 scripts/analyse-capture.py ~/.local/share/rfeye/captures/*.iq8
```

That re-runs the identical verification and prints every measured quantity
with each check marked pass or fail, so a borderline result shows precisely
what was borderline.

Pre-v8 recordings contain only the old energy statistics. Nothing in them can
be re-analysed with the v8 waveform tests, so they are shown read-only and
labelled **PRE-v8 ARCHIVE**; v8 files are labelled **PHY v8**. Delete is
protected by a separate YES/NO confirmation page.

Replay is offline and does not stop or reopen the live RTL-SDR backend. Since RF Eye 0.7.34, the scan worker owns the persistent librtlsdr handle and closes it only after synchronous capture work has finished. The UI/service thread never closes the handle underneath an active `rtlsdr_read_sync()` call.

## Buzzer wiring

RF Eye 0.7.37 uses a **TMB12A03 active buzzer**:

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

The updater downloads the ZIP, requires and verifies its SHA-256, validates every archive path before extraction, makes a backup and replaces the RF Eye application contents. Starting with 0.7.37, replacement is a real content replacement rather than an overlay, so a file deliberately removed by a release cannot survive as a stale Python module. A copy/delete failure triggers best-effort restoration from the backup. Starting with 0.7.30, a successful OTA install automatically exits the running app after showing `RESTARTING`; the `rfeye-user.service` (`Restart=always`) then relaunches RF Eye from the newly installed files. The visible `RESTART` action is also a real manual restart fallback.

Application-only OTA updates update `/opt/rfeye/rfeye`. Device Tree, systemd, Plymouth and boot-service changes are applied by `install-cuqi35.sh` and therefore require root.

## Release build

`VERSION` and `rfeye/config.py` identify this release as **0.7.37**.

Build the OTA package with:

```bash
./scripts/build-release.sh
```

The build normalizes archive metadata so unchanged source produces the same ZIP SHA-256 and compiles every shipped Python runtime module. GitHub Actions rebuilds the `main` manifest/ZIP after a release commit and runs syntax, detector, headless compact-App/UI, updater/rollback, startup-snapshot and deterministic-release checks.

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
rfeye/tetra_phy.py             ETSI TETRA waveform verification (no hardware)
rfeye/tetra_detector.py        network lock, duplex maths, alarm hysteresis
rfeye/tetra_sim.py             synthetic TETRA and interferers for testing
scripts/tetra-phy-selftest.py  waveform test suite, known-truth signals
scripts/detector-selftest.py   end-to-end test against a simulated air interface
scripts/analyse-capture.py     re-verify a saved .iq8 capture offline
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
