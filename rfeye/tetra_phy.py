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
        self._psd_cache = {}
        self._prep_cache = {}

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

    def psd(self, nfft=None, percentile=92.0, dc_notch_hz=2500.0):
        """Occupancy spectrum as (offset_hz, power_db).

        ``nfft`` defaults to 1024 bins per 288 kS/s, so every sample rate gets
        the ~281 Hz resolution and ~3.6 ms rows the limits were calibrated
        with: 1024 at 288 kS/s, 7168 at 2.016 MS/s.

        This is a short-time spectrum reduced over time by a high percentile
        rather than a mean.  That matters for the uplink: a handset transmits
        one 14.1667 ms slot out of every 56.6667 ms, so a mean spectrum
        understates its real level by about 6 dB and would push a genuine
        police transmission below the verification floor.  The percentile
        recovers the level while the transmitter is actually keyed up.

        The same percentile is used for the signal and the noise reference, so
        its positive bias cancels in every ratio derived from this spectrum.

        A higher percentile recovers a shorter transmission: the default 92
        needs the channel busy for at least ~8% of the dwell, so a single
        14 ms control burst (2-3% duty) is averaged away and reads as noise.

        ``percentile=100`` is a peak hold, and it is what the sensitive uplink
        path asks for.  98 was not enough: one dwell is ~146 rows of 3.56 ms,
        so the top 2% is three rows, and a 14 ms burst is four.  Measured
        against simulated TETRA at a true 25 dB:

            burst        p92    p98   p99.5   peak hold
            7 ms          0.6    1.6   18.1        20.9
            14 ms         1.0   14.5   20.8        22.6
            28 ms         2.0   20.6   23.2        23.7
            held call    22.8   24.4   25.1        25.3
            empty         0.3    0.5    0.7         0.6

        The last row is why a peak hold is safe: the bias applies to the
        noise reference as much as to the channel, so an empty channel still
        reads 0.6 dB and no ratio derived from this spectrum moves.  What a
        peak hold really buys is that the measurement stops depending on the
        duty cycle of the thing being measured, which is the one property a
        detector for short bursts cannot do without.

        The result is cached per percentile, so a dwell that measures both
        costs each FFT reduction only once.
        """
        if nfft is None:
            nfft = 1024 * max(1, int(round(self.sample_rate / 288_000.0)))
        nfft = int(nfft)
        key = (nfft, float(percentile), float(dc_notch_hz))
        cached = self._psd_cache.get(key)
        if cached is not None:
            return cached
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
        out = (np.fft.fftshift(f).astype(np.float64),
               _db(np.fft.fftshift(red)))
        self._psd_cache[key] = out
        return out

    def shape_prepared(self, nfft=None, percentile=92.0, dc_notch_hz=2500.0):
        """``prepare_shape`` for this dwell's spectrum, computed once.

        Every channel of a dwell shares it.  Doing it per channel meant the
        dB-to-linear conversion and the smoothing convolution ran up to 96
        times over 7168 bins for one capture, which is analysis time the
        receiver is not spending listening.
        """
        key = (nfft, float(percentile), float(dc_notch_hz))
        prep = self._prep_cache.get(key)
        if prep is None:
            freqs, db = self.psd(nfft=nfft, percentile=percentile,
                                 dc_notch_hz=dc_notch_hz)
            prep = prepare_shape(freqs, db)
            self._prep_cache[key] = prep
        return prep

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


def prepare_shape(freqs, psd_db, smooth_hz=1400.0):
    """The part of ``channel_shape`` that does not depend on the channel.

    A dwell measures up to 48 channels out of one spectrum, and until 0.9.37
    each of them re-did the whole-array work: the dB-to-linear conversion over
    7168 bins, the smoothing convolution, and boolean masks the width of the
    capture.  Nothing in any of that knows where the channel is.

    That mattered because the analysis, not the radio, is what limits how
    often a channel comes round.  Measured on the field unit: 0.70 s to
    capture a dwell and 0.93 s to analyse it, so the receiver spent 11% of its
    time actually listening to 380-385 MHz.

    ``channel_shape`` accepts the result as ``prepared``; without it, it still
    works out the same numbers on its own, which is what the offline tools do.
    """
    freqs = np.asarray(freqs, dtype=np.float64)
    p_db = np.asarray(psd_db, dtype=np.float64)
    bin_hz = float(np.median(np.diff(freqs))) if len(freqs) > 1 else 1.0
    n = int(round(float(smooth_hz) / max(1.0, bin_hz)))
    n = max(1, n | 1)
    if n > 1 and len(p_db) >= 4 * n:
        lin = 10.0 ** (p_db / 10.0)
        kernel = np.ones(n, dtype=np.float64) / float(n)
        w_lin = np.convolve(lin, kernel, mode='same')
        edge = n // 2
        w_lin[:edge] = lin[:edge]
        w_lin[len(w_lin) - edge:] = lin[len(lin) - edge:]
        w_db = _db(w_lin)
    else:
        w_db = p_db
    return {'freqs': freqs, 'p_db': p_db, 'w_db': w_db, 'bin_hz': bin_hz}


def channel_shape(freqs, psd_db, centre_hz=0.0, prepared=None):
    """How much a channel looks like a 25 kHz RRC(0.35) TETRA carrier.

    The RTL-SDR has roughly 45 dB of usable dynamic range, so this does not
    attempt the real ETSI spectrum mask.  It measures the three properties
    that actually separated TETRA from the observed false positives: the
    carrier is about 25 kHz wide, it stops before the neighbouring 25 kHz
    channels, and its passband is flat rather than one spur line.

    ``prepared`` is ``prepare_shape``'s result for this spectrum, shared by
    every channel of the dwell.  The windows below are contiguous runs of a
    uniformly spaced spectrum, so they are taken as index slices rather than
    boolean masks over the whole capture; the numbers are identical and the
    work is proportional to the channel rather than to the dwell.
    """
    if prepared is None:
        prepared = prepare_shape(freqs, psd_db)
    fr = prepared['freqs']
    p_db = prepared['p_db']
    w_db = prepared['w_db']
    centre = float(centre_hz)

    def span(lo_hz, hi_hz, lo_open=True, hi_open=True):
        """Indices of ``lo_hz < f-centre < hi_hz`` (open ends by default)."""
        i0 = int(np.searchsorted(fr, centre + lo_hz, 'right' if lo_open else 'left'))
        i1 = int(np.searchsorted(fr, centre + hi_hz, 'left' if hi_open else 'right'))
        return i0, max(i0, i1)

    # |f - centre| < 8000
    c0, c1 = span(-8000.0, 8000.0)
    core = slice(c0, c1)
    # 30000 <= |f - centre| < 140000, which is one window either side
    n0a, n1a = span(-140000.0, -30000.0, lo_open=True, hi_open=False)
    n0b, n1b = span(30000.0, 140000.0, lo_open=False, hi_open=True)
    if (c1 - c0) < 3 or (n1a - n0a) + (n1b - n0b) < 3:
        raise ValueError('capture bandwidth too small for shape analysis')
    noise_vals = np.concatenate((p_db[n0a:n1a], p_db[n0b:n1b]))

    core_db = float(np.median(p_db[core]))
    # A low percentile, not the median: in a busy band part of the reference
    # window is occupied by neighbouring carriers and would inflate a median.
    noise_db = float(np.percentile(noise_vals, 20))

    # Is there a spectral valley where the 25 kHz channel ends?  This is the
    # test that separates one TETRA carrier from one wide hump spilling across
    # several raster points -- the exact shape that produced the field false
    # alarms.  It is measured as the deepest point in the guard region on each
    # side, and scored by the *shallower* of the two, so a single strong
    # neighbour cannot mask a carrier that is genuinely wide.
    valleys = []
    for lo, hi in ((-15500.0, -10500.0), (10500.0, 15500.0)):
        v0, v1 = span(lo, hi, lo_open=False, hi_open=False)
        if v1 - v0 >= 3:
            valleys.append(core_db - float(np.min(p_db[v0:v1])))
    boundary_db_reject = min(valleys) if valleys else 0.0

    # Occupied bandwidth: the *contiguous* -10 dB width around the channel
    # centre, walking outward until the spectrum first drops away.  Measuring
    # a contiguous run rather than integrating power is what keeps this honest
    # on a busy site: energy from an active neighbour sits on the far side of
    # the guard valley, so it is never counted, while a genuinely wide emitter
    # has no valley to stop the walk and runs straight to the cap.
    # TETRA lands near 21 kHz, a CW spur under 1 kHz, 12.5 kHz FM near 6 kHz.
    #
    # The walk runs on a spectrum smoothed to ~1.4 kHz first, because a walk
    # that stops at the *first* bin below the edge is only as stable as the
    # single noisiest bin it passes.  At 281 Hz resolution a 25 kHz carrier is
    # ~89 bins, far more than a width measurement needs, and under the peak
    # hold the sensitive uplink path uses, each bin of a 7 ms burst rests on
    # one or two FFT rows -- so the raw profile carries several dB of scatter
    # and the walk terminated at random.  Measured over 16 random burst
    # positions per point, before and after:
    #
    #     burst      30 dB     22 dB     16 dB
    #     7 ms     68->100%  62->100%  81->100%
    #     14 ms    93->100% 100->100% 100->100%
    #
    # Smoothing only this measurement, and not the level or the valley, is
    # deliberate: those are calibrated ratios and a smoothed noise reference
    # would move them by several dB. A continuous downlink carrier measures
    # 21.4-21.7 kHz here against 21.4 kHz before, so the limits still mean
    # what they were calibrated to mean.
    cap_hz = 20000.0
    edge_db = float(np.median(w_db[core])) - 10.0
    # argmin(a) without touching the whole array: the nearest bin is one of
    # the two either side of the centre, and a tie goes to the lower index
    # exactly as argmin would resolve it.
    j = int(np.searchsorted(fr, centre))
    cands = [k for k in (j - 1, j) if 0 <= k < len(fr)] or [0]
    centre_idx = min(cands, key=lambda k: (abs(float(fr[k]) - centre), k))
    # The walk, as a run length rather than a Python loop. On flat noise the
    # spectrum never drops 10 dB, so it used to step all the way to the cap --
    # ~71 bins each side, twice per channel, for every one of the 48 channels
    # in a dwell. That was the single hottest thing in a quiet sweep, and a
    # quiet sweep is what the band mostly is.
    cap_bins = int(cap_hz / max(1.0, prepared['bin_hz'])) + 2
    lo_i = max(0, centre_idx - cap_bins)
    hi_i = min(len(fr), centre_idx + cap_bins + 1)

    def run(seg_db, seg_fr):
        """How far the condition holds from the centre outwards."""
        ok = (seg_db > edge_db) & (np.abs(seg_fr - centre) <= cap_hz)
        if ok.size == 0 or not ok[0]:
            return 0
        bad = np.flatnonzero(~ok)
        return int(bad[0]) if bad.size else int(ok.size)

    down = run(w_db[lo_i:centre_idx + 1][::-1], fr[lo_i:centre_idx + 1][::-1])
    up = run(w_db[centre_idx:hi_i], fr[centre_idx:hi_i])
    lo_hz = abs(float(fr[centre_idx - down + 1]) - centre) if down else 0.0
    hi_hz = abs(float(fr[centre_idx + up - 1]) - centre) if up else 0.0
    occupied_bw = lo_hz + hi_hz

    # Flatness: modulation is noise-like and flat across the passband, a
    # carrier or spur is not.  Peak-to-median so one hot bin is punished.
    flatness_db = float(np.max(p_db[core])) - core_db

    # Power centroid, used to strip the residual tuner error before the
    # symbol-rate test runs.
    f0, f1 = span(-13000.0, 13000.0, lo_open=False, hi_open=False)
    fit_lin = 10.0 ** (p_db[f0:f1] / 10.0)
    w = np.clip(fit_lin - 10.0 ** (noise_db / 10.0), 0.0, None)
    centre_err = (float(np.sum((fr[f0:f1] - centre) * w) / np.sum(w))
                  if float(np.sum(w)) > 0 else 0.0)

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
    # The high reference has to be reachable by the shortest transmission
    # worth finding, not by the average one.  At 95 it was not: one dwell is
    # ~585 envelope boxes, a single 14 ms slot is 16 of them (2.7%), so the
    # 95th percentile still sat in the noise.  ``span`` then came out under
    # ``min_span_db``, the flat-envelope branch below declared the channel
    # continuously occupied, and ``duty`` was reported as 1.00 for what was
    # actually one burst in half a second of silence -- which the sensitive
    # uplink rule then rejected as a base station bleeding in under overload.
    # At 99 the top six boxes decide, so a 7 ms subslot burst still sets it.
    hi = float(np.percentile(e_db, 99))
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
    # Ignore flicker shorter than a real burst.  This used to be 0.6 slots,
    # which is longer than a subslot (0.5) -- so the shortest burst ETSI
    # defines, the random-access burst a mobile sends to register, was thrown
    # away by the very filter whose comment said it kept it.  0.4 slots is
    # 5.7 ms: it still drops single-box envelope flicker (0.9 ms) by a wide
    # margin, and it keeps the 7.1 ms subslot.
    runs = [r for r in runs if r >= 0.4 * SLOT_S]

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
        # One mask entry per envelope box, and power_envelope drops the partial
        # box at the end -- so the box is the floor of the ratio, never the
        # ceiling. At 288 kS/s the two agree (32768 / 1024). At 2.016 MS/s the
        # channel is 18724 samples in 585 boxes of 32, and rounding up to 33
        # slid the mask a whole TETRA slot off the bursts by the end of the
        # dwell, which halved the symbol-rate score of every real uplink.
        rep = max(1, len(bb) // max(1, len(m)))
        valid = np.repeat(m, rep)[:len(bb)]
        if len(valid) < len(bb):
            valid = np.concatenate([valid, np.zeros(len(bb) - len(valid), bool)])
        # Enough keyed-up samples to carry the 96 symbols a timing phase needs
        # below, and no more.  A flat 256 was a hidden second opinion on how
        # short a burst may be: at 36 kS/s a 7.1 ms subslot burst is 255
        # samples, so the shortest real TETRA transmission failed here by one
        # sample, before any modulation was measured.
        if int(np.count_nonzero(valid)) < int(math.ceil(96.0 * sps)):
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
    # How far the carrier sat from its nominal raster frequency before
    # re-centring: the receiver's tuner error, not a property of the signal.
    tuner_error_hz: float = 0.0
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
    # True when judged by the relaxed sensitive-uplink rules (a short control
    # burst counts), rather than the strict sustained-call rules.
    sensitive: bool = False
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
# ``phy_max_centre_error_hz`` bounds how far the tuner may be off before a
# carrier is assumed to be a different carrier rather than a mistuned one.  It
# is not a quality limit: ``analyse`` re-centres on the measured carrier, so
# the modulation survives a large error, and the binding constraint is only
# whether the carrier still fits the extracted channel.  Measured against
# simulated TETRA at 22 dB SNR, re-centring accepts a tuner error up to about
# 31 ppm at 390 MHz (12 kHz) and correctly rejects 41 ppm, where the carrier
# leaves the channel.  8 kHz covers every uncalibrated RTL-SDR crystal while
# staying far short of the 25 kHz neighbour.
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
    'phy_max_centre_error_hz': 8000.0,
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

    # Sensitive mode also alerts on a short control/registration burst -- a
    # moving mobile keys those up crossing cells even when nobody is talking --
    # not only on a held voice call. Such a burst is a few per cent duty, so
    # the occupancy spectrum is peak held rather than averaged, or the level
    # is lost before any test sees it. Every TETRA-proving test (the 25 kHz
    # shape, the pi/4-DQPSK / 18 kbaud / selectivity tests) still has to pass;
    # only the sustained-call structure tests are relaxed. See the uplink
    # block below.
    # Sensitive mode is a mode switch, not an acceptance limit, so it is read
    # straight from the passed config rather than the merged LIMITS defaults
    # (production passes the backend cfg here, and forces it on; the tests
    # pass a small dict, and leave it off to exercise the strict path).
    cfg_src = limits if isinstance(limits, dict) else {}
    sensitive = bool(cfg_src.get('uplink_sensitive', False)) and str(role) == 'UPLINK'
    pct = (float(cfg_src.get('phy_uplink_sensitive_percentile', 100.0))
           if sensitive else 92.0)
    res.sensitive = sensitive

    prep = channelizer.shape_prepared(percentile=pct)
    freqs, psd = prep['freqs'], prep['p_db']
    shape = channel_shape(freqs, psd, centre_hz=freq_offset_hz, prepared=prep)

    # An uncalibrated tuner puts the carrier somewhere near, but not on, the
    # nominal raster frequency. Measuring the shape around the nominal centre
    # then penalises the signal for the receiver's own error: at 390 MHz a
    # 13 ppm crystal shifts the carrier 5 kHz, which pushes it toward the
    # channel edge and collapses the edge-valley and bandwidth numbers even
    # though the modulation is still perfectly recognisable.
    #
    # So re-measure around where the carrier actually is. The correction is
    # bounded by phy_max_centre_error_hz -- beyond that, what was found is
    # more likely a different carrier than a mistuned one. Carriers sit on a
    # 25 kHz grid, so a bounded correction cannot slide onto the neighbour.
    offset = float(freq_offset_hz)
    guard = float(lim['phy_max_centre_error_hz'])
    err = float(shape['centre_error_hz'])
    res.tuner_error_hz = err
    if abs(err) > 200.0 and abs(err) <= guard:
        offset = freq_offset_hz + err
        shape = channel_shape(freqs, psd, centre_hz=offset, prepared=prep)

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
        'centre': abs(res.tuner_error_hz) <= lim['phy_max_centre_error_hz'],
    }
    if not full and not all(checks.values()):
        return _finish(res, checks, lim)

    bb, rate = channelizer.extract(offset, decim)
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
    elif sensitive:
        # A mobile keyed up on the uplink -- long call or single control burst.
        # The sustained-call structure (a repeating 17.647 Hz frame line, three
        # or more slots, a clean slot grid) is not required, because a
        # registration burst has none of it. What is required is that something
        # really was keyed up (at least one burst, and not a continuously
        # transmitting carrier, which on the uplink means a base station
        # bleeding in under overload, never a handset) and that it is TETRA --
        # the shape above and the modulation tests below, which are what keep
        # this off noise, spurs and other digital systems.
        checks['on_air'] = (res.burst_count >= 1
                            and lim['phy_uplink_min_duty'] * 0.25 <= res.duty
                            <= lim['phy_uplink_max_duty'])
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
