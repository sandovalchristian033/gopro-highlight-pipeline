"""Los cortes que un archivo YA trae de fabrica, y como no caer encima de ellos.

Un MP4 crudo de la GoPro es una sola toma continua: cortar donde uno quiera es
seguro. Pero cuando el material entra al pipeline **ya editado a mano** (Chris
tenia dos videos de julio de 2026 montados antes de que existiera Fase 1), el
archivo lleva dentro los cortes de esa edicion, uno cada 2-6 segundos.

El detector de accion no los ve -- puntua por audio y acelerometro, no por
imagen -- asi que elige un tramo cualquiera y sus bordes caen a mitad de una
toma ajena. Si el borde queda a medio segundo de un corte, en el short sobrevive
una **migaja**: un plano que aparece y se va antes de que el ojo lo registre.
Chris lo cazo mirando el short #1 del 19-jul (17-ago-2026): "tres clips que lo
dejas menos de un segundo, en el que aparece y se quita". Eran de 0.53 s, 0.33 s
y 0.50 s exactos.

La solucion no es acortar el clip por si acaso, es **mover el borde hasta el
corte**: la migaja se va entera y el resto del tramo queda intacto.

Detectar escenas cuesta decodificar el video entero, asi que solo se hace donde
hace falta: un archivo sin telemetria de movimiento no salio de la camara, salio
de un editor. Un MP4 crudo de GoPro siempre trae GPMF. Ver `necesita_deteccion`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import ffmpeg

CACHE_FILE = "escenas.json"

# Dos detecciones a menos de esto son el mismo corte contado dos veces (ffmpeg
# marca el ultimo cuadro de una toma y el primero de la siguiente).
_MISMO_CORTE = 0.2

_PTS = re.compile(r"pts_time:([0-9.]+)")


def necesita_deteccion(summary: dict) -> bool:
    """Si vale la pena buscarle cortes internos a este archivo.

    Sin acelerometro no hay GPMF, y sin GPMF no salio de la camara: es un
    export de editor y puede traer cortes. Al reves, un archivo crudo de GoPro
    es una toma continua y decodificarlo entero para no encontrar nada seria
    tirar minutos por ride (17 archivos de 4K por ride, en esta maquina sin
    NVENC).
    """
    return not summary.get("accel_hz")


def detect(path: Path, umbral: float) -> list[float]:
    """Los segundos donde el archivo cambia de plano, en su propia linea de tiempo."""
    proc = ffmpeg.run(
        [
            "-i", str(path),
            "-vf", f"select='gt(scene,{umbral})',metadata=print:file=-",
            "-f", "null", "-",
        ],
    )
    crudos = sorted(float(m) for m in _PTS.findall(proc.stdout))

    cortes: list[float] = []
    for t in crudos:
        if not cortes or t - cortes[-1] > _MISMO_CORTE:
            cortes.append(t)
    return cortes


def load_or_detect(ride, cfg, on_progress=None) -> dict[str, list[float]]:
    """Los cortes de cada archivo del ride que los necesite, cacheados.

    La cache vive en el ride y se invalida por duracion, igual que
    `render.clip_is_current`: si el archivo cambio, el numero de segundos
    cambia y se vuelve a detectar.
    """
    path = ride.root / CACHE_FILE
    cache: dict = {}
    if path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}  # una cache ilegible se rehace, no revienta el render

    out: dict[str, list[float]] = {}
    pendientes = [f for f in ride.files if necesita_deteccion(f.summary)]
    for index, source in enumerate(pendientes, start=1):
        key = source.path.name
        guardado = cache.get(key)
        if isinstance(guardado, dict) and abs(guardado.get("duracion_s", -1) - source.duration) < 0.1:
            out[key] = [float(t) for t in guardado.get("cortes", [])]
            continue
        if on_progress:
            on_progress(index, len(pendientes), key)
        cortes = detect(source.path, cfg.escena_umbral)
        out[key] = cortes
        cache[key] = {"duracion_s": round(source.duration, 2), "cortes": cortes}

    if pendientes:
        path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def snap(start: float, end: float, cortes: list[float], min_fragmento: float) -> tuple[float, float]:
    """Corre los bordes de un tramo hasta que no le sobre ninguna migaja.

    Solo mira los extremos: un corte a mitad del tramo esta bien, es una
    transicion de la edicion original con material de sobra a los dos lados.
    Lo que molesta es un plano de 0.3 s pegado al principio o al final.

    Se aplica en bucle porque una migaja puede venir seguida de otra: si el
    tramo arranca 0.2 s antes de un corte y 0.3 s despues hay otro, mover el
    borde una sola vez deja una segunda migaja de 0.3 s.
    """
    dentro = [c for c in cortes if start < c < end]

    while dentro and dentro[0] - start < min_fragmento:
        start = dentro.pop(0)
    while dentro and end - dentro[-1] < min_fragmento:
        end = dentro.pop()

    return start, end


def apply(segments: list, cortes_por_archivo: dict[str, list[float]], cfg, on_note=None) -> list:
    """Aplica `snap` a cada segmento y descarta los que se quedan en nada.

    Devuelve la lista ya ajustada. Un tramo que despues de sacarle las migajas
    no llega a `min_segment_seconds` era casi todo migaja: se va entero, y se
    avisa en vez de dejar un corte de dos segundos en el short.
    """
    kept = []
    for segment in segments:
        cortes = cortes_por_archivo.get(segment.source.name)
        if not cortes:
            kept.append(segment)
            continue

        start, end = snap(segment.start, segment.end, cortes, cfg.shorts_min_fragmento)
        if start == segment.start and end == segment.end:
            kept.append(segment)
            continue

        # Quitar una migaja no puede costar un cuarto del clip. Cuando cuesta
        # tanto no hay migajas: es el detector equivocandose. Pasa de verdad --
        # el sol filtrado entre palmas y el motion blur de un tramo rapido
        # disparan `scene` igual que un corte, y en el ride del 26-jul cuatro
        # falsos positivos seguidos se comian un clip entero de 5.85 s de
        # material continuo. Subir el umbral no arregla eso: a 0.35 se pierden
        # los cortes de verdad, porque dos tomas seguidas de bosque se parecen
        # mucho. El limite es lo que hace seguro un detector imperfecto.
        if end - start < segment.duration * (1 - cfg.shorts_snap_max_recorte):
            if on_note:
                on_note(
                    f"NO ajuste {segment.source.stem}@{segment.start:.0f}s: hacerlo se "
                    f"llevaba {segment.duration - (end - start):.1f}s de {segment.duration:.1f}s. "
                    "Eso no son migajas, es el detector de escenas equivocandose "
                    "(sol entre las hojas, motion blur); lo dejo entero."
                )
            kept.append(segment)
            continue

        if end - start < cfg.min_segment_seconds:
            if on_note:
                on_note(
                    f"descarto {segment.source.stem}@{segment.start:.0f}s: entre los "
                    f"cortes de la edicion original solo quedaban {end - start:.1f}s."
                )
            continue

        if on_note:
            on_note(
                f"ajuste {segment.source.stem}@{segment.start:.0f}s a "
                f"{start:.1f}-{end:.1f}s ({segment.duration:.1f}s -> {end - start:.1f}s): "
                "el borde caia encima de un corte de la edicion original."
            )
        # El puntaje y los eventos siguen valiendo: el pico que hizo elegir
        # este tramo esta en el medio, que es justo lo que no se toca.
        segment.start, segment.end = start, end
        kept.append(segment)
    return kept
