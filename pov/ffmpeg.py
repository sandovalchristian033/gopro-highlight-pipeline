"""Thin wrappers around the ffmpeg / ffprobe binaries."""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path

# Hide the ffmpeg banner and per-frame noise; we only ever want errors.
_QUIET = ["-hide_banner", "-loglevel", "error"]


class FFmpegMissing(RuntimeError):
    pass


def _resolve(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise FFmpegMissing(
            f"No se encontro '{name}' en el PATH.\n"
            "Instalalo con:  winget install Gyan.FFmpeg\n"
            "y abre una terminal nueva para que tome el PATH."
        )
    return path


def ffmpeg_path() -> str:
    return _resolve("ffmpeg")


def ffprobe_path() -> str:
    return _resolve("ffprobe")


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run ffmpeg and raise with its stderr attached if it fails."""
    proc = subprocess.run(
        [ffmpeg_path(), *_QUIET, *args],
        capture_output=True,
        text=True,
        errors="replace",
        **kwargs,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg fallo:\n{proc.stderr.strip()}")
    return proc


def probe(path: Path, *extra: str) -> dict:
    """Run ffprobe with JSON output."""
    proc = subprocess.run(
        [ffprobe_path(), "-v", "error", "-print_format", "json", *extra, str(path)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe fallo en {path.name}:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout or "{}")


def streams(path: Path) -> list[dict]:
    return probe(path, "-show_streams").get("streams", [])


def video_info(path: Path) -> dict:
    """Width, height, fps and duration of the first video stream."""
    for stream in streams(path):
        if stream.get("codec_type") != "video":
            continue
        num, _, den = stream.get("avg_frame_rate", "0/1").partition("/")
        try:
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        return {
            "width": int(stream.get("width", 0)),
            "height": int(stream.get("height", 0)),
            "fps": round(fps, 3),
            "duration": float(stream.get("duration") or 0.0),
            "codec": stream.get("codec_name", ""),
        }
    raise RuntimeError(f"{path.name} no tiene stream de video.")


def duration(path: Path) -> float:
    info = probe(path, "-show_format").get("format", {})
    return float(info.get("duration") or 0.0)


@functools.cache
def has_nvenc() -> bool:
    """True if the NVIDIA hardware encoder actually works right now.

    Listing `h264_nvenc` in `-encoders` only proves ffmpeg was *built* with it.
    It still fails at runtime when the installed driver is older than the NVENC
    API the build was compiled against, so the only honest test is to encode a
    frame and see what happens.
    """
    try:
        binary = ffmpeg_path()
    except FFmpegMissing:
        return False

    listing = subprocess.run(
        [binary, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if "h264_nvenc" not in listing.stdout:
        return False

    probe_run = subprocess.run(
        [
            binary, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=256x144:rate=30:duration=0.1",
            "-c:v", "h264_nvenc",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return probe_run.returncode == 0


def nvenc_problem() -> str:
    """Why NVENC is unavailable, for reporting. Empty string if it works."""
    if has_nvenc():
        return ""
    try:
        binary = ffmpeg_path()
    except FFmpegMissing:
        return "ffmpeg no esta instalado"

    probe_run = subprocess.run(
        [
            binary, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=256x144:rate=30:duration=0.1",
            "-c:v", "h264_nvenc",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        errors="replace",
    )
    for line in probe_run.stderr.splitlines():
        if "driver" in line.lower() or "nvenc" in line.lower():
            return line.split("] ", 1)[-1].strip()
    return "el encoder por GPU no esta disponible"


def encoder_args(use_nvenc: bool, quality: int, x264_preset: str = "faster") -> list[str]:
    """Video encoder flags, hardware if we have it, x264 otherwise.

    `quality` is a CQ/CRF value: lower is better quality and a bigger file.
    """
    if use_nvenc:
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p5",
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", str(quality),
            "-b:v", "0",
            "-pix_fmt", "yuv420p",
        ]
    return [
        "-c:v", "libx264",
        "-preset", x264_preset,
        "-crf", str(quality),
        "-pix_fmt", "yuv420p",
    ]
