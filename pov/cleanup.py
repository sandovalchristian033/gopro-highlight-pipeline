"""Free disk space from a ride without ever losing something irreplaceable.

The asymmetry here is the whole point. `clips/`, `reel/` and `.huellas/` are
derived: given `raw/` and `analysis.json` they come back exactly as they were,
it just costs render time. `raw/` is different -- Chris formats the SD card
after every ride, so once these files are gone the footage does not exist
anywhere. A cleanup tool that treats those two categories the same is a tool
that eventually deletes a ride.

So derived data is the default and raw has to be asked for by name.

`final/` sits in between and also stays out of the default sweep. It *is*
reproducible, but it is the file Chris uploads, and it used to live inside
`reel/` -- which means the command whose whole job is "free space now that you
are done" would have deleted the deliverable. Being technically regenerable is
not a good enough reason to sweep away the thing the ride was for.

`shorts/` (Fase 2) is in that same category and for the same reason: son
entregables, no material de revision. Rehacerlos no cuesta solo tiempo de
render -- el texto quemado sale de `shorts_guion.toml`, escrito a mano, y hay
que revisar que siga cuadrando. Por eso van detras de la misma bandera que
`final/` y no entran en la barrida por defecto.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Never removed, at any setting: kilobytes each, and they are the only record
# of what the detector found in a ride once the video is out.
KEEP_ALWAYS = {"analysis.json", "cortes.csv"}


@dataclass
class Removable:
    """One folder that could be deleted, and what it would cost to lose it."""

    path: Path
    label: str
    files: int
    size: int
    # Whether re-running the pipeline reproduces this exactly.
    regenerable: bool
    note: str = ""


def _measure(folder: Path) -> tuple[int, int]:
    if not folder.is_dir():
        return 0, 0
    files = [p for p in folder.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def survey(
    ride_root: Path, include_raw: bool = False, include_final: bool = False
) -> list[Removable]:
    """What could be freed in this ride, biggest saving first."""
    found: list[Removable] = []

    candidates = [
        ("clips", "clips limpios", True, "se regeneran con: run.py reel"),
        ("reel", "reel de revision", True, "se regenera con: run.py reel"),
        (".huellas", "cache de huellas", True, "solo cache de 'comparar'"),
    ]
    if include_final:
        candidates.append(
            ("final", "VIDEO FINAL", True, "es el que subes; vuelve con: run.py completo")
        )
        candidates.append(
            ("shorts", "SHORTS 9:16", True, "los que subes; vuelven con: run.py shorts")
        )
    if include_raw:
        candidates.append(
            ("raw", "ARCHIVOS ORIGINALES", False, "no se pueden recuperar")
        )

    for name, label, regenerable, note in candidates:
        folder = ride_root / name
        files, size = _measure(folder)
        if files:
            found.append(
                Removable(
                    path=folder,
                    label=label,
                    files=files,
                    size=size,
                    regenerable=regenerable,
                    note=note,
                )
            )

    return sorted(found, key=lambda r: r.size, reverse=True)


def remove(items: list[Removable]) -> int:
    """Delete the surveyed folders. Returns bytes actually freed.

    Deletes file by file rather than with `rmtree` so that anything not put
    there by the pipeline -- a note, a rendered export someone parked in the
    folder -- survives, along with the folder itself.
    """
    freed = 0
    for item in items:
        if not item.path.is_dir():
            continue
        for path in sorted(item.path.rglob("*"), reverse=True):
            if path.is_file():
                if path.name in KEEP_ALWAYS:
                    continue
                freed += path.stat().st_size
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    return freed
