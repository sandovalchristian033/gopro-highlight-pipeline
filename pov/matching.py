"""Find which parts of the raw footage survived into an edited video.

An export from CapCut has no telemetry left in it, so the only way back to the
original timeline is the picture itself. We fingerprint both sides, match
frames, and keep the runs where the correspondence advances at a steady offset.

That gives ground truth: the exact ranges of the source files that were worth
keeping, according to the person who did the edit. Which is the only opinion
that matters when calibrating the detector.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import ffmpeg

# Fingerprint geometry. Small enough to be fast, big enough to discriminate:
# POV footage is highly self-similar (the handlebar fills a third of every
# frame), so a coarser hash produces false matches between unrelated trails.
FP_WIDTH, FP_HEIGHT = 64, 36
FP_FPS = 5.0

# A real match correlates near 1.0. Unrelated POV frames from the same ride
# still sit around 0.70, so the bar has to be high.
MATCH_THRESHOLD = 0.95
MIN_RUN_FRAMES = 2
MAX_DRIFT_SECONDS = 0.5

# Once a seed match is confirmed we walk outward along the diagonal at a much
# lower bar. Finding the clip at all is the hard part and needs a strict
# threshold; deciding where it ends does not, because we already know exactly
# which edit frame every raw frame should line up with. Without this the
# matcher returns half-second slivers of six-second cuts, which understates
# what the editor actually kept and poisons any calibration built on it.
EXTEND_FLOOR = 0.85


@dataclass
class KeptRange:
    """A slice of a source file that appears in the edit."""

    source: Path
    start: float
    end: float
    edit_time: float
    confidence: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlaps(self, start: float, end: float) -> float:
        """Seconds of overlap with an arbitrary window."""
        return max(0.0, min(self.end, end) - max(self.start, start))


def fingerprint(path: Path, cache_dir: Path | None = None) -> np.ndarray:
    """Per-frame appearance vectors, standardised so grading cannot fool them.

    Each frame becomes a unit vector with its mean removed, which makes the
    dot product between two frames their correlation directly. Removing the
    mean is what buys immunity to the brightness and contrast changes an
    editor applies.
    """
    cached = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(f"{path.resolve()}|{path.stat().st_mtime_ns}|{FP_FPS}".encode()).hexdigest()[:16]
        cached = cache_dir / f"{path.stem}_{key}.npy"
        if cached.exists():
            return np.load(cached)

    command = [
        ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-vf", f"fps={FP_FPS:g},scale={FP_WIDTH}:{FP_HEIGHT},format=gray",
        "-f", "rawvideo", "-",
    ]
    proc = subprocess.run(command, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return np.empty((0, FP_WIDTH * FP_HEIGHT), dtype=np.float32)

    pixels = FP_WIDTH * FP_HEIGHT
    data = np.frombuffer(proc.stdout, dtype=np.uint8)
    count = data.size // pixels
    if count == 0:
        return np.empty((0, pixels), dtype=np.float32)

    frames = data[: count * pixels].reshape(count, pixels).astype(np.float32)
    frames -= frames.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(frames, axis=1, keepdims=True)
    norms[norms < 1e-6] = 1.0
    frames /= norms

    if cached is not None:
        np.save(cached, frames)
    return frames


def _runs_above(mask: np.ndarray, minimum: int) -> list[tuple[int, int]]:
    runs = []
    start = None
    for i in range(mask.size + 1):
        active = bool(mask[i]) if i < mask.size else False
        if active and start is None:
            start = i
        elif not active and start is not None:
            if i - start >= minimum:
                runs.append((start, i))
            start = None
    return runs


def _extend(similarity: np.ndarray, a: int, b: int, lag: int) -> tuple[int, int]:
    """Grow a confirmed seed along its diagonal for as long as it holds up."""
    n_raw, n_edit = similarity.shape

    start = a
    while start > 0:
        j = start - 1 + lag
        if not (0 <= j < n_edit) or similarity[start - 1, j] < EXTEND_FLOOR:
            break
        start -= 1

    end = b
    while end < n_raw:
        j = end + lag
        if not (0 <= j < n_edit) or similarity[end, j] < EXTEND_FLOOR:
            break
        end += 1

    return start, end


def _merge_spans(spans: list[tuple[int, int, float]]) -> list[tuple[int, int, float]]:
    """Fuse overlapping or adjacent extended spans."""
    if not spans:
        return []
    spans = sorted(spans)
    merged = [list(spans[0])]
    for start, end, confidence in spans[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = max(merged[-1][2], confidence)
        else:
            merged.append([start, end, confidence])
    return [(int(a), int(b), float(c)) for a, b, c in merged]


def find_kept(edit_path: Path, raw_paths: list[Path], cache_dir: Path | None = None) -> list[KeptRange]:
    """Locate every slice of `raw_paths` that made it into `edit_path`."""
    edit = fingerprint(edit_path, cache_dir)
    if edit.shape[0] == 0:
        return []

    kept: list[KeptRange] = []
    for raw_path in raw_paths:
        raw = fingerprint(raw_path, cache_dir)
        if raw.shape[0] == 0:
            continue

        # Both sides are unit vectors, so this product is the correlation.
        similarity = raw @ edit.T
        best = similarity.max(axis=1)
        best_index = similarity.argmax(axis=1)

        raw_time = np.arange(raw.shape[0]) / FP_FPS
        edit_time = best_index / FP_FPS

        spans: list[tuple[int, int, float]] = []
        for a, b in _runs_above(best > MATCH_THRESHOLD, MIN_RUN_FRAMES):
            # A genuine cut advances through both timelines together, so the
            # offset between them holds steady. Isolated high-correlation
            # frames are coincidence and drift all over the place.
            offsets = edit_time[a:b] - raw_time[a:b]
            if float(offsets.std()) > MAX_DRIFT_SECONDS:
                continue

            lag = int(round(float(np.median(best_index[a:b] - np.arange(a, b)))))
            start, end = _extend(similarity, a, b, lag)
            spans.append((start, end, float(best[a:b].mean())))

        for start, end, confidence in _merge_spans(spans):
            kept.append(
                KeptRange(
                    source=raw_path,
                    start=float(start / FP_FPS),
                    end=float(end / FP_FPS),
                    edit_time=float(edit_time[start]),
                    confidence=confidence,
                )
            )

    kept.sort(key=lambda k: (str(k.source), k.start))
    return kept


# --------------------------------------------------------------------------
# agreement report
# --------------------------------------------------------------------------

@dataclass
class Agreement:
    """How well the detector's picks line up with what was actually kept."""

    kept_seconds: float = 0.0
    picked_seconds: float = 0.0
    # Seconds the detector picked that overlap something the editor kept.
    hit_seconds: float = 0.0
    # Seconds the editor kept that the detector never proposed.
    missed_seconds: float = 0.0
    per_file: dict[str, dict] = None  # type: ignore[assignment]

    @property
    def precision(self) -> float:
        """Share of proposed footage that the editor actually wanted."""
        return self.hit_seconds / self.picked_seconds if self.picked_seconds else 0.0

    @property
    def recall(self) -> float:
        """Share of wanted footage the detector managed to propose."""
        return (self.kept_seconds - self.missed_seconds) / self.kept_seconds if self.kept_seconds else 0.0


def compare(kept: list[KeptRange], segments, tolerance: float = 1.0) -> Agreement:
    """Score the detector's segments against the editor's real choices."""
    report = Agreement(per_file={})

    by_file_kept: dict[str, list[KeptRange]] = {}
    for item in kept:
        by_file_kept.setdefault(item.source.name, []).append(item)

    by_file_picked: dict[str, list] = {}
    for segment in segments:
        by_file_picked.setdefault(segment.source.name, []).append(segment)

    names = set(by_file_kept) | set(by_file_picked)
    for name in sorted(names):
        keeps = by_file_kept.get(name, [])
        picks = by_file_picked.get(name, [])

        kept_total = sum(k.duration for k in keeps)
        picked_total = sum(p.duration for p in picks)

        hit = 0.0
        for pick in picks:
            if any(k.overlaps(pick.start - tolerance, pick.end + tolerance) > 0 for k in keeps):
                hit += pick.duration

        missed = 0.0
        for keep in keeps:
            covered = any(
                keep.overlaps(p.start - tolerance, p.end + tolerance) > 0 for p in picks
            )
            if not covered:
                missed += keep.duration

        report.kept_seconds += kept_total
        report.picked_seconds += picked_total
        report.hit_seconds += hit
        report.missed_seconds += missed
        report.per_file[name] = {
            "conservado_s": round(kept_total, 1),
            "propuesto_s": round(picked_total, 1),
            "acertado_s": round(hit, 1),
            "perdido_s": round(missed, 1),
        }

    return report
