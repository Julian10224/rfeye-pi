"""Offline replay of RF Eye recordings.

Detector profile v8 changes what a recording is for.

Up to v7 a recording stored derived statistics -- channel power, duty, burst
span, novelty scores -- and replay re-ran the old scoring maths over them.
That could never answer the question that actually mattered about a false
alarm, namely *what was that signal*, because the signal itself was never
kept.  Nothing in a v5/v6/v7 file can be re-analysed with the v8 waveform
tests, so those recordings are now presented read-only, exactly as they were
decided at the time, and clearly labelled as such.

v8 recordings instead store the physical-layer verdict for every channel that
was examined -- the pi/4-DQPSK score, the symbol-rate selectivity, the TDMA
timing, and which individual check failed -- so replaying one shows the real
reasoning.  When a raw IQ sidecar was saved alongside it, the capture can be
re-verified from scratch with ``scripts/analyse-capture.py``.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _ts(value, fallback=0.0):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return float(fallback)


def load_recording(path):
    p = Path(path)
    data = json.loads(p.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
        raise ValueError("invalid RF Eye recording")
    return data


def recording_label(data):
    raw = data.get("recorded_from") or ""
    try:
        return datetime.fromisoformat(str(raw)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(raw)[:19] or "Unknown time"


def schema_mode(data):
    """How much of this recording the current detector can actually explain."""
    schema = str(data.get("schema", ""))
    samples = data.get("samples") or []
    has_phy = any((s.get("detector") or {}).get("phy") for s in samples)
    if "v8" in schema and has_phy:
        return "PHY v8"
    if "v8" in schema:
        return "PHY v8 (no detail)"
    return "PRE-v8 ARCHIVE"


def is_replayable(data):
    """True when the stored verdicts come from the current detector."""
    return schema_mode(data).startswith("PHY v8")


class ReplayEngine:
    """Turn stored samples back into snapshots the UI can draw.

    There is deliberately no re-scoring here.  A v8 recording already contains
    the physical-layer verdict for every channel that was examined, and that
    verdict *is* the decision -- re-deriving it from summary numbers is what
    made older replays quietly disagree with what the device had actually
    done.  To re-decide a capture under different settings, put the raw IQ
    sidecar through ``scripts/analyse-capture.py`` instead.
    """

    def __init__(self, cfg, data):
        self.cfg = dict(cfg)
        self.data = data
        self.mode = schema_mode(data)
        self.replayable = is_replayable(data)

    def process(self, sample, index=0):
        d = sample.get("detector") or {}
        spec = sample.get("spectrum") or {}
        peaks = [dict(q) for q in (d.get("peaks") or [])]
        # Pre-v8 archives are shown exactly as they were recorded. Their
        # alerts came from a detector that no longer exists, so re-presenting
        # them as current findings would be misleading.
        confirmed = bool(d.get("mobile_confirmed", bool(peaks)))
        return {
            "status": "REPLAY",
            "replay_mode": self.mode,
            "replay_index": int(index),
            "replayable": bool(self.replayable),
            "legacy_approx": not self.replayable,
            "detector_state": str(d.get("detector_state",
                                        "ALERT" if confirmed else "REPLAY")),
            "activity_confidence": float(d.get("activity_confidence", 0.0) or 0.0),
            "mobile_confirmed": confirmed,
            "peaks": peaks,
            "mobile_peaks": [dict(q) for q in (d.get("mobile_peaks") or [])],
            "site_peaks": [dict(q) for q in (d.get("site_peaks") or [])],
            "phy": [dict(q) for q in (d.get("phy") or [])],
            "network_locked": bool(d.get("network_locked",
                                         bool(d.get("site_peaks")))),
            "mobile_level": float(d.get("mobile_level", 0.0) or 0.0),
            "site_level": float(d.get("site_level", 0.0) or 0.0),
            "noise": float(d.get("noise", -100.0) or -100.0),
            "freqs": list(spec.get("freq_hz") or []),
            "spectrum": list(spec.get("power_db") or []),
            "source_captured_at": sample.get("captured_at"),
        }


def replay_recording(cfg, path):
    data = load_recording(path)
    engine = ReplayEngine(cfg, data)
    results = [engine.process(s, i) for i, s in enumerate(data.get("samples") or [])]
    alerts = [r for r in results if r.get("peaks")]
    return data, engine.mode, results, alerts
