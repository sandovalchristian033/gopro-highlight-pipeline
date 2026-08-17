"""El guion de texto de cada short: escrito a mano por Claude, siguiendo el
prompt de Chris (17-ago-2026) y mirando el timing real de cada clip -- nunca
generado por formula. `pov/shorts_textos.py` sigue siendo el respaldo
automatico para cuando todavia no existe un guion para un ride.

Flujo real:
  1. `run.py shorts` corre una vez sin guion (texto automatico) y escribe
     `shorts_plan.json` con el timing exacto de cada clip y cada evento
     dentro de la linea de tiempo del short (`write_plan`).
  2. Claude lee ese plan y escribe `shorts_guion.toml` a mano, con lineas
     ancladas a esos segundos.
  3. `run.py shorts` se corre de nuevo: si el clip climax de un short sigue
     siendo el mismo que anoto el guion, quema esas lineas en vez de las
     automaticas.

El ancla (`climax = "GX011147@172.9"`) existe porque el agrupado no es
estable ante cualquier cambio: tocar `shorts_min_score` u otro parametro
puede correr los ordenes o cambiar que clip es el climax de cada short. Sin
el ancla, un guion viejo se aplicaria en silencio al short equivocado -- la
misma clase de bug que ya costo caro en Fase 1 (ver `render.clip_is_current`
y la memoria del proyecto).

    # rides/2026-08-16_halpatiokee/shorts_guion.toml
    [[shorts]]
    order = 1
    climax = "GX011147@172.9"
    alt_hook = "watch what the roots do here"
    lineas = [
      { t = 0.0, texto = "this section humbled me last time" },
      { t = 9.8, texto = "did NOT see that root" },
      { t = 14.5, texto = "made it" },
    ]
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .ajustes import TOLERANCIA

GUION_FILE = "shorts_guion.toml"
PLAN_FILE = "shorts_plan.json"


@dataclass
class GuionCorto:
    anchor: str
    lineas: list[tuple[float, str]] = field(default_factory=list)
    alt_hook: str = ""


def matches(entry: GuionCorto, climax) -> bool:
    """Si el clip climax de hoy sigue siendo el que el guion anoto.

    Misma tolerancia que `ajustes.descartado`: sobrevive a que un reanalisis
    mueva el corte un par de segundos, pero no a que el climax sea otro clip.
    """
    name, _, when = entry.anchor.partition("@")
    if not name.strip():
        return False
    try:
        return (
            name.strip().lower() == climax.source.stem.lower()
            and abs(float(when) - climax.start) <= TOLERANCIA
        )
    except ValueError:
        return False


def load(ride_root: Path) -> dict[int, GuionCorto]:
    """Guiones por numero de short. Un archivo mal escrito a mano no debe
    reventar el render: una entrada invalida se ignora, no las demas."""
    path = ride_root / GUION_FILE
    if not path.exists():
        return {}

    with path.open("rb") as handle:
        data = tomllib.load(handle)

    result: dict[int, GuionCorto] = {}
    for entry in data.get("shorts", []):
        try:
            order = int(entry["order"])
            lineas = [
                (float(linea["t"]), str(linea["texto"]))
                for linea in entry.get("lineas", [])
            ]
        except (KeyError, TypeError, ValueError):
            continue
        result[order] = GuionCorto(
            anchor=str(entry.get("climax", "")),
            lineas=sorted(lineas, key=lambda item: item[0]),
            alt_hook=str(entry.get("alt_hook", "")),
        )
    return result


def write_plan(ride, shorts: list) -> Path:
    """Vuelca el timing exacto de cada short a JSON, para escribir el guion
    contra segundos reales en vez de a ojo.

    Los offsets son relativos al inicio del short ya pegado (el mismo eje de
    tiempo donde despues se anclan las lineas del guion), no al archivo
    crudo: el `t` de cada evento ya viene sumado al desplazamiento de los
    clips anteriores del grupo.
    """
    payload: dict = {"shorts": []}
    for short in shorts:
        cursor = 0.0
        clips = []
        for segment in short.clips:
            offset_start = cursor
            events = [
                {
                    "kind": event.kind,
                    "magnitude": round(event.magnitude, 2),
                    "offset_start": round(offset_start + (event.start - segment.start), 2),
                    "offset_end": round(offset_start + (event.end - segment.start), 2),
                }
                for event in segment.events
            ]
            clips.append(
                {
                    "source": segment.source.name,
                    "start": segment.start,
                    "end": segment.end,
                    "offset_start": round(offset_start, 2),
                    "offset_end": round(offset_start + segment.duration, 2),
                    "headline": segment.headline(),
                    "peak_speed_kmh": round(segment.peak_speed_kmh, 1),
                    "events": events,
                }
            )
            cursor += segment.duration
        payload["shorts"].append(
            {
                "order": short.order,
                "duration": round(short.duration, 2),
                "climax_anchor": f"{short.climax.source.stem}@{short.climax.start:.1f}",
                "clips": clips,
            }
        )

    ride.shorts_dir.mkdir(parents=True, exist_ok=True)
    path = ride.shorts_dir / PLAN_FILE
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
