# RF Eye buzzer / speaker installation — Raspberry Pi 3B+

RF Eye currently drives its alert sound from **BCM GPIO18** using `RPi.GPIO`. On a Raspberry Pi 3B+ this is **physical header pin 12**.

The software defaults are:

```text
buzzer_gpio = 18
buzzer_passive = true
buzzer_active_high = true
buzzer_low_hz = 900
buzzer_high_hz = 1500
buzzer_duration_ms = 85
```

The recommended hardware is a **small passive piezo buzzer**. Do **not** connect a normal 4 Ω or 8 Ω loudspeaker directly to a Raspberry Pi GPIO pin.

## Raspberry Pi 3B+ pins used

With the USB/Ethernet connectors at the bottom and the 40-pin header at the top of the board:

```text
Raspberry Pi 3B+ 40-pin header

        3V3  (1) (2)  5V
              .   .
              .   .
              .   .
 GPIO18 (12)  .   . (11) GPIO17
     GND (14) .   . (13) GPIO27
              .   .
```

For RF Eye:

| Function | BCM name | Physical pin |
|---|---:|---:|
| Alert output | GPIO18 | 12 |
| Ground | GND | 14 (or another GND pin) |
| 3.3 V, if required | 3V3 | 1 or 17 |
| 5 V, only if the selected module requires it | 5V | 2 or 4 |

**Important:** RF Eye uses BCM numbering. `GPIO18` means BCM18 / physical pin 12, not physical pin 18.

---

## Option A — recommended: small passive piezo buzzer

For a low-current passive piezo buzzer that is explicitly suitable for 3.3 V GPIO drive:

```text
Pi pin 12 / GPIO18  --------  buzzer +
Pi pin 14 / GND     --------  buzzer -
```

A passive buzzer does not generate its own fixed tone. RF Eye supplies PWM and therefore can use the configured 900 Hz and 1500 Hz alert tones.

### Installation

1. Shut the Raspberry Pi down completely.
2. Disconnect USB-C/micro-USB power before touching the GPIO header.
3. Connect the buzzer positive lead to **physical pin 12 (GPIO18)**.
4. Connect the buzzer negative lead to **physical pin 14 (GND)**.
5. Check polarity if the buzzer has `+` and `-` markings.
6. Power the Pi back on.

Use this direct connection only for a small, low-current piezo device. If the component is magnetic, has an unknown current requirement, or draws more than a small GPIO load, use Option B.

---

## Option B — preferred for a louder buzzer: transistor driver

A transistor prevents the buzzer current from being supplied by GPIO18 itself. This is the safer layout for a larger passive buzzer or a small magnetic sounder.

Parts:

- NPN transistor such as **2N2222**, **PN2222** or **BC547**
- **1 kΩ** resistor between GPIO18 and transistor base
- buzzer rated for the supply voltage used
- optional flyback diode for a magnetic/inductive buzzer; it is not required for an ordinary piezo element

Wiring:

```text
                         +3.3 V or +5 V
                              |
                              |
                           buzzer +
                           buzzer -
                              |
                              +--------- Collector
                                        NPN transistor
Pi GPIO18 / pin 12 -- 1kΩ -- Base
                                        Emitter
                                           |
Pi GND / pin 14 ---------------------------+
```

If the buzzer is a magnetic/inductive type, place a flyback diode across the buzzer according to the buzzer/transistor datasheet. Do not guess the polarity or supply voltage: use the voltage printed on the component/module.

GPIO18 remains a **3.3 V logic signal**, even if the buzzer itself is powered from 5 V through the transistor.

---

## Option C — actual 4 Ω / 8 Ω loudspeaker

Do **not** wire a normal loudspeaker between GPIO18 and GND. Its impedance is far too low for a Raspberry Pi GPIO pin.

Use a small audio amplifier/module between the Pi and speaker, for example a 3.3/5 V amplifier that accepts a logic/PWM or audio input. The basic arrangement is:

```text
GPIO18 / pin 12  ---> amplifier input
Pi GND            ---> amplifier GND
amplifier OUT     ---> 4 Ω / 8 Ω speaker
amplifier power   ---> supply specified by amplifier manufacturer
```

The amplifier and Pi must share ground unless the amplifier documentation explicitly says otherwise.

RF Eye currently outputs simple PWM alert tones on GPIO18; it is not a hi-fi analogue audio output. A passive piezo buzzer is therefore the simplest supported choice.

---

## Three-pin buzzer module

Some buzzer boards have pins marked `S`, `+`, `-` or `SIG`, `VCC`, `GND`.

Typical connection **only when the module documentation confirms 3.3 V logic compatibility**:

```text
S / SIG  -> Pi physical pin 12 / GPIO18
- / GND  -> Pi physical pin 14 / GND
+ / VCC  -> 3.3 V or 5 V according to the module specification
```

Do not connect a module marked 5 V-only to the 3.3 V rail and do not put 5 V onto GPIO18.

---

## Software configuration

RF Eye already defaults to GPIO18 and a passive buzzer, so no configuration change is normally required.

The relevant defaults are in `rfeye/config.py`:

```python
"buzzer_gpio": 18,
"buzzer_passive": True,
"buzzer_active_high": True,
"buzzer_low_hz": 900,
"buzzer_high_hz": 1500,
"buzzer_duration_ms": 85,
```

The driver is implemented in `rfeye/buzzer.py` and uses PWM when `buzzer_passive` is enabled.

### Active buzzer instead of passive buzzer

An active buzzer already contains its own oscillator. For that hardware set:

```json
"buzzer_passive": false
```

RF Eye will then switch GPIO18 on/off rather than trying to generate different PWM pitches. With an active buzzer the 900/1500 Hz distinction is generally not audible because the buzzer determines its own pitch.

---

## Quick hardware test

After wiring a supported passive buzzer, the following test can be run on the Pi:

```bash
cd /opt/rfeye/rfeye
/opt/rfeye/.venv/bin/python - <<'PY'
import time
from buzzer import GPIOBuzzer

b = GPIOBuzzer(pin=18, passive=True, active_high=True)
print("buzzer available:", b.available)
b.beep(900, 250, 50)
time.sleep(0.4)
b.beep(1500, 250, 50)
time.sleep(0.4)
b.close()
PY
```

Expected result: two short tones, with the second tone higher than the first.

If `buzzer available: False` is printed, check the `RPi.GPIO` installation and GPIO permissions. The RF Eye installer installs the required Raspberry Pi GPIO package and adds the RF Eye user to the `gpio` group.

---

## Troubleshooting

### No sound

Check, in this order:

1. Pi is fully powered and RF Eye is not muted.
2. Buzzer `+` really goes to GPIO18 / physical pin 12.
3. Buzzer `-` goes to GND, for example physical pin 14.
4. The component is a **passive** buzzer when `buzzer_passive=true`.
5. Run the quick hardware test above.
6. Check the component voltage/current specification.

### Only one fixed tone

You probably have an active buzzer. Either replace it with a passive piezo buzzer or set `buzzer_passive` to `false`.

### Pi becomes unstable or GPIO gets hot

Disconnect power immediately. Do not drive a low-impedance speaker or high-current buzzer directly from GPIO. Use the transistor or amplifier arrangement described above.

## Recommended RF Eye build

For the simplest reliable installation on a Raspberry Pi 3B+:

```text
Passive 3.3 V piezo buzzer
+  -> physical pin 12 (BCM GPIO18)
-  -> physical pin 14 (GND)
```

For anything louder or with an unknown current draw, use a transistor driver rather than powering it directly from GPIO18.
