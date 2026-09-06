"""TETRA physical-layer signature analysis for RF Eye.

Everything here is pure NumPy and completely hardware free, so the whole C2000
decision can be replayed, simulated and unit tested off the Raspberry Pi.

Why this module exists
----------------------
Detector profiles up to v7 decided "C2000" from wideband energy statistics
only: channel power, duty cycle, burst span, and how much those had moved
since the previous sweep.  None of that is specific to TETRA.  Any bursty RF in
380-385 MHz -- an RTL-SDR spur, an AGC step, a passing wideband emitter --
produces the same numbers, which is why the alert rate could never be tuned
into something trustworthy: no threshold on those features separates an
emergency-services handset from clutter, because the features do not contain
that information in the first place.

Profile v8 therefore verifies the actual ETSI EN 300 392-2 waveform before
anything may alert:

  * pi/4-DQPSK at exactly 18000 symbols/s              (the decisive test)
  * a 25 kHz RRC(0.35) channel shape with real adjacent-channel isolation
  * TDMA burst timing on the 255-symbol / 14.1667 ms slot grid

Each test is scored against deliberately mismatched "decoy" hypotheses -- a
wrong symbol rate, a wrong frame rate -- so the headline numbers are ratios
against the signal's own structure rather than absolute magic levels.  Noise
scores about 1.0 on every ratio however strong it is; only a real TETRA
carrier separates.  That is what makes the thresholds stable across gain
settings, antennas, locations and hardware units.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

import numpy as np

# --- ETSI EN 300 392-2 V+D air interface constants ------------------------
SYMBOL_RATE_HZ = 18000.0            # 36 kbit/s gross, 2 bits per symbol
SYMBOLS_PER_SLOT = 255
SLOTS_PER_FRAME = 4
FRAMES_PER_MULTIFRAME = 18
RRC_ROLLOFF = 0.35

SLOT_S = SYMBOLS_PER_SLOT / SYMBOL_RATE_HZ                     # 14.1667 ms
FRAME_S = SLOT_S * SLOTS_PER_FRAME                             # 56.6667 ms
MULTIFRAME_S = FRAME_S * FRAMES_PER_MULTIFRAME                 # 1.02 s
FRAME_RATE_HZ = 1.0 / FRAME_S                                  # 17.6471 Hz
SLOT_RATE_HZ = 1.0 / SLOT_S                                    # 70.5882 Hz

CHANNEL_SPACING_HZ = 25000.0
OCCUPIED_BW_HZ = SYMBOL_RATE_HZ * (1.0 + RRC_ROLLOFF)          # 24.3 kHz

# Control hypotheses.  Not TETRA rates and not simple ratios of 18000, so a
# genuine TETRA carrier scores clearly worse on all of them.
DECOY_BAUDS = (13100.0, 15300.0, 21700.0, 24900.0)
DECOY_FRAME_RATIOS = (0.6100, 0.7300, 0.8700, 1.1900, 1.3700, 1.6100)


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(v)))


def _db(x, floor=1e-30):
    return 10.0 * np.log10(np.maximum(np.asarray(x, dtype=np.float64), floor))


def rrc_taps(sps, span_symbols=8, rolloff=RRC_ROLLOFF):
    """Root-raised-cosine matched filter, normalised to unit energy."""
    n = int(round(span_symbols * float(sps)))
    if n % 2 == 0:
        n += 1
    t = (np.arange(n, dtype=np.float64) - (n - 1) / 2.0) / float(sps)
    a = float(rolloff)
    out = np.empty(n, dtype=np.float64)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-9:
            out[i] = 1.0 - a + 4.0 * a / math.pi
        elif a > 0 and abs(abs(ti) - 1.0 / (4.0 * a)) < 1e-9:
            out[i] = (a / math.sqrt(2.0)) * (
                (1.0 + 2.0 / math.pi) * math.sin(math.pi / (4.0 * a))
                + (1.0 - 2.0 / math.pi) * math.cos(math.pi / (4.0 * a)))
        else:
            num = (math.sin(math.pi * ti * (1.0 - a))
                   + 4.0 * a * ti * math.cos(math.pi * ti * (1.0 + a)))
            den = math.pi * ti * (1.0 - (4.0 * a * ti) ** 2)
            out[i] = num / den
    return (out / math.sqrt(float(np.sum(out ** 2)))).astype(np.float32)


class Channelizer:
    """Frequency-domain channel filter over one dwell capture.

    One forward FFT serves everything: the smoothed spectrum, and every 25 kHz
    channel we want to look at.  Extracting a channel is then a bin slice plus
    a small inverse FFT, so checking several uplink partners inside the same
    dwell costs almost nothing extra -- which is what makes this affordable on
    a Pi 3 B+.

    The brick-wall response is also the point, not a shortcut: it gives real
    adjacent-channel rejection, so the "three neighbouring raster channels all
    light up at once" pattern that produced the field false positives cannot
    survive into the modulation tests.
    """

    def __init__(self, iq, sample_rate):
        iq = np.asarray(iq)
        n = 1 << int(math.floor(math.log2(max(2, len(iq)))))
        self.n = int(n)
        self.sample_rate = float(sample_rate)
        self.bin_hz = self.sample_rate / self.n
        self.iq = iq[:n].astype(np.complex64)
        self._spectrum = None
        self._psd = None

    @property
    def spectrum(self):
        """Forward FFT of the whole dwell, computed on first use.

        Channel extraction needs it; the occupancy spectrum does not.  Most
        dwells reject every channel on cheap spectral checks alone and never
        extract anything, so on a Pi 3 deferring this saves about a third of a
        second on every quiet cycle -- which is most of them.
        """
        if self._spectrum is None:
            self._spectrum = np.fft.fft(self.iq)
        return self._spectrum

    def psd(self, nfft=1024, percentile=92.0, dc_notch_hz=2500.0):
        """Occupancy spectrum as (offset_hz, power_db).

        This is a short-time spectrum reduced over time by a high percentile
        rather than a mean.  That matters for the uplink: a handset transmits
        one 14.1667 ms slot out of every 56.6667 ms, so a mean spectrum
        understates its real level by about 6 dB and would push a genuine
        police transmission below the verification floor.  The percentile
        recovers the level while the transmitter is actually keyed up.

        The same percentile is used for the signal and the noise reference, so
        its positive bias cancels in every ratio derived from this spectrum.
        """
        if self._psd is not None:
            return self._psd
        nfft = int(nfft)
        rows = self.n // nfft
        if rows < 8:
            raise ValueError('capture too short for spectrum analysis')
        win = np.hanning(nfft).astype(np.float32)
        block = self.iq[:rows * nfft].reshape(rows, nfft) * win
        power = np.abs(np.fft.fft(block, axis=1)).astype(np.float32) ** 2
        red = np.percentile(power, float(percentile), axis=0)
        f = np.fft.fftfreq(nfft, 1.0 / self.sample_rate)
        notch = np.abs(f) <= float(dc_notch_hz)
        if np.any(notch) and not np.all(notch):
            red[notch] = float(np.median(red[~notch]))
        self._psd = (np.fft.fftshift(f).astype(np.float64),
                     _db(np.fft.fftshift(red)))
        return self._psd

    def extract(self, offset_hz, decim, edge_taper=0.12):
        """Return (baseband, rate) for the channel centred on ``offset_hz``.

        ``offset_hz`` is relative to the tuner centre.  The result is complex
        baseband centred on that frequency at ``sample_rate / decim``.
        """
        decim = int(decim)
        m = self.n // decim
        if m < 64:
            raise ValueError('decimation too aggressive for this capture')
        k0 = int(round(float(offset_hz) / self.bin_hz))
        shifted = np.arange(-(m // 2), m - (m // 2))
        idx = (k0 + np.fft.ifftshift(shifted)) % self.n
        sub = self.spectrum[idx].astype(np.complex64)
        if edge_taper > 0:
            w = np.ones(m, dtype=np.float32)
            e = max(1, int(m * float(edge_taper) / 2.0))
            ramp = (0.5 * (1.0 - np.cos(np.pi * (np.arange(e) + 0.5) / e))).astype(np.float32)
            w[:e] = ramp
            w[m - e:] = ramp[::-1]
            sub *= np.fft.ifftshift(w)
        # np.fft.ifft normalises by m; rescale so absolute levels survive.
        bb = np.fft.ifft(sub) * (float(m) / float(self.n))
        return bb.astype(np.complex64), self.sample_rate / decim


def channel_shape(freqs, psd_db, centre_hz=0.0):
    """How much a channel looks like a 25 kHz RRC(0.35) TETRA carrier.

    The RTL-SDR has roughly 45 dB of usable dynamic range, so this does not
    attempt the real ETSI spectrum mask.  It measures the three properties
    that actually separated TETRA from the observed false positives: the
    carrier is about 25 kHz wide, it stops before the neighbouring 25 kHz
    channels, and its passband is flat rather than one spur line.
    """
    d = np.asarray(freqs, dtype=np.float64) - float(centre_hz)
    p_db = np.asarray(psd_db, dtype=np.float64)
    p_lin = 10.0 ** (p_db / 10.0)
    a = np.abs(d)

    def sel(lo, hi):
        m = (a >= lo) & (a < hi)
        return m if int(np.count_nonzero(m)) >= 3 else None

    core = sel(0.0, 8000.0)
    noise = sel(30000.0, 140000.0)
    if core is None or noise is None:
        raise ValueError('capture bandwidth too small for shape analysis')

    core_db = float(np.median(p_db[core]))
    # A low percentile, not the median: in a busy band part of the reference
    # window is occupied by neighbouring carriers and would inflate a median.
    noise_db = float(np.percentile(p_db[noise], 20))

    # Is there a spectral valley where the 25 kHz channel ends?  This is the
    # test that separates one TETRA carrier from one wide hump spilling across
    # several raster points -- the exact shape that produced the field false
    # alarms.  It is measured as the deepest point in the guard region on each
    # side, and scored by the *shallower* of the two, so a single strong
    # neighbour cannot mask a carrier that is genuinely wide.
    valleys = []
    for lo, hi in ((-15500.0, -10500.0), (10500.0, 15500.0)):
        m = (d >= lo) & (d <= hi)
        if int(np.count_nonzero(m)) >= 3:
            valleys.append(core_db - float(np.min(p_db[m])))
    boundary_db_reject = min(valleys) if valleys else 0.0

    # Occupied bandwidth: the *contiguous* -10 dB width around the channel
    # centre, walking outward until the spectrum first drops away.  Measuring
    # a contiguous run rather than integrating power is what keeps this honest
    # on a busy site: energy from an active neighbour sits on the far side of
    # the guard valley, so it is never counted, while a genuinely wide emitter
    # has no valley to stop the walk and runs straight to the cap.
    # TETRA lands near 21 kHz, a CW spur under 1 kHz, 12.5 kHz FM near 6 kHz.
    cap_hz = 20000.0
    edge_db = core_db - 10.0
    centre_idx = int(np.argmin(a))
    lo_hz = hi_hz = 0.0
    i = centre_idx
    while i >= 0 and p_db[i] > edge_db and a[i] <= cap_hz:
        lo_hz = a[i]
        i -= 1
    i = centre_idx
    while i < len(d) and p_db[i] > edge_db and a[i] <= cap_hz:
        hi_hz = a[i]
        i += 1
    occupied_bw = lo_hz + hi_hz

    # Flatness: modulation is noise-like and flat across the passband, a
    # carrier or spur is not.  Peak-to-median so one hot bin is punished.
    flatness_db = float(np.max(p_db[core]) - np.median(p_db[core]))

    # Power centroid, used to strip the residual tuner error before the
    # symbol-rate test runs.
    fit = a <= 13000.0
    w = np.clip(p_lin[fit] - 10.0 ** (noise_db / 10.0), 0.0, None)
    centre_err = float(np.sum(d[fit] * w) / np.sum(w)) if float(np.sum(w)) > 0 else 0.0

    return {
        'snr_db': core_db - noise_db,
        'noise_db': noise_db,
        'core_db': core_db,
        # Measured at the 25 kHz channel boundary rather than at the
        # neighbour's centre.  A single wide hump has no dip there at all,
        # while a real TETRA carrier still rolls off even when both of its
        # neighbours are transmitting -- which is the distinction that matters
        # on a busy site.
        'boundary_reject_db': boundary_db_reject,
        'occupied_bw_hz': occupied_bw,
        'flatness_db': flatness_db,
        'centre_error_hz': centre_err,
    }


def power_envelope(bb, rate, hop_s=1.0 / 1125.0):
    """Box-averaged linear power envelope of a channelized baseband."""
    bb = np.asarray(bb)
    box = max(1, int(round(float(rate) * float(hop_s))))
    n = (len(bb) // box) * box
    if n < box * 32:
        raise ValueError('capture too short for envelope analysis')
    p = (np.abs(bb[:n]).astype(np.float32) ** 2).reshape(-1, box).mean(axis=1)
    return p.astype(np.float64), float(rate) / box


def _line_power(x, t, f0, harmonics):
    """Summed spectral line power of ``x`` at ``f0`` and its harmonics."""
    total = 0.0
    for k in range(1, harmonics + 1):
        total += abs(complex(np.dot(x, np.exp(-2j * math.pi * k * f0 * t)))) ** 2
    return total


def tdma_timing(env, rate, harmonics=5, snr_db=0.0, min_snr_db=8.0,
                min_span_db=4.0):
    """Score TDMA structure in an envelope against decoy frame rates.

    A TETRA mobile transmits one 14.1667 ms slot out of every 56.6667 ms
    frame, so the envelope carries a strong line at 17.647 Hz plus harmonics.
    Scoring it against mismatched frame rates turns this into a ratio against
    the signal's own fluctuation, so it needs no recalibration for gain,
    distance or noise floor.
    """
    env = np.asarray(env, dtype=np.float64)
    n = len(env)
    empty = {'frame_line_ratio': 0.0, 'duty': 0.0, 'burst_ms_median': 0.0,
             'slot_quantisation': 0.0, 'burst_count': 0,
             'on_mask': np.zeros(max(n, 1), dtype=bool)}
    if n < 64:
        return empty
    t = np.arange(n, dtype=np.float64) / float(rate)
    x = (env - float(np.mean(env))) * np.hanning(n)
    if float(np.dot(x, x)) <= 0:
        return empty

    matched = _line_power(x, t, FRAME_RATE_HZ, harmonics)
    decoys = [_line_power(x, t, FRAME_RATE_HZ * r, harmonics)
              for r in DECOY_FRAME_RATIOS]
    ref = float(np.median(decoys))
    ratio = (matched / ref) if ref > 0 else 0.0

    # Duty cycle needs an absolute reference, not the signal's own spread.
    # A continuously keyed carrier and an empty channel both have a nearly
    # flat envelope; only the spectrum SNR tells them apart.  Getting this
    # wrong is what makes a base station look like a silent channel.
    e_db = _db(env)
    lo = float(np.percentile(e_db, 10))
    hi = float(np.percentile(e_db, 95))
    span = hi - lo
    if span < float(min_span_db):
        occupied = float(snr_db) >= float(min_snr_db)
        on = np.ones(n, dtype=bool) if occupied else np.zeros(n, dtype=bool)
        duty = 1.0 if occupied else 0.0
    else:
        thr = lo + max(3.0, 0.40 * span)
        on = e_db > thr
        duty = float(np.mean(on))

    runs = []
    start = None
    for i, v in enumerate(on):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((i - start) / float(rate))
            start = None
    if start is not None:
        runs.append((n - start) / float(rate))
    # Ignore sub-slot flicker: no real TETRA burst is shorter than a subslot.
    runs = [r for r in runs if r >= 0.6 * SLOT_S]

    if runs:
        slots = np.asarray(runs, dtype=np.float64) / SLOT_S
        err = np.abs(slots - np.round(slots))
        # 1.0 when every burst length lands on the 14.1667 ms slot grid; a
        # uniform (unrelated) error averages 0.25 and scores 0.
        quant = clamp(1.0 - 4.0 * float(np.mean(err)))
        burst_ms = float(np.median(runs)) * 1000.0
    else:
        quant = 0.0
        burst_ms = 0.0

    return {'frame_line_ratio': float(ratio), 'duty': duty,
            'burst_ms_median': burst_ms, 'slot_quantisation': float(quant),
            'burst_count': len(runs), 'on_mask': on}


def dqpsk_moments(bb, rate, baud, mask=None, timing_phases=8,
                  matched_filter=True):
    """Differential phase moments ``(|E[u]|, |E[u**2]|, |E[u**4]|)``.

    ``u`` is the unit differential phasor between consecutive symbols.  The
    fourth moment alone is not a modulation test: a bare carrier, or any
    signal whose phase barely moves between symbols, also concentrates on a
    single point and scores ~1.0 on it.  The first and second moments are what
    prove the constellation really uses four distinct phases -- for
    pi/4-DQPSK the four differential phases are symmetric, so E[u] and E[u**2]
    both average to zero while E[u**4] does not.  Requiring all three together
    is what makes this a TETRA test rather than an "is something there" test.
    """
    bb = np.asarray(bb, dtype=np.complex64)
    sps = float(rate) / float(baud)
    if sps < 1.5 or len(bb) < 512:
        return (1.0, 1.0, 0.0)
    if matched_filter:
        taps = rrc_taps(sps).astype(np.complex64)
        bb = np.convolve(bb, taps, mode='same')

    nsym = int((len(bb) - 4) / sps)
    if nsym < 96:
        return (1.0, 1.0, 0.0)

    valid = None
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        rep = int(math.ceil(len(bb) / max(1, len(m))))
        valid = np.repeat(m, rep)[:len(bb)]
        if len(valid) < len(bb):
            valid = np.concatenate([valid, np.zeros(len(bb) - len(valid), bool)])
        if int(np.count_nonzero(valid)) < 256:
            return (1.0, 1.0, 0.0)

    best = (1.0, 1.0, 0.0)
    base = np.arange(nsym, dtype=np.float64) * sps
    for p in range(int(timing_phases)):
        pos = base + (p / float(timing_phases)) * sps
        i0 = np.clip(np.floor(pos).astype(np.int64), 0, len(bb) - 2)
        frac = (pos - i0).astype(np.float32)
        y = bb[i0] * (1.0 - frac) + bb[i0 + 1] * frac
        if valid is not None:
            keep = valid[i0] & valid[i0 + 1]
            y = y[keep]
            if len(y) < 96:
                continue
        d = y[1:] * np.conj(y[:-1])
        mag = np.abs(d)
        good = mag > (1e-3 * float(np.median(mag)) + 1e-20)
        if int(np.count_nonzero(good)) < 96:
            continue
        u = d[good] / mag[good]
        m4 = float(abs(np.mean(u ** 4)))
        if m4 > best[2]:
            best = (float(abs(np.mean(u))), float(abs(np.mean(u ** 2))), m4)
    return best


def dqpsk_metric(bb, rate, baud, mask=None, timing_phases=8, matched_filter=True):
    """Fourth-moment concentration only, kept for symbol-rate scans."""
    return dqpsk_moments(bb, rate, baud, mask=mask, timing_phases=timing_phases,
                         matched_filter=matched_filter)[2]


def modulation_scores(bb, rate, mask=None, min_m=0.0, timing_phases=8):
    """The TETRA symbol-rate test, its four-phase proof, and decoy controls.

    Three numbers come out of this and all three must hold:

    ``dqpsk_m``            fourth-moment concentration at exactly 18 kbaud.
    ``dqpsk_phase_spread`` 1 - max(first, second moment).  Near 1 for a real
                           four-phase constellation, near 0 for a bare carrier
                           or any slowly rotating phase, which is what stops a
                           spur or narrowband FSK passing the fourth moment on
                           its own.
    ``dqpsk_selectivity``  how much better 18 kbaud scores than the decoy
                           rates.  Structured non-TETRA signals can score well
                           at 18 kbaud by accident; very little scores well at
                           18 kbaud *and* poorly at every other rate.

    The decoy rates cost four times as much as the real one, and they only
    matter when 18 kbaud already passed, so ``min_m`` skips them otherwise.
    On a Pi that is the difference between a quiet channel costing 20 ms and
    costing a quarter of a second.
    """
    m1, m2, m4 = dqpsk_moments(bb, rate, SYMBOL_RATE_HZ, mask=mask,
                               timing_phases=timing_phases)
    spread = clamp(1.0 - max(m1, m2))
    if m4 < float(min_m):
        return {'dqpsk_m': float(m4), 'dqpsk_phase_spread': float(spread),
                'dqpsk_decoy_max': 0.0, 'dqpsk_selectivity': 0.0,
                'decoys_run': False}
    decoys = [dqpsk_metric(bb, rate, b, mask=mask, timing_phases=timing_phases)
              for b in DECOY_BAUDS]
    worst = max(decoys) if decoys else 0.0
    if worst > 1e-6:
        sel = m4 / worst
    else:
        sel = 10.0 if m4 > 0.02 else 0.0
    return {'dqpsk_m': float(m4), 'dqpsk_phase_spread': float(spread),
            'dqpsk_decoy_max': float(worst),
            'dqpsk_selectivity': float(min(sel, 99.0)),
            'decoys_run': True}


@dataclass
class PhyResult:
    """Verdict for one candidate 25 kHz channel in one dwell."""
    freq_hz: float = 0.0
    role: str = 'UPLINK'
    ok: bool = False
    reason: str = 'NOT_ANALYSED'
    quality: float = 0.0
    snr_db: float = 0.0
    noise_db: float = 0.0
    occupied_bw_hz: float = 0.0
    boundary_reject_db: float = 0.0
    flatness_db: float = 0.0
    centre_error_hz: float = 0.0
    dqpsk_m: float = 0.0
    dqpsk_phase_spread: float = 0.0
    dqpsk_decoy_max: float = 0.0
    dqpsk_selectivity: float = 0.0
    frame_line_ratio: float = 0.0
    duty: float = 0.0
    burst_ms_median: float = 0.0
    slot_quantisation: float = 0.0
    burst_count: int = 0
    # True for a continuously transmitting main carrier, false for a
    # discontinuous traffic carrier. Diagnostic only; both are real.
    continuous: bool = False
    checks: dict = field(default_factory=dict)

    def as_dict(self):
        d = asdict(self)
        d['checks'] = {k: bool(v) for k, v in self.checks.items()}
        return d

    def failed(self):
        return sorted(k for k, v in self.checks.items() if not v)


# Acceptance limits.  Every one is also a backend config key of the same name,
# so a unit can be retuned without editing code.  They come from
# scripts/tetra-phy-selftest.py against simulated TETRA at known SNRs and the
# negative cases that caused real field false alarms -- and, where measurement
# disagreed with simulation, from real C2000 carriers.
#
# ``phy_min_dqpsk_selectivity`` is the one the simulator got wrong.  Clean
# simulated TETRA reaches 3.2-3.4 because AWGN barely concentrates the fourth
# moment at the decoy rates.  Six real C2000 downlink carriers, measured at
# 12-29 dB SNR, scored 1.63 to 2.90: a real signal keeps enough correlation at
# a wrong symbol rate to lift the decoy floor to ~0.2.  The old 1.6 limit
# therefore sat 2% below a strong genuine carrier.  Non-TETRA pi/4-DQPSK at
# 15300, 16000 and 21700 baud scores 0.3-1.1, so 1.35 sits between what is
# really TETRA and what is really not, with margin on both sides.  See
# docs/FIELD-CALIBRATION.md.
LIMITS = {
    'phy_min_snr_db': 8.0,
    'phy_min_occupied_bw_hz': 14000.0,
    'phy_max_occupied_bw_hz': 30000.0,
    'phy_min_boundary_reject_db': 6.0,
    'phy_max_flatness_db': 14.0,
    'phy_max_centre_error_hz': 4000.0,
    'phy_min_dqpsk_m': 0.20,
    'phy_min_dqpsk_phase_spread': 0.50,
    'phy_min_dqpsk_selectivity': 1.35,
    'phy_downlink_min_duty': 0.15,
    'phy_uplink_min_duty': 0.04,
    'phy_uplink_max_duty': 0.85,
    'phy_uplink_min_frame_ratio': 3.0,
    'phy_uplink_min_slot_quantisation': 0.45,
    'phy_uplink_min_bursts': 3,
}


def limits_from(cfg=None):
    lim = dict(LIMITS)
    if cfg:
        for k in LIMITS:
            if k in cfg:
                lim[k] = type(LIMITS[k])(cfg[k])
    return lim


def analyse(channelizer, freq_offset_hz, role='UPLINK', limits=None,
            freq_hz=0.0, decim=8, full=False, timing_phases=8):
    """Run the full TETRA verification on one channel of a dwell capture.

    ``role`` picks the burst-structure expectation: a base station downlink
    transmits continuously, a mobile uplink transmits in 14.1667 ms slots.

    Tests run cheapest first and stop at the first hard failure, because a
    failed check is decisive on its own -- no later measurement can rescue it.
    Most channels in a dwell are empty, so this is what keeps a Pi 3 able to
    watch a whole site's worth of them. Pass ``full=True`` to measure
    everything regardless, which is what the offline analysis tool does.
    """
    lim = limits_from(limits)
    res = PhyResult(freq_hz=float(freq_hz), role=str(role))

    freqs, psd = channelizer.psd()
    shape = channel_shape(freqs, psd, centre_hz=freq_offset_hz)
    res.snr_db = shape['snr_db']
    res.noise_db = shape['noise_db']
    res.occupied_bw_hz = shape['occupied_bw_hz']
    res.boundary_reject_db = shape['boundary_reject_db']
    res.flatness_db = shape['flatness_db']
    res.centre_error_hz = shape['centre_error_hz']

    checks = {
        'snr': res.snr_db >= lim['phy_min_snr_db'],
        'bandwidth': (lim['phy_min_occupied_bw_hz'] <= res.occupied_bw_hz
                      <= lim['phy_max_occupied_bw_hz']),
        'boundary': res.boundary_reject_db >= lim['phy_min_boundary_reject_db'],
        'flatness': res.flatness_db <= lim['phy_max_flatness_db'],
        'centre': abs(res.centre_error_hz) <= lim['phy_max_centre_error_hz'],
    }
    if not full and not all(checks.values()):
        return _finish(res, checks, lim)

    bb, rate = channelizer.extract(freq_offset_hz, decim)
    # Strip the residual tuner error so the matched filter and symbol timing
    # see a centred carrier. |bb| is unchanged, so the envelope stays aligned.
    if abs(res.centre_error_hz) > 1.0:
        t = np.arange(len(bb), dtype=np.float64) / float(rate)
        bb = (bb * np.exp(-2j * math.pi * res.centre_error_hz * t)).astype(np.complex64)

    env, env_rate = power_envelope(bb, rate)
    timing = tdma_timing(env, env_rate, snr_db=res.snr_db,
                         min_snr_db=lim['phy_min_snr_db'])
    res.frame_line_ratio = timing['frame_line_ratio']
    res.duty = timing['duty']
    res.burst_ms_median = timing['burst_ms_median']
    res.slot_quantisation = timing['slot_quantisation']
    res.burst_count = int(timing['burst_count'])

    if role == 'DOWNLINK':
        # Only base stations transmit in 390-395 MHz, so a carrier that has
        # already passed the full TETRA waveform test here *is* a base
        # station, whatever its duty cycle. The floor is therefore only a
        # sanity check that something was actually on the air.
        #
        # ETSI EN 300 392-2 requires the main carrier to be transmitted
        # continuously, but secondary traffic carriers are discontinuous, and
        # timeshared control-channel modes exist too. Demanding near-100% duty
        # here would have locked only the main carrier -- and since the uplink
        # watch list is derived from locked downlinks, every call that moved
        # to a traffic carrier would have gone unwatched.
        checks['on_air'] = res.duty >= lim['phy_downlink_min_duty']
        res.continuous = res.duty >= 0.80
    else:
        checks['burst_duty'] = (lim['phy_uplink_min_duty'] <= res.duty
                                <= lim['phy_uplink_max_duty'])
        checks['frame_rate'] = (res.frame_line_ratio
                                >= lim['phy_uplink_min_frame_ratio'])
        checks['slot_grid'] = (res.slot_quantisation
                               >= lim['phy_uplink_min_slot_quantisation'])
        checks['burst_count'] = res.burst_count >= int(lim['phy_uplink_min_bursts'])
    if not full and not all(checks.values()):
        return _finish(res, checks, lim)

    # An uplink is silent for most of the dwell, so the symbol-rate test must
    # only look at the samples where the mobile is actually keyed up.
    mask = timing['on_mask'] if (role == 'UPLINK' and res.duty < 0.9) else None
    mod = modulation_scores(bb, rate, mask=mask, timing_phases=timing_phases,
                            min_m=0.0 if full else lim['phy_min_dqpsk_m'])
    res.dqpsk_m = mod['dqpsk_m']
    res.dqpsk_phase_spread = mod['dqpsk_phase_spread']
    res.dqpsk_decoy_max = mod['dqpsk_decoy_max']
    res.dqpsk_selectivity = mod['dqpsk_selectivity']

    checks['symbol_rate'] = res.dqpsk_m >= lim['phy_min_dqpsk_m']
    checks['four_phase'] = (res.dqpsk_phase_spread
                            >= lim['phy_min_dqpsk_phase_spread'])
    checks['rate_selectivity'] = (res.dqpsk_selectivity
                                  >= lim['phy_min_dqpsk_selectivity'])
    return _finish(res, checks, lim)


def _finish(res, checks, lim):
    res.checks = checks
    failed = res.failed()
    res.ok = not failed
    res.reason = 'TETRA' if res.ok else 'FAIL:' + ','.join(failed)
    # Quality only means anything once every hard check passed. It drives the
    # UI bar height and nothing else; it can never rescue a failed check.
    if res.ok:
        res.quality = clamp(
            0.40 * clamp((res.dqpsk_m - lim['phy_min_dqpsk_m']) / 0.45)
            + 0.25 * clamp((res.snr_db - lim['phy_min_snr_db']) / 22.0)
            + 0.20 * clamp((res.boundary_reject_db
                            - lim['phy_min_boundary_reject_db']) / 18.0)
            + 0.15 * (clamp(res.slot_quantisation) if res.role == 'UPLINK'
                      else clamp(res.duty)))
    return res
