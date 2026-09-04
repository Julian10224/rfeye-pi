#!/usr/bin/env python3
"""Hardware-free regression checks for the RF Eye C2000/TETRA detector."""
from __future__ import annotations

import copy
import os
import tempfile

import numpy as np

os.environ.setdefault("RFEYE_ARTIFACT_BASELINE", tempfile.mktemp(prefix="rfeye-selftest-", suffix=".json"))

from config import DEFAULTS
from sdr_backend import SDRBackend


def backend():
    cfg=copy.deepcopy(DEFAULTS)
    cfg["detector_profile_version"]=6
    cfg["artifact_baseline_persist"]=False
    return SDRBackend(cfg)


def candidate(freq=383_437_500.0, dep=1.5, conf=.80, pair=.90,
              paired_now=True, pair_age=0.0, pair_hits=2):
    return {
        "freq_hz":float(freq),
        "temporal_departure":float(dep),
        "confidence":float(conf),
        "pair_quality":float(pair),
        "paired_now":bool(paired_now),
        "pair_age_s":float(pair_age),
        "pair_hits":int(pair_hits),
        "burst_quality":.85,
        "duty_quality":.80,
        "rf_quality":.85,
        "signal_strength":.80,
        "level":.80,
    }


def stable_peak(freq):
    return {
        "freq_hz":float(freq),
        "power_db":20.0,
        "duty":.25,
        "burst_span_db":12.0,
        "rf_snr_db":15.0,
    }


def pipeline_backend(mobile_sequences):
    """Return a backend whose full _scan_cycle runs on synthetic candidates."""
    b=backend()
    b.cfg["site_scan_interval"]=1
    seq=iter(mobile_sequences)
    current=[None]
    site=[{"freq_hz":393_437_500.0,"site_quality":1.0,
           "signal_strength":1.0,"level":1.0}]
    def scan(_a,_b,label):
        if label=="MOBILE":
            try: current[0]=next(seq)
            except StopIteration: current[0]=[]
            return [dict(q) for q in current[0]],np.array([380e6,385e6]),np.array([-100.,-90.]),-100.
        return [dict(q) for q in site],np.array([390e6,395e6]),np.array([-100.,-90.]),-100.
    b._scan_band=scan
    b._reject_static_artifacts=lambda peaks: peaks
    b._apply_broadband_guard=lambda peaks,_limit: peaks
    return b


def main():
    b=backend()

    # ETSI/C2000 +12.5 kHz offset raster: adjacent carriers must never merge.
    freqs=[380_012_500,380_037_500,380_062_500,380_087_500,384_987_500]
    keys=[b._carrier_key(f) for f in freqs]
    assert keys==freqs
    assert len(set(keys))==len(keys)
    assert all(k % 25_000 == 12_500 for k in keys)

    # The artifact map must also keep adjacent TETRA carriers separate.
    b=backend()
    for _ in range(5):
        b._reject_static_artifacts([stable_peak(380_037_500),stable_peak(380_062_500)])
    assert set(b._artifact_baseline)=={380_037_500,380_062_500}

    # Memory-only duplex context needs multiple site refresh hits.
    b=backend()
    site=[{"freq_hz":393_437_500.0,"site_quality":1.0,"level":1.0}]
    b._remember_sites(site,100.0)
    q,now,age,hits=b._pair_info(393_437_500,site,100.0)
    assert now and q>.95 and age==0 and hits>=1
    q,now,age,hits=b._pair_info(393_437_500,[],101.0)
    assert not now and q==0.0  # one remembered observation is insufficient
    b._remember_sites(site,102.0)
    q,now,age,hits=b._pair_info(393_437_500,[],103.0)
    assert not now and q>.70 and age<=1.01 and hits>=2
    q,now,age,hits=b._pair_info(393_437_500,[],108.0)
    assert q==0.0

    # Moderate evidence needs two hits on the same carrier.
    b=backend()
    _,ok=b._hysteresis([candidate(dep=1.5)],100.0)
    assert not ok
    _,ok=b._hysteresis([candidate(dep=1.5)],101.0)
    assert ok

    # Adjacent carriers cannot combine their hit counts.
    b=backend()
    _,ok=b._hysteresis([candidate(freq=380_037_500,dep=1.5)],100.0)
    assert not ok
    _,ok=b._hysteresis([candidate(freq=380_062_500,dep=1.5)],101.0)
    assert not ok

    # A strong novelty event may confirm immediately only with fresh pair data.
    b=backend()
    _,ok=b._hysteresis([candidate(dep=2.5)],100.0)
    assert ok

    b=backend()
    _,ok=b._hysteresis([candidate(dep=2.5,pair=.55,paired_now=True)],100.0)
    assert not ok  # current but weak pair may not single-shot confirm

    b=backend()
    _,ok=b._hysteresis([candidate(dep=2.5,paired_now=False,pair_age=3.0,pair_hits=2)],100.0)
    assert not ok

    b=backend()
    _,ok=b._hysteresis([candidate(dep=2.5,paired_now=False,pair_age=1.0,pair_hits=2)],100.0)
    assert ok

    # Novelty contributes materially to confidence.
    b=backend()
    base=candidate(dep=1.25); strong=candidate(dep=2.0)
    c0=b._confidence(base,.9); c1=b._confidence(strong,.9)
    assert c1>c0+.15

    # Full scan-cycle regression: a perfectly paired but stationary candidate
    # must be rejected before it can preload hysteresis.
    b=pipeline_backend([[candidate(dep=.4)]])
    assert b._scan_cycle()
    s=b.snapshot()
    assert not s["mobile_peaks"] and not s["peaks"]
    assert s["novelty_rejected_count"]==1

    # Moderate novel evidence requires two valid cycles on the same carrier.
    b=pipeline_backend([[candidate(dep=1.5)],[candidate(dep=1.5)]])
    assert b._scan_cycle()
    assert b.snapshot()["mobile_peaks"] and not b.snapshot()["peaks"]
    assert b._scan_cycle()
    assert b.snapshot()["peaks"]

    # Very strong novelty + strong current pair can alert in one cycle.
    b=pipeline_backend([[candidate(dep=2.5)]])
    assert b._scan_cycle()
    assert b.snapshot()["peaks"]

    print("RF Eye detector profile v6 self-test: OK")


if __name__=="__main__":
    main()
