"""What a signal is doing over time, separate from what one capture measured.

``tetra_phy`` answers a question about one channel in one capture: is this the
ETSI TETRA waveform, and how strong is it.  That answer used to be thrown away
as soon as the next dwell started, which is why a frequency was the only
identity the detector had -- every look at a vehicle began from nothing and had
to prove the whole case again.

A ``RadioTrack`` is that missing identity.  It remembers when a channel was
first heard, when it was last heard, how strong it was, how often it has come
back and how far apart those returns were.  Three things follow from having it,
and none of them need any more signal processing:

  * the scan can spend its time where something actually is, and back off
    where nothing is;
  * the display can show how *close* a vehicle is (RF level) rather than how
    TETRA-like its waveform looked (PHY quality).  Those are different
    questions, and only one of them is what someone glances at the screen for;
  * a transmission that repeats can be recognised as repeating, which is most
    of what a vehicle that nobody is talking on ever offers.

Deliberately cheap: a handful of floats per channel and no arrays.  The whole
point is to stop paying for heavy analysis on channels whose own history
already says they are empty.
"""
from __future__ import annotations

import time

# What a track is doing right now, judged from how long ago it was last heard.
ACTIVE = 'ACTIVE'       # heard just now: worth watching closely
FADING = 'FADING'       # heard recently: worth checking back on
LOST = 'LOST'           # gone: kept only long enough to notice it return


class RadioTrack:
    """One channel's history.  All times are wall-clock seconds."""

    __slots__ = ('freq_hz', 'first_seen', 'last_seen', 'last_hit', 'hit_times',
                 'visits', 'hit_visits', 'snr_db', 'best_snr_db', 'quality',
                 'confirmed', 'confirmed_at', 'trusted')

    def __init__(self, freq_hz, now):
        self.freq_hz = float(freq_hz)
        self.first_seen = float(now)
        self.last_seen = float(now)
        self.last_hit = 0.0
        self.hit_times = []          # when this channel verified as TETRA
        self.visits = 0              # how often it has been looked at
        self.hit_visits = []         # which of those visits were hits
        self.snr_db = 0.0            # the level last measured
        self.best_snr_db = 0.0
        self.quality = 0.0           # how TETRA-like, which is not the level
        self.confirmed = False
        self.confirmed_at = 0.0
        self.trusted = False         # a locked site's duplex partner

    # -- history -----------------------------------------------------------
    def note_visit(self, now, snr_db=None):
        self.visits += 1
        self.last_seen = float(now)
        if snr_db is not None:
            self.snr_db = float(snr_db)

    def note_hit(self, now, snr_db, quality, span):
        self.last_hit = float(now)
        self.hit_times.append(float(now))
        if len(self.hit_times) > 8:
            del self.hit_times[:-8]
        self.hit_visits.append(self.visits)
        self.hit_visits = [v for v in self.hit_visits if self.visits - v < span]
        self.snr_db = float(snr_db)
        self.best_snr_db = max(self.best_snr_db, float(snr_db))
        # Smoothed, so one lucky capture does not set the bar height for the
        # next minute and one poor one does not empty it.
        self.quality = max(float(quality),
                           self.quality * 0.5 + float(quality) * 0.5)

    def forget_stale_hits(self, span):
        """Drop hits that are too many visits back to still count."""
        self.hit_visits = [v for v in self.hit_visits if self.visits - v < span]

    # -- what it is doing --------------------------------------------------
    def state(self, now, active_s=2.0, fading_s=5.0):
        if self.last_hit <= 0.0:
            return LOST
        gap = float(now) - self.last_hit
        if gap <= float(active_s):
            return ACTIVE
        if gap <= float(fading_s):
            return FADING
        return LOST

    def period_s(self):
        """Median gap between transmissions, or 0 when there is no pattern.

        A vehicle that reports in rather than talking produces a gap that
        repeats.  Knowing it is what lets the scan come back at about the right
        moment instead of guessing.
        """
        if len(self.hit_times) < 3:
            return 0.0
        gaps = sorted(b - a for a, b in zip(self.hit_times, self.hit_times[1:]))
        if not gaps:
            return 0.0
        mid = len(gaps) // 2
        median = gaps[mid] if len(gaps) % 2 else 0.5 * (gaps[mid - 1] + gaps[mid])
        return float(median) if 0.2 <= median <= 60.0 else 0.0

    def level(self, weak_db=8.0, strong_db=26.0):
        """0..1 from the measured RF level, for the bar on the screen.

        Not the PHY quality: that says how much the waveform looked like TETRA,
        which barely moves once a signal is decodable at all.  What someone
        glancing at the screen wants to know is whether the vehicle is getting
        closer, and that is the level.
        """
        span = max(1.0, float(strong_db) - float(weak_db))
        return max(0.0, min(1.0, (float(self.snr_db) - float(weak_db)) / span))

    def as_dict(self, now=None, weak_db=8.0, strong_db=26.0):
        now = time.time() if now is None else float(now)
        level = self.level(weak_db, strong_db)
        return {
            'freq_hz': float(self.freq_hz),
            'snr_db': float(self.snr_db),
            'rf_snr_db': float(self.snr_db),
            'best_snr_db': float(self.best_snr_db),
            # The bar is the RF level now; the waveform score is kept beside
            # it rather than driving it.
            'level': float(level),
            'signal_strength': float(level),
            'confidence': float(level),
            'quality': float(self.quality),
            'phy_quality': float(self.quality),
            'state': self.state(now),
            'hits': int(len(self.hit_times)),
            'period_s': float(self.period_s()),
            'age_s': float(now - self.first_seen),
            'since_hit_s': float(now - self.last_hit) if self.last_hit else -1.0,
            'confirmed': bool(self.confirmed),
            'trusted': bool(self.trusted),
            'band': 'MOBILE',
            'role': 'UPLINK',
            'reason': 'TETRA' if self.last_hit else 'TRACK',
            'last_seen': float(self.last_seen),
        }


class TrackRegistry:
    """Every uplink channel the receiver currently knows anything about."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.tracks = {}             # int(freq) -> RadioTrack

    @staticmethod
    def key(freq_hz):
        return int(round(float(freq_hz)))

    def get(self, freq_hz, now=None, create=True):
        now = time.time() if now is None else float(now)
        k = self.key(freq_hz)
        tr = self.tracks.get(k)
        if tr is None and create:
            tr = RadioTrack(float(freq_hz), now)
            self.tracks[k] = tr
        return tr

    def note_visits(self, freqs, now, levels=None):
        """Record that these channels were examined, hit or not."""
        levels = levels or {}
        for f in freqs:
            self.get(f, now).note_visit(now, levels.get(self.key(f)))

    def prune(self, now):
        """Forget tracks nothing has been heard on for a while.

        A track that never produced a hit is only a channel that was looked
        at, so it goes as soon as it is stale.  One that did produce a hit is
        kept longer, because a vehicle that goes quiet for a moment has not
        gone away.
        """
        quiet = max(5.0, float(self.cfg.get('track_forget_quiet_s', 20.0)))
        heard = max(quiet, float(self.cfg.get('track_forget_s', 90.0)))
        for k, tr in list(self.tracks.items()):
            limit = heard if tr.last_hit > 0 else quiet
            if now - max(tr.last_seen, tr.last_hit) > limit:
                del self.tracks[k]

    def _states(self):
        return (float(self.cfg.get('track_active_s', 2.0)),
                float(self.cfg.get('track_fading_s', 5.0)))

    def heard(self, now, states=(ACTIVE, FADING)):
        """Tracks in one of those states, most interesting first."""
        act, fad = self._states()
        # A copy first: the display asks this from its own thread (for the
        # radio's duty limit) while the scan thread may be adding a track.
        out = [t for t in list(self.tracks.values()) if t.state(now, act, fad) in states]
        out.sort(key=lambda t: (t.confirmed, t.snr_db), reverse=True)
        return out

    def top(self, now, limit=3):
        """What the screen shows: the strongest signals that are really there.

        Taken from the tracks rather than from whatever the last scan round
        happened to return, so the bars stop jumping between channels every
        dwell and start meaning "these are the ones near me".
        """
        return self.heard(now)[:max(0, int(limit))] if limit else []

    def expire_confirmations(self, now):
        """A track that has gone quiet is no longer a confirmed signal.

        Without this the bar would keep a vehicle on screen long after it
        drove away, which is the opposite of what someone glancing at it
        needs.
        """
        act, fad = self._states()
        for tr in self.tracks.values():
            if tr.confirmed and tr.state(now, act, fad) == LOST:
                tr.confirmed = False

    def revisit_due(self, now):
        """Channels worth going back to, soonest first.

        An active track is revisited at its own cadence: fast while it is
        still proving itself, then at the rate it actually transmits once it
        has, because measuring a channel more often than it says anything only
        costs power.
        """
        act, fad = self._states()
        fast = max(0.2, float(self.cfg.get('track_revisit_fast_s', 0.8)))
        slow = max(fast, float(self.cfg.get('track_revisit_s', 2.5)))
        # A channel stays in the fast rotation for a while after it goes quiet,
        # not only while it counts as live. A vehicle reporting in every few
        # seconds is LOST between transmissions by definition, and dropping it
        # back to the band sweep the moment it stops talking means the sweep
        # only comes back about once every fifteen dwells -- which is how a
        # single hit used to be the only one a channel ever got.
        hot = max(fad, float(self.cfg.get('track_hot_s', 60.0)))
        cool = max(slow, float(self.cfg.get('track_revisit_cool_s', 3.0)))
        due = []
        for tr in self.tracks.values():
            if tr.last_hit <= 0.0 or now - tr.last_hit > hot:
                continue
            st = tr.state(now, act, fad)
            if st == ACTIVE:
                want = slow if tr.confirmed else fast
            elif st == FADING:
                want = fast * 1.5
            else:
                want = cool          # quiet but recent: keep checking back
            period = tr.period_s()
            if tr.confirmed and period > 0.0:
                # It has a rhythm: follow that rather than a fixed rate.
                want = max(fast, min(cool, period * 0.5))
            if now - tr.last_seen >= want:
                due.append((now - tr.last_seen - want, tr))
        due.sort(key=lambda x: -x[0])
        return [tr for _, tr in due]
