"""Respaldo automatico de texto quemado, para cuando todavia no existe un
guion escrito a mano para el ride (ver `pov/shorts_guion.py`). En ingles
(Chris vive en EE.UU. y ya editaba sus shorts en ingles buscando mas alcance
-- ver memoria del proyecto).

**Tono** (mismas reglas que el guion escrito a mano, resumidas para que el
respaldo no suene distinto): voz de rider intermedio, nunca de pro --
nada de "full send", "sending it", "hucking". Fragmentos cortos y honestos,
no oraciones de anuncio. Si el respaldo suena a comercial, esta mal escrito.

Deliberadamente **no** llama a ningun modelo en tiempo de render: el pipeline
tiene que poder correr offline y dar el mismo resultado dos veces sobre el
mismo analisis. La eleccion dentro de cada lista es determinista (hash
estable de la identidad del clip), nunca aleatoria.
"""

from __future__ import annotations

import hashlib

from .segments import Segment

# --------------------------------------------------------------------------
# categoria de un segmento
# --------------------------------------------------------------------------

# Umbral para separar un impacto grande (aterrizaje fuerte, casi caida) de
# uno moderado (raiz, piedra). Coincide con donde `headline()` en segments.py
# empieza a sentirse "fuerte" en material real: el impacto de 6.2g de
# Halpatiokee fue el mejor momento del ride.
IMPACT_BIG_G = 6.0


def category_for(segment: Segment) -> str:
    """En que categoria de HOOK_TEMPLATES cae este segmento."""
    kinds = segment.kinds
    if "crash" in kinds:
        return "crash"
    if "air" in kinds:
        return "air"
    if "impact" in kinds:
        biggest = max(
            (e.magnitude for e in segment.events if e.kind == "impact"), default=0.0
        )
        return "impact_big" if biggest >= IMPACT_BIG_G else "impact"
    if segment.peak_speed_kmh > 0:
        return "speed"
    return "generic"


# --------------------------------------------------------------------------
# plantillas
# --------------------------------------------------------------------------

HOOK_TEMPLATES: dict[str, list[str]] = {
    "crash": [
        "this one hurt",
        "did not see that coming",
        "yeah that wasn't the plan",
        "here's where it went wrong",
        "still feeling that one",
    ],
    "air": [
        "more air than planned",
        "that gap looked smaller",
        "wasn't ready for that",
        "still learning this jump",
        "didn't expect that much pop",
    ],
    "impact_big": [
        "felt that one",
        "rough landing",
        "that hit was loud",
        "teeth still rattling",
        "hardest hit of the day",
    ],
    "impact": [
        "did not see that root",
        "small hit, big scare",
        "caught me off guard",
        "bike handled that better than me",
        "that one snuck up on me",
    ],
    "speed": [
        "pushing it a bit here",
        "faster than I meant to go",
        "not touching the brakes",
        "still working on this line",
        "went a little quick there",
    ],
    "generic": [
        "still learning this trail",
        "another Sunday out here",
        "not a pro, just having fun",
        "this section humbled me before",
        "{trail}, one more lap",
    ],
}

CLOSING_TEMPLATES: list[str] = [
    "how would you take this one?",
    "tell me this looked harder than it is",
    "made it though",
    "worse than it looked, promise",
    "anyone else ride like this?",
    "still figuring this trail out",
    "would you have slowed down here?",
    "more from {trail} soon",
]


# --------------------------------------------------------------------------
# seleccion determinista
# --------------------------------------------------------------------------

def _stable_index(key: str, count: int) -> int:
    """Un indice reproducible entre corridas: nunca `hash()` de Python, que
    cambia de proceso a proceso a proposito (PYTHONHASHSEED)."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest, 16) % count


def _fill(text: str, trail: str) -> str:
    if "{trail}" not in text:
        return text
    return text.format(trail=trail or "this trail")


def pick_hook(segment: Segment, trail: str) -> str:
    """El hook se elige sobre el clip CLIMAX del grupo (el mas fuerte), no
    sobre el de apertura: tiene que prometer lo que se ve al final."""
    category = category_for(segment)
    templates = HOOK_TEMPLATES[category]
    key = f"hook:{segment.source.name}@{segment.start:.2f}"
    return _fill(templates[_stable_index(key, len(templates))], trail)


def pick_closing(segment: Segment, trail: str) -> str:
    """Tambien anclado al clip climax, con una sal distinta a `pick_hook`
    para que no compartan indice por coincidencia."""
    key = f"closing:{segment.source.name}@{segment.start:.2f}"
    return _fill(
        CLOSING_TEMPLATES[_stable_index(key, len(CLOSING_TEMPLATES))], trail
    )
