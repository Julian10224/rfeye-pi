#!/usr/bin/env python3
"""Fit the simulator's channel to what real C2000 carriers measure.

    python scripts/simulator/calibrate_channel.py

docs/FIELD-CALIBRATION.md has six real downlink carriers, received by an
RTL-SDR Blog V4 on a Pi 3 B+ indoors at a window: symbol-rate selectivity
1.63-2.90, fourth-moment concentration 0.33-0.63, four-phase spread
0.77-0.89, occupied width 21.7-25.6 kHz, edge valley 10.8-19.8 dB. Clean
simulated TETRA scores selectivity 3.2-3.4.

This runs the real waveform test on simulated downlinks through each candidate
channel (``channel.py``) at the field SNRs, and ranks the candidates by how
close their measurements land to the field's. The winner is what the uplink
acceptance test then uses for the receiver side.
"""
import itertools
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "..", "..", "rfeye")]

import numpy as np                                    # noqa: E402

import tetra_phy                                      # noqa: E402
import tetra_sim as sim                               # noqa: E402
from channel import Path, Receiver                    # noqa: E402
from config import DEFAULTS                           # noqa: E402

SR = 2_016_000
N = 1 << 20
FIELD = {          # median and range of the six real carriers
    "dqpsk_selectivity": (2.09, 1.63, 2.90),
    "dqpsk_m": (0.43, 0.331, 0.632),
    "dqpsk_phase_spread": (0.81, 0.774, 0.891),
    "occupied_bw_hz": (21900.0, 21700.0, 25600.0),
    "boundary_reject_db": (17.0, 10.8, 19.8),
}
SNRS = (11.6, 14.9, 23.2, 25.6, 26.3, 28.9)


def measure(taps, phase_deg, pa, iq=0.3, seeds=1):
    rows = []
    lim = dict(DEFAULTS)
    off = 312_500.0
    for snr in SNRS:
        for seed in range(seeds):
            dur = N / SR
            clean = sim.tetra_carrier(dur, SR, role="DOWNLINK", seed=100 + seed)
            path = Path("static", speed_kmh=0.0, carrier_hz=391e6, taps=taps, pa_a3=pa,
                        seed=seed)
            x = path.apply(clean, 0.0, SR)
            x *= np.exp(2j * np.pi * off * np.arange(len(x)) / SR).astype(np.complex64)
            cap = sim.make_capture([(x, 1.0)], dur, SR, snr_db=snr, seed=300 + seed,
                                   quantise=False)
            rx = Receiver(tuner_error_hz=220.0, phase_rms_deg=phase_deg, phase_hi_hz=30000.0,
                          iq_gain_db=iq, iq_phase_deg=2.0 * iq, seed=seed)
            cap = rx.apply(cap, 0.0, SR)
            peak = float(np.max(np.abs(np.concatenate([cap.real, cap.imag])))) or 1.0
            g = 100.0 / peak
            cap = ((np.clip(np.round(cap.real * g) + 127.5, 0, 255) - 127.5)
                   + 1j * (np.clip(np.round(cap.imag * g) + 127.5, 0, 255) - 127.5)
                   ).astype(np.complex64)
            r = tetra_phy.analyse(tetra_phy.Channelizer(cap[:N], SR), off, role="DOWNLINK",
                                  limits=lim, decim=56, full=True)
            rows.append({k: float(getattr(r, k)) for k in FIELD} | {"ok": r.ok})
    return rows


def score(rows):
    """How far the medians are from the field's, in units of the field's spread."""
    total = 0.0
    for k, (med, lo, hi) in FIELD.items():
        m = float(np.median([r[k] for r in rows]))
        total += abs(m - med) / max(1e-9, hi - lo)
    return total


def main():
    echoes = {"none": [(0.0, 0.0)], "4us-10": [(0.0, 0.0), (4.0, -10.0)],
              "8us-10": [(0.0, 0.0), (8.0, -10.0)], "8us-6": [(0.0, 0.0), (8.0, -6.0)],
              "12us-12": [(0.0, 0.0), (12.0, -12.0)]}
    grid = list(itertools.product(echoes, (2.0, 4.0, 8.0, 12.0), (0.0, 0.08)))
    results = []
    for echo, ph, pa in grid:
        rows = measure(echoes[echo], ph, pa)
        results.append((score(rows), echo, ph, pa, 0.3, rows))
        sys.stdout.write(".")
        sys.stdout.flush()
    print()
    results.sort(key=lambda r: r[0])
    clean = measure([(0.0, 0.0)], 0.0, 0.0, 0.0)
    print("%-40s %s" % ("field (median, range)", "  ".join(
        "%s %.3g [%.3g-%.3g]" % (k.replace("dqpsk_", ""), *v) for k, v in FIELD.items())))
    for label, rows in [("clean simulation", clean)] + [
            ("%s pn %.0f deg pa %.2f (%.2f)" % (m, ph, k, sc), rows)
            for sc, m, ph, k, iq, rows in results[:6]]:
        meds = "  ".join("%s %.3g [%.3g-%.3g]" % (
            k.replace("dqpsk_", ""), np.median([r[k] for r in rows]),
            min(r[k] for r in rows), max(r[k] for r in rows)) for k in FIELD)
        print("%-40s %s  pass %d/%d" % (label[:40], meds, sum(r["ok"] for r in rows), len(rows)))
    best = results[0]
    print("BEST", {"echo": best[1], "phase_rms_deg": best[2], "pa_a3": best[3]})


if __name__ == "__main__":
    main()
