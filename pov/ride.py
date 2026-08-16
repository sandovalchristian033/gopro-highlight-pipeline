"""A ride: one folder of GoPro files, analysed end to end."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import ajustes as ajustes_mod
from . import audio as audio_mod
from . import bookends as bookends_mod
from . import ffmpeg, naming, segments as segments_mod, signals as signals_mod, telemetry as telemetry_mod
from .naming import recording_order
from .segments import Segment

VIDEO_SUFFIXES = {".mp4", ".MP4", ".mov", ".MOV", ".lrv", ".LRV"}


@dataclass
class SourceFile:
    path: Path
    duration: float = 0.0
    summary: dict = field(default_factory=dict)
    segments: list[Segment] = field(default_factory=list)
    note: str = ""

    @property
    def recording_id(self) -> str:
        """Chapters of one long recording share this id."""
        return naming.recording_id(self.path)


@dataclass
class Ride:
    root: Path
    files: list[SourceFile] = field(default_factory=list)
    selected: list[Segment] = field(default_factory=list)
    threshold: float = 0.0  # ride-wide score bar a moment had to clear
    # Los clips de apertura y cierre, si se pudieron encontrar. Van dentro de
    # `selected` (primero y ultimo) para que se rendericen y se numeren como
    # todo lo demas; se guardan aparte solo para poder reportarlos.
    intro: Segment | None = None
    outro: Segment | None = None
    bookend_notes: list[str] = field(default_factory=list)
    ajustes: "ajustes_mod.Ajustes" = field(default_factory=lambda: ajustes_mod.Ajustes())

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def clips_dir(self) -> Path:
        return self.root / "clips"

    @property
    def reel_dir(self) -> Path:
        return self.root / "reel"

    @property
    def final_dir(self) -> Path:
        """El video que se sube. Aparte de `reel/` a proposito: `limpiar` barre
        el reel por ser material de revision regenerable, y el video final no
        puede irse en esa barrida."""
        return self.root / "final"

    @property
    def analysis_file(self) -> Path:
        return self.root / "analysis.json"

    @property
    def cutlist_file(self) -> Path:
        return self.root / "cortes.csv"

    def total_raw_seconds(self) -> float:
        return sum(f.duration for f in self.files)

    def total_selected_seconds(self) -> float:
        return sum(s.duration for s in self.selected)


def source_files(ride_root: Path) -> list[Path]:
    """Every source video in the ride, in recording order.

    Prefers `raw/` but falls back to the ride root, so pointing the tool at a
    folder you already dumped footage into just works.
    """
    for folder in (ride_root / "raw", ride_root):
        if not folder.is_dir():
            continue
        found = [
            p
            for p in sorted(folder.iterdir())
            if p.is_file() and p.suffix.lower() == ".mp4" and not p.stem.startswith(".")
        ]
        if found:
            return sorted(found, key=recording_order)
    return []


def analyse(ride_root: Path, cfg, on_progress=None) -> Ride:
    """Analyse every file in a ride and pick the candidate segments."""
    ride = Ride(root=ride_root, ajustes=ajustes_mod.load(ride_root))
    paths = source_files(ride_root)
    if not paths:
        raise FileNotFoundError(
            f"No hay archivos .mp4 en {ride_root / 'raw'} ni en {ride_root}."
        )

    # Pass 1: read every file's signals, still on their own raw scales.
    raws: list[signals_mod.RawSignals] = []
    for index, path in enumerate(paths, start=1):
        if on_progress:
            on_progress(index, len(paths), path.name)

        source = SourceFile(path=path)
        try:
            tele = telemetry_mod.extract(path)
        except Exception as exc:  # a single bad file must not kill the ride
            source.note = f"telemetria ilegible: {exc}"
            tele = telemetry_mod.Telemetry(source=path, duration=ffmpeg.duration(path))

        source.duration = tele.duration
        source.summary = tele.summary()
        if not tele.has_motion:
            source.note = source.note or "sin telemetria: puntaje solo por audio"
        elif not tele.has_gps:
            source.note = source.note or "sin GPS: sin velocidad ni deteccion de caidas"

        loudness = audio_mod.loudness(path)
        raws.append(signals_mod.analyse_file(tele, cfg, audio=loudness))
        ride.files.append(source)

    # Derive one scale for the whole ride, so scores from different files are
    # actually comparable. Normalising each file against itself would make the
    # roughest moment of the dullest fire road tie with the best descent.
    stats = signals_mod.RideStats.collect(raws, cfg)

    # Pass 2: score every file against that shared scale.
    curves = [signals_mod.finalise(raw, cfg, stats) for raw in raws]

    # One bar for the whole ride. A file that is dull compared to the rest of
    # the day should contribute nothing, not its own personal best.
    threshold = segments_mod.ride_threshold(curves, cfg)
    ride.threshold = threshold

    all_segments: list[Segment] = []
    eligible_seconds = 0.0
    for source, curve in zip(ride.files, curves):
        if ride.ajustes.excluido(source.path):
            # Sigue contando como material del ride y sigue pudiendo aportar
            # apertura; simplemente no propone accion.
            source.note = source.note or "excluido a mano en ajustes.toml"
            continue
        eligible_seconds += source.duration
        source.segments = [
            s
            for s in segments_mod.build(source.path, curve, cfg, threshold)
            if not ride.ajustes.descartado(s.source, s.start)
        ]
        all_segments.extend(source.segments)

    # El presupuesto se mide contra el material del que se puede sacar, no
    # contra el ride entero: si media grabacion queda fuera, pedirle el mismo
    # numero de segundos al resto obliga a bajar el liston.
    budget = ride.ajustes.reel_segundos or cfg.reel_budget(eligible_seconds)
    ride.selected = segments_mod.select(all_segments, budget)

    # La apertura y el cierre se buscan aparte, y despues de seleccionar: no
    # compiten por el presupuesto del reel ni se rankean contra la accion.
    # Salen del primer y del ultimo archivo en orden de grabacion, que es donde
    # por construccion estan el arranque y el final del ride.
    intros = bookends_mod.find_intros(paths, curves, cfg)
    missing: list[str] = []
    if ride.ajustes.aperturas:
        # Cuando Chris ya eligio la apertura, su orden manda sobre las reglas.
        found = {b.segment.source.stem.lower(): b for b in intros if b.segment is not None}
        chosen = []
        for wanted in ride.ajustes.aperturas:
            match = found.get(Path(wanted).stem.lower())
            if match is not None:
                chosen.append(match)
            else:
                missing.append(f"intro: pediste {wanted} pero no hay toma de arranque ahi")
        intros = chosen

    if ride.ajustes.cierre:
        wanted, when = ride.ajustes.cierre
        index = next(
            (i for i, p in enumerate(paths) if p.stem.lower() == Path(wanted).stem.lower()),
            None,
        )
        if index is None:
            outro = bookends_mod.Bookend(None, f"pediste cerrar con {wanted} y no existe ese archivo")
        else:
            outro = bookends_mod.forced_outro(paths[index], curves[index], when)
    else:
        outro = bookends_mod.find_outro(paths[-1], curves[-1], cfg)
    ride.intro = next((b.segment for b in intros if b.segment is not None), None)
    ride.outro = outro.segment
    ride.bookend_notes = (
        [f"intro: {b.note}" for b in intros] + missing + [f"final: {outro.note}"]
    )

    # Las aperturas van todas al frente para poder compararlas de una, y el
    # cierre al final. Su lugar es fijo: no es un orden cronologico, es la
    # forma del video.
    for offset, candidate in enumerate(b.segment for b in intros if b.segment is not None):
        ride.selected.insert(offset, candidate)
    if ride.outro is not None:
        ride.selected.append(ride.outro)
    return ride


def write_reports(ride: Ride, cfg) -> None:
    """Persist analysis.json and the human-readable cut list."""
    ride.root.mkdir(parents=True, exist_ok=True)

    payload = {
        "ride": ride.name,
        "generado": datetime.now().isoformat(timespec="seconds"),
        "material_bruto_min": round(ride.total_raw_seconds() / 60, 1),
        "seleccionado_min": round(ride.total_selected_seconds() / 60, 1),
        "archivos": [
            {
                "archivo": f.path.name,
                "duracion_s": round(f.duration, 1),
                "telemetria": f.summary,
                "candidatos": len(f.segments),
                "nota": f.note,
            }
            for f in ride.files
        ],
        "segmentos": [
            {"orden": i, **s.to_dict(), "ranking": s.rank}
            for i, s in enumerate(ride.selected, start=1)
        ],
        "extremos": ride.bookend_notes,
        "config": {
            "presupuesto_reel_s": round(cfg.reel_budget(ride.total_raw_seconds()), 1),
            "umbral_ride": round(ride.threshold, 1),
            "pre_roll": cfg.pre_roll,
            "post_roll": cfg.post_roll,
            "weights": cfg.weights,
        },
    }
    ride.analysis_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    with ride.cutlist_file.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(
            ["orden", "ranking", "archivo", "inicio", "fin", "duracion_s", "puntaje", "vel_max_kmh", "etiqueta"]
        )
        for order, segment in enumerate(ride.selected, start=1):
            writer.writerow(
                [
                    order,
                    segment.rank,
                    segment.source.name,
                    segment.timecode(),
                    f"{int(segment.end // 60):02d}:{segment.end % 60:05.2f}",
                    f"{segment.duration:.2f}",
                    f"{segment.score:.1f}",
                    f"{segment.peak_speed_kmh:.1f}",
                    segment.headline(),
                ]
            )


def create(library: Path, label: str | None = None, when: datetime | None = None) -> Path:
    """Make a new dated ride folder and return its path."""
    when = when or datetime.now()
    slug = _slugify(label) if label else ""
    name = f"{when:%Y-%m-%d}" + (f"_{slug}" if slug else "")

    root = library / name
    suffix = 2
    while root.exists() and any(root.iterdir()):
        root = library / f"{name}-{suffix}"
        suffix += 1

    (root / "raw").mkdir(parents=True, exist_ok=True)
    return root


def _slugify(text: str) -> str:
    text = text.strip().lower()
    replacements = {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ñ": "n", "ü": "u"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def resolve(library: Path, name: str | None) -> Path:
    """Find a ride folder by name, by path, or default to the newest one."""
    if name:
        candidate = Path(name)
        if candidate.is_dir():
            return candidate.resolve()
        candidate = library / name
        if candidate.is_dir():
            return candidate
        raise FileNotFoundError(f"No encontre el ride '{name}' en {library}.")

    if not library.is_dir():
        raise FileNotFoundError(f"Todavia no existe {library}. Corre primero: run.py nuevo")

    rides = sorted((p for p in library.iterdir() if p.is_dir()), reverse=True)
    if not rides:
        raise FileNotFoundError(f"No hay rides en {library}.")
    return rides[0]
