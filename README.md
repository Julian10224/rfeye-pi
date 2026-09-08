# RF Eye 0.9.10 for Raspberry Pi

This repository contains the complete **RF Eye 0.9.10 reference appliance** for the MHS35/CUQI-style 3.5-inch SPI touchscreen.

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

The installer reproduces the working 0.9.10 appliance path:

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

## User interface in 0.9.10

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

The fourth-moment test is frequency-offset invariant, so it needs no frequency
correction of its own. That invariance covers the measurement, not the tuning:
getting the carrier into the extracted channel still depends on the receiver,
so the analysis re-centres on the measured carrier first. Together that
tolerates about 31 ppm of crystal error at 390 MHz -- more than an
uncalibrated RTL-SDR needs -- and rejects beyond it rather than reporting a
frequency that is wrong. Each score is
measured against deliberately mismatched decoy hypotheses -- a wrong symbol
rate, a wrong frame rate -- so the headline numbers are ratios rather than
absolute levels, and need no recalibration per unit, antenna or location.

### Two stages: lock the network, then watch it

**Stage 1 -- network lock.** C2000 base stations transmit continuously in
390-395 MHz, so they can be verified thoroughly and repeatedly. A carrier must
pass the full waveform test on several separate dwells before it counts. The
result is stored in `~/.local/state/rfeye/c2000-sites.json` and survives
restarts.

**A lock is only kept while it keeps proving itself.** Locked carriers are
re-verified on their own timer, independent of the band pass, and a base
station's main carrier is continuous by definition -- so a locked site whose
carriers all stop answering is not a quiet site, it is a receiver that has
lost it. After a few such rounds the lock is dropped and the display returns
to searching. Unscrewing the antenna clears it within a couple of minutes
rather than leaving `C2000 NETWORK LOCKED` on screen for the best part of an
hour, which is what 0.9.3 and earlier did.

**If no base station verifies, the device stays silent and says so.** Without
C2000 coverage there is nothing to be near, so any alert would be wrong. The
main screen shows `SEARCHING FOR C2000 NETWORK` rather than an implied
all-clear.

### How the band is searched (0.9.10)

Ranking downlink candidates by raw power does not survive contact with real
hardware. On the reference unit the ten strongest channels in 390-395 MHz sat
on an 800 kHz grid with +/-25 kHz siblings, and their level did not improve
relative to the noise floor as tuner gain rose from 20 to 50 dB. That is the
RTL-SDR's own internal spur comb, not a network -- and it filled every slot of
the survey shortlist, so a genuine but weaker C2000 carrier was never handed
to the verifier at all.

Two changes follow from that:

**The survey scores shape, not just level.** A TETRA carrier is flat right
across its 25 kHz; a spur is a narrow line towering over its own median. The
score subtracts that peakiness, so carriers outrank artefacts. This only
reorders work -- it can never admit anything, because every channel still has
to pass the full waveform test.

**The survey is no longer the only path.** Every channel it can score goes to
the front of the queue, best-looking first, followed by *every* remaining
raster channel in the band. The survey can reorder the work but cannot hide
any of it. A full pass is about 50 dwells, roughly 70 seconds, and it keeps
running after the first lock: a TETRA site operates several carriers and a
handset can be on any of them, so stopping at the first would leave real
uplink channels unwatched.

**Queue order is the time-to-lock budget.** One dwell covers four channels and
costs about 1.1 s, so the position of a real carrier in a 200-channel queue is
the difference between locking in five seconds and locking in seventy. Up to
0.9.4 only the survey's top twelve were promoted and the other 188 followed in
frequency order, which is uncorrelated with signal strength; since 0.9.10 the
whole ranking is used. A carrier that passes the waveform test is re-tested on
every following cycle, so the three hits a lock needs cost three cycles, not
three band passes.

**Where the tuner is parked matters.** The RTL-SDR's DC spike sits exactly at
the tuner centre, and channels lie on a continuous 25 kHz grid, so a tuner
centred on a group of carriers lands the spike on one of them -- there is no
gap in a contiguous run to hide in. The dwell planner therefore parks the
tuner clear of the highest member of the group, which also bounds a dwell to
the four channels that still fit inside the usable window.

### Main carriers and traffic carriers

ETSI EN 300 392-2 requires only the **main carrier** to be transmitted
continuously; it is what mobiles synchronise to. A site's **secondary traffic
carriers are discontinuous**, and timeshared control-channel modes exist as
well.

Up to 0.9.0 a downlink had to be transmitting at least 80% of the time to
lock. That reliably found the main carrier, but nothing else -- and since the
uplink watch list is derived from locked downlinks, every call that moved to a
traffic carrier went unwatched. On a busy site that is where the traffic is.

The duty floor is now 0.15, because in 390-395 MHz *only base stations
transmit*: a carrier that has already passed the full waveform test there is a
base station whatever its duty cycle. Duty is still measured, and a carrier
above 80% is flagged as the continuous main carrier, but it is diagnostic
rather than a gate. A known carrier that is simply idle during a re-check is
recorded as "no information" instead of counting against it, so an idle
traffic carrier does not slowly unlock itself.

**Stage 2 -- uplink watch.** TETRA duplex spacing in this band is 10 MHz, so
each verified downlink names exactly one uplink channel where handsets on that
site transmit: a verified downlink at 391.2375 MHz means handsets transmit at
381.2375 MHz. The uplink search is therefore a short watch list rather than a
blind sweep of 200 channels, and one 288 kS/s dwell covers a whole site's
worth of channels at once.

An alert means: a handset physically near this receiver is transmitting on a
carrier belonging to a base station this device independently verified.

Confirmation is counted in **visits to that channel**, not in seconds. How
often a given uplink channel comes round depends on how many carriers the site
runs, and on a busy site a seconds-based window can expire between two looks
at the same channel -- silently making confirmation unreachable exactly where
detection matters most. Counting visits makes the rule independent of cycle
duration, hardware speed and watch-list length.

### Why a search found nothing

"SEARCHING" for half an hour is indistinguishable, from the outside, between
no C2000 in range, an antenna that fell off, and one acceptance limit set too
tight. Since 0.9.10 the debug page carries the best channel of the current band
pass with its SNR and the check it failed on, and every completed pass appends
one line to `~/.local/state/rfeye/search.log`:

```text
2026-09-07T20:44:11 pass=3 69s best=391.1875MHz snr=10.6 fail:bandwidth locked=0 cand=0
```

`fail:bandwidth` with the occupied width pinned at the 40 kHz cap means the
spectrum never dropped 10 dB anywhere inside the channel -- that is flat noise,
not a carrier the limits refused. Cross-check with a tool that shares no code
with this one:

```bash
rtl_power -f 390M:395M:12.5k -g 37.2 -i 20 -1 /tmp/band.csv
```

If the strongest channels there sit on an 810 kHz grid, that is the dongle's
own spur comb and there is genuinely nothing to lock.

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
first hard failure, so a dwell full of empty channels costs the same as one.

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
in `tetra_phy.LIMITS` were first chosen.

A simulator proves the tests are correct. It does not prove that real C2000
signals on a real RTL-SDR land inside limits derived from it, and on one of
them it turned out they nearly did not: measured against a live multi-carrier
site, symbol-rate selectivity came out at 1.63-2.90 where simulation predicted
3.2-3.4, leaving a genuine carrier two per cent above a limit of 1.60. The
limit is now 1.35. Channel shape, by contrast, matched almost exactly.
[docs/FIELD-CALIBRATION.md](docs/FIELD-CALIBRATION.md) records the real
measurements, how they were cross-checked with `rtl_power`, and what was ruled
out.

`detector-selftest.py` drives the whole backend against a simulated air
interface, over eight scenarios drawn from what the hardware actually did:

1. empty band with clutter -- never locks, never alerts
2. C2000 site present, nobody transmitting -- locks, stays silent
3. C2000 site with a handset keyed -- locks, then alerts on the right channel
4. a TETRA-shaped burst with no network behind it -- never alerts
5. interference on exactly the watched uplink channel -- locked, still silent
6. a real site buried under an RTL-SDR spur comb -- locks the carriers, never
   a comb tooth
7. a full band pass over an empty band -- makes progress, never alerts
8. dwell planning and raster arithmetic

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

RF Eye 0.9.10 uses a **TMB12A03 active buzzer**:

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

### A mouse cursor on a black panel

This one is not the application at all. The desktop is up and RF Eye is simply
not running, and until 0.9.10 it could not come back on its own.

`rfeye-user.service` carries `Restart=always`, but systemd also enforces a
start limit: fail `StartLimitBurst` times inside `StartLimitIntervalSec` and it
stops trying and leaves the unit `failed`, until someone resets it by hand. The
defaults are five starts in ten seconds, and the unit asked for `RestartSec=0.5`
-- so five quick failures took about two and a half seconds. At boot, with the
compositor, the panel and the USB bus all still settling, that is easy to hit
once; after that the appliance stays dead through every later power cycle,
writing nothing anywhere, because it is never started again.

```bash
systemctl --user status rfeye-user.service     # Result: start-limit-hit
```

Since 0.9.10 the unit sets `StartLimitIntervalSec=0` and `RestartSec=2`, and the
application retries opening the display in-process instead of exiting, so a
display that is a second late costs nothing. Fresh installs get this from
`scripts/apply-cuqi35-system-fixes.sh`. An already-installed unit can be
repaired without root, because the drop-in directory belongs to the user:

```bash
mkdir -p ~/.config/systemd/user/rfeye-user.service.d
printf '[Unit]\nStartLimitIntervalSec=0\n\n[Service]\nRestartSec=2\n' \
  > ~/.config/systemd/user/rfeye-user.service.d/30-rfeye-restart.conf
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user reset-failed rfeye-user.service
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart rfeye-user.service
```

### If the panel goes black after an update

Since 0.9.10 every start appends a line to `~/.local/state/rfeye/boot.log`:
`start` with the release, `ui-loop` with the display profile and geometry,
`first-frame` the first time a frame is presented, and `exit` with the fault
count. The user journal does not survive a power cut on these units, so after
one comes up dark this file is what says whether the app started at all,
whether it ever drew anything, and which layout it chose:

```bash
tail -20 ~/.local/state/rfeye/boot.log
cat ~/.local/state/rfeye/crash.log
journalctl -b -1 _SYSTEMD_USER_UNIT=rfeye-user.service   # the boot that failed
```

A black panel should no longer be possible from an application fault. Any
exception raised while drawing a frame is caught, shown on the panel as
**RF EYE FAULT** with the exception text, the page and the release, and
appended once per distinct fault to `crash.log`. Before 0.9.7 one exception
ended the process, systemd restarted it half a second later
(`Restart=always`), and a fault that repeated every frame became a restart
loop whose only visible symptom was a black screen.

That restart was also the accidental recovery from a transient failure at
boot -- a display not ready yet, a device not enumerated yet -- so catching
everything would have replaced a restart loop with a unit stuck in a fault for
ever. Since 0.9.10 a fault that has not cleared after about three seconds hands
the process back to systemd deliberately, which keeps both the message and the
recovery.

If a unit goes dark and `boot.log` shows `first-frame`, the application drew
something and the cause is below it: the compositor, the panel, or the layout
problem described next.


That split is also the one thing an OTA update can get wrong on a unit whose
system files were installed by an older release. The runtime learns which panel
it is driving from `RFEYE_DISPLAY_PROFILE`, exported by the systemd user unit --
and that unit is exactly what an application-only update may not rewrite. A unit
predating that variable keeps the 480x800 layout on a 480x320 panel, which puts
the top-left corner of a much larger screen on the display: mostly empty
background, no buttons, no readings. It looks like a dead device.

Since 0.9.10 the runtime falls back to the `display_profile` already recorded in
the saved config, so this repairs itself on the next start. An explicit
environment value still wins. To check a unit:

```bash
systemctl --user show rfeye-user.service -p Environment
grep display_profile ~/.config/rfeye/config.json
WAYLAND_DISPLAY=wayland-0 grim /tmp/screen.png   # what the panel is actually showing
```

The permanent fix for such a unit is to re-run `install-cuqi35.sh`, which brings
its system files back up to date. Every OTA also leaves the previous runtime in
`~/.local/state/rfeye/rfeye.backup`, so a bad update can be undone without
network access:

```bash
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop rfeye-user.service
cd /opt/rfeye/rfeye && find . -mindepth 1 -maxdepth 1 ! -name __pycache__ -exec rm -rf {} +
cp -a ~/.local/state/rfeye/rfeye.backup/. /opt/rfeye/rfeye/
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start rfeye-user.service
```

## Release build

`VERSION` and `rfeye/config.py` identify this release as **0.9.10**.

Build the OTA package with:

```bash
./scripts/build-release.sh
```

The build normalizes archive metadata so unchanged source produces the same ZIP SHA-256 and compiles every shipped Python runtime module. GitHub Actions rebuilds the `main` manifest/ZIP after a release commit and runs syntax, detector, headless compact-App/UI, updater/rollback, startup-snapshot and deterministic-release checks.

## Power mode

Settings carries a **Power** row with two positions, and **ECO is the default**.

|  | ECO (default) | MAX |
|---|---|---|
| UI frame rate | 8 fps | 20 fps |
| Pause between dwells | 1.5 s | none |
| Measured app CPU (Pi 3 B+) | **~39 % of a core** | **~85 %** |

The measurement above was taken on the reference unit over an 8 second average,
twice each way. Most of that load is the UI, not the detector: every frame
rotates and rescales a 320x480 surface for the panel, so the frame rate is the
biggest single lever. The dwell pause is the second one, and it is what lets the
governor clock back down instead of sitting at full speed indefinitely.

ECO costs time to lock. A full 200-channel band pass is about 70 s in MAX and
roughly twice that in ECO, because each dwell is followed by the pause. If a
unit has a healthy supply and you want the fastest acquisition, switch to MAX.

**Switching the mode also re-initialises USB.** That is deliberate rather than
tidy: the reason to reach for this setting is an RTL-SDR that has just dropped
off a marginal 5 V rail, and having to reboot to find out whether the change
helped would make the setting useless. Toggling the row closes the librtlsdr
handle, clears the failure counters and the reset back-off, and resets the
device over USB if it is on the bus. The panel reports what it found --
`SDR found, USB reset`, `SDR on the bus`, or `SDR not on the USB bus`. A device
that is not enumerated cannot be reset by anyone, so that case is reported
rather than papered over.

## Supply voltage

A Raspberry Pi that browns out and an RTL-SDR that has failed look identical
on screen, and they need completely different fixes. When the Pi reports
under-voltage RF Eye raises a **USB POWER TOO LOW** notice once per session,
carrying the measured core voltage and the raw `get_throttled` word, dismissed
with a **BEGREPEN** button. It is drawn over whatever page is up and the scan
thread never sees it: searching, locking and alerting all continue behind it.
If the dongle has actually dropped off the bus the headline says
`SDR LOST - USB POWER LOW` instead of `SDR NOT CONNECTED`, and the debug page
carries `Supply` and `Supply detail` rows.

Only bit 0 of `get_throttled` -- the rail is low *right now* -- raises the
notice. Bit 16 latches at the first dip and never clears, so up to 0.9.4 a
single brown-out during power-up left the warning on screen for the rest of
the session over the top of a perfectly healthy scan; it is now reported as
history on the debug page and nowhere else.

This matters more than it sounds. A USB reset is the right response to a
dongle that is attached but has stopped answering, and the wrong response to
one that is browning out: forcing re-enumeration during a supply sag stops the
device coming back at all. On the reference unit that turned a momentary dip
into a dongle that stayed gone until the Pi was rebooted. RF Eye now resets
only a device that is actually present on the USB bus, backs off between
attempts and gives up after three, clearing the counter as soon as a scan
succeeds.

If the display shows this, check the supply before anything else:

```bash
vcgencmd get_throttled
```

`0x0` is healthy. Bit 0 set means under-voltage right now; bit 16 means it has
happened since boot. An RTL-SDR draws around 300 mA, so a Pi 3 B+ needs a real
5 V / 3 A supply with a short, thick cable, or the dongle on a powered hub.

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
docs/FIELD-CALIBRATION.md      what real C2000 carriers actually measure
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
