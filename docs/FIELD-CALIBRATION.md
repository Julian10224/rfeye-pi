# Field calibration of the C2000 detector

The acceptance limits in `rfeye/tetra_phy.py` were first chosen from simulated
TETRA (`scripts/tetra-phy-selftest.py`). A simulator proves the tests are
*correct*; it does not prove that real C2000 signals, received on an RTL-SDR,
land inside the limits derived from it. This file records what real carriers
actually measure, so the limits can be argued from data rather than defended
from theory.

## Reference measurement

Raspberry Pi 3 B+, RTL-SDR Blog V4 (R828D), indoor antenna at a window, mains
supply healthy (`vcgencmd get_throttled` = `0x0` at the time of measurement).
A multi-carrier C2000 site was present. The three strongest carriers were
independently confirmed with `rtl_power`, which is not part of this codebase:

```bash
rtl_power -f 390M:395M:12.5k -g 37.2 -i 20 -1 dl.csv
```

`rtl_power` reported +27 dB over the median floor at 390.7375, 391.1875 and
391.7625 MHz. All three sit exactly on the ETSI raster (`390.0125 MHz +
N x 25 kHz`), which is itself a useful cross-check that the frequency mapping
in this codebase is right.

## What real carriers score

Six downlink carriers of the same site, `DOWNLINK` role, 0.910 s dwell:

| carrier (MHz) | SNR dB | occupied BW | edge valley | dqpsk_m | 4-phase | selectivity |
| --- | --- | --- | --- | --- | --- | --- |
| 390.7375 | 26.3 | 21.7 kHz | 18.5 dB | 0.347 | 0.803 | **1.63** |
| 391.1875 | 25.6 | 21.9 kHz | 18.1 dB | 0.632 | 0.774 | 2.90 |
| 391.7625 | 28.9 | 21.9 kHz | 19.8 dB | 0.508 | 0.787 | 2.07 |
| 390.0375 | 23.2 | 21.7 kHz | 16.0 dB | 0.510 | 0.811 | 2.10 |
| 390.1625 | 14.9 | 25.6 kHz | 11.7 dB | 0.339 | 0.891 | 2.24 |
| 392.4875 | 11.6 | 22.8 kHz | 10.8 dB | 0.331 | 0.818 | **1.79** |

Every one peaks at exactly 18000 baud on a symbol-rate scan, with a clear
falloff either side — the property that makes this a TETRA test rather than an
"is something there" test:

```
390.7375 MHz   14k=0.04  16k=0.16  17k=0.24  18k=0.35  19k=0.29  20k=0.27  22k=0.21
```

## Where simulation and reality disagreed

**Occupied bandwidth and channel-edge valley matched almost exactly.**
Simulation predicted 21.4 kHz and ~20 dB; reality gave 21.7-25.6 kHz and
10.8-19.8 dB. The channel-shape model needed no change.

**Symbol-rate selectivity did not match.** Clean simulated TETRA reaches
3.2-3.4, because AWGN barely concentrates the fourth moment at a wrong symbol
rate. A real signal keeps enough correlation at the decoy rates to lift their
floor to ~0.2, so real selectivity is 1.63-2.90.

The limit was 1.60. A genuine carrier at 26 dB SNR measured 1.63 — two per
cent of margin. That is a false-negative waiting to happen, and a false
negative here means missing an emergency services transmission.

Non-TETRA pi/4-DQPSK, which is what this test exists to reject, scores 0.3-1.1
(15300, 16000 and 21700 baud in the waveform suite). The limit is therefore
**1.35**: between what is really TETRA and what is really not, with margin on
both sides.

## What was ruled out

The dongle's sample rate was suspected of drifting against the assumed 18000
baud, which would smear the metric across a 16000-symbol capture. It does not:
the dongle reports exactly 288000 S/s, and sweeping the assumed symbol rate
over +/-200 ppm peaks at or within 10 ppm of 18000. The degradation is
channel and receiver impairment, not a clock error.

## Caveats

This is one site, one location, one receiver, one moment. It is enough to show
that the simulator over-estimated selectivity and by roughly how much; it is
not enough to characterise the limits across the country. Re-run the
measurement above at any new site and add the numbers here.
