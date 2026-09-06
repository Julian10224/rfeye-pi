"""C2000 decision layer: network lock, uplink alarm, dwell planning.

``tetra_phy`` answers one question about one channel in one capture: is this
the ETSI TETRA waveform?  This module turns that into the product behaviour.

The central idea of detector profile v8 is that the network comes first.

C2000 base stations transmit continuously in 390-395 MHz.  They are the only
part of the network that is always on, so they are also the only part that can
be verified at leisure, repeatedly, to a very high standard.  Once a downlink
carrier has been confirmed as real TETRA several times over, two useful things
follow:

  * The device knows it is inside C2000 coverage at all.  Without a verified
    base station there is nothing to be near, so no alert can be correct, and
    v8 stays silent instead of guessing.  Profile v7 had no such anchor, which
    is why it alarmed in places where no C2000 traffic was possible.
  * Every uplink channel worth watching is known exactly: TETRA duplex spacing
    in this band is 10 MHz, so a verified downlink at 391.2375 MHz means
    handsets on that site transmit at 381.2375 MHz.  The uplink search stops
    being a blind sweep of 200 channels and becomes a short watch list.

An alert then means: a handset that is physically near this receiver is
transmitting on a carrier belonging to a base station this device has
independently verified.  That is a much stronger statement than "something
changed in the band".

A note on what this can and cannot tell you.  C2000 carries police, ambulance,
fire and the KMar on one shared, encrypted (TEA2) network.  Nothing in the RF
layer identifies the service, the unit or the user, and this code makes no
attempt to decode traffic -- it measures modulation structure only.  So a
confirmed alert means "an emergency services radio is transmitting nearby",
not specifically "police".
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from tetra_phy import CHANNEL_SPACING_HZ, clamp

STATE_SCHEMA = 'rfeye-c2000-sites-v1'


def raster_snap(freq_hz, band_start_hz, raster_offset_hz=12500.0,
                spacing_hz=CHANNEL_SPACING_HZ):
    """Snap a frequency onto the ETSI TETRA channel raster for this band.

    C2000 carriers sit on ``band_start + 12.5 kHz + N * 25 kHz``.  Rounding an
    absolute frequency to a plain 25 kHz multiple instead would land halfway
    between two real carriers.
    """
    base = float(band_start_hz) + float(raster_offset_hz)
    step = max(1.0, float(spacing_hz))
    n = math.floor((float(freq_hz) - base) / step + 0.5)
    return base + n * step


class SiteRegistry:
    """Verified C2000 downlink carriers, persisted across restarts.

    Locking is deliberately slow and unlocking is deliberately slower.  A
    carrier must verify as TETRA on several separate dwells before it counts,
    which costs a minute or so on first power-up but means the anchor is not
    something a single lucky capture can create.  Losing it takes sustained
    failure, so driving through a tunnel does not throw the network away.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.entries = {}
        self.loaded_from_disk = False
        self._load()

    # -- persistence -------------------------------------------------------
    def _path(self):
        override = os.getenv('RFEYE_SITE_STATE')
        if override:
            return Path(override)
        return Path.home() / '.local' / 'state' / 'rfeye' / 'c2000-sites.json'

    def _load(self):
        if not bool(self.cfg.get('site_state_persist', True)):
            return False
        try:
            p = self._path()
            if not p.exists():
                return False
            data = json.loads(p.read_text())
            if data.get('schema') != STATE_SCHEMA:
                return False
            if int(data.get('detector_profile_version', 0)) != int(
                    self.cfg.get('detector_profile_version', 0)):
                return False
            max_age = max(1.0, float(self.cfg.get('site_lock_max_age_days', 21.0)))
            now = time.time()
            out = {}
            for k, v in (data.get('sites') or {}).items():
                last = float(v.get('last_ok', 0.0))
                if now - last > max_age * 86400.0:
                    continue
                out[int(k)] = {
                    'hits': int(v.get('hits', 0)),
                    'misses': int(v.get('misses', 0)),
                    'quality': float(v.get('quality', 0.0)),
                    'last_ok': last,
                    'first_seen': float(v.get('first_seen', last)),
                }
            if not out:
                return False
            self.entries = out
            self.loaded_from_disk = True
            return True
        except Exception:
            return False

    def save(self):
        if not bool(self.cfg.get('site_state_persist', True)):
            return
        try:
            p = self._path()
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {
                'schema': STATE_SCHEMA,
                'detector_profile_version': int(
                    self.cfg.get('detector_profile_version', 0)),
                'saved_at': time.time(),
                'sites': {str(f): dict(v) for f, v in self.entries.items()},
            }
            tmp = p.with_suffix('.tmp')
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
            tmp.replace(p)
        except Exception:
            pass

    # -- updates -----------------------------------------------------------
    def observe(self, freq_hz, ok, quality=0.0, now=None, silent=False):
        """Record one verification attempt on a downlink carrier.

        ``silent`` marks a channel that simply had nothing on it this time.
        For a discontinuous traffic carrier that is the normal idle state, and
        counting it against a carrier that has already proved itself would
        slowly unlock exactly the carriers a busy site puts calls on. Absence
        of transmission is not evidence that a carrier is not real.
        """
        now = time.time() if now is None else float(now)
        f = int(round(float(freq_hz)))
        e = self.entries.get(f)
        if e is None:
            if not ok:
                return None
            e = {'hits': 0, 'misses': 0, 'quality': 0.0,
                 'last_ok': 0.0, 'first_seen': now}
            self.entries[f] = e
        need = max(2, int(self.cfg.get('site_lock_hits', 3)))
        if ok:
            e['hits'] = min(int(e['hits']) + 1, need * 4)
            e['misses'] = 0
            e['last_ok'] = now
            e['quality'] = float(e['quality'] * 0.6 + float(quality) * 0.4)
        elif silent and int(e['hits']) > 0:
            # Known carrier, nothing on air: no information either way.
            return e
        else:
            e['misses'] = int(e['misses']) + 1
            drop = max(1, int(self.cfg.get('site_unlock_misses', 4)))
            if e['misses'] >= drop:
                # Decay rather than delete: a carrier that verified before is
                # worth re-checking sooner than a fresh one.
                e['hits'] = max(0, int(e['hits']) - 1)
                e['misses'] = 0
                if e['hits'] <= 0 and now - float(e['last_ok']) > max(
                        60.0, float(self.cfg.get('site_forget_s', 1800.0))):
                    self.entries.pop(f, None)
                    return None
        return e

    def locked(self, now=None):
        """Downlink carriers currently accepted as real C2000 base stations."""
        now = time.time() if now is None else float(now)
        need = max(2, int(self.cfg.get('site_lock_hits', 3)))
        stale = max(60.0, float(self.cfg.get('site_lock_stale_s', 3600.0)))
        out = []
        for f, e in self.entries.items():
            if int(e['hits']) >= need and now - float(e['last_ok']) <= stale:
                out.append({'freq_hz': float(f), 'quality': float(e['quality']),
                            'hits': int(e['hits']), 'last_ok': float(e['last_ok'])})
        out.sort(key=lambda x: (x['quality'], x['hits']), reverse=True)
        return out

    def candidates(self, now=None):
        """Known-but-not-yet-locked carriers, most promising first."""
        now = time.time() if now is None else float(now)
        need = max(2, int(self.cfg.get('site_lock_hits', 3)))
        out = [{'freq_hz': float(f), 'hits': int(e['hits']),
                'last_ok': float(e['last_ok'])}
               for f, e in self.entries.items() if int(e['hits']) < need]
        out.sort(key=lambda x: (x['hits'], x['last_ok']), reverse=True)
        return out

    def uplink_partners(self, now=None):
        """Uplink frequencies to watch, from the locked downlink carriers."""
        split = float(self.cfg.get('duplex_split_hz', 10_000_000.0))
        return [round(s['freq_hz'] - split) for s in self.locked(now)]


def plan_dwell(freqs, sample_rate, max_offset_hz=100_000.0,
               dc_guard_hz=20_000.0, spacing_hz=CHANNEL_SPACING_HZ):
    """Choose a tuner centre covering as many of ``freqs`` as possible.

    One 288 kHz dwell holds several 25 kHz channels, so a group of nearby
    carriers is checked in a single capture.

    The tuner is parked ``dc_guard_hz`` clear of the *highest* member rather
    than in the middle of the group.  The RTL-SDR's DC spike sits exactly at
    the tuner centre, and channels are on a 25 kHz grid, so a centred tuner
    lands on one of the very carriers being measured -- there is no gap in a
    contiguous run to hide in.  Parking outside the run is the only placement
    that keeps the spike off every channel, and it bounds the group to what
    still fits inside ``max_offset_hz``.

    The **first** entry of ``freqs`` anchors the group.  That is what lets a
    caller work through a long list: sorting the input and always starting
    from the lowest frequency would pin the window to one end of the band.

    Returns ``(centre_hz, [(freq_hz, offset_hz), ...])``.
    """
    seq = [float(f) for f in freqs]
    if not seq:
        return None, []
    ordered = sorted(set(seq))
    i = ordered.index(seq[0])
    room = max(0.0, float(max_offset_hz) - float(dc_guard_hz))
    lo = hi = i
    while True:
        grew = False
        if hi + 1 < len(ordered) and ordered[hi + 1] - ordered[lo] <= room:
            hi += 1
            grew = True
        if lo - 1 >= 0 and ordered[hi] - ordered[lo - 1] <= room:
            lo -= 1
            grew = True
        if not grew:
            break
    group = ordered[lo:hi + 1]
    centre = group[-1] + float(dc_guard_hz)
    return centre, [(f, f - centre) for f in group]


class UplinkAlarm:
    """Confirm/clear hysteresis over verified uplink transmissions.

    Every input here has already passed the full physical-layer verification,
    so this is not another chance to be convinced -- it only smooths the
    output, and guards against a single freak capture.

    Confirmation is counted in **visits to that channel**, not in wall-clock
    seconds.  How often a given uplink channel comes round depends on how many
    carriers the site runs and how many dwells they take, and a site with
    several carriers can easily leave more than a few seconds between two
    looks at the same one.  A seconds-based window silently became
    unsatisfiable in exactly those cases -- the busiest sites, where a
    detection matters most.  Counting visits makes the rule independent of
    cycle duration, hardware speed and watch-list length.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = {}
        self.confirmed = False
        self.level = 0.0
        self.last_hit = 0.0
        self.peaks = []
        self.streak = 0

    def reset(self):
        self.state = {}
        self.confirmed = False
        self.level = 0.0
        self.last_hit = 0.0
        self.peaks = []
        self.streak = 0

    def update(self, verified, watched=(), now=None):
        """``verified`` are the PhyResults that passed; ``watched`` is every
        channel actually examined in this dwell, hit or not."""
        now = time.time() if now is None else float(now)
        need = max(1, int(self.cfg.get('uplink_confirm_dwells', 2)))
        span = max(need, int(self.cfg.get('uplink_confirm_visits', 4)))
        hold = max(0.0, float(self.cfg.get('uplink_alert_hold_s', 12.0)))
        max_age = max(10.0, float(self.cfg.get('uplink_state_max_age_s', 90.0)))

        hits = {int(round(float(r.freq_hz))): r for r in verified}
        seen = {int(round(float(f))) for f in watched} | set(hits)
        for f in seen:
            st = self.state.get(f)
            if st is None or now - float(st.get('last', now)) > max_age:
                st = {'visits': 0, 'hits': [], 'quality': 0.0}
            st['visits'] += 1
            st['last'] = now
            if f in hits:
                st['hits'].append(st['visits'])
                st['quality'] = max(float(hits[f].quality),
                                    float(st['quality']) * 0.5
                                    + float(hits[f].quality) * 0.5)
            st['hits'] = [h for h in st['hits'] if st['visits'] - h < span]
            self.state[f] = st

        self.state = {f: st for f, st in self.state.items()
                      if now - float(st.get('last', 0.0)) <= max_age}

        qualified = [r for f, r in hits.items()
                     if len(self.state.get(f, {}).get('hits', [])) >= need]

        if qualified:
            self.confirmed = True
            self.last_hit = now
            self.streak = max(len(self.state[int(round(float(r.freq_hz)))]['hits'])
                              for r in qualified)
            self.peaks = sorted(qualified, key=lambda r: r.quality, reverse=True)
            self.level = clamp(max(r.quality for r in qualified))
        elif self.confirmed and now - self.last_hit <= hold:
            # Hold through the gap between transmissions, fading the bar so
            # the display shows the alert ageing rather than freezing.
            age = (now - self.last_hit) / max(hold, 1e-6)
            self.level = clamp(self.level * (1.0 - 0.35 * age))
        else:
            self.confirmed = False
            self.level = 0.0
            self.peaks = []
            self.streak = max([len(st.get('hits', []))
                               for st in self.state.values()], default=0)
        return self.confirmed, self.level, list(self.peaks)
