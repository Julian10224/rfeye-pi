"""Offline replay helpers for RF Eye recordings.

Schema v5 recordings contain the post-artifact/pre-pair candidates required for
an almost exact replay of the current downstream detector. Older recordings do
not; they use a clearly labelled legacy approximation based on the stored
spectrum plus recorded downlink/site context.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

from sdr_backend import SDRBackend, clamp


def _ts(value, fallback=0.0):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return float(fallback)


def load_recording(path):
    p=Path(path)
    data=json.loads(p.read_text())
    if not isinstance(data,dict) or not isinstance(data.get("samples"),list):
        raise ValueError("invalid RF Eye recording")
    return data


def recording_label(data):
    raw=data.get("recorded_from") or ""
    try:
        dt=datetime.fromisoformat(str(raw))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(raw)[:19] or "Unknown time"


def schema_mode(data):
    schema=str(data.get("schema",""))
    samples=data.get("samples") or []
    if "v5" in schema and any((s.get("detector") or {}).get("debug_mobile_candidates") is not None for s in samples):
        return "EXACT v5"
    return "LEGACY APPROX"


class ReplayEngine:
    def __init__(self,cfg,data):
        self.cfg=dict(cfg)
        self.data=data
        self.mode=schema_mode(data)
        self.det=SDRBackend(self.cfg)
        # Replay never starts the SDR thread. Only pairing/hysteresis helpers are
        # used, so the live hardware remains owned by the running RF Eye backend.
        self.legacy_ref=None
        self.legacy_freqs=None
        self.legacy_last_ts=None

    def _v5_candidates(self,d):
        rows=[dict(q) for q in (d.get("debug_mobile_candidates") or [])]
        post=int(d.get("post_artifact_candidate_count",len(rows)) or 0)
        limit=max(0,int(self.cfg.get("max_mobile_candidates_per_sweep",12)))
        if limit and post>limit:
            mindep=max(.5,float(self.cfg.get("broadband_temporal_min_departure",1.25)))
            keep=max(1,int(self.cfg.get("broadband_dynamic_keep_max",6)))
            rows=[q for q in rows if float(q.get("temporal_departure",0))>=mindep or q.get("warmup_transient")]
            rows.sort(key=lambda q:(float(q.get("temporal_departure",0)),
                                    float(q.get("baseline_departure",0)),
                                    float(q.get("burst_quality",0)),
                                    float(q.get("rf_quality",0))),reverse=True)
            rows=rows[:keep]
        return rows

    def _legacy_candidates(self,sample):
        spec=sample.get("spectrum") or {}
        f=np.asarray(spec.get("freq_hz") or [],dtype=float)
        y=np.asarray(spec.get("power_db") or [],dtype=float)
        if len(f)<8 or len(f)!=len(y):
            return []
        if self.legacy_ref is None:
            self.legacy_freqs=f.copy()
            self.legacy_ref=y.copy()
            return []
        if len(f)!=len(self.legacy_freqs) or np.max(np.abs(f-self.legacy_freqs))>1.0:
            y=np.interp(self.legacy_freqs,f,y)
            f=self.legacy_freqs
        delta=y-self.legacy_ref
        common=float(np.median(delta))
        residual=np.abs(delta-common)
        scale=max(.5,float(self.cfg.get("temporal_rf_snr_scale_db",4.0)))
        score=residual/scale
        mindep=max(.5,float(self.cfg.get("broadband_temporal_min_departure",1.25)))
        # Keep local maxima only, then map the downsampled display bin to the
        # 25 kHz TETRA raster. This avoids turning one spectral hump into many
        # duplicate replay candidates.
        idx=[]
        for i in range(1,len(score)-1):
            if score[i]>=mindep and score[i]>=score[i-1] and score[i]>=score[i+1]:
                idx.append(i)
        idx.sort(key=lambda i:float(score[i]),reverse=True)
        out=[];seen=set()
        a=float(self.cfg.get("mobile_band_start_hz",380e6))
        raster0=a+12500.0
        for i in idx[:8]:
            rf=raster0+round((float(f[i])-raster0)/25000.0)*25000.0
            if rf in seen: continue
            seen.add(rf)
            dep=float(score[i])
            novelty=clamp((dep-mindep)/max(.5,1.25))
            out.append({
                "freq_hz":rf,
                "temporal_departure":dep,
                "temporal_rf_delta_db":float(residual[i]),
                "legacy_spectrum_freq_hz":float(f[i]),
                "legacy_replay":True,
                "burst_quality":novelty,
                "duty_quality":novelty,
                "rf_quality":novelty,
                "signal_strength":clamp(.25+.65*novelty),
                "level":clamp(.25+.65*novelty),
            })
        alpha=clamp(float(self.cfg.get("temporal_baseline_alpha",.08)),.01,.35)
        self.legacy_ref=(1.-alpha)*self.legacy_ref+alpha*y
        return out

    def process(self,sample,index=0):
        d=sample.get("detector") or {}
        t=_ts(sample.get("captured_at"),float(index))
        site=[dict(q) for q in (d.get("site_peaks") or [])]
        self.det._remember_sites(site,t)
        candidates=self._v5_candidates(d) if self.mode=="EXACT v5" else self._legacy_candidates(sample)

        require=bool(self.cfg.get("require_duplex_pair",True))
        require_now=bool(self.cfg.get("require_current_duplex_pair",False))
        minpair=float(self.cfg.get("duplex_pair_min_quality",.28))
        minconf=float(self.cfg.get("candidate_min_confidence",.48))
        accepted=[]
        for x in candidates:
            pq,pnow=self.det._pair_q(round(float(x["freq_hz"])+10000000),site,t)
            if require and (pq<minpair or (require_now and not pnow)):
                continue
            q=dict(x)
            if self.mode=="EXACT v5":
                conf=self.det._confidence(q,pq)
            else:
                # v3/v4 did not store block-level duty/burst metrics after the
                # old broadband kill-switch. This conservative legacy score is
                # explicitly approximate: strong local temporal novelty must be
                # accompanied by recorded duplex context.
                novelty=clamp((float(q.get("temporal_departure",0))-1.25)/1.25)
                conf=clamp(.55*novelty+.45*pq)
            q.update(paired=pq>=minpair,paired_now=pnow,pair_quality=pq,confidence=conf)
            if conf>=minconf:
                accepted.append(q)
        accepted.sort(key=lambda q:(float(q.get("confidence",0)),
                                    float(q.get("temporal_departure",0)),
                                    float(q.get("burst_quality",0))),reverse=True)
        shown=accepted[:int(self.cfg.get("max_signals",3))]
        confidence,confirmed=self.det._hysteresis(shown,t)
        peaks=shown if confirmed else []
        level=max([float(q.get("signal_strength",q.get("level",0))) for q in peaks],default=0.0)

        spec=sample.get("spectrum") or {}
        freqs=list(spec.get("freq_hz") or [])
        power=list(spec.get("power_db") or [])
        return {
            "status":"REPLAY",
            "replay_mode":self.mode,
            "replay_index":int(index),
            "activity_confidence":float(confidence),
            "mobile_confirmed":bool(confirmed),
            "peaks":peaks,
            "mobile_peaks":accepted,
            "site_peaks":site,
            "mobile_level":float(level),
            "site_level":max([float(q.get("signal_strength",q.get("level",0))) for q in site],default=0.0),
            "noise":float(d.get("noise",-100.0) or -100.0),
            "freqs":freqs,
            "spectrum":power,
            "source_captured_at":sample.get("captured_at"),
            "legacy_approx":self.mode!="EXACT v5",
        }


def replay_recording(cfg,path):
    data=load_recording(path)
    engine=ReplayEngine(cfg,data)
    results=[]
    for i,s in enumerate(data.get("samples") or []):
        results.append(engine.process(s,i))
    alerts=[r for r in results if r.get("peaks")]
    return data,engine.mode,results,alerts
