"""Las decisiones manuales de Chris sobre un ride concreto.

El detector propone; Chris dispone. Cuando mira el reel y dice "estos archivos
no los quiero", "la apertura es esta", "este corte sobra", eso no es un
parametro que haya que retunear: es una decision suya sobre **este** material,
y tiene que sobrevivir a que se vuelva a analizar y renderizar.

Vive en `ajustes.toml` dentro de la carpeta del ride, para que no contamine la
configuracion global ni afecte a los demas rides:

    # rides/2026-08-16_halpatiokee/ajustes.toml

    # Archivos que no aportan candidatos de accion. Siguen contando como
    # material del ride y siguen pudiendo aportar apertura.
    excluir = ["GX011136", "GX011137"]

    # Que archivos abren el video, en este orden. Si esta puesto, manda sobre
    # lo que encuentren las reglas de `bookends`.
    aperturas = ["GX011145", "GX011141"]

    # Cortes concretos que no quiere, por archivo y segundo de inicio. Se
    # descarta el segmento que empiece a menos de `tolerancia` de ese punto.
    descartar = ["GX011146@87.8", "GX011146@131.2"]

    # Con que cierra el video, desde ese segundo hasta el final del archivo.
    # Manda sobre la regla automatica, que exige haberse detenido del todo.
    cierre = "GX011151@14.0"

    # Cuanto material debe llevar el reel, en segundos. Sobreescribe el
    # calculo por porcentaje cuando el ride se recorta mucho.
    reel_segundos = 330
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

AJUSTES_FILE = "ajustes.toml"
# Un segmento se reconoce como "el que Chris descarto" si empieza a menos de
# esto del punto anotado. Suficiente para que sobreviva a un reanalisis que
# mueva el corte un poco, y demasiado poco para tumbar al vecino.
TOLERANCIA = 2.5


@dataclass
class Ajustes:
    excluir: list[str] = field(default_factory=list)
    aperturas: list[str] = field(default_factory=list)
    descartar: list[tuple[str, float]] = field(default_factory=list)
    cierre: tuple[str, float] | None = None
    reel_segundos: float = 0.0

    @property
    def activos(self) -> bool:
        return bool(
            self.excluir
            or self.aperturas
            or self.descartar
            or self.cierre
            or self.reel_segundos
        )

    def excluido(self, path: Path) -> bool:
        """Si este archivo no debe aportar candidatos de accion."""
        return any(_matches(path, pattern) for pattern in self.excluir)

    def descartado(self, path: Path, start: float) -> bool:
        return any(
            _matches(path, name) and abs(start - when) <= TOLERANCIA
            for name, when in self.descartar
        )

    def resumen(self) -> list[str]:
        lines = []
        if self.excluir:
            lines.append(f"archivos sin candidatos: {', '.join(self.excluir)}")
        if self.aperturas:
            lines.append(f"aperturas elegidas a mano: {' -> '.join(self.aperturas)}")
        if self.descartar:
            lines.append(
                "cortes descartados a mano: "
                + ", ".join(f"{n}@{w:.1f}" for n, w in self.descartar)
            )
        if self.cierre:
            lines.append(f"cierre elegido a mano: {self.cierre[0]} desde {self.cierre[1]:.1f}s")
        if self.reel_segundos:
            lines.append(f"largo del reel fijado en {self.reel_segundos:.0f}s")
        return lines


def _matches(path: Path, pattern: str) -> bool:
    """Compara ignorando extension y mayusculas, para poder anotar solo el nombre."""
    return path.stem.lower() == Path(pattern).stem.lower()


def load(ride_root: Path) -> Ajustes:
    path = ride_root / AJUSTES_FILE
    if not path.exists():
        return Ajustes()

    with path.open("rb") as handle:
        data = tomllib.load(handle)

    descartar = [p for p in (_anchor(x) for x in data.get("descartar", [])) if p]

    return Ajustes(
        excluir=[str(x) for x in data.get("excluir", [])],
        aperturas=[str(x) for x in data.get("aperturas", [])],
        descartar=descartar,
        cierre=_anchor(data.get("cierre", "")),
        reel_segundos=float(data.get("reel_segundos", 0.0)),
    )


def _anchor(item) -> tuple[str, float] | None:
    """Convierte "GX011146@87.8" en ("GX011146", 87.8).

    Una linea mal escrita se ignora en vez de reventar el analisis: estos
    archivos los escribe una persona a mano mientras mira el reel.
    """
    name, _, when = str(item).partition("@")
    try:
        return (name.strip(), float(when))
    except ValueError:
        return None
