#!/usr/bin/env python3
"""Does RF Eye recognise a real emergency-services burst? Per burst, through a real channel.

    python scripts/simulator/acceptance.py            # full table, a few minutes
    python scripts/simulator/acceptance.py --quick

Every trial goes through exactly what the unit does with a stream: the
per-channel energy trigger (as shipped, every other FFT row), then the 65 ms
around what it reports into ``tetra_phy.analyse`` with the shipped limits. A
burst counts as recognised only if both happen on its own channel.

What is sent:
  * ``subslot`` -- a 7 ms random-access burst: what a terminal sends first,
    to register or ask for anything. The burst that decides whether a
    vehicle shows on its first message.
  * ``slot`` -- a whole 14 ms slot: a status message or the burst after it.

Through what (``channel.py``, receiver side fitted to the field carriers by
calibrate_channel.py):
  * ``awgn``        clean, as the unit tests use -- the optimistic reference
  * ``los``         a vehicle passing close by: line of sight, Rician K=6 dB, 50 km/h
  * ``TU50``        typical urban multipath, Rayleigh fading, 50 km/h
  * ``HT200``       hilly terrain, long echo, 120 km/h
And what must stay silent: empty band, the RTL-SDR's spur comb and the panel's
SPI line at 383.333 MHz, through the same receiver.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "..", "..", "rfeye")]

import numpy as np                                    # noqa: E402

import tetra_phy                                      # noqa: E402
import tetra_sim as sim                               # noqa: E402
from channel import Path, Receiver                    # noqa: E402
from config import DEFAULTS                           # noqa: E402
from stream_watch import StreamTrigger                # noqa: E402

SR = 2_016_000
OFF = 300_000.0                      # the burst's channel, from the tuner
DUR = 0.40
from sim_air import receiver_params                  # noqa: E402
RX = receiver_params()
CHANNELS = {
    "clean": None,
    "los": dict(model="static", speed_kmh=50.0, k_db=6.0),
    "TU50": dict(model="TU50", speed_kmh=50.0),
    "harsh": dict(model="harsh", speed_kmh=50.0),
    "HT200": dict(model="HT200", speed_kmh=120.0),
}
OFFSETS = [float(o) for o in np.arange(-850_000, 850_001, 25_000) if abs(o) >= 20_000]


def receive(sig, seed):
    """Noise is already in ``sig``; the dongle adds its own and quantises."""
    rx = Receiver(tuner_error_hz=220.0, phase_rms_deg=RX.get("phase_rms_deg", 0.0),
                  phase_hi_hz=RX.get("phase_hi_hz", 30000.0),
                  iq_gain_db=RX.get("iq_gain_db", 0.0),
                  iq_phase_deg=2.0 * RX.get("iq_gain_db", 0.0), dc=(0.6, -0.4), seed=seed)
    x = rx.apply(sig, 0.0, SR)
    # The unit's gain puts noise at ~6 counts; a strong burst may clip.
    return ((np.clip(np.round(x.real) + 127.5, 0, 255) - 127.5)
            + 1j * (np.clip(np.round(x.imag) + 127.5, 0, 255) - 127.5)).astype(np.complex64)


def air(n, seed):
    rng = np.random.default_rng(seed)
    return ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * (6.0 / np.sqrt(2))
            ).astype(np.complex64)


def burst(kind, snr, chan, seed):
    n = int(DUR * SR)
    x = air(n, seed)
    slots = 0.5 if kind == "subslot" else 1.0
    b = sim.control_burst(DUR, SR, seed=seed, n_slots=slots, start_frac=0.5)
    if chan is not None:
        b = Path(carrier_hz=383e6, seed=seed, pa_a3=RX.get("pa_a3", 0.0),
                 **chan).apply(b, seed * 1.37, SR)
    amp = 6.0 * np.sqrt(25_000.0 / SR) * 10 ** (snr / 20.0)
    t = np.arange(n) / SR
    x += (b * amp * np.exp(2j * np.pi * OFF * t)).astype(np.complex64)
    return receive(x, seed)


def detect(iq, want_off=OFF):
    """The stream path on one capture: trigger, then the waveform test."""
    lim = dict(DEFAULTS)
    trig = StreamTrigger(SR, OFFSETS, nfft=int(DEFAULTS["stream_fft_size"]),
                         trigger_db=float(DEFAULTS["stream_trigger_db"]),
                         min_s=float(DEFAULTS["stream_trigger_min_s"]),
                         max_s=float(DEFAULTS["stream_trigger_max_s"]),
                         smooth_rows=int(DEFAULTS["stream_smooth_rows"]),
                         gap_rows=int(DEFAULTS["stream_gap_rows"]),
                         row_stride=int(DEFAULTS["stream_row_stride"]))
    hits = []
    for a in range(0, len(iq), 65536):
        hits += trig.feed(iq[a:a + 65536], a)
    found, triggered = [], False
    L = 1 << int(DEFAULTS["stream_snippet_log2"])
    for c, s, e in hits:
        off = OFFSETS[c]
        triggered = triggered or abs(off - want_off) < 1.0
        a = max(0, min(len(iq) - L, (s + e) // 2 - L // 2))
        r = tetra_phy.analyse(tetra_phy.Channelizer(iq[a:a + L], SR), off, role="UPLINK",
                              limits=lim, decim=int(DEFAULTS["phy_decimation"]))
        if r.ok:
            found.append(off)
    return triggered, found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    trials = 8 if args.quick else 24
    snrs = (6, 8, 10, 12, 15, 20, 25, 30)
    print("receiver:", RX)
    print("recognised per burst, %d trials each; (triggered)" % trials)
    print("%-8s %-8s " % ("burst", "channel") + " ".join("%7d dB" % s for s in snrs))
    table = {}
    for kind in ("subslot", "slot"):
        for name, chan in CHANNELS.items():
            row = []
            for snr in snrs:
                ok = trg = 0
                for k in range(trials):
                    seed = 1000 * snr + 37 * k + (0 if kind == "subslot" else 500000)
                    tr, found = detect(burst(kind, snr, chan, seed))
                    trg += tr
                    ok += any(abs(f - OFF) < 1.0 for f in found)
                row.append((ok, trg))
            table[(kind, name)] = row
            print("%-8s %-8s " % (kind, name) + " ".join(
                "%3d%%(%3d)" % (100 * o // trials, 100 * t // trials) for o, t in row),
                flush=True)

    # What must stay silent.
    false = 0
    looks = 0
    for k in range(trials * 4):
        n = int(DUR * SR)
        x = air(n, 9000 + k)
        t = np.arange(n) / SR
        if k % 2:
            # Comb teeth and the panel's line: narrow, strong, never TETRA.
            for f, db in ((383_333_333.0 - 383_000_000.0, 30.0), (-400_000.0, 25.0),
                          (400_000.0, 22.0)):
                x += (6.0 * 10 ** (db / 20.0) * np.sqrt(25_000.0 / SR)
                      * np.exp(2j * np.pi * f * t)).astype(np.complex64)
        _tr, found = detect(receive(x, 9000 + k), want_off=1e9)
        false += len(found)
        looks += len(OFFSETS)
    print("false alarms: %d in %d channel-captures (empty band, spurs, panel line)"
          % (false, looks))


if __name__ == "__main__":
    main()
