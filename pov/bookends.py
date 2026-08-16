"""Los dos clips que no son de accion: como abre el video y como cierra.

Todo el resto del pipeline busca lo mas fuerte del ride. Estos dos buscan lo
contrario -- el instante antes de que empiece y el instante despues de que
termina -- porque un video POV que entra a mitad de trail y sale a mitad de
trail no tiene forma. El video de referencia abre parado en el parqueadero, y
Chris quiere que cierre igual: frenando hasta quedar quieto.

Por eso **no** pueden salir del puntaje de accion. Ese puntaje multiplica por
`stopped_penalty` todo lo que este por debajo de `moving_speed_kmh`, a
proposito: justo el material que estas dos reglas buscan es el que el detector
esta disenado para tirar a la basura. De ahi que sean una pasada aparte, con
sus propias reglas, despues de la seleccion.

La senal que las hace posibles es el GPS. Sin velocidad no hay forma de
distinguir "parado en el parqueadero" de "rodando suave", y el acelerometro no
ayuda: una bici quieta y una bici rodando por asfalto liso se parecen mucho.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .segments import Segment
from .signals import Signals, _runs


@dataclass
class Bookend:
    """Un extremo del video, o la razon por la que no se pudo encontrar."""

    segment: Segment | None
    note: str


def _spans(mask: np.ndarray, t: np.ndarray) -> list[tuple[float, float]]:
    """Los tramos True de `mask`, en segundos."""
    return [
        (float(t[start]), float(t[min(end, t.size - 1)]))
        for start, end in _runs(mask)
    ]


def _make(source: Path, start: float, end: float, signals: Signals, role: str) -> Segment | None:
    if end - start < 1.0:
        return None
    window = (signals.t >= start) & (signals.t <= end)
    return Segment(
        source=source,
        start=round(max(0.0, start), 2),
        end=round(end, 2),
        score=float(signals.score[window].mean()) if window.any() else 0.0,
        peak_time=round(start, 2),
        peak_speed_kmh=signals.peak_speed(start, end),
        events=[],
        role=role,
    )


def find_intro(source: Path, signals: Signals, cfg) -> Bookend:
    """El arranque: quieto, y despues la primera pedaleada de verdad.

    Se ancla en la *salida* -- el primer tramo sostenido por encima de la
    velocidad de marcha -- y se cuenta hacia atras. Anclarse en el principio
    del archivo no sirve: entre que enciendes la camara y arrancas pueden pasar
    cinco segundos o cuarenta.
    """
    t = signals.t
    if t.size == 0:
        return Bookend(None, "el primer archivo no trae telemetria")
    duration = float(t[-1])

    if signals.speed_kmh is None:
        end = min(duration, cfg.intro_lead_seconds + cfg.intro_tail_seconds)
        return Bookend(
            _make(source, 0.0, end, signals, "intro"),
            "sin GPS en el primer archivo: tome el principio tal cual, "
            "sin poder confirmar que estuvieras parado",
        )

    moving = signals.speed_kmh >= cfg.moving_speed_kmh
    departure = next(
        (a for a, b in _spans(moving, t) if b - a >= cfg.bookend_move_seconds),
        None,
    )

    if departure is None:
        end = min(duration, cfg.intro_lead_seconds + cfg.intro_tail_seconds)
        return Bookend(
            _make(source, 0.0, end, signals, "intro"),
            "no encontre un arranque claro; tome el principio del archivo",
        )

    if departure < cfg.bookend_hold_seconds:
        # Ya venia rodando cuando empezo a grabar: no hay parqueadero que usar,
        # pero el principio del archivo sigue siendo el principio del ride.
        end = min(duration, departure + cfg.intro_tail_seconds + cfg.intro_lead_seconds)
        return Bookend(
            _make(source, 0.0, end, signals, "intro"),
            f"ya venias rodando a los {departure:.1f}s, o sea que empezaste a "
            "grabar en movimiento: no hay toma quieta para abrir",
        )

    start = max(0.0, departure - cfg.intro_lead_seconds)
    return Bookend(
        _make(source, start, min(duration, departure + cfg.intro_tail_seconds), signals, "intro"),
        f"quieto hasta {departure:.1f}s y ahi arrancas "
        f"({departure - start:.1f}s de toma parada antes de la salida)",
    )


def find_intros(sources: list[Path], curves: list[Signals], cfg) -> list[Bookend]:
    """La apertura del ride, mas la de cualquier tramo que arranque desde parado.

    Un ride normal solo tiene una: el primer archivo. Pero si te detienes a
    mitad de ride y vuelves a arrancar -- cambiar el montaje de la camara, por
    ejemplo -- ese archivo tambien abre con una toma quieta, y puede ser mejor
    apertura que la primera. El caso concreto que motivo esto: la primera mitad
    del ride grabada desde el casco, donde no se te ve la cara, y la segunda
    desde el pecho, donde si.

    Se exige que el tramo sea de verdad un tramo (`bookend_section_seconds` de
    rodada) para no confundirlo con las paradas cortas de acomodarse, que
    dejan archivos de cinco o veinte segundos.
    """
    if not sources:
        return []
    found = [find_intro(sources[0], curves[0], cfg)]
    for source, signals in zip(sources[1:], curves[1:]):
        extra = _section_opening(source, signals, cfg) or _handling_moment(source, signals, cfg)
        if extra is not None:
            found.append(extra)
    return found


def _handling_moment(source: Path, signals: Signals, cfg) -> Bookend | None:
    """Un archivo corto y casi todo detenido: la camara en la mano.

    Cuando Chris para a cambiar el montaje, la camara sigue grabando mientras
    la acomoda, y **es el unico momento del ride en que apunta hacia el**. Todo
    lo demas mira hacia adelante: ni el casco ni el pecho lo enfocan a el.

    Medido en Halpatiokee (16-ago-2026): `GX011145.MP4`, 20 s detenido entre la
    mitad de casco y la de pecho. En el segundo 3 el encuadre es su casco visto
    desde el pecho, que es exactamente la apertura que pidio; a los 15 s la
    camara ya esta acomodada mirando al frente. La regla de tramo lo descarta
    por corto, asi que hace falta esta.
    """
    t = signals.t
    if t.size == 0 or signals.speed_kmh is None:
        return None

    duration = float(t[-1])
    if duration < cfg.intro_lead_seconds:
        return None

    riding = float((signals.speed_kmh >= cfg.moving_speed_kmh).sum()) / signals.hz
    if riding >= cfg.bookend_section_seconds:
        return None  # es un tramo de verdad, no un ajuste de camara
    if duration - riding < cfg.bookend_hold_seconds:
        return None

    end = min(duration, cfg.intro_lead_seconds + cfg.intro_tail_seconds)
    segment = _make(source, 0.0, end, signals, "intro-cam")
    if segment is None:
        return None
    return Bookend(
        segment,
        f"{source.name} son {duration:.0f}s casi todos detenido: la camara en "
        "la mano, el unico angulo donde puedes salir tu",
    )


def _section_opening(source: Path, signals: Signals, cfg) -> Bookend | None:
    t = signals.t
    if t.size == 0 or signals.speed_kmh is None:
        return None

    moving = signals.speed_kmh >= cfg.moving_speed_kmh
    riding = float(moving.sum()) / signals.hz
    if riding < cfg.bookend_section_seconds:
        return None

    spans = _spans(moving, t)
    departure = next((a for a, b in spans if b - a >= cfg.bookend_move_seconds), None)
    if departure is None or departure < cfg.bookend_hold_seconds:
        return None

    start = max(0.0, departure - cfg.intro_lead_seconds)
    segment = _make(
        source, start, min(float(t[-1]), departure + cfg.intro_tail_seconds), signals, "intro-alt"
    )
    if segment is None:
        return None
    return Bookend(
        segment,
        f"{source.name} tambien abre desde parado: quieto hasta {departure:.1f}s",
    )


def forced_outro(source: Path, signals: Signals, start: float) -> Bookend:
    """El cierre que eligio Chris, desde ese segundo hasta el final del archivo.

    Existe porque la regla automatica exige haberse detenido del todo, y hay
    finales validos que no cumplen eso. El caso real: llegar rodando despacio
    al parqueadero del parque. Es un final perfectamente bueno -- el mismo lo
    describio asi -- pero la bici nunca se para en camara, y ninguna
    relajacion honesta de la regla lo distingue de quedarse a mitad de trail.
    Asi que lo decide el, no el detector.
    """
    t = signals.t
    if t.size == 0:
        return Bookend(None, f"{source.name} no trae telemetria")

    duration = float(t[-1])
    start = max(0.0, min(start, duration - 1.0))
    segment = _make(source, start, duration, signals, "outro")
    if segment is None:
        return Bookend(None, f"{source.name} es demasiado corto desde {start:.1f}s")
    return Bookend(
        segment,
        f"elegido a mano: {source.name} desde {start:.1f}s hasta el final "
        f"({duration - start:.1f}s)",
    )


def find_outro(source: Path, signals: Signals, cfg) -> Bookend:
    """El cierre: la ultima frenada hasta quedar completamente detenido.

    Se ancla en el final del ultimo tramo en movimiento y exige que la camara
    haya seguido grabando un rato despues. Si no siguio, no hay final: se dice
    y ya. Inventar uno con los ultimos segundos del archivo reproduce
    exactamente el problema que esto viene a resolver, un video que se corta a
    mitad de trail.
    """
    t = signals.t
    if t.size == 0:
        return Bookend(None, "el ultimo archivo no trae telemetria")
    duration = float(t[-1])

    if signals.speed_kmh is None:
        return Bookend(
            None,
            "sin GPS en el ultimo archivo: no hay forma de saber si paraste. "
            "Elige tu el cierre entre los clips",
        )

    moving = signals.speed_kmh >= cfg.moving_speed_kmh
    arrivals = [b for a, b in _spans(moving, t) if b - a >= cfg.bookend_move_seconds]
    if not arrivals:
        return Bookend(None, "en el ultimo archivo nunca detecte que rodaras")

    arrival = arrivals[-1]
    held = duration - arrival
    if held < cfg.bookend_hold_seconds:
        return Bookend(
            None,
            f"la grabacion termina {held:.1f}s despues de la ultima frenada, "
            "asi que no hay un final con la bici quieta. Para el proximo ride: "
            "deja la camara grabando hasta que estes detenido del todo",
        )

    start = max(0.0, arrival - cfg.outro_lead_seconds)
    return Bookend(
        _make(source, start, min(duration, arrival + cfg.outro_tail_seconds), signals, "outro"),
        f"frenas hasta parar en {arrival:.1f}s y la camara sigue grabando "
        f"{held:.1f}s mas",
    )
