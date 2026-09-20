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
  * The likeliest uplink channels are known exactly: TETRA duplex spacing in
    this band is 10 MHz, so a verified downlink at 391.2375 MHz means handsets
    on that site transmit at 381.2375 MHz.  Those partners are swept first and
    are the only channels that confirm on a single hit.

    They are no longer the *whole* watch list, though.  Up to 0.9.34 they were,
    and on a real unit that meant two channels of the two hundred in
    380-385 MHz -- so a vehicle registered on another carrier or a neighbouring
    site was never looked at.  The band is swept in full since 0.9.35; a
    channel with no locked partner simply has to be heard twice.

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

    def heard_recently(self, now=None):
        """Has *any* proven carrier verified lately?

        The liveness condition behind the silence exemption in ``observe``,
        and it has to be collective. Hanging it on one designated carrier gave
        that channel a veto over every other: three quiet re-tests of it set
        ``hits = 0`` on all of them, and a site that was plainly still there --
        24 channels through the full waveform test, the best at 37.6 dB -- came
        back reporting nothing locked and everything demoted to a candidate.
        Silence is evidence of a deaf receiver only when it is everywhere at
        once; one quiet carrier is just a quiet carrier.
        """
        now = time.time() if now is None else float(now)
        window = max(30.0, float(self.cfg.get('site_silence_window_s', 180.0)))
        return any(now - float(e['last_ok']) <= window
                   for e in self.entries.values() if int(e['hits']) > 0)

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
                    # save() writes this; not reading it back meant every
                    # carrier came out of a restart with a lifetime score of
                    # zero, so anything ranking on it ranked on nothing.
                    'ok_total': int(v.get('ok_total', 0)),
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
        slowly unlock exactly the carriers a busy site puts calls on.

        That excuse only holds while the receiver is demonstrably still
        hearing the site. Once nothing verifies anywhere, silence stops being
        evidence of an idle carrier and becomes evidence of a deaf receiver,
        so the exemption is withdrawn -- see ``heard_recently``.
        """
        now = time.time() if now is None else float(now)
        f = int(round(float(freq_hz)))
        e = self.entries.get(f)
        if e is None:
            if not ok:
                return None
            e = {'hits': 0, 'misses': 0, 'quality': 0.0,
                 'last_ok': 0.0, 'first_seen': now, 'ok_total': 0}
            self.entries[f] = e
        need = max(2, int(self.cfg.get('site_lock_hits', 3)))
        if ok:
            # Capped just above the lock threshold. A deep reserve of hits
            # only means a carrier that has genuinely gone takes proportionally
            # longer to admit it.
            e['hits'] = min(int(e['hits']) + 1, need + 1)
            e['misses'] = 0
            e['last_ok'] = now
            # Lifetime successes, which is how the health carrier is chosen.
            # Unlike 'hits' this is not capped and not reset by a miss, so it
            # measures how consistently a carrier has been on the air rather
            # than how it is doing this minute.
            e['ok_total'] = int(e.get('ok_total', 0)) + 1
            e['quality'] = float(e['quality'] * 0.6 + float(quality) * 0.4)
        elif silent and int(e['hits']) > 0 and self.heard_recently(now):
            # Known carrier, nothing on air, and the site is still audible
            # elsewhere: no information either way.
            return e
        else:
            e['misses'] = int(e['misses']) + 1
            drop = max(1, int(self.cfg.get('site_unlock_misses', 4)))
            if e['misses'] >= drop:
                e['hits'] = 0
                e['misses'] = 0
                if now - float(e['last_ok']) > max(
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

    One dwell holds many 25 kHz channels -- +-600 kHz of them at 2.016 MS/s --
    so a group of nearby carriers is checked in a single capture.

    The RTL-SDR's DC spike sits exactly at the tuner centre, so no member may
    be closer to it than ``dc_guard_hz``.  The centre is therefore always
    parked ``dc_guard_hz`` to one side of some member, and of all those
    placements the one covering the most members wins.  In a contiguous run
    that costs the one channel next to the spike, which simply stays in the
    caller's list for the next dwell.  When the old one-sided placement was
    the only option -- a dwell barely wider than the group -- this finds it
    too.

    The **first** entry of ``freqs`` anchors the group: it is always a member.
    That is what lets a caller work through a long list: sorting the input and
    always starting from the lowest frequency would pin the window to one end
    of the band.  Ties go to the centre nearest the anchor.

    Returns ``(centre_hz, [(freq_hz, offset_hz), ...])``.
    """
    seq = [float(f) for f in freqs]
    if not seq:
        return None, []
    anchor = seq[0]
    lim = float(max_offset_hz)
    guard = float(dc_guard_hz)
    eps = 1e-6
    near = sorted({f for f in seq if abs(f - anchor) <= 2.0 * lim + eps})

    def fits(f, c):
        return guard - eps <= abs(f - c) <= lim + eps

    # Candidate tuner placements.  ``f +/- guard`` for every pending channel
    # keeps the DC spike off a channel under test; ``anchor +/- lim`` puts the
    # anchor on the very edge of the window instead, which is the placement
    # that lets a channel left behind by an earlier dwell bring company.
    #
    # Without those last two, the pass alternated 47 channels and *one*: a
    # full-width dwell always leaves the channel beside the spike behind, that
    # channel then anchors the next dwell, and every centre offered by a still
    # pending channel missed it -- by 5 kHz, since the nearest pending channel
    # above sat 605 kHz away and the window is 600. So half of every band pass
    # was a 0.52 s capture that carried one channel. Measured over 380-385 MHz:
    # sizes [47,1,47,1,47,1,47,1,8] in 9 dwells, against [48,25,48,25,48,6] in
    # 6 -- a third off every pass, and off the time to lock a site with it.
    candidates = []
    for f in near:
        candidates.append(f - guard)
        candidates.append(f + guard)
    candidates.append(anchor - lim)
    candidates.append(anchor + lim)

    best = None
    for c in candidates:
        if not fits(anchor, c):
            continue
        group = [g for g in near if fits(g, c)]
        key = (len(group), -abs(c - anchor))
        if best is None or key > best[0]:
            best = (key, c, group)
    if best is None:
        centre, group = anchor + guard, [anchor]
    else:
        _, centre, group = best
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

    def confirmed_channels(self):
        """Uplink channels whose alert is already up.

        The follow-up exists to fetch the *second* hit while the handset is
        still keyed. Once a channel is confirmed it has nothing left to prove,
        and following it further only costs the rest of the band its turn --
        which, sweeping two hundred channels, is the whole band rather than the
        two partners it used to be.
        """
        return {int(round(float(r.freq_hz))) for r in self.peaks} if self.confirmed else set()

    def update(self, verified, watched=(), now=None, trusted=()):
        """``verified`` are the PhyResults that passed; ``watched`` is every
        channel actually examined in this dwell, hit or not.

        ``trusted`` are the uplink channels that belong to a base station this
        device verified itself -- the duplex partners of its locked downlinks.
        A hit there is corroborated by the site lock; a hit on one of the other
        ~198 channels of the band is not, so it has to come twice.
        """
        now = time.time() if now is None else float(now)
        # Sensitive mode confirms on a single verified hit. A control burst is
        # a one-off -- it will not come round twice on the same channel -- so
        # requiring two hits would silently make it unconfirmable, which is the
        # whole failure this mode exists to fix. Every hit reaching here has
        # already passed the full shape and modulation verification, so one is
        # real evidence; the trade is that a single freak pass can now alert.
        #
        # That trade was priced for the two or three partner channels a locked
        # site names. Since the whole 380-385 MHz band is swept there are two
        # orders of magnitude more channels for a freak pass to land on, so
        # outside the partners the second hit is required again. The follow-up
        # in the backend is what keeps that affordable: one verified hit parks
        # the next dwells back on that window, so the second look costs about a
        # second rather than a whole sweep.
        trust = {int(round(float(f))) for f in trusted}
        if bool(self.cfg.get('uplink_sensitive', False)):
            need_trusted = 1
            need_unknown = max(1, int(self.cfg.get('uplink_unknown_confirm_dwells', 2)))
        else:
            need_trusted = max(1, int(self.cfg.get('uplink_confirm_dwells', 2)))
            need_unknown = need_trusted
        need = max(need_trusted, need_unknown)
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
                     if len(self.state.get(f, {}).get('hits', []))
                     >= (need_trusted if f in trust else need_unknown)]

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
