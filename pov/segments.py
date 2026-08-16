"""Turn a score curve into a ranked list of cuttable segments."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .naming import recording_order
from .signals import Event, Signals, _runs


@dataclass
class Segment:
    """A candidate cut, in the time base of its own source file."""

    source: Path
    start: float
    end: float
    score: float
    peak_time: float
    peak_speed_kmh: float = 0.0
    events: list[Event] = field(default_factory=list)
    rank: int = 0
    # "" para los candidatos de accion, "intro" / "outro" para los dos clips
    # que arma `bookends` con reglas propias. Marcarlos importa porque no
    # compiten con los demas: no se rankean, no gastan presupuesto de reel, y
    # su lugar en el orden es fijo.
    role: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def kinds(self) -> list[str]:
        """Event kinds present, most interesting first."""
        order = {"crash": 0, "air": 1, "impact": 2}
        seen = sorted({e.kind for e in self.events}, key=lambda k: order.get(k, 9))
        return seen

    def headline(self) -> str:
        """Short human label describing why this segment was picked."""
        if self.role == "intro":
            return "INTRO"
        if self.role == "intro-alt":
            return "INTRO alt"
        if self.role == "intro-cam":
            return "INTRO camara"
        if self.role == "outro":
            return "FINAL"

        crashes = [e for e in self.events if e.kind == "crash"]
        if crashes:
            return f"CAIDA {max(e.magnitude for e in crashes):.0f}g"

        air = [e for e in self.events if e.kind == "air"]
        if air:
            biggest = max(e.magnitude for e in air)
            if len(air) > 1:
                return f"{len(air)}x AIRE  max {biggest:.2f}s"
            return f"AIRE {biggest:.2f}s"

        impacts = [e for e in self.events if e.kind == "impact"]
        if impacts:
            return f"IMPACTO {max(e.magnitude for e in impacts):.1f}g"

        if self.peak_speed_kmh > 0:
            return f"VELOCIDAD {self.peak_speed_kmh:.0f} km/h"
        return "ACCION"

    def timecode(self) -> str:
        minutes, seconds = divmod(self.start, 60)
        return f"{int(minutes):02d}:{seconds:05.2f}"

    def to_dict(self) -> dict:
        return {
            "rol": self.role or "accion",
            "archivo": self.source.name,
            "inicio": round(self.start, 2),
            "fin": round(self.end, 2),
            "duracion": round(self.duration, 2),
            "puntaje": round(self.score, 1),
            "vel_max_kmh": round(self.peak_speed_kmh, 1),
            "etiqueta": self.headline(),
            "eventos": [e.to_dict() for e in self.events],
        }


def ride_threshold(curves: list[Signals], cfg) -> float:
    """The score a moment must beat to become a candidate, for a whole ride.

    Computed across every file at once, never per file. Per-file thresholds
    were the reason dull footage still made the cut: each file surrendered its
    own top slice regardless of how it compared to the rest of the day, so
    twenty minutes of fire road donated highlights it had no business donating.
    """
    pooled = [c.score for c in curves if c.score.size]
    if not pooled:
        return cfg.min_segment_score
    scores = np.concatenate(pooled)
    return max(cfg.min_segment_score, float(np.percentile(scores, cfg.peak_percentile)))


def build(source: Path, signals: Signals, cfg, threshold: float | None = None) -> list[Segment]:
    """Find every segment worth considering in one source file.

    `threshold` should come from `ride_threshold` so the whole ride is judged
    on one bar. It falls back to this file alone only for single-file use.
    """
    if signals.t.size == 0:
        return []

    score = signals.score
    duration = float(signals.t[-1])

    if threshold is None:
        threshold = ride_threshold([signals], cfg)
    runs = _runs(score > threshold)
    if not runs:
        return []

    # Widen each run by the roll-in / roll-out, then clamp to the file.
    spans: list[tuple[float, float]] = []
    for start_i, end_i in runs:
        start = float(signals.t[start_i]) - cfg.pre_roll
        end = float(signals.t[min(end_i, signals.t.size - 1)]) + cfg.post_roll
        spans.append((max(0.0, start), min(duration, end)))

    spans = _merge(spans, cfg.merge_gap_seconds)

    segments: list[Segment] = []
    for start, end in spans:
        for piece_start, piece_end in _split_long(start, end, score, signals.t, cfg):
            piece_start = _trim_head(
                piece_start, piece_end, signals, cfg, threshold
            )
            piece_end = _trim_tail(
                piece_start, piece_end, signals, cfg, threshold
            )
            segment = _make(source, piece_start, piece_end, signals, cfg)
            if segment is not None:
                segments.append(segment)

    return segments


def _merge(spans: list[tuple[float, float]], gap: float) -> list[tuple[float, float]]:
    if not spans:
        return []
    spans = sorted(spans)
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def _trim_head(
    start: float, end: float, signals: Signals, cfg, threshold: float
) -> float:
    """Pull the start forward to where the action actually begins.

    A span opens where the score first crosses the bar, and then `pre_roll` is
    added in front of *that*. But the continuous signals ramp in gradually, so
    the crossing happens well before anything is worth watching: measured on a
    real ride, segments opened a median of 5.1 s before their first event and
    as much as 15 s. The editor's own cuts start a median of 1.1 s before it.
    Dead air at the head of every clip is what makes a reel feel slow.

    So instead of anchoring on the crossing, anchor on the moment the score
    *commits* — climbs a real fraction of the way from the bar to this span's
    own peak — and keep exactly `pre_roll` in front of that. Defining the
    anchor relative to the span's own peak rather than as an absolute level is
    what keeps a genuinely fast section intact: if it is already well above the
    bar when it opens, it commits immediately and nothing gets trimmed. Only
    the slow ramp-in gets cut.

    The commit is measured on the *continuous* part of the score, never the
    total. Measured on real footage, using the total instead cost us the one
    thing we were trying to protect: a clip the editor opened six seconds
    ahead of an 8 g landing, on the fast approach into it. The landing's bonus
    put the peak so high that the approach fell under any sensible commit
    level and got trimmed away, turning a 6 s early start into a 5 s late one.
    On the base curve that approach is genuinely high, so it survives.

    An event never gets trimmed past, whatever the curve is doing.
    """
    window = (signals.t >= start) & (signals.t <= end)
    if not window.any():
        return start

    times = signals.t[window]
    anchors: list[float] = []

    riding = signals.base if signals.base is not None else signals.score
    inside = riding[window]
    peak = float(inside.max())
    if peak > threshold:
        commit = threshold + cfg.commit_fraction * (peak - threshold)
        reached = np.flatnonzero(inside >= commit)
        if reached.size:
            anchors.append(float(times[reached[0]]))

    events = [e for e in signals.events if e.start < end and e.end > start]
    if events:
        anchors.append(min(e.start for e in events))

    if not anchors:
        return start

    trimmed = max(start, min(anchors) - cfg.pre_roll)
    # Never trim so hard the segment stops being usable.
    return min(trimmed, max(start, end - cfg.min_segment_seconds))


def _trim_tail(
    start: float, end: float, signals: Signals, cfg, threshold: float
) -> float:
    """Cortar la cola cuando la accion ya se acabo.

    El espejo exacto de `_trim_head`, y por la misma razon. Un tramo termina
    donde el puntaje vuelve a cruzar el umbral hacia abajo, mas `post_roll`,
    pero las senales continuas **bajan** igual de despacio que suben, asi que
    la cola se llena de rodada intrascendente. Medido en Halpatiokee: un clip
    de 18.6 s con todos sus impactos entre el segundo 1.8 y el 8.9, y los
    ultimos 8.6 s por debajo del umbral del ride. Chris lo describio como
    "empieza con buena accion y despues solo pedaleo sin sentido".

    Se mide sobre la curva continua, nunca sobre el puntaje total, por la misma
    razon que en la cabeza: el bono de un golpe distorsiona el pico del tramo.
    Y nunca se corta antes del ultimo evento, pase lo que pase con la curva.
    """
    window = (signals.t >= start) & (signals.t <= end)
    if not window.any():
        return end

    times = signals.t[window]
    anchors: list[float] = []

    riding = signals.base if signals.base is not None else signals.score
    inside = riding[window]
    peak = float(inside.max())
    if peak > threshold:
        commit = threshold + cfg.commit_fraction * (peak - threshold)
        # Sostenido, no instantaneo. En la cabeza basta con tocar el nivel una
        # vez -- si la intensidad va subiendo, ya empezo. En la cola es al
        # reves: un repunte de medio segundo mientras la rodada se apaga no
        # significa que la accion siga, y tomarlo como ancla alarga el clip
        # varios segundos. Medido: un pico aislado en el segundo 14.8 estaba
        # estirando un clip que se acababa de verdad en el 9.5.
        hold = max(1, round(cfg.tail_hold_seconds * signals.hz))
        runs = [(a, b) for a, b in _runs(inside >= commit) if b - a >= hold]
        if runs:
            anchors.append(float(times[min(runs[-1][1], times.size - 1)]))

    events = [e for e in signals.events if e.start < end and e.end > start]
    if events:
        anchors.append(max(e.end for e in events))

    if not anchors:
        return end

    trimmed = min(end, max(anchors) + cfg.post_roll)
    # Nunca dejar el segmento por debajo de lo utilizable.
    return max(trimmed, min(end, start + cfg.min_segment_seconds))


def _split_long(
    start: float, end: float, score: np.ndarray, t: np.ndarray, cfg
) -> list[tuple[float, float]]:
    """Chop an over-long span into pieces, cutting at its quietest moments.

    Walking the span in fixed `max_segment_seconds` strides is the obvious
    implementation and it is wrong: the boundary lands wherever the arithmetic
    puts it, which on real footage meant slicing a 43 s span straight through
    the middle and handing back two clips whose action started 14 and 15 s in.
    Cutting at the valley between two peaks instead gives every piece its own
    action, near the front where it belongs.
    """
    if end - start <= cfg.max_segment_seconds:
        return [(start, end)]

    # Only consider split points that leave both halves usable, and stay near
    # the middle so the recursion actually converges instead of shaving slivers.
    duration = end - start
    margin = max(cfg.min_segment_seconds, 0.3 * duration)
    low, high = start + margin, end - margin
    if high <= low:
        middle = 0.5 * (start + end)
    else:
        window = (t >= low) & (t <= high)
        if not window.any():
            middle = 0.5 * (start + end)
        else:
            middle = float(t[window][int(np.argmin(score[window]))])

    return (
        _split_long(start, middle, score, t, cfg)
        + _split_long(middle, end, score, t, cfg)
    )


def _make(source: Path, start: float, end: float, signals: Signals, cfg) -> Segment | None:
    if end - start < cfg.min_segment_seconds:
        return None

    window = (signals.t >= start) & (signals.t <= end)
    if not window.any():
        return None

    inside = signals.score[window]
    times = signals.t[window]

    # Score on the strongest third, not the mean: a segment with one huge drop
    # and four calm seconds is a good segment, and averaging would hide that.
    top_count = max(1, int(inside.size * 0.3))
    strength = float(np.sort(inside)[-top_count:].mean())

    peak_time = float(times[int(np.argmax(inside))])
    events = [e for e in signals.events if e.start < end and e.end > start]

    return Segment(
        source=source,
        start=round(start, 2),
        end=round(end, 2),
        score=strength,
        peak_time=peak_time,
        peak_speed_kmh=signals.peak_speed(start, end),
        events=events,
    )


def select(segments: list[Segment], target_seconds: float) -> list[Segment]:
    """Pick the best segments up to a total runtime, then restore ride order.

    Selecting by score but presenting chronologically matters: the reel should
    still read like one descent, not a random shuffle of highlights.

    "Chronologically" means the camera's recording order, not alphabetical
    order of the filenames. Those are the same thing right up until a long
    recording chapters: GX011134 continues as GX021134, and sorting by name
    puts every file numbered 1135 or higher in between the two halves.
    """
    ranked = sorted(segments, key=lambda s: s.score, reverse=True)

    chosen: list[Segment] = []
    total = 0.0
    for segment in ranked:
        if total + segment.duration > target_seconds and chosen:
            continue
        chosen.append(segment)
        total += segment.duration
        if total >= target_seconds:
            break

    for position, segment in enumerate(sorted(chosen, key=lambda s: s.score, reverse=True), start=1):
        segment.rank = position

    return sorted(chosen, key=lambda s: (recording_order(s.source), s.start))
