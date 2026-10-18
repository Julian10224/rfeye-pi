"""What the air and the receiver do to a TETRA signal before RF Eye sees it.

``tetra_sim`` makes clean TETRA plus white noise. Real C2000 carriers do not
look like that: measured on an RTL-SDR (docs/FIELD-CALIBRATION.md) they score a
symbol-rate selectivity of 1.5-2.9 where the clean simulation scores 3.2-3.4,
and a fourth-moment concentration of 0.33-0.63 against ~0.7. A detector judged
only against the clean signal is judged against something easier than the
road. This module adds what makes the difference, each part as a function of
absolute time so a signal can be rendered buffer by buffer without seams:

  * **Multipath and fading** -- the tapped-delay-line propagation models TETRA
    itself is specified against (EN 300 392-2): TU50 (typical urban, 50 km/h),
    HT200 (hilly terrain, 200 km/h), and a static model for a receiver and a
    base station that do not move. Each tap fades with the classical Doppler
    spectrum (sum of sinusoids); a line-of-sight path can be added (Rician K).
  * **Transmitter frequency error and Doppler shift.**
  * **Receiver phase noise** (the R828D's PLL), as a 1/f^2 random-walk-like
    process built from slow sinusoids.
  * **Receiver impairments**: tuner frequency error, I/Q gain and phase
    imbalance, DC offset -- applied once to everything the dongle receives.

Parameters are calibrated in ``calibrate_channel.py`` against the field table.
"""
from __future__ import annotations

import math

import numpy as np

C = 299_792_458.0

# (delay_us, power_dB) per tap. EN 300 392-2 propagation models.
MODELS = {
    "static": [(0.0, 0.0)],
    "TU50": [(0.0, 0.0), (5.0, -22.3)],
    "HT200": [(0.0, 0.0), (15.0, -8.6)],
    # A receiver indoors at a window: a strong direct path and a short echo.
    "indoor": [(0.0, 0.0), (0.6, -6.0), (1.8, -12.0)],
    # TU50 plus a 15 us echo: past the field's modulation quality, on purpose.
    "harsh": [(0.0, 0.0), (5.0, -22.3), (15.0, -8.0)],
}


def _coarse(t, step):
    """Every ``step``-th time of ``t``, and always its last."""
    tc = t[::step]
    return tc if tc[-1] == t[-1] else np.append(tc, t[-1])


class _Fading:
    """Unit-power complex Rayleigh process with a Jakes spectrum, g(t)."""

    def __init__(self, fd_hz, rng, n=12):
        self.fd = float(fd_hz)
        k = np.arange(1, n + 1)
        alpha = (2 * np.pi * k - np.pi + rng.uniform(-np.pi, np.pi, n)) / (4 * n)
        self.w = 2 * np.pi * self.fd * np.cos(alpha)
        self.phi_i = rng.uniform(-np.pi, np.pi, n)
        self.phi_q = rng.uniform(-np.pi, np.pi, n)
        self.n = n

    def __call__(self, t):
        # Fading moves at a few tens of Hz: evaluated every 64 samples (32 us)
        # and interpolated, which is exact to far below the noise.
        tc = _coarse(t, 64)
        wt = np.outer(tc, self.w)
        g = (np.cos(wt + self.phi_i).sum(axis=1)
             + 1j * np.cos(wt + self.phi_q).sum(axis=1)) / math.sqrt(self.n)
        return (np.interp(t, tc, g.real) + 1j * np.interp(t, tc, g.imag)).astype(np.complex64)


class _SlowPhase:
    """Receiver phase noise: ``rms_deg`` spread over lo..hi Hz, PSD ~ 1/f^slope.

    A PLL's phase noise is not only slow wander: its skirt reaches tens of kHz,
    where it smears a carrier into its own channel edges and jitters the phase
    from one symbol to the next -- which is what costs a real carrier its
    edge valley and its four-phase score.
    """

    def __init__(self, rms_deg, rng, lo_hz=10.0, hi_hz=30000.0, slope=1.0, n=40):
        self.f = np.geomspace(lo_hz, hi_hz, n)
        # Log-spaced lines: each stands for a band ~f wide, so power ~ f * S(f).
        amp = np.sqrt(self.f * self.f ** (-float(slope)))
        amp *= math.radians(rms_deg) * math.sqrt(2.0) / math.sqrt(np.sum(amp ** 2))
        self.a = amp
        self.p = rng.uniform(-np.pi, np.pi, n)

    def __call__(self, t):
        if not np.any(self.a):
            return np.zeros(len(t))
        tc = _coarse(t, 16)               # up to 3 kHz, sampled at 126 kHz
        return np.interp(t, tc, np.cos(2 * np.pi * np.outer(tc, self.f) + self.p) @ self.a)


class Path:
    """One transmitter to this receiver: multipath, fading, Doppler, its own offset."""

    def __init__(self, model="static", speed_kmh=0.0, carrier_hz=385e6, k_db=None,
                 tx_offset_hz=0.0, seed=0, fd_floor_hz=0.3, taps=None, pa_a3=0.0):
        rng = np.random.default_rng(seed)
        self.taps = []
        fd = max(float(fd_floor_hz), speed_kmh / 3.6 * carrier_hz / C)
        self.fd = fd
        # The transmitter's power amplifier: a little cubic compression puts
        # energy outside the 25 kHz channel, as a real linearised PA does.
        self.pa_a3 = float(pa_a3)
        for delay_us, p_db in (taps if taps is not None else MODELS[model]):
            self.taps.append((delay_us * 1e-6, 10 ** (p_db / 20.0), _Fading(fd, rng)))
        norm = math.sqrt(sum(a * a for _, a, _ in self.taps))
        self.taps = [(d, a / norm, f) for d, a, f in self.taps]
        # Line of sight on the first tap: Rician with factor K.
        self.k = None if k_db is None else 10 ** (k_db / 10.0)
        self.los_doppler = fd * math.cos(rng.uniform(0, math.pi))
        self.offset = float(tx_offset_hz) + (self.los_doppler if self.k else 0.0)
        self.los_phase = rng.uniform(-np.pi, np.pi)

    def apply(self, x, t0, sr):
        """``x`` sampled from absolute time ``t0`` at ``sr``; returns what arrives."""
        n = len(x)
        t = t0 + np.arange(n) / float(sr)
        if self.pa_a3:
            p = np.abs(x) ** 2
            ref = float(np.mean(p[p > 1e-3 * p.max()])) if p.max() > 0 else 1.0
            x = (x * (1.0 - self.pa_a3 * p / ref)).astype(np.complex64)
        y = np.zeros(n, dtype=np.complex64)
        for i, (delay, amp, fade) in enumerate(self.taps):
            d = int(round(delay * sr))
            xs = x if d == 0 else np.concatenate((np.zeros(d, x.dtype), x[:n - d]))
            g = fade(t)
            if i == 0 and self.k is not None:
                los = np.exp(1j * (2 * np.pi * self.los_doppler * t + self.los_phase))
                g = (math.sqrt(self.k / (self.k + 1)) * los
                     + math.sqrt(1 / (self.k + 1)) * g).astype(np.complex64)
            y += (amp * g) * xs
        if self.offset:
            y *= np.exp(2j * np.pi * self.offset * t).astype(np.complex64)
        return y


class Receiver:
    """The RTL-SDR's own contribution, applied to everything it receives."""

    def __init__(self, tuner_error_hz=220.0, phase_rms_deg=0.0, iq_gain_db=0.0,
                 iq_phase_deg=0.0, dc=(0.0, 0.0), seed=11, phase_hi_hz=30000.0):
        rng = np.random.default_rng(seed)
        self.err = float(tuner_error_hz)
        self.pn = _SlowPhase(phase_rms_deg, rng, hi_hz=phase_hi_hz)
        g = 10 ** (iq_gain_db / 20.0)
        ph = math.radians(iq_phase_deg)
        # y = a*x + b*conj(x): the usual image model of gain/phase imbalance.
        self.a = 0.5 * (1 + g * np.exp(-1j * ph))
        self.b = 0.5 * (1 - g * np.exp(1j * ph))
        self.dc = complex(*dc)

    def apply(self, x, t0, sr):
        t = t0 + np.arange(len(x)) / float(sr)
        ph = 2 * np.pi * self.err * t + self.pn(t)
        y = x * np.exp(1j * ph).astype(np.complex64)
        y = self.a * y + self.b * np.conj(y) + self.dc
        return y.astype(np.complex64)
