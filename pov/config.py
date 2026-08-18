"""Tunable settings, with defaults that work out of the box.

Everything here can be overridden from `config.toml` at the project root, so
you can retune the detector for your trails without touching code.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_weights() -> dict[str, float]:
    """Relative weights of the continuous signals inside the base score.

    Only continuous signals belong here. Air and impact are events, not
    levels, so they are gains added on top (see `air_gain` / `impact_gain`)
    rather than terms in this average. Averaging them in would let an absent
    component eat its share of the scale as a constant zero, which crushes
    the score of any file that simply has no jumps in it.

    Tuned for what actually gets kept: speed, jumps, crashes and flowing
    corners. Pedalling along is what we are trying to throw away.
    """
    # Everything below except `speed` was fitted against 45 s of ground truth
    # recovered from an edit Chris made himself, validated on files the search
    # never saw. See `run.py comparar`.
    return {
        # NOT CALIBRATED. Every file in the calibration ride was recorded with
        # GPS off, so this weight was never actually exercised and the search
        # could put anything here. Left at a value that keeps speed dominant
        # when it does exist, which is what the physics says it should be.
        # Recalibrate as soon as there is a ride recorded with GPS on.
        "speed": 1.5,
        # The camera's own wind meter. Measured 2.51x separation on real
        # footage, and crucially it splits the case chatter cannot: rough but
        # slow. Works with GPS off, so it rescues already-recorded rides.
        "wind": 1.08,
        # Reads "rough ground" rather than "fast", so pedalling over gravel
        # looks the same to it as descending over gravel. The fit still liked
        # it slightly more than wind.
        "chatter": 1.12,
        "gyro": 0.74,
        # Kept low because the Hero 9 runs automatic gain control on the mic,
        # so recorded loudness barely tracks speed: -23.9 dBFS riding hard
        # versus -23.2 dBFS pedalling. Direct measurement said 0.10; the fit
        # over 19 files preferred a bit more, so this is the fitted value.
        # It still gets promoted automatically when a file has no telemetry
        # at all and loudness is the only signal left.
        "audio": 0.26,
        # Off by default, and worth explaining so nobody re-enables it blind.
        # Lean is measured as deviation from the file's median camera attitude.
        # On a helmet or chest mount that is mostly the rider's *head*, not the
        # bike, and riders look around most while soft-pedalling. Measured
        # separation was 0.71x: actively inverted, higher during the boring
        # parts. Only worth revisiting gated behind GPS speed.
        "lean": 0.0,
    }


@dataclass
class Config:
    # --- where things live ------------------------------------------------
    library_root: str = "rides"

    # --- analysis grid ----------------------------------------------------
    analysis_hz: float = 20.0
    weights: dict[str, float] = field(default_factory=_default_weights)

    # How the final score is assembled:
    #     score = base_gain * base + air_gain * air + impact_gain * impact
    # A mellow section tops out near base_gain; a jump pushes well past it.
    # Fitted against real ground truth, validated on held-out files.
    base_gain: float = 0.65
    air_gain: float = 0.62
    impact_gain: float = 0.69

    # --- airtime detection ------------------------------------------------
    # Below this many g, sustained for this long, you are in the air.
    air_threshold_g: float = 0.45
    # 0.22 s was too permissive. On real footage it fired twice on a flat
    # gravel doubletrack with no jump in it at all: rolling over a crest
    # unweights the bike for a quarter second without leaving the ground, and
    # labelling that "AIRE" in the reel is worse than missing it. Anything
    # that reads as a jump on camera holds for longer than this.
    air_min_seconds: float = 0.35
    air_smooth_seconds: float = 0.15
    # Airtime that scores a full 1.0. 0.8 s is a genuinely big jump.
    air_reference_seconds: float = 0.80

    # --- impacts ----------------------------------------------------------
    impact_threshold_g: float = 2.8
    impact_reference_g: float = 6.0
    impact_window_seconds: float = 0.30
    landing_window_seconds: float = 1.5
    combo_bonus: float = 0.20

    # --- crashes ----------------------------------------------------------
    crash_impact_g: float = 4.5
    crash_min_speed_kmh: float = 12.0
    crash_stop_kmh: float = 3.0
    crash_stop_window: float = 4.0
    crash_bonus: float = 0.50

    # --- general ----------------------------------------------------------
    moving_speed_kmh: float = 5.0
    stopped_penalty: float = 0.15
    score_smooth_seconds: float = 0.80
    # Lean is smoothed harder than everything else so only *held* bank angle
    # counts. A carved corner lasts a second or more; a jolt over a root does
    # not, and should not read as flow.
    lean_smooth_seconds: float = 1.00

    # --- segment building -------------------------------------------------
    # Lead-in matters: a jump is boring without the run-up to it. But it is
    # measured from where the action *commits*, not from where the score first
    # crosses the bar -- see `commit_fraction` and `_trim_head`.
    #
    # 3.4 was fitted before the commit anchor existed, so it was absorbing the
    # ramp-in as well as the lead-in and clips opened a median 5.1 s before
    # anything happened. Measured against the editor's own cuts, which open a
    # median 1.1 s before their first event, this lands within a second of him.
    pre_roll: float = 1.8
    post_roll: float = 3.6
    # How far from the bar toward a span's own peak the riding intensity has to
    # climb before the segment is allowed to open. 0 restores the old behaviour
    # of starting at the bare threshold crossing, which buried every clip under
    # several seconds of ramp-in. Higher trims harder; past ~0.5 it starts
    # cutting into genuine approaches, so this stays deliberately gentle.
    commit_fraction: float = 0.20
    # Cuanto tiene que sostenerse la intensidad por encima del nivel de commit
    # para que cuente como "la accion todavia sigue" al cerrar el clip. Solo
    # aplica a la cola: la cabeza se ancla en el primer contacto a proposito,
    # porque ahi la senal viene subiendo. Ver `_trim_tail`.
    tail_hold_seconds: float = 1.0
    min_segment_seconds: float = 3.1
    # 15 s was cutting good runs short. Two independent measurements agreed on
    # roughly this number: scene detection on a finished edit put the 90th
    # percentile clip at 19.4 s (longest 38 s), and the parameter fit landed
    # on 21.5 s on its own. When a section is good it is allowed to breathe.
    max_segment_seconds: float = 21.5
    merge_gap_seconds: float = 1.5
    # Share of ride time allowed through as candidates. Fitted value; drop it
    # toward 85 for a longer reel with more borderline moments.
    peak_percentile: float = 90.7
    # Absolute floor on a 0..100 scale, applied on top of the percentile. Stops
    # a whole ride of gentle pedalling from donating "highlights" just because
    # something has to be in the top 12%.
    min_segment_score: float = 28.0
    # How much footage the candidate reel should hold.
    #
    # Measured on a real ride: 208 s of finished video came out of 2479 s of
    # raw, so 8.4% survives. The reel needs to be a superset of that, because
    # its whole job is to give something to reject. 15% lands at roughly twice
    # the final cut, which is the budget the parameter fit was validated at.
    #
    # Expressed as a share rather than a fixed length: a 30 minute ride and a
    # 60 minute ride should not produce the same amount of review footage.
    # Set `target_reel_seconds` above zero to override with a fixed length.
    target_reel_ratio: float = 0.15
    target_reel_seconds: float = 0.0

    # --- intro y final ----------------------------------------------------
    # Los dos clips que le dan forma al video: abrir parado antes de arrancar y
    # cerrar frenando hasta detenerse. Ver `pov/bookends.py`. No salen del
    # puntaje de accion porque ese puntaje castiga a proposito todo lo que pase
    # por debajo de `moving_speed_kmh`, que es justo lo que estas reglas buscan.
    intro_lead_seconds: float = 4.0    # cuanto de la toma quieta se conserva
    intro_tail_seconds: float = 2.5    # cuanto se sigue despues de arrancar
    outro_lead_seconds: float = 6.0    # cuanto de la frenada final se conserva
    outro_tail_seconds: float = 3.5    # cuanto se aguanta ya con la bici quieta
    # Un tramo en movimiento tiene que durar esto para contar como salida o
    # llegada de verdad. Sin este minimo, un empujon de dos segundos para
    # acomodarse en el parqueadero se lee como el arranque del ride.
    bookend_move_seconds: float = 4.0
    # Y la quietud alrededor tiene que durar esto. Es tambien lo que la camara
    # tiene que seguir grabando despues de la ultima frenada para que exista un
    # final; si no, se reporta y no se inventa nada.
    bookend_hold_seconds: float = 2.0
    # Cuanto tiene que rodarse dentro de un archivo para que su arranque desde
    # parado cuente como apertura de tramo y no como una parada de acomodarse.
    bookend_section_seconds: float = 60.0

    def reel_budget(self, source_seconds: float) -> float:
        """Seconds of candidate footage to select for a ride this long."""
        if self.target_reel_seconds > 0:
            return self.target_reel_seconds
        return max(30.0, self.target_reel_ratio * source_seconds)

    # --- shorts 9:16 --------------------------------------------------------
    # Piso de puntaje para que un segmento entre a un short. Distinto de
    # `min_segment_score` (que es el piso del reel del video largo): el short
    # es lo mejor de lo mejor, no todo lo que vale la pena revisar. SIN
    # CALIBRAR: en Halpatiokee da los 5 mejores momentos del ride. Primer
    # candidato a tocar despues de ver el primer lote real de shorts.
    shorts_min_score: float = 55.0
    # Cuantos segmentos como maximo se pegan en un mismo short.
    shorts_max_clips: int = 3
    # Ventana de duracion util de un short. El tope de plataforma son 59 s,
    # pero Chris lo bajo a 40 el 17-ago-2026 mirando los primeros lotes
    # reales: mas alla de eso el short se hace largo para lo que entrega.
    # El piso descarta los sobrantes de un solo clip, demasiado cortos para
    # que alguien alcance a engancharse. Lo comodo esta entre 20 y 30 s; hay
    # que revisar estos dos numeros contra las metricas de retencion de
    # YouTube/TikTok/Reels cuando haya datos, no son definitivos.
    shorts_min_seconds: float = 15.0
    shorts_max_seconds: float = 40.0
    # Lo mas corto que puede durar un plano dentro de un short. Solo aplica a
    # material que entra YA editado (ver `pov/escenas.py`): si el borde de un
    # tramo cae cerca de un corte de esa edicion previa, queda una migaja que
    # aparece y se va sin que el ojo la registre. Chris cazo tres de 0.53,
    # 0.33 y 0.50 s en el short #1 del 19-jul. 1.2 s es el punto en que un
    # plano se lee como plano y no como parpadeo.
    shorts_min_fragmento: float = 1.2
    # Cuanto tiene que cambiar la imagen para contarlo como corte de edicion.
    # 0.12 encuentra los cortes reales de los dos MP4 de julio; subirlo a
    # 0.35 se salta la mitad porque en POV de bosque dos tomas seguidas se
    # parecen mucho (todo es follaje verde).
    escena_umbral: float = 0.12
    # Cuanto del clip puede costar, como maximo, quitarle las migajas. Por
    # encima de esto no hay migajas: es el detector confundiendo sol entre las
    # hojas o motion blur con un corte, y el clip se deja entero. Sin este
    # limite, cuatro falsos positivos seguidos se comieron un clip continuo de
    # 5.85 s en el ride del 26-jul.
    shorts_snap_max_recorte: float = 0.25
    # Cuanto se queda en pantalla cada linea del guion (o del texto
    # automatico de respaldo) antes de desaparecer -- fragmentos cortos,
    # legibles en un vistazo, no parrafos.
    shorts_line_seconds: float = 2.2
    # Solo para el respaldo automatico (sin guion escrito a mano): a cuantos
    # segundos del final arranca la pregunta de cierre.
    shorts_closing_seconds: float = 2.5
    # Lienzo vertical de salida.
    shorts_width: int = 1080
    shorts_height: int = 1920
    # Techo de bitrate del short, en Mbit/s. Mismo problema que el reel (ver
    # `reel_max_mbps`): el CRF fija calidad pero no acota el tamaño, y el
    # follaje POV en movimiento es patologico para el encoder. Sin techo, los
    # primeros shorts reales salieron entre 34 y 48 Mbps -- 1.1 GB por 7
    # shorts de JD Park. Ninguna plataforma conserva eso: YouTube/TikTok
    # reencodean y recomiendan ~8-12 Mbps para 1080p vertical, asi que por
    # encima de 12 solo se gasta disco y tiempo de subida.
    shorts_max_mbps: float = 12.0
    # Cuanto del ancho original se conserva en el primer plano, centrado.
    # Probado visualmente sobre Halpatiokee el 17-ago-2026: ni el recorte
    # central puro (0.316 -- pierde la sensacion de velocidad, se ven muy
    # pocos laterales) ni el cuadro completo (1.0 -- deja mucho vacio arriba
    # y abajo). 0.5 quedo como el punto intermedio que Chris aprobo. El resto
    # del cuadro se rellena con una version desenfocada del video completo, y
    # ahi es donde va el texto.
    shorts_crop_width_ratio: float = 0.5

    # --- rendering --------------------------------------------------------
    reel_height: int = 1080
    clip_quality: int = 20  # CQ/CRF for the clean clips you edit with
    # The reel only exists to decide which clips to keep, so it can be cheap.
    # POV foliage is expensive to encode: at CRF 25 with a fast preset this
    # came out at 30 Mbps, which is absurd for a review copy.
    reel_quality: int = 28
    # Hard bitrate ceiling for the reel, in Mbit/s. CRF alone does not bound
    # the file size, and grass and palm fronds at speed are pathological for
    # any encoder. This keeps a six minute reel around 350 MB instead of 900.
    reel_max_mbps: float = 8.0
    use_nvenc: bool = True
    # Only used when the GPU encoder is unavailable. "faster" keeps 4K
    # rendering tolerable on a laptop CPU; "medium" looks marginally better.
    x264_preset: str = "faster"

    def merged(self, overrides: dict) -> "Config":
        """Return a copy with `overrides` applied, ignoring unknown keys."""
        known = {f.name for f in fields(self)}
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        for key, value in overrides.items():
            if key not in known:
                continue
            if key == "weights" and isinstance(value, dict):
                merged_weights = dict(data["weights"])
                merged_weights.update({k: float(v) for k, v in value.items()})
                data["weights"] = merged_weights
            else:
                data[key] = value
        return Config(**data)


def load(path: Path | None = None) -> Config:
    """Load config.toml if it exists, otherwise return the defaults."""
    config = Config()
    path = path or PROJECT_ROOT / "config.toml"
    if not path.exists():
        return config
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    # Sections exist only to keep config.toml readable; they all flatten into
    # one namespace. The one exception is the weights table, which stays nested.
    weight_sections = {"pesos", "weights"}
    flat: dict = {}
    for key, value in data.items():
        if key in weight_sections and isinstance(value, dict):
            flat.setdefault("weights", {}).update(value)
        elif isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return config.merged(flat)


def library_path(config: Config) -> Path:
    root = Path(config.library_root)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root
