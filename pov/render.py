"""Render the two deliverables: clean clips for editing, and a labelled reel.

`clips/`  full resolution, no overlays. The individual cuts.
`reel/`   one 1080p video with every candidate back to back and a burned-in
          label saying which file it came from, its timecode, why it was
          picked and how it ranked. This is the only thing you have to watch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg
from .ride import Ride
from .segments import Segment

REEL_NAME = "reel_candidatos.mp4"
LABELS_NAME = "labels.ass"
MASTER_NAME = "video_completo.mp4"
ORDER_NAME = "orden.txt"


@dataclass
class RenderResult:
    clips: list[Path]
    reel: Path | None


# --------------------------------------------------------------------------
# clean clips
# --------------------------------------------------------------------------

def clip_filename(order: int, segment: Segment) -> str:
    minutes, seconds = divmod(segment.start, 60)
    label = _slug(segment.headline())
    return (
        f"{order:02d}_{segment.score:03.0f}pts_{label}"
        f"_{segment.source.stem}_{int(minutes):02d}m{int(seconds):02d}s.mp4"
    )


def _slug(text: str) -> str:
    text = text.replace(".", "").replace(" ", "-")
    return re.sub(r"[^A-Za-z0-9\-]", "", text)[:28].strip("-") or "accion"


# Matches only what `clip_filename` produces, so a stray file someone dropped
# in the folder by hand is never touched.
_GENERATED = re.compile(r"^\d{2}_\d{3}pts_.*\.mp4$")


def _clear_stale_clips(folder: Path, keep: set[str]) -> None:
    """Delete clips from an earlier run that this run no longer produces.

    Re-analysing with different settings changes both the numbering and the
    timecodes in the filenames, so without this the folder ends up holding two
    generations of clips side by side. That is worse than useless: the numbers
    in the reel no longer line up with the numbers on disk, which is the one
    thing this whole workflow depends on.
    """
    for path in folder.glob("*.mp4"):
        if path.name not in keep and _GENERATED.match(path.name):
            path.unlink()


# Cuanto puede desviarse la duracion real de un clip de la que dice el analisis
# antes de considerarlo otro clip. Medido sobre un ride entero: la diferencia
# maxima entre lo que pide el analisis y lo que produce ffmpeg fue 0.05 s.
CLIP_TOLERANCE_SECONDS = 0.30


def clip_is_current(path: Path, segment: Segment) -> bool:
    """Si el archivo en disco es de verdad el corte que pide el analisis.

    Reutilizar clips ya renderizados ahorra media hora, pero "existe y pesa
    algo" no alcanza como prueba de que sea el correcto, y las dos formas de
    equivocarse ya pasaron o estuvieron a punto:

    - un render interrumpido a mitad deja un archivo truncado, con tamano > 0;
    - cambiar `post_roll` o el recorte de cola mueve el final del corte **sin
      cambiar el nombre**, porque el nombre solo lleva orden, puntaje, etiqueta
      y segundo de inicio.

    En los dos casos el clip viejo se colaria al video final sin un solo error
    en pantalla, que es la peor forma de fallar. Un ffprobe por clip cuesta
    milisegundos y lo cierra.
    """
    try:
        return abs(ffmpeg.duration(path) - segment.duration) <= CLIP_TOLERANCE_SECONDS
    except Exception:
        return False  # ilegible es motivo de sobra para rehacerlo


def render_clips(ride: Ride, cfg, use_nvenc: bool, on_progress=None) -> list[Path]:
    ride.clips_dir.mkdir(parents=True, exist_ok=True)
    encoder = ffmpeg.encoder_args(use_nvenc, cfg.clip_quality, cfg.x264_preset)
    _clear_stale_clips(
        ride.clips_dir,
        {clip_filename(i, s) for i, s in enumerate(ride.selected, start=1)},
    )
    written: list[Path] = []

    for order, segment in enumerate(ride.selected, start=1):
        out = ride.clips_dir / clip_filename(order, segment)
        if on_progress:
            on_progress(order, len(ride.selected), out.name)
        if out.exists() and out.stat().st_size > 0 and clip_is_current(out, segment):
            written.append(out)
            continue

        ffmpeg.run(
            [
                "-y",
                "-ss", f"{segment.start:.3f}",
                "-i", str(segment.source),
                "-t", f"{segment.duration:.3f}",
                *encoder,
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(out),
            ]
        )
        written.append(out)

    return written


# --------------------------------------------------------------------------
# labelled review reel
# --------------------------------------------------------------------------

def _has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in ffmpeg.streams(path))


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def build_labels(ride: Ride, width: int, height: int) -> str:
    """An ASS subtitle track describing every clip on the reel timeline."""
    total = len(ride.selected)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Info,Arial,{max(22, height // 30)},&H00FFFFFF,&H000000FF,&H00000000,&HB4000000,1,0,0,0,100,100,0,0,3,2,0,7,36,36,30,1
Style: Flash,Arial,{max(34, height // 16)},&H0000D7FF,&H000000FF,&H00000000,&HB4000000,1,0,0,0,100,100,0,0,3,3,0,2,36,36,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = []
    cursor = 0.0
    for order, segment in enumerate(ride.selected, start=1):
        start, end = cursor, cursor + segment.duration
        cursor = end

        role = {
            "intro": "APERTURA del video",
            "intro-alt": "APERTURA alternativa (otro angulo)",
            "intro-cam": "APERTURA con la camara en la mano",
            "outro": "CIERRE del video",
        }.get(
            segment.role, f"ranking {segment.rank}  ·  {segment.score:.0f} pts"
        )
        info = " \\N ".join(
            [
                f"#{order:02d}/{total}  ·  {role}",
                f"{segment.source.name}  @  {segment.timecode()}",
                f"{_ass_escape(segment.headline())}"
                + (f"  ·  {segment.peak_speed_kmh:.0f} km/h" if segment.peak_speed_kmh else ""),
            ]
        )
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Info,,0,0,0,,{info}")

        flash = f"#{order:02d}  {_ass_escape(segment.headline())}"
        lines.append(
            f"Dialogue: 1,{_ass_time(start)},{_ass_time(min(start + 1.6, end))},Flash,,0,0,0,,{flash}"
        )

    return header + "\n".join(lines) + "\n"


def render_reel(ride: Ride, clips: list[Path], cfg, use_nvenc: bool) -> Path | None:
    """Concatenate the clean clips into one 1080p reel with labels burned in."""
    if not clips:
        return None

    ride.reel_dir.mkdir(parents=True, exist_ok=True)

    # Fixed 16:9 canvas rather than one derived from the source. Deriving it
    # gave odd sizes like 1922x1080 for a 2704x1520 GoPro file, and any source
    # aspect is handled by the decrease-and-pad below anyway.
    height = int(cfg.reel_height)
    width = int(round(height * 16 / 9 / 2)) * 2

    labels = ride.reel_dir / LABELS_NAME
    labels.write_text(build_labels(ride, width, height), encoding="utf-8")

    # The concat *filter* (not the demuxer) so clips with different source
    # resolutions or frame rates still stitch together cleanly.
    inputs: list[str] = []
    graph: list[str] = []
    concat_refs: list[str] = []
    silent_index: int | None = None

    for i, clip in enumerate(clips):
        inputs += ["-i", str(clip)]
        graph.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]"
        )
        if _has_audio(clip):
            graph.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
            concat_refs.append(f"[v{i}][a{i}]")
        else:
            if silent_index is None:
                silent_index = len(clips)
                inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            graph.append(f"[{silent_index}:a]aformat=channel_layouts=stereo,atrim=0:30[a{i}]")
            concat_refs.append(f"[v{i}][a{i}]")

    graph.append(f"{''.join(concat_refs)}concat=n={len(clips)}:v=1:a=1[cv][ca]")
    graph.append(f"[cv]ass={LABELS_NAME}[vout]")

    out = ride.reel_dir / REEL_NAME
    args = [
        "-y",
        *inputs,
        "-filter_complex", ";".join(graph),
        "-map", "[vout]",
        "-map", "[ca]",
        *ffmpeg.encoder_args(use_nvenc, cfg.reel_quality, cfg.x264_preset),
        # CRF sets quality but does not bound size; the cap does.
        "-maxrate", f"{cfg.reel_max_mbps:g}M",
        "-bufsize", f"{cfg.reel_max_mbps * 2:g}M",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        out.name,
    ]
    # Run from the reel folder so the .ass path in the filter needs no escaping
    # (Windows drive colons are a nightmare inside ffmpeg filter graphs).
    ffmpeg.run(args, cwd=ride.reel_dir)
    return out


def render_master(ride: Ride, clips: list[Path]) -> Path | None:
    """Los mismos clips, uno detras de otro, sin etiquetas y sin recomprimir.

    El reel lleva las etiquetas quemadas y esta comprimido para revisar; esto es
    el material tal cual, en resolucion nativa. Sirve para dos cosas: subirlo
    directo si la seleccion ya te gusta como esta, o meterlo en CapCut como una
    sola pista en vez de arrastrar treinta archivos.

    Va con `-c copy`: los clips salen todos del mismo render, con identicos
    codec, resolucion y fps, asi que pegarlos es copiar bytes. Tarda segundos
    en vez de media hora y **no pierde nada de calidad**. Si algun dia dejan de
    ser homogeneos, ffmpeg avisa en vez de producir un archivo roto.
    """
    if not clips:
        return None

    ride.final_dir.mkdir(parents=True, exist_ok=True)
    listing = ride.final_dir / ORDER_NAME
    # Rutas relativas y comillas simples: el demuxer concat las quiere asi, y
    # los dos puntos de la unidad en Windows lo confunden.
    listing.write_text(
        "\n".join(f"file '../clips/{clip.name}'" for clip in clips) + "\n",
        encoding="utf-8",
    )

    out = ride.final_dir / MASTER_NAME
    ffmpeg.run(
        [
            "-y",
            "-f", "concat", "-safe", "0",
            "-i", ORDER_NAME,
            "-c", "copy",
            "-movflags", "+faststart",
            MASTER_NAME,
        ],
        cwd=ride.final_dir,
    )
    return out


def render(ride: Ride, cfg, on_progress=None) -> RenderResult:
    use_nvenc = bool(cfg.use_nvenc) and ffmpeg.has_nvenc()
    clips = render_clips(ride, cfg, use_nvenc, on_progress=on_progress)
    reel = render_reel(ride, clips, cfg, use_nvenc)
    return RenderResult(clips=clips, reel=reel)
