"""Pull the GPMF telemetry track out of a GoPro MP4 and turn it into time series.

A GoPro writes one GPMF payload per second into a data track tagged `gpmd`.
We ask ffprobe for the packet table (so we know each payload's presentation
timestamp), dump the track to a temporary file, split it back into payloads,
and decode each one.

The result is `Telemetry`: plain numpy arrays on a shared time base, in SI
units and camera-independent (we use vector magnitudes, so it does not matter
which axis GoPro assigned to which direction on which model).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import ffmpeg, gpmf

# Data keys we know how to read, mapped to the attribute we store them under.
_VECTOR_KEYS = {"ACCL": "accl", "GYRO": "gyro"}
_GPS_KEYS = ("GPS5", "GPS9")

GRAVITY = 9.80665  # m/s^2


@dataclass
class Series:
    """A sampled signal: timestamps in seconds, values on the same index."""

    t: np.ndarray = field(default_factory=lambda: np.empty(0))
    v: np.ndarray = field(default_factory=lambda: np.empty(0))

    def __len__(self) -> int:
        return int(self.t.size)

    @property
    def rate(self) -> float:
        """Approximate sample rate in Hz."""
        if self.t.size < 2:
            return 0.0
        span = float(self.t[-1] - self.t[0])
        return (self.t.size - 1) / span if span > 0 else 0.0


@dataclass
class Telemetry:
    source: Path
    duration: float
    accel: Series = field(default_factory=Series)  # magnitude, m/s^2
    gyro: Series = field(default_factory=Series)   # magnitude, rad/s
    speed: Series = field(default_factory=Series)  # 3D ground speed, m/s
    altitude: Series = field(default_factory=Series)  # metres
    # The camera's own wind meter, from the WNDM stream it keeps so it can
    # decide when to switch on wind noise reduction. It is measured before the
    # automatic gain control touches anything, which makes it the one usable
    # speed proxy on a file recorded with GPS off: measured 2.51x separation
    # between riding hard and pedalling, against 1.27x for recorded loudness.
    wind: Series = field(default_factory=Series)
    track: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))  # lat, lon
    gps_fix: int = 0
    # Gravity direction in camera coordinates: (t, x, y, z) per row. Lets us
    # measure how far the bike is tipped away from its neutral attitude, which
    # is what a banked corner or a steep chute actually looks like physically.
    gravity: np.ndarray = field(default_factory=lambda: np.empty((0, 4)))

    @property
    def has_motion(self) -> bool:
        """True if we got usable accelerometer data."""
        return len(self.accel) > 0

    @property
    def has_gps(self) -> bool:
        return len(self.speed) > 0 and self.gps_fix >= 2

    @property
    def has_gravity(self) -> bool:
        return self.gravity.shape[0] > 0

    def lean_angles(self) -> Series:
        """Degrees away from this file's neutral camera attitude.

        Taken relative to the median orientation rather than to any fixed
        axis, so it does not matter how the camera is mounted or aimed.
        A sustained excursion is a carved turn or a steep pitch; a spiky one
        is just the bike bucking underneath you.
        """
        if not self.has_gravity:
            return Series()

        vectors = self.gravity[:, 1:4]
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        good = norms[:, 0] > 1e-6
        if not good.any():
            return Series()

        unit = np.zeros_like(vectors)
        unit[good] = vectors[good] / norms[good]

        neutral = np.median(unit[good], axis=0)
        neutral_norm = np.linalg.norm(neutral)
        if neutral_norm < 1e-6:
            return Series()
        neutral = neutral / neutral_norm

        cosine = np.clip(unit @ neutral, -1.0, 1.0)
        return Series(self.gravity[good, 0], np.degrees(np.arccos(cosine[good])))

    def summary(self) -> dict:
        out: dict = {
            "duracion_s": round(self.duration, 1),
            "accel_hz": round(self.accel.rate),
            "gyro_hz": round(self.gyro.rate),
            "gps": bool(self.has_gps),
        }
        if self.has_gps:
            moving = self.speed.v[self.speed.v > 1.0]
            if moving.size:
                out["vel_max_kmh"] = round(float(moving.max()) * 3.6, 1)
                out["vel_media_kmh"] = round(float(moving.mean()) * 3.6, 1)
        if self.has_motion:
            out["impacto_max_g"] = round(float(self.accel.v.max()) / GRAVITY, 1)
        return out


def find_gpmd_stream(path: Path) -> int | None:
    """Index of the `gpmd` data stream, or None if the file has no telemetry."""
    for stream in ffmpeg.streams(path):
        if stream.get("codec_tag_string") == "gpmd":
            return int(stream["index"])
    return None


def _packet_table(path: Path, stream_index: int) -> list[tuple[float, float, int]]:
    """(pts_seconds, duration_seconds, size_bytes) for every telemetry packet."""
    data = ffmpeg.probe(
        path,
        "-select_streams", str(stream_index),
        "-show_packets",
        "-show_entries", "packet=pts_time,duration_time,size",
    )
    table: list[tuple[float, float, int]] = []
    for packet in data.get("packets", []):
        try:
            table.append(
                (
                    float(packet.get("pts_time") or 0.0),
                    float(packet.get("duration_time") or 0.0),
                    int(packet.get("size") or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    return table


def _dump_stream(path: Path, stream_index: int) -> bytes:
    with tempfile.TemporaryDirectory(prefix="gpmf_") as tmp:
        out = Path(tmp) / "telemetry.bin"
        ffmpeg.run(["-y", "-i", str(path), "-map", f"0:{stream_index}", "-c", "copy", "-f", "data", str(out)])
        return out.read_bytes()


def extract(path: Path, time_offset: float = 0.0) -> Telemetry:
    """Read every telemetry signal out of one MP4.

    `time_offset` is added to all timestamps, so chaptered recordings
    (GX01xxxx, GX02xxxx) can be stitched onto one continuous timeline.
    """
    result = Telemetry(source=path, duration=ffmpeg.duration(path))

    stream_index = find_gpmd_stream(path)
    if stream_index is None:
        return result

    packets = _packet_table(path, stream_index)
    blob = _dump_stream(path, stream_index)
    if not packets or not blob:
        return result

    # Collected per-sample values before we turn them into arrays.
    buckets: dict[str, list[tuple[float, float]]] = {"accl": [], "gyro": [], "wind": []}
    gps_rows: list[tuple[float, float, float, float, float]] = []  # t, lat, lon, alt, speed
    gravity_rows: list[tuple[float, float, float, float]] = []  # t, x, y, z

    cursor = 0
    for pts, packet_duration, size in packets:
        payload = blob[cursor : cursor + size]
        cursor += size
        if len(payload) < 8:
            continue

        # Fall back to a nominal 1 s payload when the container omits duration.
        span = packet_duration if packet_duration > 0 else 1.0
        base = pts + time_offset

        for devc in gpmf.parse(payload):
            if devc.key != "DEVC":
                continue
            for strm in devc.find_all("STRM"):
                scal_node = strm.find("SCAL")
                scal = [float(x) for x in scal_node.values()] if scal_node else []

                for key, attr in _VECTOR_KEYS.items():
                    node = strm.find(key)
                    if node is None:
                        continue
                    samples = gpmf.scale_values(node.values(), scal)
                    if not samples:
                        continue
                    step = span / len(samples)
                    for i, axes in enumerate(samples):
                        if not isinstance(axes, list):
                            continue
                        magnitude = float(np.sqrt(sum(a * a for a in axes)))
                        buckets[attr].append((base + i * step, magnitude))

                # WNDM is [wind_enable, meter]; only the meter carries level.
                wind_node = strm.find("WNDM")
                if wind_node is not None:
                    samples = wind_node.values()
                    if samples:
                        step = span / len(samples)
                        for i, row in enumerate(samples):
                            if isinstance(row, list) and len(row) >= 2:
                                buckets["wind"].append((base + i * step, float(row[1])))

                grav_node = strm.find("GRAV")
                if grav_node is not None:
                    samples = gpmf.scale_values(grav_node.values(), scal)
                    if samples:
                        step = span / len(samples)
                        for i, axes in enumerate(samples):
                            if isinstance(axes, list) and len(axes) >= 3:
                                gravity_rows.append((base + i * step, axes[0], axes[1], axes[2]))

                for key in _GPS_KEYS:
                    node = strm.find(key)
                    if node is None:
                        continue
                    fix_node = strm.find("GPSF")
                    if fix_node:
                        fix_values = fix_node.values()
                        if fix_values:
                            result.gps_fix = max(result.gps_fix, int(fix_values[0]))
                    samples = gpmf.scale_values(node.values(), scal)
                    if not samples:
                        continue
                    step = span / len(samples)
                    for i, row in enumerate(samples):
                        if not isinstance(row, list) or len(row) < 5:
                            continue
                        # GPS5: lat, lon, altitude, 2D speed, 3D speed
                        # GPS9: lat, lon, altitude, 2D speed, 3D speed, days, secs, dop, fix
                        gps_rows.append((base + i * step, row[0], row[1], row[2], row[4]))

    result.accel = _to_series(buckets["accl"])
    result.gyro = _to_series(buckets["gyro"])
    result.wind = _to_series(buckets["wind"])

    if gps_rows:
        gps_rows.sort(key=lambda r: r[0])
        arr = np.asarray(gps_rows, dtype=float)
        result.speed = Series(arr[:, 0], arr[:, 4])
        result.altitude = Series(arr[:, 0], arr[:, 3])
        result.track = arr[:, 1:3]

    if gravity_rows:
        gravity_rows.sort(key=lambda r: r[0])
        result.gravity = np.asarray(gravity_rows, dtype=float)

    return result


def _to_series(pairs: list[tuple[float, float]]) -> Series:
    if not pairs:
        return Series()
    pairs.sort(key=lambda p: p[0])
    arr = np.asarray(pairs, dtype=float)
    return Series(arr[:, 0], arr[:, 1])
