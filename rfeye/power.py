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
minutes of trouble-free listening buys one step back. And the step it failed
on is remembered, so the ladder settles just below what this supply can carry
instead of climbing back into the same failure every quarter of an hour.

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
        return min(4 * 3600.0, base * (2 ** max(0, self.strikes - 1)))

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
    def event(self, now, why='', in_use=None):
        """Something went wrong: drop to a safe step.

        ``in_use`` is the share the radio was actually held to when it
        happened. That step, and anything above it, is then off limits for a
        while -- half an hour the first time, doubling if it fails there again
        -- which is what stops the "fine, drop, recover, drop" cycle. Leave it
        out for trouble the radio cannot have caused, such as a dip at
        power-up before it ever streamed.

        Returns True when this is a new incident, False when it is more of the
        one already being handled: an under-voltage and the dongle dropping a
        second later are one event, not two steps down.
        """
        now = float(now)
        self.stable_s = 0.0
        debounce = max(0.0, float(self.cfg.get('power_ladder_debounce_s', 30.0)))
        if self.last_drop and 0.0 <= now - self.last_drop < debounce:
            return False
        drop_to = self._index_at_or_below(float(self.cfg.get('power_ladder_drop_to', 0.30)))
        if in_use is not None:
            failed = self.rungs[self._index_at_or_above(min(float(in_use), self.cap))]
            again = (abs(failed - self.last_failed) < 1e-6 and self.last_drop
                     and 0.0 <= now - self.last_drop < STRIKE_MEMORY_S)
            self.strikes = self.strikes + 1 if again else 1
            self.last_failed = failed
            self.ceiling = failed
            self.ceiling_until = now + self._hold_s()
        # Straight to the safe step -- or, when the radio itself failed while
        # already at or below it, one further down: a supply that still fails
        # at 30% needs less, not the same again. A dip the radio cannot have
        # caused never pushes it under the safe step.
        new = min(drop_to, self.index - 1 if in_use is not None else self.index)
        self.index = max(0, new)
        self.last_drop = now
        self.last_change = now
        self.incidents += 1
        self.last_why = str(why)
        self._save(now)
        return True

    def boot_dip(self, now, why='under-voltage before start'):
        """A dip that happened before the radio started, counted once per boot.

        A restart of the app without a reboot -- an update, a crash -- sees the
        same since-boot bit again, and must not take the same dip twice.
        """
        if self.boot_id and self.boot_dip_counted == self.boot_id:
            return False
        self.boot_dip_counted = self.boot_id
        return self.event(now, why)

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
        try:
            cap = float(d.get('cap', 1.0))
            self.index = self._index_at_or_below(cap)
            self.ceiling = float(d.get('ceiling', 0.0) or 0.0)
            self.ceiling_until = float(d.get('ceiling_until', 0.0) or 0.0)
            self.last_failed = float(d.get('last_failed', 0.0) or 0.0)
            self.strikes = int(d.get('strikes', 0) or 0)
            self.last_drop = float(d.get('last_drop', 0.0) or 0.0)
            self.incidents = int(d.get('incidents', 0) or 0)
            self.last_why = str(d.get('last_why', '') or '')
            self.boot_dip_counted = str(d.get('boot_dip_counted', '') or '')
        except Exception:
            self.index = len(self.rungs) - 1

    def _save(self, now):
        if not self.path or not bool(self.cfg.get('power_ladder_persist', True)):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps({
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
    if snap.get('power_warning'):
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

