"""How hard the radio may work, learned from what the supply actually does.

Until 0.10.2 the receiver only looked at the 5 V rail after the RTL-SDR had
already gone, and what it looked at was bit 16 of ``get_throttled``: "the rail
has sagged at some point since boot". That bit never clears, so one dip -- and
a unit in a car dips at every power-up -- pinned the radio at its recovery
share until the next reboot, while a unit that had not failed yet ran at full
share right up to the moment it did. Neither is adapting to anything.

``PowerLadder`` is the replacement: a small set of steps for the share of the
time the dongle may stream. Something going wrong -- a fresh under-voltage, or
the dongle falling off the bus -- drops it straight to a safe step. Every five
minutes of trouble-free listening buys one step back. And the step the dongle
was lost on is remembered, so the ladder settles just below what this supply
can carry instead of climbing back into the same failure every quarter of an
hour.

0.10.8 took the ladder's teeth out of everything but a lost dongle, because of
what the first week in the field showed it doing. A unit on a weak supply had
counted 219 under-voltage incidents and 43 strikes on its lowest step; the
step was off limits for four hours at a time, and that survived every reboot.
It sat at 22% for the whole of a drive during which the rail was in fact fine
for twenty-six minutes -- and stood next to a police car hearing a fifth of
what it could have. Meanwhile the dips went on at 22% exactly as they had at
40%: the radio's share was not what was pulling that rail down, so taking it
away bought nothing and cost the one thing the unit is for.

So the two kinds of trouble are now kept apart. A lost dongle is still a
failure of the step it happened on. An under-voltage is a hint: it takes the
radio to the safe step and no further, it is never remembered against a step,
and when the rail goes on dipping there anyway the ladder says so once and
stops limiting the radio for it. A dip before the radio has even started says
nothing about the radio and costs nothing.

Kept free of the radio and of the Pi so the rules can be tested on their own.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

# The share of the time the dongle may stream, lowest first. 1.0 is "no cap":
# whatever the mode itself allows. The normal ECO shares (0.45 idle, 0.55 with
# something heard) sit on it, so a fully recovered ECO unit is not held back
# at all, and Max power climbs further on the same steps.
DEFAULT_RUNGS = (0.22, 0.30, 0.35, 0.40, 0.45, 0.55, 0.70, 0.85, 1.0)

# A second failure on the same step within this long counts as the same
# weakness coming back, and doubles how long that step stays off limits.
STRIKE_MEMORY_S = 12 * 3600.0

# The longest a step stays off limits. It was four hours; a lost dongle costs
# about five seconds of listening, and four hours at a lower step costs far
# more than the handful of drops it could possibly prevent.
MAX_HOLD_S = 3600.0

# What the state file has to say it is before it is believed. Files written
# before 0.10.8 carry ceilings and strikes earned from under-voltage alone.
STATE_SCHEMA = 2


def _boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except Exception:
        return ''


class PowerLadder:
    """The radio's share of the time, stepped down on trouble and up on calm."""

    def __init__(self, cfg, state_path=None, boot_id=None):
        self.cfg = cfg
        rungs = set()
        for x in cfg.get('power_ladder', DEFAULT_RUNGS) or DEFAULT_RUNGS:
            try:
                v = float(x)
            except (TypeError, ValueError):
                continue
            if 0.05 <= v <= 1.0:
                rungs.add(round(v, 4))
        rungs.add(1.0)
        self.rungs = sorted(rungs)
        self.index = len(self.rungs) - 1
        self.stable_s = 0.0          # trouble-free listening on this step
        self.last_tick = 0.0
        self.last_drop = 0.0
        self.last_change = 0.0
        self.incidents = 0
        self.last_why = ''
        # The step something last went wrong on, and until when it is off
        # limits. Kept by value, not index, so a changed ladder still reads it.
        self.ceiling = 0.0
        self.ceiling_until = 0.0
        # Which step failed last and how often in a row; outlives the ceiling
        # itself, so a step that holds for five minutes and then fails again
        # is kept away for longer next time rather than just as long.
        self.last_failed = 0.0
        self.strikes = 0
        self.boot_id = _boot_id() if boot_id is None else str(boot_id)
        self.boot_dip_counted = ''
        # Under-voltage, as opposed to a lost dongle: when the last one was
        # (for the debounce), the ones that came while the radio was already
        # at the safe step, and whether this boot has shown that limiting the
        # radio does not stop them.
        self.last_dip = 0.0
        self.dip_from = 0.0
        self.floor_dips = []
        self.weak_supply = False
        self.path = Path(state_path) if state_path else None
        self._load()

    # -- where it stands ---------------------------------------------------
    @property
    def cap(self):
        return float(self.rungs[self.index])

    def at_top(self):
        return self.index >= len(self.rungs) - 1

    def _index_at_or_below(self, value):
        idx = 0
        for i, r in enumerate(self.rungs):
            if r <= float(value) + 1e-9:
                idx = i
        return idx

    def _index_at_or_above(self, value):
        for i, r in enumerate(self.rungs):
            if r >= float(value) - 1e-9:
                return i
        return len(self.rungs) - 1

    def _step_s(self):
        return max(30.0, float(self.cfg.get('power_ladder_step_s', 300.0)))

    def _hold_s(self):
        """How long the step that failed stays off limits, doubling per strike."""
        base = max(self._step_s(), float(self.cfg.get('power_ladder_retry_s', 1800.0)))
        return min(max(base, MAX_HOLD_S), base * (2 ** max(0, self.strikes - 1)))

    def blocked(self, now, index=None):
        """Is the next step up the one that failed, and still off limits?

        Reads the step once: the display asks from its own thread while the
        scan thread may be moving it.
        """
        i = self.index if index is None else int(index)
        if i >= len(self.rungs) - 1 or not self.ceiling or now >= self.ceiling_until:
            return False
        return self.rungs[i + 1] >= self.ceiling - 1e-9

    def next_step_s(self, now):
        """Seconds of calm still needed before the next step up; 0 at the top."""
        i = self.index
        if i >= len(self.rungs) - 1:
            return 0.0
        left = max(0.0, self._step_s() - self.stable_s)
        if self.blocked(now, i):
            left = max(left, self.ceiling_until - now)
        return float(left)

    # -- what moves it -----------------------------------------------------
    def dip(self, now, why='', in_use=None):
        """The rail sagged while the radio was running.

        Not the same thing as the dongle failing, and no longer treated as
        one. A dip takes the radio to the safe step, where five calm minutes
        buy each step back -- and that is all it does: no step is put off
        limits for it and nothing is carried into the next boot.

        A dip that arrives when the radio is already at the safe step or under
        it is the supply saying something else: the radio has given what it
        can and the rail sags regardless. ``power_ladder_floor_dips`` of those
        inside ``power_ladder_floor_window_s`` and the ladder stops limiting
        the radio for under-voltage for the rest of this boot, and gives back
        what it took. A lost dongle still counts, exactly as before.

        Returns what it did: ``'drop'``, ``'floor'`` (at the safe step
        already, counted), ``'weak'`` (that count just ran out), ``'same'``
        (part of the incident already being handled) or ``'ignored'``.
        """
        now = float(now)
        if self.weak_supply:
            return 'ignored'
        debounce = max(0.0, float(self.cfg.get('power_ladder_debounce_s', 30.0)))
        last = max(self.last_dip, self.last_drop)
        if last and 0.0 <= now - last < debounce:
            self.stable_s = 0.0
            return 'same'
        self.last_dip = now
        self.stable_s = 0.0
        self.incidents += 1
        self.last_why = str(why)
        safe = self._index_at_or_below(float(self.cfg.get('power_ladder_drop_to', 0.30)))
        # The share the radio really had, for a dongle that goes a moment later.
        self.dip_from = self.cap if in_use is None else min(self.cap, float(in_use))
        if self.index > safe:
            self.index = safe
            self.last_change = now
            self._save(now)
            return 'drop'
        window = max(60.0, float(self.cfg.get('power_ladder_floor_window_s', 1800.0)))
        need = max(1, int(self.cfg.get('power_ladder_floor_dips', 3)))
        self.floor_dips = [t for t in self.floor_dips if 0.0 <= now - t < window] + [now]
        if len(self.floor_dips) < need:
            self._save(now)
            return 'floor'
        self.weak_supply = True
        self.floor_dips = []
        # Give back what was taken for it: everything up to the step the
        # dongle itself was last lost on, if that is still off limits.
        top = len(self.rungs) - 1
        if self.ceiling and now < self.ceiling_until:
            top = max(self.index, self._index_at_or_above(self.ceiling) - 1)
        if top > self.index:
            self.index = top
            self.last_change = now
        self._save(now)
        return 'weak'

    def event(self, now, why='', in_use=None):
        """The dongle itself failed: drop to a safe step.

        ``in_use`` is the share the radio was actually held to when it
        happened. That step, and anything above it, is then off limits for a
        while -- half an hour the first time, doubling if it fails there again
        up to an hour -- which is what stops the "fine, drop, recover, drop"
        cycle.

        Returns True when this is a new incident, False when it is more of the
        one already being handled: an under-voltage and the dongle dropping a
        second later are one event, not two steps down.
        """
        now = float(now)
        self.stable_s = 0.0
        debounce = max(0.0, float(self.cfg.get('power_ladder_debounce_s', 30.0)))
        if self.last_drop and 0.0 <= now - self.last_drop < debounce:
            return False
        # The rail sagging and the dongle going a moment later are one
        # incident, and it belongs to the step the radio was on before the
        # dip took it down -- not to the safe step the dip left it on.
        after_dip = bool(self.last_dip and 0.0 <= now - self.last_dip < debounce)
        was = max(self.cap, self.dip_from) if after_dip else self.cap
        drop_to = self._index_at_or_below(float(self.cfg.get('power_ladder_drop_to', 0.30)))
        if in_use is not None:
            if after_dip:
                in_use = max(float(in_use), was)
            failed = self.rungs[self._index_at_or_above(min(float(in_use), was))]
            again = (abs(failed - self.last_failed) < 1e-6 and self.last_drop
                     and 0.0 <= now - self.last_drop < STRIKE_MEMORY_S)
            self.strikes = self.strikes + 1 if again else 1
            self.last_failed = failed
            self.ceiling = failed
            self.ceiling_until = now + self._hold_s()
        # Straight to the safe step -- or, when the radio failed while already
        # at or below it, one further down: a supply that still fails at 30%
        # needs less, not the same again.
        held = self._index_at_or_below(was)
        new = min(drop_to, held - 1 if in_use is not None else held)
        self.index = max(0, new)
        self.last_drop = now
        self.last_change = now
        if not after_dip:
            self.incidents += 1
        self.last_why = str(why)
        self._save(now)
        return True

    def boot_dip(self, now, why='under-voltage before start'):
        """A dip that happened before the radio started: noted, once per boot.

        It used to cost the radio a drop to the safe step. A unit in a car
        dips at every start -- the engine cranking, the dongle's inrush -- so
        every drive began at 30% and spent a quarter of an hour climbing back,
        which for most drives is the drive. A dip the radio was not running
        for is no evidence about what the supply carries with it running.

        Returns True the first time in a boot, so it can be written down. A
        restart of the app without a reboot -- an update, a crash -- sees the
        same since-boot bit again.
        """
        if self.boot_id and self.boot_dip_counted == self.boot_id:
            return False
        self.boot_dip_counted = self.boot_id
        self.last_why = str(why)
        self._save(float(now))
        return True

    def tick(self, now, live):
        """Count calm time, and climb one step when enough has built up.

        Only time the receiver was actually listening counts: a dongle that
        is off the bus has proved nothing about what the supply can carry.
        """
        now = float(now)
        dt = (now - self.last_tick) if self.last_tick else 0.0
        self.last_tick = now
        # A wall clock that jumps (NTP after a cold start) or a thread that
        # stalled is not five minutes of evidence. One pass round the scan
        # loop -- a survey plus the duty pause -- can take several seconds,
        # so the bound sits well above that rather than at it.
        dt = max(0.0, min(dt, 30.0))
        if self.at_top():
            self.stable_s = 0.0
            return False
        if not live:
            return False
        self.stable_s += dt
        if self.stable_s < self._step_s() or self.blocked(now):
            return False
        self.index += 1
        self.stable_s = 0.0
        self.last_change = now
        # Held the step that failed for a full step without trouble: the
        # supply carries it now, so stop treating it as a ceiling.
        if self.ceiling and self.cap > self.ceiling + 1e-9:
            self.ceiling = 0.0
            self.ceiling_until = 0.0
        self._save(now)
        return True

    # -- memory across reboots ---------------------------------------------
    def _load(self):
        if not self.path or not bool(self.cfg.get('power_ladder_persist', True)):
            return
        try:
            d = json.loads(self.path.read_text())
        except Exception:
            return
        # A file from before 0.10.8 holds ceilings and strikes that were
        # earned from under-voltage alone -- the reference unit's said 43
        # strikes and four hours. None of it is evidence under these rules.
        try:
            if int(d.get('schema', 0) or 0) != STATE_SCHEMA:
                return
        except (TypeError, ValueError, AttributeError):
            return
        try:
            same_boot = bool(self.boot_id) and str(d.get('boot_id', '')) == self.boot_id
            # What a lost dongle taught outlives a reboot: it is about the
            # dongle and the supply, and both are still there afterwards.
            self.ceiling = float(d.get('ceiling', 0.0) or 0.0)
            self.ceiling_until = float(d.get('ceiling_until', 0.0) or 0.0)
            self.last_failed = float(d.get('last_failed', 0.0) or 0.0)
            self.strikes = int(d.get('strikes', 0) or 0)
            self.last_drop = float(d.get('last_drop', 0.0) or 0.0)
            self.incidents = int(d.get('incidents', 0) or 0)
            self.last_why = str(d.get('last_why', '') or '')
            self.boot_dip_counted = str(d.get('boot_dip_counted', '') or '')
            if same_boot:
                # The app restarted, the supply did not: carry on where it was.
                self.index = self._index_at_or_below(float(d.get('cap', 1.0)))
                self.weak_supply = bool(d.get('weak_supply', False))
                self.last_dip = float(d.get('last_dip', 0.0) or 0.0)
            elif self.ceiling:
                # A new boot starts as high as the step the dongle was lost on
                # allows, not wherever the last drive's dips had left it. The
                # clock may not be set yet, so "still off limits" is not asked
                # here; tick() asks it before every step up.
                self.index = max(0, self._index_at_or_above(self.ceiling) - 1)
        except Exception:
            self.index = len(self.rungs) - 1

    def _save(self, now):
        if not self.path or not bool(self.cfg.get('power_ladder_persist', True)):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps({
                'schema': STATE_SCHEMA, 'boot_id': self.boot_id,
                'weak_supply': bool(self.weak_supply), 'last_dip': self.last_dip,
                'cap': self.cap, 'ceiling': self.ceiling,
                'ceiling_until': self.ceiling_until, 'strikes': self.strikes,
                'last_failed': self.last_failed, 'last_drop': self.last_drop,
                'incidents': self.incidents, 'last_why': self.last_why,
                'boot_dip_counted': self.boot_dip_counted,
                'saved_at': float(now)}, indent=1))
            tmp.replace(self.path)
        except Exception:
            pass


def supply_text(snap):
    """One short line for the debug page: the rail, and what it has cost.

    Fits the compact panel's 22 characters, e.g. ``DIPPED x3 cap 30% +4m``.
    """
    events = int(snap.get('power_events', 0) or 0)
    if snap.get('power_weak'):
        # The rail sags whatever the radio does, and the radio is no longer
        # held back for it: a supply to fix, not a state that will pass.
        base = 'WEAK SUPPLY'
        if events:
            base += ' x%d' % events
    elif snap.get('power_warning'):
        base = 'LOW NOW'
    elif events:
        base = 'DIPPED x%d' % events
    elif snap.get('power_history'):
        base = 'DIPPED'
    else:
        base = 'OK'
    cap = float(snap.get('power_cap', 1.0) or 1.0)
    if cap >= 0.999:
        return base
    tail = ' cap %d%%' % int(round(cap * 100))
    nxt = float(snap.get('power_next_step_s', 0.0) or 0.0)
    if nxt > 0.0:
        tail += ' +%dm' % max(1, int(math.ceil(nxt / 60.0)))
    return base + tail


def read_uv_alarm(state):
    """The kernel's own under-voltage alarm, or None where there is none.

    ``raspberrypi-hwmon`` asks the firmware every two seconds and exposes the
    answer as ``in0_lcrit_alarm`` on the ``rpi_volt`` device. Reading it is a
    file read rather than a ``vcgencmd`` process, so it can be polled often
    enough to see a dip that is over before the next ``vcgencmd`` would run.
    ``state`` is a dict the caller keeps, so the search runs once.
    """
    path = state.get('path')
    if path is None and not state.get('looked'):
        state['looked'] = True
        try:
            for name in Path('/sys/class/hwmon').glob('hwmon*/name'):
                if name.read_text().strip() == 'rpi_volt':
                    alarm = name.parent / 'in0_lcrit_alarm'
                    if alarm.exists():
                        state['path'] = path = alarm
                        break
        except Exception:
            pass
    if path is None:
        return None
    try:
        return int(Path(path).read_text().strip() or '0')
    except Exception:
        return None


def read_soc_temp():
    try:
        return int(Path('/sys/class/thermal/thermal_zone0/temp').read_text().strip()) / 1000.0
    except Exception:
        return None

