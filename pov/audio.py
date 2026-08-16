"""Loudness as a secondary action signal.

On a helmet-mounted GoPro the microphone is a surprisingly good sensor:
wind noise scales with speed, and impacts, skids and landings are loud. It is
never as precise as the accelerometer, but it costs one cheap ffmpeg pass and
it is the only thing left if a file has no telemetry at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from . import ffmpeg
from .telemetry import Series

SAMPLE_RATE = 8000  # plenty for an energy envelope
FRAME_SECONDS = 0.05


def loudness(path: Path, time_offset: float = 0.0) -> Series:
    """RMS loudness envelope in dBFS, or an empty Series if there is no audio."""
    command = [
        ffmpeg.ffmpeg_path(),
        "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-f", "s16le",
        "-",
    ]
    proc = subprocess.run(command, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return Series()

    samples = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
    frame = int(SAMPLE_RATE * FRAME_SECONDS)
    frames = samples.size // frame
    if frames == 0:
        return Series()

    blocks = samples[: frames * frame].reshape(frames, frame)
    rms = np.sqrt((blocks * blocks).mean(axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-6))

    times = time_offset + (np.arange(frames) + 0.5) * FRAME_SECONDS
    return Series(times, db)
