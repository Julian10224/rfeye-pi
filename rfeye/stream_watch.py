"""Continuous uplink watch: a ring buffer and a per-channel energy trigger.

Why this exists
---------------
Up to 0.10.9 the uplink was watched in separate dwells: tune, read 0.52 s,
analyse, retune.  power.log measured what that bought -- ``hear=9.4%``: each
uplink channel was being listened to about 9% of the time.  A vehicle that
sends a handful of short bursts while it drives past is then mostly missed,
however good the waveform test is:

    bursts in range    chance at 9.4%    chance listening all the time
          1                 9%                    ~100%
          5                39%                    ~100%
         10                63%                    ~100%

A Blu Eye hears the whole band continuously.  Separate captures cannot.

So the dongle streams.  librtlsdr's async reader fills a ring buffer, a cheap
short-time FFT measures every 25 kHz channel of the window on every block,
and only the ~65 ms around something that stayed up for 5 ms or longer is
cut out and handed to the full test in ``tetra_phy``.  Listening is no longer
interrupted by analysis, retuning or settling, a burst can no longer be cut
in half by the edge of a capture, and the heavy tests only run where there
is something to test.

Everything here is NumPy and hardware free, like ``tetra_phy``: the backend
owns the radio, this module only sees samples.
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np


def _u8_pairs():
    """Every (I, Q) byte pair as complex64, indexed by the pair read as a uint16."""
    v = np.arange(65536, dtype=np.uint32)
    lo, hi = (v & 0xFF).astype(np.float32), (v >> 8).astype(np.float32)
    if np.little_endian:
        i, q = lo, hi
    else:
        i, q = hi, lo
    return ((i - 127.5) + 1j * (q - 127.5)).astype(np.complex64)


# One gather instead of four passes over every block: the conversion was a
# fifth of what listening cost.
_U8_PAIR = _u8_pairs()


class StreamRing:
    """Raw u8 IQ from the async reader, addressed by absolute sample index.

    Written by librtlsdr's callback thread, read by the scan thread. The
    newest ``capacity`` samples are kept; asking for anything older returns
    None rather than samples that have already been overwritten.

    ``blank`` marks samples the panel may have been writing over (see
    SDRBackend.claim_panel): they are delivered, but the trigger ignores
    them.
    """

    def __init__(self, capacity_samples):
        n = 1 << int(math.ceil(math.log2(max(1024, int(capacity_samples)))))
        self.capacity = n                      # samples
        self.buf = np.zeros(2 * n, dtype=np.uint8)
        self.written = 0                       # samples ever written
        self.written_at = None                 # monotonic time of the last write
        self.blanks = []                       # [(start, end)] in samples
        self.arrivals = 0
        self._lock = threading.Lock()

    def write(self, data, blank=False):
        """Append raw interleaved u8 IQ (a bytes-like or uint8 array)."""
        a = np.frombuffer(data, dtype=np.uint8) if not isinstance(
            data, np.ndarray) else data.reshape(-1).view(np.uint8)
        a = a[:len(a) & ~1]
        n = len(a) // 2
        if n <= 0:
            return
        if n > self.capacity:
            a = a[-2 * self.capacity:]
            skip = n - self.capacity
            n = self.capacity
        else:
            skip = 0
        with self._lock:
            start = self.written + skip
            pos = (start % self.capacity) * 2
            first = min(len(a), len(self.buf) - pos)
            self.buf[pos:pos + first] = a[:first]
            if first < len(a):
                self.buf[:len(a) - first] = a[first:]
            if blank:
                self.blanks.append((start, start + n))
            self.written = start + n
            self.written_at = time.monotonic()
            self.arrivals += 1
            # Forget blanks nobody can read any more.
            oldest = self.written - self.capacity
            if self.blanks and self.blanks[0][1] < oldest:
                self.blanks = [b for b in self.blanks if b[1] >= oldest]

    def blank_at(self, t, before_s, after_s, sample_rate):
        """Mark what was on the air from ``t - before_s`` to ``t + after_s``.

        A USB buffer is delivered when it is full, so the newest sample in
        the ring is about as old as its delivery: sample time maps onto the
        ring from the last write by the sample rate. Exact to a few ms, which
        is what lets a panel frame cost its ~0.15 s on the wire instead of
        whole buffers either side of it.
        """
        with self._lock:
            if self.written_at is None:
                return None
            s = self.written + int((float(t) - self.written_at) * float(sample_rate))
            a = s - int(float(before_s) * float(sample_rate))
            b = s + int(float(after_s) * float(sample_rate))
            self.blanks.append((a, b))
            return (a, b)

    def oldest(self):
        return max(0, self.written - self.capacity)

    def read(self, start, end):
        """Complex64 samples ``[start, end)``, or None if not (or no longer) here."""
        start = int(start); end = int(end)
        with self._lock:
            if start < self.oldest() or end > self.written or end <= start:
                return None
            n = end - start
            pos = (start % self.capacity) * 2
            first = min(2 * n, len(self.buf) - pos)
            raw = np.empty(2 * n, dtype=np.uint8)
            raw[:first] = self.buf[pos:pos + first]
            if first < 2 * n:
                raw[first:] = self.buf[:2 * n - first]
        return _U8_PAIR[raw.view(np.uint16)]

    def blanked(self, start, end):
        """Blank ranges overlapping ``[start, end)``."""
        with self._lock:
            return [(a, b) for a, b in self.blanks if a < end and b > start]


class StreamTrigger:
    """Which channels of a streaming window just carried a burst.

    Per channel and per FFT row (1024 points at 2.016 MS/s is 0.51 ms) the
    power over the central 17.7 kHz of the 25 kHz channel is measured,
    smoothed over ``smooth_rows`` and compared with that channel's own noise
    floor. A channel that stays ``trigger_db`` above it for ``min_s`` -- gaps
    of up to ``gap_rows`` bridged, because a pi/4-DQPSK envelope is not
    constant -- is reported, with where the burst began and ended.

    The floor is per channel, learned from rows that are not above it, and
    slowly: one strong neighbour or the tuner's own ripple must not decide
    what counts as loud everywhere. A carrier that never goes away (a spur,
    the panel's own SPI line) is absorbed into its floor after
    ``absorb_s``, so it triggers once rather than for ever; a TETRA uplink is
    off for three slots of every four, which is what keeps a held call from
    being absorbed.

    Noise is not a burst: at 3.5 dB and 2 ms of smoothing a row of empty
    channel sits about seven standard deviations below the threshold, and it
    would have to stay there for ten rows in a row.
    """

    def __init__(self, sample_rate, offsets_hz, nfft=1024, trigger_db=3.5,
                 min_s=0.005, max_s=0.060, smooth_rows=4, gap_rows=3,
                 holdoff_s=0.30, absorb_s=2.0, floor_tau_s=2.0,
                 warmup_rows=64, half_width_hz=9000.0, row_stride=1):
        self.sr = float(sample_rate)
        self.offsets = np.asarray([float(o) for o in offsets_hz], dtype=np.float64)
        self.nfft = int(nfft)
        # A row is ``hop`` samples, of which the first ``nfft`` are measured.
        # A stride of 2 halves the FFT work -- the part that decides whether
        # a Pi 3 B+ keeps up -- and a 5 ms burst still spans five rows.
        self.hop = self.nfft * max(1, int(row_stride))
        self.row_s = self.hop / self.sr
        self.trigger_db = float(trigger_db)
        self.min_rows = max(1, int(round(float(min_s) / self.row_s)))
        self.max_rows = max(self.min_rows + 1, int(round(float(max_s) / self.row_s)))
        self.smooth = max(1, int(smooth_rows))
        self.gap_rows = max(0, int(gap_rows))
        self.holdoff = int(round(float(holdoff_s) * self.sr))
        self.absorb_rows = max(self.max_rows + 1, int(round(float(absorb_s) / self.row_s)))
        self.alpha = 1.0 - math.exp(-self.row_s / max(1e-3, float(floor_tau_s)))
        self.warmup_rows = max(16, int(warmup_rows))
        self.win = np.hanning(self.nfft).astype(np.float32)
        bin_hz = self.sr / self.nfft
        half = max(1, int(round(float(half_width_hz) / bin_hz)))
        k0 = np.round(self.offsets / bin_hz).astype(np.int64)
        self.idx = (k0[:, None] + np.arange(-half, half + 1)[None, :]) % self.nfft
        nch = len(self.offsets)
        self.floor = None                       # dB, per channel
        self._warm = []
        self._carry = np.zeros(0, dtype=np.complex64)
        self._carry_lin = np.zeros((0, nch), dtype=np.float64)
        self.next_sample = None                 # absolute index of _carry[0]
        # Per channel: open run start row (absolute), last above row, emitted.
        self.run_start = np.full(nch, -1, dtype=np.int64)
        self.run_last = np.full(nch, -1, dtype=np.int64)
        self.run_emitted = np.zeros(nch, dtype=bool)
        self.quiet_until = np.zeros(nch, dtype=np.int64)
        self.rows_seen = 0
        self.triggers = 0

    def reset_floor(self):
        """Learn the floor again, e.g. after the gain has changed."""
        self.floor = None
        self._warm = []
        self.run_start[:] = -1
        self.run_last[:] = -1
        self.run_emitted[:] = False
        self._carry_lin = self._carry_lin[:0]

    def _row_power(self, iq):
        rows = len(iq) // self.hop
        block = iq[:rows * self.hop].reshape(rows, self.hop)[:, :self.nfft] * self.win
        x = np.fft.fft(block, axis=1)[:, self.idx]
        return (x.real ** 2 + x.imag ** 2).sum(axis=2)   # rows x channels, linear

    def feed(self, iq, start_sample, blanks=()):
        """Measure ``iq`` (complex, starting at absolute ``start_sample``).

        Returns ``[(channel_index, start_sample, end_sample)]`` for every
        burst that ended in this block, or reached ``max_s`` while still on.
        """
        iq = np.asarray(iq, dtype=np.complex64)
        start_sample = int(start_sample)
        if self.next_sample is None or start_sample != self.next_sample + len(self._carry):
            # A gap in the stream: start the rows afresh from here.
            self._carry = np.zeros(0, dtype=np.complex64)
            self.next_sample = start_sample
            self._carry_lin = self._carry_lin[:0]
            self.run_start[:] = -1
            self.run_last[:] = -1
            self.run_emitted[:] = False
        data = np.concatenate((self._carry, iq)) if len(self._carry) else iq
        rows = len(data) // self.hop
        if rows <= 0:
            self._carry = data
            return []
        base = int(self.next_sample)            # absolute sample of row 0
        lin = self._row_power(data[:rows * self.hop])
        self._carry = data[rows * self.hop:]
        self.next_sample = base + rows * self.hop
        # Rows are addressed by their first absolute sample: base + r*nfft.
        starts = base + np.arange(rows, dtype=np.int64) * self.hop

        # Rows the panel may have written over say nothing either way.
        bad = np.zeros(rows, dtype=bool)
        for a, b in blanks:
            bad |= (starts < int(b)) & (starts + self.hop > int(a))

        # Box-smooth along time, continuing across blocks.
        prev = self._carry_lin
        cat = np.concatenate((prev, lin)) if len(prev) else lin
        if self.smooth > 1:
            cs = np.cumsum(np.concatenate((np.zeros((1, cat.shape[1])), cat)), axis=0)
            k = self.smooth
            sm = np.empty_like(cat)
            for i in range(min(k - 1, len(cat))):
                sm[i] = (cs[i + 1] - cs[0]) / float(i + 1)
            if len(cat) >= k:
                sm[k - 1:] = (cs[k:] - cs[:-k]) / float(k)
            sm = sm[len(prev):]
        else:
            sm = lin
        self._carry_lin = cat[-(self.smooth - 1):] if self.smooth > 1 else cat[:0]
        db = 10.0 * np.log10(np.maximum(sm, 1e-20))
        self.rows_seen += rows

        if self.floor is None:
            good = db[~bad]
            if len(good):
                self._warm.append(good)
            have = sum(len(w) for w in self._warm)
            if have < self.warmup_rows:
                return []
            # Learnt: and the block it was learnt from is judged against it
            # too, or a burst in a long first block would go unseen.
            self.floor = np.median(np.concatenate(self._warm), axis=0)
            self._warm = []

        above = (db > (self.floor[None, :] + self.trigger_db)) & ~bad[:, None]

        # The floor learns from what is not above it, a little per row.
        quiet = ~above & ~bad[:, None]
        cnt = quiet.sum(axis=0)
        if np.any(cnt):
            mean = np.where(cnt > 0, np.sum(np.where(quiet, db, 0.0), axis=0)
                            / np.maximum(cnt, 1), self.floor)
            w = 1.0 - (1.0 - self.alpha) ** cnt
            self.floor = self.floor + w * (mean - self.floor)

        out = []
        open_runs = self.run_start >= 0
        active = np.flatnonzero(np.any(above, axis=0) | open_runs)
        for c in active:
            col = above[:, c]
            rs, rl = int(self.run_start[c]), int(self.run_last[c])
            emitted = bool(self.run_emitted[c])
            for r in range(rows):
                s = int(starts[r])
                if col[r]:
                    if rs < 0:
                        if s < int(self.quiet_until[c]):
                            continue
                        rs, emitted = s, False
                    rl = s
                    length = (rl - rs) // self.hop + 1
                    if not emitted and length >= self.max_rows:
                        out.append((int(c), rs, rl + self.hop))
                        emitted = True
                        self.quiet_until[c] = rl + self.hop + self.holdoff
                    if length >= self.absorb_rows:
                        # Never goes away: a carrier, not a burst. The rest of
                        # this block is judged against the new floor too.
                        self.floor[c] = float(db[r, c])
                        rs, rl, emitted = -1, -1, False
                        col = col.copy()
                        col[r + 1:] = ((db[r + 1:, c] > self.floor[c] + self.trigger_db)
                                       & ~bad[r + 1:])
                elif rs >= 0 and (s - rl) // self.hop > self.gap_rows:
                    length = (rl - rs) // self.hop + 1
                    if not emitted and length >= self.min_rows:
                        out.append((int(c), rs, rl + self.hop))
                        self.quiet_until[c] = rl + self.hop + self.holdoff
                    rs, rl, emitted = -1, -1, False
            self.run_start[c], self.run_last[c] = rs, rl
            self.run_emitted[c] = emitted
        self.triggers += len(out)
        return out


def plan_windows(groups, place):
    """Cover each group of frequencies with as few windows as it takes.

    ``place(freqs)`` is a dwell planner (``plan_dwell`` with its limits
    bound): given a list it returns ``(centre, [(freq, offset), ...])`` for a
    window anchored on the first entry. Groups are covered in order, and a
    later group only adds windows for what the earlier ones left out.
    Returns ``[(centre, group_index)]``.
    """
    out = []
    covered = set()
    for gi, group in enumerate(groups):
        todo = [float(f) for f in group if int(round(float(f))) not in covered]
        while todo:
            centre, members = place(todo)
            if centre is None or not members:
                break
            got = {int(round(f)) for f, _ in members}
            out.append((float(centre), gi))
            covered |= got
            rest = [f for f in todo if int(round(f)) not in got]
            if len(rest) == len(todo):
                break
            todo = rest
    return out
