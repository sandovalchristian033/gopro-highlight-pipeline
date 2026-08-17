"""Get footage off the camera and into a dated ride folder.

Three sources, tried in this order:

  1. A removable drive with a DCIM/xxxGOPRO tree. This is what you get with an
     SD card reader, and it is by far the fastest path.
  2. The camera over USB, which Windows mounts as an MTP device with no drive
     letter. Handled by scripts/gopro_mtp.ps1 through the Explorer shell.
  3. A folder you already dumped the files into yourself.
"""

from __future__ import annotations

import json
import shutil
import string
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT

MTP_SCRIPT = PROJECT_ROOT / "scripts" / "gopro_mtp.ps1"
VIDEO_EXT = ".mp4"


@dataclass
class IngestResult:
    source: str
    copied: list[Path]
    skipped: list[str]
    seconds: float

    @property
    def total_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.copied if p.exists())


# --------------------------------------------------------------------------
# 0. does it even fit
# --------------------------------------------------------------------------

# Un poco de margen sobre lo que dicen pesar los archivos: dejar el disco
# exactamente a cero rompe cualquier otra cosa que este corriendo, y los
# tamanos que reporta la camara sobre MTP son aproximados.
SPACE_MARGIN = 1.05


def check_space(destination: Path, sizes: list[int]) -> None:
    """Fallar antes de copiar, no a los 20 GB.

    Un ride son ~33 GB. Si no caben, `shutil.copyfile` revienta a mitad de la
    transferencia, despues de media hora, y deja el trabajo por la mitad. Vale
    mucho mas decirlo al principio, cuando el material todavia esta entero en
    la tarjeta y todavia se puede liberar espacio.
    """
    needed = sum(sizes)
    if needed <= 0:
        return
    destination.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination).free
    if free >= needed * SPACE_MARGIN:
        return
    raise RuntimeError(
        f"No cabe: hacen falta {needed / 2**30:.1f} GB y quedan "
        f"{free / 2**30:.1f} GB libres en el disco.\n"
        "  Libera espacio sin tocar nada irrecuperable:\n"
        "    python run.py limpiar --todos\n"
        "  (borra clips, reel y cache de los rides viejos; los originales, el\n"
        "   video final y el analisis se quedan)"
    )


def _pending(sizes: dict[str, int], destination: Path) -> list[int]:
    """Los tamanos de lo que falta por copiar, ignorando lo que ya esta."""
    pending = []
    for name, size in sizes.items():
        target = destination / name
        if target.exists() and size and target.stat().st_size == size:
            continue
        pending.append(size)
    return pending


# --------------------------------------------------------------------------
# 1. removable drive / SD card reader
# --------------------------------------------------------------------------

def find_card_dcim() -> Path | None:
    """A DCIM folder on a removable drive that looks like a GoPro card."""
    for letter in string.ascii_uppercase:
        dcim = Path(f"{letter}:/DCIM")
        try:
            if not dcim.is_dir():
                continue
        except OSError:
            continue
        for sub in dcim.iterdir():
            if sub.is_dir() and "GOPRO" in sub.name.upper():
                return dcim
    return None


def _card_videos(dcim: Path) -> list[Path]:
    found: list[Path] = []
    for sub in sorted(dcim.iterdir()):
        if not sub.is_dir():
            continue
        found += sorted(p for p in sub.iterdir() if p.suffix.lower() == VIDEO_EXT)
    return found


def copy_from_folder(sources: list[Path], destination: Path, on_progress=None) -> tuple[list[Path], list[str]]:
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    skipped: list[str] = []

    for index, source in enumerate(sources, start=1):
        target = destination / source.name
        if target.exists() and target.stat().st_size == source.stat().st_size:
            skipped.append(source.name)
            continue
        if on_progress:
            on_progress(index, len(sources), source.name, source.stat().st_size)
        # Copy to a temp name first so an interrupted transfer never looks
        # like a complete file on the next run.
        staging = destination / (source.name + ".part")
        shutil.copyfile(source, staging)
        staging.replace(target)
        copied.append(target)

    return copied, skipped


# --------------------------------------------------------------------------
# 2. camera over USB (MTP)
# --------------------------------------------------------------------------

def _run_mtp(args: list[str], timeout: int) -> dict:
    if not MTP_SCRIPT.exists():
        return {"ok": False, "error": f"Falta {MTP_SCRIPT}"}

    proc = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(MTP_SCRIPT),
            *args,
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    # The script prints progress lines with Write-Host and one JSON object at
    # the end with Write-Output; take the last line that parses as JSON.
    for line in reversed([l.strip() for l in proc.stdout.splitlines() if l.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"ok": False, "error": (proc.stderr or proc.stdout or "sin respuesta").strip()}


def find_camera_mtp() -> dict | None:
    """Ask Windows whether a GoPro is plugged in over USB."""
    result = _run_mtp(["-List"], timeout=120)
    return result if result.get("ok") else None


def copy_from_mtp(destination: Path, timeout: int = 3600) -> tuple[list[Path], list[str]]:
    destination.mkdir(parents=True, exist_ok=True)
    result = _run_mtp(["-Destination", str(destination)], timeout=timeout)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "fallo la copia por MTP"))

    copied = [destination / name for name in result.get("copied", [])]
    skipped = list(result.get("skipped", []))

    # Finding the camera and copying nothing is not success, it is a silent
    # failure -- and it happened for real: over MTP the shell reports display
    # names with no extension, so the .MP4 filter matched nothing and the run
    # ended in three seconds looking perfectly healthy.
    if not copied and not skipped:
        raise RuntimeError(
            "Encontre la camara pero no copie ningun .MP4.\n"
            "  Revisa que este en modo de transferencia de archivos, y prueba:\n"
            "    powershell -File scripts\\gopro_mtp.ps1 -List"
        )

    return [p for p in copied if p.exists()], skipped


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def run(destination: Path, from_folder: Path | None = None, on_progress=None) -> IngestResult:
    """Copy footage into `destination`, choosing the best available source."""
    started = time.monotonic()

    if from_folder is not None:
        sources = sorted(p for p in from_folder.rglob("*") if p.suffix.lower() == VIDEO_EXT)
        if not sources:
            raise FileNotFoundError(f"No hay .mp4 en {from_folder}")
        check_space(destination, _pending({p.name: p.stat().st_size for p in sources}, destination))
        copied, skipped = copy_from_folder(sources, destination, on_progress)
        return IngestResult("carpeta", copied, skipped, time.monotonic() - started)

    dcim = find_card_dcim()
    if dcim is not None:
        sources = _card_videos(dcim)
        check_space(destination, _pending({p.name: p.stat().st_size for p in sources}, destination))
        copied, skipped = copy_from_folder(sources, destination, on_progress)
        return IngestResult(f"tarjeta SD ({dcim})", copied, skipped, time.monotonic() - started)

    camera = find_camera_mtp()
    if camera is not None:
        listing = {
            str(f.get("name")): int(f.get("size") or 0)
            for f in camera.get("files", [])
            if str(f.get("name", "")).lower().endswith(VIDEO_EXT)
        }
        check_space(destination, _pending(listing, destination))
        copied, skipped = copy_from_mtp(destination)
        return IngestResult(f"USB / MTP ({camera.get('device', 'GoPro')})", copied, skipped, time.monotonic() - started)

    raise FileNotFoundError(
        "No encontre la GoPro.\n"
        "  - Si usas cable: enciende la camara y elige el modo de conexion USB.\n"
        "  - Si usas lector SD: revisa que la tarjeta este montada.\n"
        "  - O pasa la carpeta a mano:  run.py ingesta --desde \"C:\\ruta\\a\\los\\videos\""
    )
