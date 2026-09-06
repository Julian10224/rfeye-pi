#!/usr/bin/env python3
"""Re-verify a saved RF Eye IQ capture, off the device.

When RF Eye alerts during a recording it also writes the raw dwell that caused
it as an ``.iq8`` sidecar.  This tool runs exactly the same physical-layer
tests over that file, so any alert -- or any alert you think should have
happened and did not -- can be settled from the evidence instead of argued
about.  It prints every measured quantity and marks which individual check
passed, so a borderline result shows you precisely what was borderline.

    python3 scripts/analyse-capture.py ~/.local/share/rfeye/captures/*.iq8
    python3 scripts/analyse-capture.py capture.iq8 --role DOWNLINK
    python3 scripts/analyse-capture.py capture.iq8 --channel 381237500

The file is plain unsigned 8-bit interleaved I/Q at the dwell sample rate, the
same format rtl_sdr writes, so captures from other tools work too -- give
``--centre`` and ``--rate`` when there is no ``.iq8.json`` beside the file.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rfeye"))

import numpy as np                                              # noqa: E402

import tetra_phy as phy                                         # noqa: E402
from config import DEFAULTS                                     # noqa: E402


def load_iq(path):
    raw = np.fromfile(str(path), dtype=np.uint8)
    if len(raw) < 4:
        raise SystemExit(f"{path}: too short to be an IQ capture")
    n = (len(raw) // 2) * 2
    return ((raw[0:n:2].astype(np.float32) - 127.5)
            + 1j * (raw[1:n:2].astype(np.float32) - 127.5)).astype(np.complex64)


def sidecar(path):
    meta = Path(str(path) + ".json")
    if meta.exists():
        try:
            return json.loads(meta.read_text())
        except Exception:
            pass
    return {}


def report(res, verbose):
    mark = "TETRA CONFIRMED" if res.ok else "rejected"
    print(f"\n  {res.freq_hz/1e6:.4f} MHz  {res.role:<8}  {mark}")
    if not res.ok:
        print(f"    failed checks: {', '.join(res.failed())}")
    rows = [
        ("channel SNR", f"{res.snr_db:.1f} dB"),
        ("occupied bandwidth", f"{res.occupied_bw_hz/1e3:.1f} kHz"),
        ("channel-edge valley", f"{res.boundary_reject_db:.1f} dB"),
        ("passband flatness", f"{res.flatness_db:.1f} dB"),
        ("centre error", f"{res.centre_error_hz:+.0f} Hz"),
        ("pi/4-DQPSK @ 18 kbaud", f"{res.dqpsk_m:.3f}"),
        ("four-phase spread", f"{res.dqpsk_phase_spread:.3f}"),
        ("symbol-rate selectivity", f"{res.dqpsk_selectivity:.2f}x"),
        ("TDMA frame line", f"{res.frame_line_ratio:.1f}x"),
        ("duty cycle", f"{res.duty*100:.1f} %"),
        ("median burst", f"{res.burst_ms_median:.2f} ms"
                         f"  (TETRA slot = {phy.SLOT_S*1000:.2f} ms)"),
        ("slot-grid fit", f"{res.slot_quantisation:.2f}"),
        ("bursts seen", str(res.burst_count)),
    ]
    for label, value in rows:
        print(f"    {label:<24} {value}")
    if verbose:
        for name, ok in sorted(res.checks.items()):
            print(f"    {'PASS' if ok else 'FAIL'}  {name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--role", choices=["UPLINK", "DOWNLINK"],
                    help="override the role stored in the sidecar")
    ap.add_argument("--centre", type=float, help="tuner centre in Hz")
    ap.add_argument("--rate", type=float, help="sample rate in Hz")
    ap.add_argument("--channel", type=float, action="append",
                    help="absolute channel frequency to test (repeatable); "
                         "defaults to every 25 kHz raster point in the window")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = dict(DEFAULTS)
    worst = 0
    for f in args.files:
        meta = sidecar(f)
        centre = args.centre if args.centre else float(meta.get("centre_hz", 0.0))
        rate = args.rate if args.rate else float(
            meta.get("sample_rate", cfg["phy_sample_rate"]))
        role = args.role or str(meta.get("role", "UPLINK"))
        iq = load_iq(f)
        print(f"\n=== {f}")
        print(f"    {len(iq)} samples, {len(iq)/rate:.3f} s at {rate/1e3:.0f} kS/s"
              + (f", centre {centre/1e6:.4f} MHz" if centre else ""))
        if not centre:
            print("    no centre frequency known; pass --centre to label channels")

        ch = phy.Channelizer(iq, rate)
        if args.channel:
            targets = [(c, c - centre) for c in args.channel]
        else:
            # Every raster point that fits in the window, so a capture can be
            # searched without knowing in advance which channel was busy.
            step = float(cfg["tetra_channel_spacing_hz"])
            limit = float(cfg["phy_max_offset_hz"])
            base = centre if centre else 0.0
            offs = np.arange(-limit, limit + 1, step)
            targets = [(base + o, o) for o in offs
                       if abs(o) >= float(cfg["tetra_channel_spacing_hz"])]

        hits = 0
        for freq, offset in targets:
            try:
                res = phy.analyse(ch, offset, role=role, limits=cfg,
                                  freq_hz=freq, full=True,
                                  decim=int(cfg["phy_decimation"]))
            except Exception as e:
                print(f"    {freq/1e6:.4f} MHz: {e}")
                continue
            if res.ok or args.verbose or res.snr_db >= float(cfg["phy_min_snr_db"]):
                report(res, args.verbose)
            if res.ok:
                hits += 1
        print(f"\n    {hits} channel(s) confirmed as TETRA")
        worst = max(worst, 0 if hits else 0)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
