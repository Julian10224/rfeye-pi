"""Live C2000 air for the simulator: base stations and vehicles, as IQ.

Everything is synthesised for whatever frequency the fake dongle is tuned to,
at 2.016 MS/s, by the wall clock -- so the backend sees what a real RTL-SDR
would hand it: noise, base-station downlinks in 390-395 MHz, and vehicles in
380-385 MHz.

It follows how TETRA is used, because that decides what a receiver can hear:

  * A site has one **main carrier**, on the air all the time; its timeslot 1
    is the cell's main control channel (MCCH). Its other carriers are traffic
    carriers, on the air only while they carry a call.
  * A terminal belongs to a **cell**. Everything it starts -- registration,
    a status message, the request for a call -- begins with a random-access
    burst on the uplink of that cell's control channel (10 MHz below the
    main carrier). A call then runs on a traffic carrier's uplink.

And it follows what the air does (``channel.py``): every vehicle reaches the
receiver through multipath and Doppler fading of its own -- by default the
TETRA TU50 urban model at its speed, or line of sight when it passes close --
with its own small frequency error; each site through a static indoor channel;
and the RTL-SDR adds its tuner error, phase noise, I/Q imbalance and DC offset,
fitted to real carriers by calibrate_channel.py.

The air can run in the simulator's own process (``Air``), or in a process of
its own (``serve`` / ``AirClient``) -- which is what a test at Raspberry Pi
speed needs, so that synthesising the air does not come out of the emulated
Pi's time.
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time

import numpy as np

import tetra_sim as sim
from channel import Path, Receiver
from tetra_detector import carrier_number

SR = 2_016_000
SPACING = 25_000.0
NOISE = 6.0                     # ADC counts, complex rms
UP_LO, UP_HI = 380_000_000.0, 385_000_000.0
DUPLEX = 10_000_000.0
FRAME_S = 0.0566667
# Roughly the weakest burst the waveform test recognises (a slot, 7-8 dB).
AUDIBLE_DB = 8.0
HERE = os.path.dirname(os.path.abspath(__file__))


def receiver_params():
    """The fitted receiver (calibrate_channel.py), or a plain V4 if not fitted."""
    try:
        with open(os.path.join(HERE, "receiver.json")) as fh:
            return json.load(fh)
    except Exception:
        return {"phase_rms_deg": 4.0, "iq_gain_db": 0.3}


def raster(f, lo):
    return lo + 12_500.0 + round((float(f) - lo - 12_500.0) / SPACING) * SPACING


def channel_freq(ch):
    """Uplink frequency of C2000 carrier number ``ch`` (3600-3799)."""
    return 300e6 + 12_500.0 + int(ch) * SPACING - DUPLEX


class Site:
    def __init__(self, key, name, main, traffic, db, on, seed):
        self.key, self.name = key, name
        self.main = float(main)                  # downlink, continuous: the MCCH
        self.traffic = [float(f) for f in traffic]
        self.db, self.on = float(db), bool(on)
        self.busy = {}                           # traffic downlink -> vehicle id
        rp = receiver_params()
        self.path = Path("static", speed_kmh=0.0, carrier_hz=self.main, seed=seed,
                         taps=rp.get("site_taps"), pa_a3=rp.get("pa_a3", 0.0))

    @property
    def control_up(self):
        return self.main - DUPLEX


class Vehicle:
    """One terminal. ``snr_db`` is how loud it is here; distance, in effect."""

    _ids = 0

    def __init__(self, name, cell, freq_hz, snr_db=18.0, mode="bursts", every_s=4.0,
                 model="TU50", speed_kmh=50.0, k_db=None):
        Vehicle._ids += 1
        self.id = Vehicle._ids
        self.name = name
        self.cell = cell                  # 'local' | 'neighbour' | 'random'
        self.own_freq = float(freq_hz)    # only used for a 'random' cell
        self.snr_db = float(snr_db)
        self.mode = mode                  # 'bursts' | 'call' | 'silent'
        self.every_s = float(every_s)
        self.next_burst = 0.0
        self.call_since = None
        self.call_freq = None
        self.prev_call_freq = None
        self.drive = None                 # (t0, duration, peak_db, floor_db)
        self.sent = 0
        self.audible = 0                  # bursts strong enough to recognise
        self.last_tx = 0.0
        self.first_tx = None
        self.first_audible = None
        self.model = model
        # A terminal locks to its base station within about +-100 Hz.
        rp = receiver_params()
        self.path = Path(model, speed_kmh=speed_kmh, carrier_hz=freq_hz, k_db=k_db,
                         tx_offset_hz=random.uniform(-100.0, 100.0), seed=1000 + self.id,
                         pa_a3=rp.get("pa_a3", 0.0),
                         taps=rp.get("harsh_taps") if model == "harsh" else None)

    def level(self, t):
        if self.drive is None:
            return self.snr_db
        t0, dur, peak, floor = self.drive
        x = (t - t0) / dur
        if x >= 1.0:
            self.drive = None
            self.snr_db = floor
            return floor
        # Closest half way; the level falls with distance, 20 log10 r.
        r = math.hypot(1.0, (x - 0.5) * 2.0 * 20.0)
        self.snr_db = max(floor, peak - 20.0 * math.log10(r))
        return self.snr_db


class Air:
    """The whole simulated band. Thread safe: the panel edits, the dongle reads."""

    def __init__(self, seed=1, realistic=True):
        self.lock = threading.Lock()
        self.rng = np.random.default_rng(seed)
        self.t0 = time.monotonic()
        self.vehicles = []
        self.sites = {
            "local": Site("local", "eigen site", 391_187_500.0,
                          [391_462_500.0, 392_012_500.0], 22.0, True, seed=21),
            "neighbour": Site("neighbour", "buursite", 393_637_500.0,
                              [394_112_500.0], 12.0, False, seed=22),
        }
        self.realistic = bool(realistic)
        rp = receiver_params()
        self.receiver = Receiver(tuner_error_hz=220.0, dc=(0.6, -0.4), seed=seed,
                                 phase_rms_deg=rp.get("phase_rms_deg", 0.0),
                                 phase_hi_hz=rp.get("phase_hi_hz", 30000.0),
                                 iq_gain_db=rp.get("iq_gain_db", 0.0),
                                 iq_phase_deg=2.0 * rp.get("iq_gain_db", 0.0))
        self.events = []                  # (t_start, template, freq, vehicle)
        self.timers = []                  # (simulator time, vid, mode to return to)
        self.seen_at = {}                 # vid -> (burst #, audible burst #, seconds)
        n = int(2.0 * SR)
        self._noise = ((self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
                       * (NOISE / math.sqrt(2))).astype(np.complex64)
        print("simulator: making TETRA waveforms...", flush=True)
        self._burst1 = sim.control_burst(0.024, SR, seed=3, n_slots=1, start_frac=0.1)
        self._burst05 = sim.control_burst(0.024, SR, seed=4, n_slots=0.5, start_frac=0.1)
        # Whole frames, so the loops join without a jump in the slot rhythm.
        dur = 36 * FRAME_S
        self._call = sim.tetra_carrier(dur, SR, role="UPLINK", seed=7)
        self._main = sim.tetra_carrier(dur, SR, role="DOWNLINK", seed=8)
        # A traffic carrier carrying a call: two of its four slots busy.
        self._traffic = sim.tetra_carrier(dur, SR, role="UPLINK", seed=9, slot_pattern=(0, 2))
        self._chan = NOISE * math.sqrt(SPACING / SR)

    def now(self):
        return time.monotonic() - self.t0

    def control_freq(self, v):
        """Where this vehicle's first message goes."""
        if v.cell in self.sites:
            return self.sites[v.cell].control_up
        return v.own_freq

    def get(self, vid):
        return next((v for v in self.vehicles if v.id == vid), None)

    # -- everything the panel and the tests can do, as one entry point ------
    def command(self, op, *args):
        with self.lock:
            return getattr(self, "_cmd_" + op)(*args)

    def _cmd_add(self, spec):
        ch = spec.get("ch")
        cell = spec.get("cell", "local")
        if ch is not None:
            cell, f = "random", channel_freq(ch)
        else:
            f = raster(random.uniform(UP_LO + 50e3, UP_HI - 50e3), UP_LO)
        if cell in self.sites:
            f = self.sites[cell].control_up
        v = Vehicle(spec.get("name") or "voertuig %d" % (Vehicle._ids + 1), cell, f,
                    spec.get("db", 18.0), spec.get("mode", "bursts"), spec.get("every", 4.0),
                    model=spec.get("model", "TU50"), speed_kmh=spec.get("speed", 50.0),
                    k_db=spec.get("k_db"))
        v.next_burst = self.now() + spec.get("first_after", random.uniform(0.3, 1.0))
        self.vehicles.append(v)
        if spec.get("drive"):
            v.drive = (self.now(),) + tuple(spec["drive"])
        return v.id

    def _cmd_remove(self, vid):
        v = self.get(vid)
        if v is not None:
            self._end_call(v)
        self.vehicles = [v for v in self.vehicles if v.id != vid]

    def _cmd_clear(self):
        for v in list(self.vehicles):
            self._cmd_remove(v.id)

    def _cmd_burst(self, vid, subslot=False):
        v = self.get(vid)
        if v is not None:
            self._emit(v, self.now() + 0.05, subslot)

    def _cmd_mode(self, vid, mode):
        v = self.get(vid)
        if v is not None:
            v.mode = mode

    def _cmd_toggle_call(self, vid):
        v = self.get(vid)
        if v is not None:
            v.mode = "bursts" if v.mode == "call" else "call"

    def _cmd_ptt(self, vid, seconds):
        v = self.get(vid)
        if v is not None:
            back = v.mode if v.mode != "call" else "bursts"
            v.mode = "call"
            self.timers.append((self.now() + float(seconds), vid, back))

    def _cmd_drive(self, vid, seconds=30.0, peak_db=30.0, floor_db=-5.0):
        v = self.get(vid)
        if v is not None:
            v.drive = (self.now(), float(seconds), float(peak_db), float(floor_db))
            if v.mode == "silent":
                v.mode = "bursts"

    def _cmd_snr(self, vid, db):
        v = self.get(vid)
        if v is not None:
            v.drive = None
            v.snr_db = float(db)

    def _cmd_site(self, key, on, db):
        s = self.sites.get(key)
        if s is not None:
            s.on, s.db = bool(on), float(db)

    def _cmd_scenario(self, key):
        if key == "clear":
            self._cmd_clear()
        elif key in ("police_driveby", "police_neighbour"):
            cell = "local" if key == "police_driveby" else "neighbour"
            if cell == "neighbour":
                self.sites["neighbour"].on = True
            self._cmd_add({"name": "politie", "cell": cell, "db": -5.0, "every": 2.5,
                           "drive": (30.0, 32.0, -5.0)})
        elif key == "ambulance_call":
            vid = self._cmd_add({"name": "ambulance", "cell": "local", "db": 20.0,
                                 "every": 5.0, "model": "static", "speed": 0.0, "k_db": 6.0})
            self._cmd_ptt(vid, 4.0)
        elif key == "fire_bursts":
            self._cmd_add({"name": "brandweer", "cell": "random", "db": 14.0, "every": 3.0})

    def _cmd_verified(self, freqs):
        """RF Eye verified these channels just now: note who it first saw when."""
        ok = {int(round(f)) for f in freqs}
        t = self.now()
        for v in self.vehicles:
            if v.id in self.seen_at:
                continue
            mine = {int(round(self.control_freq(v)))}
            for f in (v.call_freq, v.prev_call_freq):
                if f:
                    mine.add(int(round(f)))
            if mine & ok:
                ahead = [e for e in self.events if e[3] is v and e[0] > t]
                loud = sum(1 for e in ahead if v.level(e[0]) >= AUDIBLE_DB)
                # (burst #, audible burst #, s after the first audible burst)
                self.seen_at[v.id] = (max(1, v.sent - len(ahead)), max(1, v.audible - loud),
                                      round(t - (v.first_audible or v.first_tx or t), 2))

    def _cmd_snapshot(self):
        now = self.now()
        return {
            "sites": {k: {"on": s.on, "db": s.db, "name": s.name,
                          "main": carrier_number(s.main),
                          "traffic": [carrier_number(f) for f in s.traffic]}
                      for k, s in self.sites.items()},
            "vehicles": [{
                "id": v.id, "name": v.name, "cell": v.cell, "model": v.model,
                "ch": carrier_number(self.control_freq(v)),
                "call_ch": (carrier_number(v.call_freq or v.prev_call_freq)
                            if (v.call_freq or v.prev_call_freq) else None),
                "mhz": self.control_freq(v) / 1e6, "db": round(v.level(now), 1),
                "mode": v.mode, "every": v.every_s, "sent": v.sent,
                "audible": v.audible, "first_tx": v.first_tx,
                "seen_at": self.seen_at.get(v.id),
                "driving": v.drive is not None,
                "tx": (v.mode == "call") or (now - v.last_tx < 0.4),
            } for v in self.vehicles],
        }

    # -- scheduling --------------------------------------------------------
    def _emit(self, v, t, subslot=False):
        self.events.append((t, self._burst05 if subslot else self._burst1,
                            self.control_freq(v), v))
        v.sent += 1
        if v.level(t) >= AUDIBLE_DB:
            v.audible += 1
            if v.first_audible is None:
                v.first_audible = t
        v.last_tx = t
        if v.first_tx is None:
            v.first_tx = t

    def _start_call(self, v, t):
        # The request goes on the control channel; the call on a traffic carrier.
        self._emit(v, t)
        site = self.sites.get(v.cell)
        if site is not None and site.traffic:
            free = [f for f in site.traffic if f not in site.busy] or site.traffic
            down = random.choice(free)
            site.busy[down] = v.id
            v.call_freq = down - DUPLEX
        else:
            v.call_freq = v.own_freq
        v.call_since = t + 0.3

    def _end_call(self, v):
        site = self.sites.get(v.cell)
        if site is not None:
            site.busy = {f: i for f, i in site.busy.items() if i != v.id}
        if v.call_freq:
            # Its last bursts can still be in a capture being analysed.
            v.prev_call_freq = v.call_freq
        v.call_since = None
        v.call_freq = None

    def _schedule(self, until):
        now = self.now()
        for item in list(self.timers):
            if now >= item[0]:
                self.timers.remove(item)
                v = self.get(item[1])
                if v is not None and v.mode == "call":
                    v.mode = item[2]
        for v in self.vehicles:
            if v.mode == "call":
                if v.call_since is None:
                    self._start_call(v, now)
                v.last_tx = now
            elif v.call_since is not None:
                self._end_call(v)
            if v.mode == "bursts":
                while v.next_burst < until:
                    # Random access is a subslot; a status or the follow-up a slot.
                    self._emit(v, v.next_burst, subslot=random.random() < 0.5)
                    v.next_burst += v.every_s * random.uniform(0.7, 1.3)
            else:
                v.next_burst = max(v.next_burst, until)
        horizon = now - 5.0
        self.events = [e for e in self.events if e[0] > horizon]

    # -- what the dongle reads ---------------------------------------------
    def render(self, centre, t, n):
        """``n`` samples starting at simulator time ``t``, tuned to ``centre``."""
        with self.lock:
            self._schedule(t + n / SR + 0.1)
            if n < len(self._noise):
                k = int(self.rng.integers(0, len(self._noise) - n))
                x = self._noise[k:k + n].copy()
            else:
                x = ((self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
                     * (NOISE / math.sqrt(2))).astype(np.complex64)
            half = SR * 0.48
            tt = [None]
            real = self.realistic

            def add(sig, i0, j0, m, off, db, path=None, t_seg=0.0):
                if tt[0] is None:
                    tt[0] = t + np.arange(n, dtype=np.float64) / SR
                seg = sig[j0:j0 + m]
                if real and path is not None:
                    seg = path.apply(seg, t_seg, SR)
                amp = self._chan * 10.0 ** (db / 20.0)
                ph = np.exp(2j * np.pi * off * tt[0][i0:i0 + m]).astype(np.complex64)
                x[i0:i0 + m] += (seg * np.float32(amp)) * ph

            def loop(sig, phase, off, db, path=None):
                L = len(sig)
                s0 = int(phase) % L
                done = 0
                while done < n:
                    m = min(n - done, L - s0)
                    add(sig, done, s0, m, off, db, path, t + done / SR)
                    done += m
                    s0 = 0

            for site in self.sites.values():
                if not site.on:
                    continue
                if abs(site.main - centre) < half:
                    loop(self._main, t * SR + hash(site.key) % 9973, site.main - centre,
                         site.db, site.path)
                for f in site.busy:
                    if abs(f - centre) < half:
                        loop(self._traffic, t * SR + int(f) % 7919, f - centre, site.db,
                             site.path)
            for v in self.vehicles:
                if v.call_since is None or v.call_freq is None or t + n / SR < v.call_since:
                    continue
                off = v.call_freq - centre
                if abs(off) < half:
                    loop(self._call, (t - v.call_since) * SR, off, v.level(t), v.path)
            for (te, sig, f, v) in self.events:
                off = f - centre
                if abs(off) >= half:
                    continue
                a = int(round((te - t) * SR))
                b = a + len(sig)
                if b <= 0 or a >= n:
                    continue
                i0, j0 = max(0, a), max(0, -a)
                add(sig, i0, j0, min(n, b) - i0, off, v.level(te), v.path, te + j0 / SR)
        if self.realistic:
            x = self.receiver.apply(x, t, SR)
        return x


def to_u8(x):
    """What the RTL2832U hands over: 8-bit I/Q around 127.5, clipped."""
    out = np.empty(2 * len(x), np.uint8)
    out[0::2] = np.clip(np.round(x.real + 127.5), 0, 255)
    out[1::2] = np.clip(np.round(x.imag + 127.5), 0, 255)
    return out


def gain_scale(gain):
    try:
        return 10.0 ** ((float(gain) - 37.2) / 20.0)
    except Exception:
        return 1.0


# -- the air in a process of its own -------------------------------------
def serve(conn, seed=1, realistic=True):
    """Run ``Air`` here and answer requests on ``conn`` until told to stop."""
    air = Air(seed=seed, realistic=realistic)
    conn.send(("ready", air.t0))
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            break
        if msg[0] == "render":
            _, centre, t, n, gain = msg
            conn.send_bytes(to_u8(air.render(centre, t, n) * np.float32(gain_scale(gain))))
        elif msg[0] == "cmd":
            try:
                conn.send(("ok", air.command(msg[1], *msg[2])))
            except Exception as exc:
                conn.send(("err", repr(exc)))
        elif msg[0] == "quit":
            break


class AirClient:
    """``Air``'s command/render interface, answered by ``serve`` in another process."""

    def __init__(self, seed=1, realistic=True):
        import multiprocessing as mp
        self._conn, child = mp.Pipe()
        self._proc = mp.Process(target=serve, args=(child, seed, realistic), daemon=True)
        self._proc.start()
        tag, self.t0 = self._conn.recv()
        self._lock = threading.Lock()

    def now(self):
        return time.monotonic() - self.t0

    def command(self, op, *args):
        with self._lock:
            self._conn.send(("cmd", op, args))
            tag, val = self._conn.recv()
        if tag == "err":
            raise RuntimeError(val)
        return val

    def render_u8(self, centre, t, n, gain):
        with self._lock:
            self._conn.send(("render", float(centre), float(t), int(n), gain))
            return np.frombuffer(self._conn.recv_bytes(), dtype=np.uint8)

    def close(self):
        try:
            with self._lock:
                self._conn.send(("quit",))
        except Exception:
            pass
        self._proc.join(2.0)


class SimDongle:
    """Stands in for _PersistentRTL: the same calls, fed from ``Air`` or ``AirClient``."""

    lib_path = "SIMULATOR"
    lib_knows_model = True

    def __init__(self, air):
        self.air = air
        self.centre = 0.0
        self.rate_changed = False
        self.stream_rc = None
        self._t = None
        self._stop = threading.Event()
        self.gain = 37.2
        self.usb_lost = 0                 # samples librtlsdr would have dropped

    def configure(self, sr, ppm, gain):
        self.rate_changed = False
        self.gain = gain

    def tune(self, c):
        self.centre = float(c)

    def reset(self):
        pass

    def _u8(self, t, n):
        if hasattr(self.air, "render_u8"):
            return self.air.render_u8(self.centre, t, n, self.gain)
        return to_u8(self.air.render(self.centre, t, n) * np.float32(gain_scale(self.gain)))

    def read_complex(self, count, chunk=131072, abort=None):
        t = self.air.now()
        u8 = self._u8(t, int(count))
        end = time.monotonic() + count / SR
        while time.monotonic() < end:     # samples take real time to arrive
            if abort is not None and abort():
                raise RuntimeError("capture aborted")
            time.sleep(0.005)
        f = u8.astype(np.float32) - 127.5
        return (f[0::2] + 1j * f[1::2]).astype(np.complex64)

    def start_stream(self, on_bytes, buf_len=131072, buf_num=12):
        self._stop.clear()
        per = buf_len // 2

        def run():
            start_wall = time.monotonic()
            start_t = self.air.now()
            k = 0
            while not self._stop.is_set():
                due = int((time.monotonic() - start_wall) * SR / per)
                if due - k > buf_num:         # librtlsdr's buffers overflowed
                    self.usb_lost += (due - k - buf_num) * per
                    k = due - buf_num
                if k < due:
                    on_bytes(self._u8(start_t + k * per / SR, per))
                    k += 1
                else:
                    time.sleep(0.003)
            self.stream_rc = 0

        self._t = threading.Thread(target=run, name="sim-usb", daemon=True)
        self._t.start()

    def streaming(self):
        return self._t is not None and self._t.is_alive()

    def stop_stream(self, timeout=3.0):
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout)
        self._t = None
        return True

    def close(self):
        self.stop_stream()
