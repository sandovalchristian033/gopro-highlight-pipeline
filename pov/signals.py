"""Turn raw telemetry into a per-instant "hype score" plus discrete events.

The whole point of this module: a mountain bike POV clip is interesting for
reasons that are *physically measurable*, so we never have to watch the footage
to find them.

  air      the accelerometer reads near zero g. You are not touching the
           ground. This is a jump or a drop, and it is the single most
           reliable predictor of a good clip.
  impact   a large acceleration spike: a landing, a rock strike, a crash.
  speed    GPS ground speed, ranked against the rest of the ride, because
           40 km/h means different things on a flow trail and a tech descent.
  gyro     rotational energy: cornering, whips, the bike moving under you.
  chatter  high frequency acceleration variance: rock gardens, roots, braking
           bumps. Looks fast on camera even when it is not.
  audio    broadband loudness. Wind noise scales with speed and impacts are
           loud, so this works as a standalone fallback when telemetry is
           missing and as a cheap confirmation when it is not.

The score has two parts that behave differently on purpose:

  base     a weighted average of the *continuous* signals. Always defined.
           This is "how hard is this section being ridden".
  bonuses  air and impact are added on top, because they are events, not
           levels. A file with no jumps still gets a real base score instead
           of having its scale crushed by a zero component.

Scoring happens in two phases so a whole ride shares one scale. `analyse_file`
produces unranked signals per file, `RideStats.collect` derives the ride-wide
percentile references, and `finalise` applies them. Without that, a boring
file and a great file each get their own 0..1 range and become impossible to
compare against each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .telemetry import GRAVITY, Series, Telemetry

# Signals that describe a continuous level, averaged into the base score.
CONTINUOUS = ("speed", "wind", "chatter", "gyro", "lean", "audio")


@dataclass
class Event:
    """A discrete thing that happened, in source-file time."""

    kind: str  # "air" | "impact" | "crash"
    start: float
    end: float
    magnitude: float  # airtime in seconds, or peak g

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2

    def label(self) -> str:
        if self.kind == "air":
            return f"AIRE {self.magnitude:.2f}s"
        if self.kind == "crash":
            return f"CAIDA {self.magnitude:.0f}g"
        return f"IMPACTO {self.magnitude:.0f}g"

    def to_dict(self) -> dict:
        return {
            "tipo": self.kind,
            "inicio": round(self.start, 2),
            "fin": round(self.end, 2),
            "magnitud": round(self.magnitude, 2),
        }


@dataclass
class RawSignals:
    """Per-file signals before any ride-wide normalisation."""

    t: np.ndarray
    hz: float
    raw: dict[str, np.ndarray] = field(default_factory=dict)  # unranked continuous
    air: np.ndarray | None = None      # already on an absolute 0..1 scale
    impact: np.ndarray | None = None   # already on an absolute 0..1 scale
    events: list[Event] = field(default_factory=list)
    speed_kmh: np.ndarray | None = None
    has_telemetry: bool = True


@dataclass
class Signals:
    """Everything we know about one source file, on a uniform time grid."""

    t: np.ndarray
    score: np.ndarray
    components: dict[str, np.ndarray] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    speed_kmh: np.ndarray | None = None
    hz: float = 20.0
    source_has_telemetry: bool = True
    # The continuous part of `score` on its own, before any event bonus, on the
    # same 0..100 scale. Kept because "how hard is he riding right now" and
    # "did something just happen" are different questions, and deciding where a
    # clip should open needs the first one: an 8 g landing spikes `score` so
    # far above the fast approach leading into it that the approach looks like
    # dead air by comparison, when it is exactly the part worth keeping.
    base: np.ndarray | None = None

    def speed_at(self, when: float) -> float:
        if self.speed_kmh is None or self.t.size == 0:
            return 0.0
        return float(np.interp(when, self.t, self.speed_kmh))

    def peak_speed(self, start: float, end: float) -> float:
        if self.speed_kmh is None or self.t.size == 0:
            return 0.0
        window = (self.t >= start) & (self.t <= end)
        return float(self.speed_kmh[window].max()) if window.any() else 0.0


@dataclass
class RideStats:
    """Percentile references shared by every file in one ride.

    Without this, each file is normalised against itself, so the roughest
    moment of the dullest fire road scores the same as the roughest moment of
    the best descent. Ranking across files then means nothing.
    """

    ranges: dict[str, tuple[float, float]] = field(default_factory=dict)

    @classmethod
    def collect(cls, raws: list[RawSignals], cfg) -> "RideStats":
        ranges: dict[str, tuple[float, float]] = {}

        for name in CONTINUOUS:
            pooled = [raw.raw[name] for raw in raws if name in raw.raw and raw.raw[name].size]
            if not pooled:
                continue
            values = np.concatenate(pooled)

            if name == "speed":
                # Rank against moving samples only: idling at the trailhead
                # should not drag the scale down and make every rolling
                # section look fast.
                moving = values[values > cfg.moving_speed_kmh]
                if moving.size > values.size * 0.05:
                    ranges[name] = (float(np.percentile(moving, 10)), float(np.percentile(moving, 95)))
                    continue

            ranges[name] = (float(np.percentile(values, 5)), float(np.percentile(values, 97)))

        return cls(ranges=ranges)

    def normalise(self, name: str, values: np.ndarray) -> np.ndarray:
        low, high = self.ranges.get(name, (float(values.min()), float(values.max())))
        if high - low < 1e-9:
            return np.zeros_like(values)
        return np.clip((values - low) / (high - low), 0.0, 1.0)


# --------------------------------------------------------------------------
# resampling helpers
# --------------------------------------------------------------------------

def _bin_stats(series: Series, edges: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Mean, max and standard deviation of `series` inside each grid bin."""
    bins = edges.size - 1
    mean = np.zeros(bins)
    peak = np.zeros(bins)
    std = np.zeros(bins)
    if len(series) == 0:
        return mean, peak, std

    idx = np.searchsorted(edges, series.t, side="right") - 1
    keep = (idx >= 0) & (idx < bins)
    idx = idx[keep]
    values = series.v[keep]
    if idx.size == 0:
        return mean, peak, std

    counts = np.bincount(idx, minlength=bins)
    total = np.bincount(idx, weights=values, minlength=bins)
    total_sq = np.bincount(idx, weights=values * values, minlength=bins)
    nonempty = counts > 0

    mean[nonempty] = total[nonempty] / counts[nonempty]
    variance = np.zeros(bins)
    variance[nonempty] = np.maximum(total_sq[nonempty] / counts[nonempty] - mean[nonempty] ** 2, 0.0)
    std = np.sqrt(variance)

    np.maximum.at(peak, idx, values)

    # Bins with no samples inherit their neighbours so we do not punch holes.
    if not nonempty.all() and nonempty.any():
        grid = np.arange(bins)
        mean = np.interp(grid, grid[nonempty], mean[nonempty])
        peak = np.interp(grid, grid[nonempty], peak[nonempty])
        std = np.interp(grid, grid[nonempty], std[nonempty])

    return mean, peak, std


def _smooth(values: np.ndarray, window: int) -> np.ndarray:
    """Centred moving average, edge-padded so the ends stay honest."""
    window = max(1, int(window))
    if window <= 1 or values.size == 0:
        return values
    pad = window // 2
    padded = np.pad(values, pad, mode="edge")
    kernel = np.ones(window) / window
    return np.convolve(padded, kernel, mode="same")[pad : pad + values.size]


def _rolling_max(values: np.ndarray, window: int) -> np.ndarray:
    window = max(1, int(window))
    if window <= 1 or values.size == 0:
        return values
    pad = window // 2
    padded = np.pad(values, pad, mode="edge")
    strided = np.lib.stride_tricks.sliding_window_view(padded, window)
    return strided.max(axis=-1)[: values.size]


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Start/end indices (inclusive/exclusive) of each True run in `mask`."""
    if mask.size == 0 or not mask.any():
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[0::2], edges[1::2]))


# --------------------------------------------------------------------------
# event detection
# --------------------------------------------------------------------------

def detect_air(t: np.ndarray, accel_smooth: np.ndarray, cfg) -> list[Event]:
    """Freefall: smoothed acceleration magnitude drops toward zero g."""
    threshold = cfg.air_threshold_g * GRAVITY
    events = []
    for start_i, end_i in _runs(accel_smooth < threshold):
        start, end = float(t[start_i]), float(t[min(end_i, t.size - 1)])
        airtime = end - start
        if airtime >= cfg.air_min_seconds:
            events.append(Event("air", start, end, airtime))
    return events


def detect_impacts(t: np.ndarray, accel_peak: np.ndarray, cfg) -> list[Event]:
    """Acceleration spikes above the impact threshold."""
    threshold = cfg.impact_threshold_g * GRAVITY
    events = []
    for start_i, end_i in _runs(accel_peak > threshold):
        window = accel_peak[start_i:end_i]
        if window.size == 0:
            continue
        events.append(
            Event(
                "impact",
                float(t[start_i]),
                float(t[min(end_i, t.size - 1)]),
                float(window.max()) / GRAVITY,
            )
        )
    return events


def detect_crashes(impacts: list[Event], speed_kmh: np.ndarray | None, t: np.ndarray, cfg) -> list[Event]:
    """A hard impact that is followed by the bike stopping.

    Riding fast, then a big hit, then near-zero speed for a few seconds is
    about as unambiguous as a crash signature gets from telemetry alone.
    Needs GPS: without a speed trace there is no way to tell a crash from a
    hard landing you rode away from.
    """
    if speed_kmh is None or t.size == 0:
        return []

    crashes = []
    for impact in impacts:
        if impact.magnitude < cfg.crash_impact_g:
            continue

        before = (t >= impact.start - 3.0) & (t < impact.start)
        after = (t > impact.end) & (t <= impact.end + cfg.crash_stop_window)
        if not before.any() or not after.any():
            continue

        was_moving = float(speed_kmh[before].max()) >= cfg.crash_min_speed_kmh
        stopped = float(speed_kmh[after].min()) <= cfg.crash_stop_kmh
        if was_moving and stopped:
            crashes.append(Event("crash", impact.start, impact.end + 2.0, impact.magnitude))
    return crashes


# --------------------------------------------------------------------------
# phase 1: per-file signals, not yet normalised
# --------------------------------------------------------------------------

def analyse_file(telemetry: Telemetry, cfg, audio: Series | None = None) -> RawSignals:
    """Extract every signal from one file, leaving normalisation for later."""
    hz = float(cfg.analysis_hz)
    duration = max(telemetry.duration, 0.1)
    edges = np.arange(0.0, duration + 1.0 / hz, 1.0 / hz)
    if edges.size < 2:
        edges = np.array([0.0, duration])
    t = (edges[:-1] + edges[1:]) / 2

    result = RawSignals(t=t, hz=hz, has_telemetry=telemetry.has_motion)
    bins = t.size

    # --- accelerometer: events and chatter -------------------------------
    if telemetry.has_motion:
        accel_mean, accel_peak_raw, accel_std = _bin_stats(telemetry.accel, edges)

        # Freefall needs a short smoothing window or vibration masks it.
        accel_smooth = _smooth(accel_mean, max(1, round(cfg.air_smooth_seconds * hz)))
        accel_peak = _rolling_max(accel_peak_raw, max(1, round(cfg.impact_window_seconds * hz)))

        air_events = detect_air(t, accel_smooth, cfg)
        impact_events = detect_impacts(t, accel_peak, cfg)
        result.events.extend(air_events)
        result.events.extend(impact_events)

        # Air is scored from the detected events, not the raw curve, so a long
        # float counts for more than a brief unweighting over a root.
        air_curve = np.zeros(bins)
        for event in air_events:
            strength = min(1.0, event.magnitude / cfg.air_reference_seconds)
            window = (t >= event.start - 0.5) & (t <= event.end + 0.5)
            air_curve[window] = np.maximum(air_curve[window], strength)
        result.air = _smooth(air_curve, max(1, round(0.3 * hz)))

        # Impacts are scored on an absolute g scale, never ride-relative: 5 g
        # is 5 g whether the rest of the ride was mellow or brutal.
        impact_low = cfg.impact_threshold_g * GRAVITY
        impact_high = cfg.impact_reference_g * GRAVITY
        result.impact = np.clip(
            (accel_peak - impact_low) / max(impact_high - impact_low, 1e-6), 0.0, 1.0
        )

        result.raw["chatter"] = _smooth(accel_std, max(1, round(0.5 * hz)))

    # --- gyroscope --------------------------------------------------------
    if len(telemetry.gyro) > 0:
        gyro_mean, _, _ = _bin_stats(telemetry.gyro, edges)
        result.raw["gyro"] = _smooth(gyro_mean, max(1, round(0.6 * hz)))

    # --- wind meter: the speed proxy that survives no GPS ------------------
    if len(telemetry.wind) > 0:
        wind_mean, _, _ = _bin_stats(telemetry.wind, edges)
        result.raw["wind"] = _smooth(wind_mean, max(1, round(1.0 * hz)))

    # --- lean: banked corners and steep pitches ---------------------------
    # Smoothed over a full second on purpose. A carved turn holds the bike
    # tipped over for a second or more; the bike bucking over a root does not.
    # Only the sustained version is the "flow" worth cutting to.
    lean = telemetry.lean_angles()
    if len(lean) > 0:
        lean_mean, _, _ = _bin_stats(lean, edges)
        result.raw["lean"] = _smooth(lean_mean, max(1, round(cfg.lean_smooth_seconds * hz)))

    # --- GPS --------------------------------------------------------------
    if telemetry.has_gps:
        speed_kmh = _smooth(
            np.interp(t, telemetry.speed.t, telemetry.speed.v) * 3.6,
            max(1, round(0.5 * hz)),
        )
        result.speed_kmh = speed_kmh
        result.raw["speed"] = speed_kmh

    # --- audio ------------------------------------------------------------
    if audio is not None and len(audio) > 0:
        loudness, _, _ = _bin_stats(audio, edges)
        result.raw["audio"] = _smooth(loudness, max(1, round(0.4 * hz)))

    return result


# --------------------------------------------------------------------------
# phase 2: apply the ride-wide scale and fuse
# --------------------------------------------------------------------------

def finalise(raw: RawSignals, cfg, stats: RideStats) -> Signals:
    """Normalise against the ride and fuse everything into one score curve."""
    t = raw.t
    bins = t.size
    components: dict[str, np.ndarray] = {}

    for name, values in raw.raw.items():
        components[name] = stats.normalise(name, values)
    if raw.air is not None:
        components["air"] = raw.air
    if raw.impact is not None:
        components["impact"] = raw.impact

    # --- base level: weighted average of the continuous signals -----------
    weights = {
        name: float(cfg.weights.get(name, 0.0))
        for name in CONTINUOUS
        if name in components and cfg.weights.get(name, 0.0) > 0
    }
    if not raw.has_telemetry and "audio" in weights:
        # With no sensors, loudness is all we have; let it carry the base.
        weights["audio"] = max(weights["audio"], 1.0)

    if weights:
        base = sum(components[n] * w for n, w in weights.items()) / sum(weights.values())
    else:
        base = np.zeros(bins)

    # Smooth the base only. Doing it after the event bonuses would flatten
    # exactly the spikes we care most about: an impact lasts a fraction of a
    # second, and a 0.8 s average erases most of it.
    base = _smooth(base, max(1, round(cfg.score_smooth_seconds * cfg.analysis_hz)))

    # --- events add on top, they do not average in ------------------------
    # This is the difference that matters: a file with no jumps keeps a real
    # base score instead of having half its scale zeroed out by an absent
    # component, while a file *with* jumps still shoots well above it.
    score = cfg.base_gain * base
    if "air" in components:
        score = score + cfg.air_gain * components["air"]
    if "impact" in components:
        score = score + cfg.impact_gain * components["impact"]

    events = list(raw.events)

    # A clean landing (air immediately followed by an impact) is worth more
    # than either half on its own.
    score = _apply_combo_bonus(t, score, events, cfg)

    if raw.speed_kmh is not None:
        # Nothing that happens while stopped is worth cutting to.
        score = np.where(raw.speed_kmh < cfg.moving_speed_kmh, score * cfg.stopped_penalty, score)

    crashes = detect_crashes([e for e in events if e.kind == "impact"], raw.speed_kmh, t, cfg)
    events.extend(crashes)
    for crash in crashes:
        window = (t >= crash.start - 2.0) & (t <= crash.end + 1.0)
        score[window] = np.minimum(score[window] + cfg.crash_bonus, 1.0)

    events.sort(key=lambda e: e.start)

    return Signals(
        t=t,
        score=np.clip(score, 0.0, 1.0) * 100.0,
        components=components,
        events=events,
        speed_kmh=raw.speed_kmh,
        base=np.clip(cfg.base_gain * base, 0.0, 1.0) * 100.0,
        hz=raw.hz,
        source_has_telemetry=raw.has_telemetry,
    )


def build(telemetry: Telemetry, cfg, audio: Series | None = None, stats: RideStats | None = None) -> Signals:
    """Analyse one file end to end.

    Convenience path for a single file. A real ride goes through
    `analyse_file` for every file, then one `RideStats.collect`, then
    `finalise`, so the whole ride shares one scale.
    """
    raw = analyse_file(telemetry, cfg, audio)
    return finalise(raw, cfg, stats or RideStats.collect([raw], cfg))


def _apply_combo_bonus(t: np.ndarray, score: np.ndarray, events: list[Event], cfg) -> np.ndarray:
    air = [e for e in events if e.kind == "air"]
    impacts = [e for e in events if e.kind == "impact"]
    if not air or not impacts:
        return score

    boosted = score.copy()
    for jump in air:
        landing = next(
            (i for i in impacts if 0 <= i.start - jump.end <= cfg.landing_window_seconds),
            None,
        )
        if landing is None:
            continue
        window = (t >= jump.start - 0.5) & (t <= landing.end + 0.5)
        boosted[window] = np.minimum(boosted[window] + cfg.combo_bonus, 1.0)
    return boosted
