"""Fase 2: shorts verticales 9:16, armados de punta a punta sin intervencion
manual -- a diferencia del video largo, donde Chris corta el.

Cada short pega 2-3 momentos ya detectados por el motor de Fase 1
(`ride.selected`), ordenados de mas fuerte a mas flojo para que el primero
haga de climax -- los primeros segundos son lo que decide si alguien se
queda mirando. El texto quemado es, siempre que exista, un guion escrito a
mano por Claude (`pov/shorts_guion.py`) con lineas ancladas al segundo exacto
de cada momento -- nunca generado por formula. Sin guion, cae al respaldo
automatico (`pov/shorts_textos.py`): mas generico, pero el pipeline sigue
funcionando solo.

El encuadre es un recorte centrado al 50% del ancho original sobre un fondo
desenfocado del cuadro completo -- probado visualmente contra el recorte
central puro y el cuadro completo con barras el 17-ago-2026, ver
`config.toml`.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg, shorts_guion, shorts_textos
from .render import _ass_escape, _ass_time, _clear_stale_clips, _has_audio, _slug, clip_is_current
from .ride import Ride
from .segments import Segment

MANIFEST_NAME = "shorts.csv"


@dataclass
class Short:
    """2-3 clips pegados, ya en orden de climax -> resto."""

    clips: list[Segment]
    lines: list[tuple[float, str]] = field(default_factory=list)
    order: int = 0
    alt_hook: str = ""
    guion: bool = False  # True si `lines` viene del guion escrito a mano

    @property
    def duration(self) -> float:
        return sum(c.duration for c in self.clips)

    @property
    def climax(self) -> Segment:
        """El clip mas fuerte del grupo. Va primero a proposito: ver `select_shorts`."""
        return self.clips[0]

    @property
    def score(self) -> float:
        return self.climax.score


# --------------------------------------------------------------------------
# seleccion y agrupado
# --------------------------------------------------------------------------

def _pretty_trail(ride_name: str) -> str:
    """Respaldo cuando `ajustes.toml` no trae `nombre_trail`.

    Quita la fecha del nombre de la carpeta y pone cada palabra en
    mayuscula inicial. Queda mal con siglas (MTB -> Mtb): por eso
    `ajustes.toml` deja fijar el nombre de verdad a mano.
    """
    slug = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", ride_name)
    words = re.split(r"[-_]+", slug)
    return " ".join(w.capitalize() for w in words if w)


def _fallback_lines(short: Short, trail: str, cfg) -> list[tuple[float, str]]:
    """Hook al abrir + pregunta al cerrar, del respaldo automatico. Solo se
    usa cuando no hay guion (o el guion de este short quedo desactualizado)."""
    hook = shorts_textos.pick_hook(short.climax, trail)
    cierre = shorts_textos.pick_closing(short.climax, trail)
    closing_at = max(0.0, short.duration - cfg.shorts_closing_seconds)
    return [(0.0, hook), (closing_at, cierre)]


def _greedy_groups(eligible: list[Segment], cfg) -> list[list[Segment]]:
    """Reparto de toda la vida: ir llenando un grupo hasta que no quepa mas."""
    groups: list[list[Segment]] = []
    for segment in eligible:
        if groups and len(groups[-1]) < cfg.shorts_max_clips:
            total = sum(c.duration for c in groups[-1]) + segment.duration
            if total <= cfg.shorts_max_seconds:
                groups[-1].append(segment)
                continue
        groups.append([segment])
    return groups


def _balanced_groups(
    eligible: list[Segment], cfg, min_seconds: float
) -> list[list[Segment]] | None:
    """El reparto que saca **mas shorts validos** de los mismos clips, o None.

    Sigue siendo contiguo -- no reordena el ride, solo elige donde partir --
    y respeta los tres limites de siempre (`shorts_max_clips`,
    `shorts_max_seconds`, y ahora tambien el piso). La diferencia con el
    greedy es que mira el reparto completo antes de decidir: en el ride del
    26-jul, 3+3+1 tiraba un huerfano de 6.9 s, mientras que 2+2+3 da tres
    shorts de 28, 19 y 18 s con exactamente el mismo material (pedido de
    Chris el 17-ago-2026: "crear un short mas con los clips que califican").

    Entre repartos con la misma cantidad de shorts gana el mas parejo,
    medido contra el centro de la ventana configurada -- con 15/40 eso son
    27.5 s, dentro de los 20-30 s que Chris considera comodos.

    Devuelve None si no existe ningun reparto donde *todos* los grupos
    lleguen al piso; ahi manda el greedy y lo que sobre se descarta con
    aviso, que es el comportamiento honesto cuando el material no da.
    """
    n = len(eligible)
    objetivo = (min_seconds + cfg.shorts_max_seconds) / 2

    # best[i] = (cuantos grupos, penalizacion, cortes) para eligible[i:].
    # Se recorre de atras hacia adelante para que best[j] ya este resuelto.
    best: list[tuple[int, float, list[int]] | None] = [None] * (n + 1)
    best[n] = (0, 0.0, [])

    for i in range(n - 1, -1, -1):
        total = 0.0
        for j in range(i + 1, min(i + int(cfg.shorts_max_clips), n) + 1):
            total += eligible[j - 1].duration
            if total > cfg.shorts_max_seconds:
                break
            if total < min_seconds:
                continue
            resto = best[j]
            if resto is None:
                continue
            cand = (resto[0] + 1, resto[1] + (total - objetivo) ** 2, [j] + resto[2])
            # Mas shorts gana; a igualdad, el reparto mas parejo.
            if best[i] is None or (cand[0], -cand[1]) > (best[i][0], -best[i][1]):
                best[i] = cand

    if best[0] is None:
        return None

    groups: list[list[Segment]] = []
    start = 0
    for cut in best[0][2]:
        groups.append(eligible[start:cut])
        start = cut
    return groups


def select_shorts(ride: Ride, cfg, on_discard=None) -> list[Short]:
    """Agrupa los mejores momentos del ride en shorts de 2-3 clips.

    Recorre `ride.selected` en el mismo orden cronologico que ya trae (el
    que usa `segments.select` para el video largo), sumando segmentos a un
    grupo mientras quepan en `shorts_max_clips` / `shorts_max_seconds`.

    Los grupos que no llegan a `shorts_min_seconds` se descartan: un
    remanente de un solo clip corto no le da a nadie tiempo de engancharse
    (el short #7 de JD Park duraba 7 s). Se avisa por `on_discard` en vez de
    desaparecer en silencio, porque es accion real que se esta tirando.

    Dentro de cada grupo se reordena por puntaje descendente -- el mas
    fuerte abre, el resto sigue despues -- a proposito distinto del video
    largo, que preserva el orden cronologico para leerse como una sola
    bajada. Un short se juega la retencion en los primeros 2-3 segundos: si
    el golpe grande queda para el final, la mayoria ya se fue antes de
    verlo (aviso de Chris el 17-ago-2026, tras ver que el orden ascendente
    dejaba la mejor accion al final). El hook se quema sobre el clip que ya
    esta mostrando la accion, no promete algo que todavia no se vio.

    El texto que sale de aca es siempre el respaldo automatico: aplicar el
    guion escrito a mano es un paso aparte, `apply_guion`, porque necesita
    saber el orden y el climax de cada short -- que solo existen despues de
    armar el grupo.
    """
    # El piso del ride manda sobre el global: la escala del puntaje depende de
    # si el archivo trae telemetria (sin GPMF el techo es base_gain*100 = 65,
    # no 100), asi que un ride importado ya editado necesita su propio piso.
    # Ver el docstring de `pov/ajustes.py`.
    min_score = ride.ajustes.shorts_min_score or cfg.shorts_min_score
    min_seconds = ride.ajustes.shorts_min_seconds or cfg.shorts_min_seconds
    eligible = [s for s in ride.selected if not s.role and s.score >= min_score]
    if not eligible:
        return []

    groups = _greedy_groups(eligible, cfg)

    # El greedy llena el primer grupo hasta el tope y deja lo que sobra en el
    # ultimo, asi que suele terminar con un huerfano demasiado corto que se
    # tira entero. Ese material califico: la accion es buena, solo cayo mal
    # el reparto. Antes de descartarlo se intenta un reparto equilibrado.
    if any(sum(c.duration for c in g) < min_seconds for g in groups):
        rebalanced = _balanced_groups(eligible, cfg, min_seconds)
        if rebalanced is not None:
            groups = rebalanced

    viable: list[list[Segment]] = []
    for members in groups:
        total = sum(c.duration for c in members)
        if total < min_seconds:
            if on_discard:
                on_discard(members, total)
            continue
        viable.append(members)

    trail = ride.ajustes.nombre_trail or _pretty_trail(ride.name)

    # El `order` se numera despues de descartar, no antes: es la clave con la
    # que `shorts_guion.toml` referencia cada short, asi que tiene que quedar
    # 1..N sin huecos.
    shorts: list[Short] = []
    for order, members in enumerate(viable, start=1):
        ordered = sorted(members, key=lambda s: s.score, reverse=True)
        short = Short(clips=ordered, order=order)
        short.lines = _fallback_lines(short, trail, cfg)
        shorts.append(short)
    return shorts


def apply_guion(ride: Ride, shorts: list[Short]) -> list[str]:
    """Reemplaza el texto automatico por el guion escrito a mano, short por
    short, solo si el clip climax anotado todavia coincide con este short.

    Devuelve avisos (no excepciones) para los guiones que ya no aplican: el
    agrupado cambio -- se toco `shorts_min_score` u otro parametro -- y el
    short #N de hoy ya no es el mismo que cuando se escribio el guion. En
    ese caso se preserva el respaldo automatico en vez de quemar texto que
    quedo pegado al momento equivocado.
    """
    entries = shorts_guion.load(ride.root)
    warnings: list[str] = []
    for short in shorts:
        entry = entries.get(short.order)
        if entry is None:
            continue
        if not shorts_guion.matches(entry, short.climax):
            warnings.append(
                f"short #{short.order}: el guion no coincide con el climax actual "
                f"({entry.anchor} vs {short.climax.source.stem}@{short.climax.start:.1f}); "
                "uso el texto automatico"
            )
            continue
        if entry.lineas:
            short.lines = entry.lineas
            short.guion = True
        short.alt_hook = entry.alt_hook
    return warnings


# --------------------------------------------------------------------------
# nombre de archivo y subtitulos
# --------------------------------------------------------------------------

def _text_fingerprint(short: Short) -> str:
    """Un cambio de texto (reescribir el guion) tiene que invalidar el
    archivo ya renderizado aunque la duracion no cambie -- `clip_is_current`
    solo mira duracion. Meter el fingerprint en el nombre reutiliza toda la
    maquinaria existente de limpieza de clips viejos (`_clear_stale_clips`)
    sin tener que duplicarla: un texto nuevo simplemente produce un nombre
    nuevo, y el archivo con el texto viejo queda huerfano y se borra solo."""
    joined = "|".join(f"{t:.2f}:{text}" for t, text in short.lines)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:6]


def short_filename(order: int, short: Short) -> str:
    label = _slug(short.climax.headline())
    return f"{order:02d}_{short.score:03.0f}pts_{label}_{_text_fingerprint(short)}_short.mp4"


def build_short_labels(short: Short, cfg) -> str:
    """Subtitulo `.ass` con una linea del guion (o del respaldo) por evento.
    Mismo mecanismo que `render.build_labels`, generalizado a N eventos.

    Cada linea se queda `shorts_line_seconds`, o hasta que empiece la
    siguiente si esta mas cerca -- las lineas nunca se pisan, y el silencio
    entre una y otra es a proposito (Chris: "deja segundos sin texto para
    que se escuche el sendero").

    `WrapStyle: 0` (ajuste automatico, repartiendo parejo entre renglones) y
    no 2 como el reel de Fase 1. Con 2 libass no parte las lineas largas: se
    salen del cuadro y se cortan por los dos lados. Paso de verdad -- "how
    many g's do you think that first one was?" se veia como "w many g's do
    you think that first one w" en el short #2 de JD Park. El reel se salva
    porque sus etiquetas son cortas ("IMPACTO 6.2g"); las del guion no lo son.
    """
    width, height = int(cfg.shorts_width), int(cfg.shorts_height)
    duration = short.duration
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Line,Arial,{max(44, width // 16)},&H00FFFFFF,&H000000FF,&H00000000,&HC8000000,1,0,0,0,100,100,0,0,3,2,0,8,60,60,160,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = []
    ordered = sorted(short.lines, key=lambda item: item[0])
    for i, (t, text) in enumerate(ordered):
        start = max(0.0, min(t, duration))
        next_start = ordered[i + 1][0] if i + 1 < len(ordered) else duration
        end = min(duration, next_start, start + cfg.shorts_line_seconds)
        end = max(end, min(duration, start + 0.4))  # nunca un evento de largo cero
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Line,,0,0,0,,{_ass_escape(text)}"
        )
    return header + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def _render_one(ride: Ride, short: Short, out: Path, cfg, encoder: list[str]) -> None:
    """Recorta cada clip del grupo, lo encuadra en 9:16 (recorte centrado +
    fondo desenfocado de relleno) y los pega con el filtro concat, con las
    lineas del guion (o del respaldo) quemadas encima.
    """
    width, height, ratio = int(cfg.shorts_width), int(cfg.shorts_height), cfg.shorts_crop_width_ratio

    ass_name = f"{out.stem}.ass"
    (ride.shorts_dir / ass_name).write_text(build_short_labels(short, cfg), encoding="utf-8")

    inputs: list[str] = []
    graph: list[str] = []
    for i, segment in enumerate(short.clips):
        inputs += [
            "-ss", f"{segment.start:.3f}",
            "-t", f"{segment.duration:.3f}",
            "-i", str(segment.source.resolve()),
        ]
        graph.append(
            f"[{i}:v]split=2[bg{i}][fg{i}];"
            f"[bg{i}]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=25,eq=brightness=-0.08[bgblur{i}];"
            f"[fg{i}]crop=iw*{ratio}:ih:(iw-iw*{ratio})/2:0,scale={width}:-2:flags=lanczos[fgs{i}];"
            f"[bgblur{i}][fgs{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30[v{i}]"
        )
        if _has_audio(segment.source):
            graph.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:
            graph.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000:d={segment.duration:.3f}[a{i}]"
            )

    n = len(short.clips)
    concat_refs = "".join(f"[v{i}][a{i}]" for i in range(n))
    graph.append(f"{concat_refs}concat=n={n}:v=1:a=1[cv][ca]")
    graph.append(f"[cv]ass={ass_name}[vout]")

    args = [
        "-y",
        *inputs,
        "-filter_complex", ";".join(graph),
        "-map", "[vout]",
        "-map", "[ca]",
        *encoder,
        # Igual que el reel: el CRF fija calidad pero no acota el tamaño, y
        # el follaje en movimiento dispara el bitrate. Ver `shorts_max_mbps`.
        "-maxrate", f"{cfg.shorts_max_mbps:g}M",
        "-bufsize", f"{cfg.shorts_max_mbps * 2:g}M",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out.name,
    ]
    # Igual que render_reel: correr desde la carpeta de salida para que el
    # nombre del .ass no necesite escapar los dos puntos de una ruta de
    # Windows dentro del filtro.
    ffmpeg.run(args, cwd=ride.shorts_dir)

    # El .ass es un intermedio, no un entregable: ya quedo quemado en el
    # video. A diferencia de `labels.ass` del reel (un solo nombre fijo que
    # se pisa cada vez), el nombre de este lleva el fingerprint del texto, asi
    # que cada guion nuevo dejaria un archivo huerfano si no se borrara solo.
    # Si ffmpeg fallo arriba, no se llega hasta aca y el .ass queda para
    # depurar el error.
    (ride.shorts_dir / ass_name).unlink(missing_ok=True)


def render_shorts(
    ride: Ride, shorts: list[Short], cfg, use_nvenc: bool, on_progress=None
) -> list[Path]:
    if not shorts:
        return []

    ride.shorts_dir.mkdir(parents=True, exist_ok=True)
    encoder = ffmpeg.encoder_args(use_nvenc, cfg.clip_quality, cfg.x264_preset)
    _clear_stale_clips(
        ride.shorts_dir, {short_filename(s.order, s) for s in shorts}
    )

    written: list[Path] = []
    for index, short in enumerate(shorts, start=1):
        out = ride.shorts_dir / short_filename(short.order, short)
        if on_progress:
            on_progress(index, len(shorts), out.name)
        if out.exists() and out.stat().st_size > 0 and clip_is_current(out, short):
            written.append(out)
            continue
        _render_one(ride, short, out, cfg, encoder)
        written.append(out)

    write_manifest(ride, shorts)
    return written


def write_manifest(ride: Ride, shorts: list[Short]) -> Path:
    """`shorts.csv`, mismo estilo que `cortes.csv`: para revisar antes de
    programar la subida en Fase 3."""
    path = ride.shorts_dir / MANIFEST_NAME
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(
            [
                "orden", "archivos", "clips", "duracion_s", "puntaje_climax",
                "guion", "texto", "alt_hook",
            ]
        )
        for short in shorts:
            archivos = ", ".join(f"{c.source.stem}@{c.start:.1f}" for c in short.clips)
            texto = " / ".join(
                f"[{t:.1f}s] {text}" for t, text in sorted(short.lines, key=lambda i: i[0])
            )
            writer.writerow(
                [
                    short.order,
                    archivos,
                    len(short.clips),
                    f"{short.duration:.2f}",
                    f"{short.score:.1f}",
                    "si" if short.guion else "no (automatico)",
                    texto,
                    short.alt_hook,
                ]
            )
    return path
