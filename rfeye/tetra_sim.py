"""Synthetic TETRA and interferer generator for RF Eye.

This exists so the C2000 detector can be proved correct without a
transmitter, without driving around, and without waiting for a real police
call.  ``scripts/tetra-phy-selftest.py`` builds known-truth captures here and
asserts that ``tetra_phy`` accepts the TETRA ones and rejects everything else,
including the interferer shapes that produced the field false alarms on
detector profile v7.

The waveforms follow ETSI EN 300 392-2: pi/4-DQPSK at 18000 symbols/s, RRC
roll-off 0.35, 255-symbol slots, 4 slots per frame, 18 frames per multiframe,
with frame 18 left free by a mobile in a traffic call.
"""
from __future__ import annotations

import math

import numpy as np

from tetra_phy import (SYMBOL_RATE_HZ, SYMBOLS_PER_SLOT, SLOTS_PER_FRAME,
                       FRAMES_PER_MULTIFRAME, RRC_ROLLOFF, rrc_taps)

# pi/4-DQPSK differential phase alphabet.
_PHASE_STEPS = np.array([math.pi / 4, 3 * math.pi / 4,
                         -math.pi / 4, -3 * math.pi / 4])


def dqpsk_symbols(count, rng):
    """Unit-amplitude pi/4-DQPSK symbol sequence."""
    steps = rng.choice(_PHASE_STEPS, size=int(count))
    return np.exp(1j * np.cumsum(steps)).astype(np.complex64)


def _pulse_shape(symbols, sps):
    up = np.zeros(len(symbols) * int(sps), dtype=np.complex64)
    up[::int(sps)] = symbols
    taps = rrc_taps(sps, span_symbols=8).astype(np.complex64)
    return np.convolve(up, taps, mode='same') * math.sqrt(float(sps))


def _burst_gate(n_symbols, sps, slot_pattern, ramp_symbols=6):
    """Symbol-rate on/off gate for a mobile transmitting one slot per frame."""
    gate = np.zeros(n_symbols, dtype=np.float32)
    frame_syms = SYMBOLS_PER_SLOT * SLOTS_PER_FRAME
    for k in range(0, n_symbols, frame_syms):
        frame_index = (k // frame_syms) % FRAMES_PER_MULTIFRAME
        # Frame 18 of every multiframe is the control frame; a mobile in a
        # traffic call stays off air for it.
        if frame_index == FRAMES_PER_MULTIFRAME - 1:
            continue
        for slot in slot_pattern:
            a = k + slot * SYMBOLS_PER_SLOT
            b = min(a + SYMBOLS_PER_SLOT - 14, n_symbols)   # guard period
            if a >= n_symbols:
                break
            gate[a:b] = 1.0
    if ramp_symbols > 0:
        w = np.ones(int(ramp_symbols), dtype=np.float32) / float(ramp_symbols)
        gate = np.convolve(gate, w, mode='same').astype(np.float32)
    return np.repeat(gate, int(sps))[:n_symbols * int(sps)]


def tetra_carrier(duration_s, sample_rate, role='DOWNLINK', seed=0,
                  slot_pattern=(1,), freq_offset_hz=0.0):
    """Baseband TETRA carrier of unit average power while transmitting."""
    rng = np.random.default_rng(int(seed))
    sps = float(sample_rate) / SYMBOL_RATE_HZ
    if abs(sps - round(sps)) > 1e-6:
        raise ValueError('sample_rate must be an integer multiple of 18000')
    sps = int(round(sps))
    n_sym = int(math.ceil(float(duration_s) * SYMBOL_RATE_HZ)) + 64
    sig = _pulse_shape(dqpsk_symbols(n_sym, rng), sps)
    if role.upper() == 'UPLINK':
        sig = sig * _burst_gate(n_sym, sps, slot_pattern)[:len(sig)]
    n = int(round(float(duration_s) * float(sample_rate)))
    sig = sig[:n]
    if freq_offset_hz:
        t = np.arange(len(sig), dtype=np.float64) / float(sample_rate)
        sig = sig * np.exp(2j * math.pi * float(freq_offset_hz) * t)
    active = np.abs(sig) > 0.05 * (np.max(np.abs(sig)) + 1e-9)
    rms = float(np.sqrt(np.mean(np.abs(sig[active]) ** 2))) if np.any(active) else 1.0
    return (sig / max(rms, 1e-9)).astype(np.complex64)


def dqpsk_carrier(duration_s, sample_rate, baud, seed=0, freq_offset_hz=0.0):
    """pi/4-DQPSK at an arbitrary symbol rate.

    The hardest adversary the detector faces: identical modulation family and
    a plausible bandwidth, but not TETRA.  Only the symbol-rate selectivity
    test separates this from a real C2000 carrier, so it belongs in the test
    set as proof that the rate test is doing real work.
    """
    rng = np.random.default_rng(int(seed))
    sps = max(2, int(round(float(sample_rate) / float(baud))))
    n_sym = int(math.ceil(float(duration_s) * float(sample_rate) / sps)) + 32
    sig = _pulse_shape(dqpsk_symbols(n_sym, rng), sps)
    n = int(round(float(duration_s) * float(sample_rate)))
    sig = sig[:n]
    if len(sig) < n:
        sig = np.concatenate([sig, np.zeros(n - len(sig), np.complex64)])
    if freq_offset_hz:
        t = np.arange(len(sig), dtype=np.float64) / float(sample_rate)
        sig = sig * np.exp(2j * math.pi * float(freq_offset_hz) * t)
    rms = float(np.sqrt(np.mean(np.abs(sig) ** 2))) or 1.0
    return (sig / rms).astype(np.complex64)


# --- interferers -----------------------------------------------------------

def cw_tone(duration_s, sample_rate, freq_offset_hz=0.0):
    n = int(round(duration_s * sample_rate))
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    return np.exp(2j * math.pi * float(freq_offset_hz) * t).astype(np.complex64)


def band_noise(duration_s, sample_rate, bandwidth_hz, freq_offset_hz=0.0, seed=1):
    """Band-limited complex noise: generic wideband clutter."""
    rng = np.random.default_rng(int(seed))
    n = int(round(duration_s * sample_rate))
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / math.sqrt(2.0)
    spec = np.fft.fft(x)
    f = np.fft.fftfreq(n, 1.0 / float(sample_rate))
    spec[np.abs(f) > float(bandwidth_hz) / 2.0] = 0.0
    y = np.fft.ifft(spec)
    y = y / max(float(np.sqrt(np.mean(np.abs(y) ** 2))), 1e-12)
    if freq_offset_hz:
        t = np.arange(n, dtype=np.float64) / float(sample_rate)
        y = y * np.exp(2j * math.pi * float(freq_offset_hz) * t)
    return y.astype(np.complex64)


def gated_noise(duration_s, sample_rate, bandwidth_hz, on_s, period_s,
                freq_offset_hz=0.0, seed=2):
    """Bursty narrowband noise on a non-TETRA schedule.

    This is the important negative case: it has roughly the right bandwidth
    and roughly the right duty cycle, and detector profile v7 could not tell
    it apart from a police handset.
    """
    y = band_noise(duration_s, sample_rate, bandwidth_hz, freq_offset_hz, seed)
    t = np.arange(len(y), dtype=np.float64) / float(sample_rate)
    gate = ((t % float(period_s)) < float(on_s)).astype(np.float32)
    box = max(1, int(sample_rate * 0.0004))
    gate = np.convolve(gate, np.ones(box, np.float32) / box, mode='same')
    return (y * gate).astype(np.complex64)


def nfm_voice(duration_s, sample_rate, deviation_hz=2500.0, tone_hz=900.0,
              freq_offset_hz=0.0):
    """Narrowband FM, the classic 12.5 kHz analogue neighbour."""
    n = int(round(duration_s * sample_rate))
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    mod = np.sin(2 * math.pi * tone_hz * t) + 0.4 * np.sin(2 * math.pi * 320.0 * t)
    phase = 2 * math.pi * deviation_hz * np.cumsum(mod) / float(sample_rate)
    return np.exp(1j * (phase + 2 * math.pi * float(freq_offset_hz) * t)).astype(np.complex64)


def fsk_data(duration_s, sample_rate, baud=4800.0, deviation_hz=4500.0,
             freq_offset_hz=0.0, seed=3):
    """2-FSK data, e.g. paging / telemetry sharing the band."""
    rng = np.random.default_rng(int(seed))
    n = int(round(duration_s * sample_rate))
    sps = max(1, int(round(float(sample_rate) / float(baud))))
    bits = rng.integers(0, 2, size=n // sps + 2) * 2 - 1
    f = np.repeat(bits, sps)[:n] * float(deviation_hz)
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    phase = 2 * math.pi * np.cumsum(f) / float(sample_rate)
    return np.exp(1j * (phase + 2 * math.pi * float(freq_offset_hz) * t)).astype(np.complex64)


def fsk4_data(duration_s, sample_rate, baud=4800.0, deviation_hz=1944.0,
              freq_offset_hz=0.0, seed=4):
    """4-level FSK, the DMR / MOTOTRBO shape found next to C2000 in the field."""
    rng = np.random.default_rng(int(seed))
    n = int(round(duration_s * sample_rate))
    sps = max(1, int(round(float(sample_rate) / float(baud))))
    levels = np.array([-3.0, -1.0, 1.0, 3.0]) * float(deviation_hz) / 3.0
    sym = rng.choice(levels, size=n // sps + 2)
    f = np.repeat(sym, sps)[:n]
    box = max(1, sps // 2)
    f = np.convolve(f, np.ones(box) / box, mode='same')
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    phase = 2 * math.pi * np.cumsum(f) / float(sample_rate)
    return np.exp(1j * (phase + 2 * math.pi * float(freq_offset_hz) * t)).astype(np.complex64)


def impulse_noise(duration_s, sample_rate, rate_hz=140.0, width_s=6e-5,
                  freq_offset_hz=0.0, seed=5):
    """Ignition / switching noise: broadband spikes at an irregular rate."""
    rng = np.random.default_rng(int(seed))
    n = int(round(duration_s * sample_rate))
    out = np.zeros(n, dtype=np.complex64)
    width = max(1, int(width_s * sample_rate))
    count = max(1, int(duration_s * rate_hz))
    for start in rng.integers(0, max(1, n - width), size=count):
        seg = (rng.standard_normal(width) + 1j * rng.standard_normal(width))
        out[start:start + width] += seg.astype(np.complex64)
    rms = float(np.sqrt(np.mean(np.abs(out) ** 2))) or 1.0
    out = out / rms
    if freq_offset_hz:
        t = np.arange(n, dtype=np.float64) / float(sample_rate)
        out = out * np.exp(2j * math.pi * float(freq_offset_hz) * t)
    return out.astype(np.complex64)


# --- capture assembly ------------------------------------------------------

def make_capture(components, duration_s, sample_rate, snr_db=None,
                 noise_rms=1.0, seed=7, quantise=True):
    """Sum baseband components onto a noise floor and 8-bit quantise.

    ``components`` is a sequence of ``(signal, amplitude)``.  When ``snr_db``
    is given the first component is scaled to that SNR against ``noise_rms``
    and the amplitudes of the rest are taken relative to it.
    """
    rng = np.random.default_rng(int(seed))
    n = int(round(float(duration_s) * float(sample_rate)))
    out = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
    out *= np.float32(float(noise_rms) / math.sqrt(2.0))

    scale = 1.0
    if snr_db is not None:
        # SNR here is per-channel: signal power against the noise power inside
        # one 25 kHz channel, not across the whole capture bandwidth.
        chan_noise = float(noise_rms) * math.sqrt(25000.0 / float(sample_rate))
        scale = chan_noise * (10.0 ** (float(snr_db) / 20.0))

    for sig, amp in components:
        s = np.asarray(sig, dtype=np.complex64)
        if len(s) < n:
            s = np.concatenate([s, np.zeros(n - len(s), np.complex64)])
        out[:n] += (np.complex64(scale * float(amp)) * s[:n])

    if quantise:
        # Match the RTL-SDR front end: 8-bit unsigned I/Q around 127.5.
        peak = float(np.max(np.abs(np.concatenate([out.real, out.imag])))) or 1.0
        g = 100.0 / peak
        i = np.clip(np.round(out.real * g) + 127.5, 0, 255)
        q = np.clip(np.round(out.imag * g) + 127.5, 0, 255)
        out = ((i - 127.5) + 1j * (q - 127.5)).astype(np.complex64)
    return out
