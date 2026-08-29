# RF Eye buzzer / speaker installation — Raspberry Pi 3B+

RF Eye drives its alert sound from **BCM GPIO26**, which is **physical header pin 37** on a Raspberry Pi 3B+. The supported RF Eye hardware profile is the **TMB12A03 active buzzer**.

The software defaults are:

```text
buzzer_gpio = 26
buzzer_model = TMB12A03
buzzer_passive = false
buzzer_active_high = true
```

RF Eye uses BCM numbering. `GPIO26` means BCM26 / physical pin 37, not physical pin 26.

## Raspberry Pi 3B+ pins used

Near the bottom end of the 40-pin header:

```text
Raspberry Pi 3B+ 40-pin header

 GPIO20 (38)  .   . (37) GPIO26  <- RF Eye alert signal
     GND (40) .   . (39) GND     <- recommended nearby ground
```

For RF Eye:

| Function | BCM name | Physical pin |
|---|---:|---:|
| Alert output | GPIO26 | 37 |
| Ground | GND | 39 (or another GND pin) |
| 3.3 V, if required by a module | 3V3 | 1 or 17 |
| 5 V, only if the selected module requires it | 5V | 2 or 4 |

GPIO26 is outside the first 26 physical header pins used by the CUQI 3.5-inch SPI display, so the RF Eye CUQI display fork can use GPIO26 without consuming one of the display's 26-pin connections.

---

## Recommended RF Eye hardware — TMB12A03 active buzzer

The TMB12A03 is an **active buzzer**: it contains its own oscillator and therefore produces its own fixed pitch when enabled. RF Eye creates different alert states with different beep rhythms rather than PWM pitch changes.

For a module or driver input that is confirmed to accept 3.3 V GPIO logic:

```text
Pi physical pin 37 / BCM GPIO26  ---> SIG / control input
Pi physical pin 39 / GND         ---> GND
module VCC                         ---> supply required by the module
```

If the buzzer is a bare two-lead component and its current is not explicitly safe for direct GPIO drive, use a transistor driver rather than drawing buzzer current directly from GPIO26.

Do **not** put 5 V onto GPIO26. Raspberry Pi GPIO is 3.3 V logic.

---

## Transistor driver for a higher-current buzzer

A transistor is the preferred arrangement when the buzzer current is above a small GPIO load or when the current requirement is unknown.

Typical parts:

- NPN transistor such as **2N2222**, **PN2222** or **BC547**
- approximately **1 kΩ** base resistor
- buzzer rated for the selected supply voltage
- flyback diode if the sounder is magnetic/inductive and its datasheet requires one

Typical control wiring:

```text
                         +3.3 V or +5 V, as required by buzzer
                              |
                           buzzer +
                           buzzer -
                              |
                              +--------- Collector
                                        NPN transistor
Pi GPIO26 / pin 37 -- 1kΩ -- Base
                                        Emitter
                                           |
Pi GND / pin 39 ---------------------------+
```

The Pi and buzzer driver must share ground. The buzzer supply voltage does not change the GPIO signal level: GPIO26 remains a 3.3 V logic output.

---

## Normal 4 Ω / 8 Ω loudspeaker

Do **not** wire a normal loudspeaker directly between GPIO26 and GND. Its impedance is too low for a Raspberry Pi GPIO.

Use an audio amplifier/module between the Pi and speaker. The basic arrangement is:

```text
GPIO26 / pin 37  ---> amplifier/control input
Pi GND           ---> amplifier GND
amplifier OUT    ---> 4 Ω / 8 Ω speaker
amplifier power  ---> supply specified by amplifier manufacturer
```

RF Eye's current alert implementation is intended for the TMB12A03 active buzzer. It outputs timed on/off alert patterns, not hi-fi analogue audio.

---

## Three-pin buzzer module

Some buzzer modules expose `S`, `+`, `-` or `SIG`, `VCC`, `GND`.

When the module documentation confirms 3.3 V logic compatibility:

```text
S / SIG  -> Pi physical pin 37 / GPIO26
- / GND  -> Pi physical pin 39 / GND
+ / VCC  -> voltage required by the module
```

Do not connect a 5 V signal output to GPIO26.

---

## Software configuration

The relevant defaults are in `rfeye/config.py`:

```python
"buzzer_gpio": 26,
"buzzer_model": "TMB12A03",
"buzzer_passive": False,
"buzzer_active_high": True,
```

The installation/runtime patch also forces the current RF Eye hardware profile to GPIO26. This intentionally overrides an older persisted configuration that may still contain `buzzer_gpio: 18`.

The driver is implemented in `rfeye/buzzer.py`. Because the TMB12A03 is active, RF Eye switches the output on and off instead of using PWM to select a tone.

The current alert rhythms are:

- LOW/green: one calm pulse
- MEDIUM/yellow: double beep
- HIGH/red: urgent triple beep
- startup acknowledgement: short-short-long, once the SDR reaches `LIVE`

---

## Quick hardware test

After wiring the TMB12A03 or its driver input, run:

```bash
cd /opt/rfeye/rfeye
/opt/rfeye/.venv/bin/python - <<'PY'
import time
from buzzer import GPIOBuzzer

b = GPIOBuzzer(pin=26, passive=False, active_high=True)
print("buzzer available:", b.available)
b.beep_pattern([(100, 80), (100, 80), (220, 0)])
time.sleep(1.0)
b.close()
PY
```

Expected result: a short-short-long beep pattern.

If `buzzer available: False` is printed, check the `RPi.GPIO` installation and GPIO permissions. The RF Eye installer installs the required Raspberry Pi GPIO package and adds the RF Eye user to the `gpio` group.

---

## Troubleshooting

### No sound

Check, in this order:

1. RF Eye is not muted.
2. The signal/control wire goes to **BCM GPIO26 / physical pin 37**.
3. Ground is connected, preferably to physical pin 39.
4. The buzzer/module has the correct supply voltage.
5. The buzzer is an active TMB12A03-compatible device or uses a suitable driver.
6. Run the quick hardware test above.

### Buzzer stays on continuously

Check whether the module is active-low rather than active-high, and verify that its signal pin is not being tied directly to a supply rail. RF Eye's default is `buzzer_active_high=true`.

### Pi becomes unstable or GPIO gets hot

Disconnect power immediately. Do not power a high-current buzzer or low-impedance loudspeaker directly from GPIO26. Use a transistor driver or amplifier.

## Recommended RF Eye connection

```text
TMB12A03 active buzzer / compatible driver
signal -> physical pin 37 (BCM GPIO26)
ground -> physical pin 39 (GND)
```

Use a transistor driver whenever the buzzer's input/current specification is not clearly safe for direct 3.3 V GPIO logic.
