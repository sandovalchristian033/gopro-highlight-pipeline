"""Validacion del motor con datos sinteticos.

No necesita archivos de la GoPro: construye telemetria falsa con eventos en
posiciones conocidas y comprueba que el detector los encuentre ahi.

    python tests\\test_pipeline.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pov import ajustes as ajustes_mod
from pov import bookends as bookends_mod
from pov import cleanup as cleanup_mod
from pov import config as config_mod
from pov import escenas as escenas_mod
from pov import ffmpeg as ffmpeg_mod
from pov import gpmf, render as render_mod, ride as ride_mod
from pov import segments as segments_mod, signals as signals_mod
from pov import shorts as shorts_mod, shorts_guion as shorts_guion_mod, shorts_textos
from pov.signals import Event
from pov.telemetry import GRAVITY, Series, Telemetry

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  OK   {name}")
    else:
        FAILED.append(name)
        print(f"  FALLA {name}  {detail}")


def _ass_seconds(stamp: str) -> float:
    """Inversa de `render._ass_time`, para comprobar tiempos del .ass."""
    hours, minutes, secs = stamp.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(secs)


# --------------------------------------------------------------------------
# 1. parser GPMF
# --------------------------------------------------------------------------

def klv(key: str, type_char: str, item_size: int, repeat: int, payload: bytes) -> bytes:
    header = key.encode("ascii") + type_char.encode("latin-1") + bytes([item_size]) + struct.pack(">H", repeat)
    padding = b"\x00" * ((-len(payload)) % 4)
    return header + payload + padding


def test_gpmf_parser() -> None:
    print("\n[1] Parser GPMF")

    # Tres muestras de acelerometro crudas, escala 418 (como una Hero real).
    raw_samples = [(4180, 0, 0), (0, 4180, 0), (2090, 2090, 2090)]
    accl_payload = b"".join(struct.pack(">hhh", *sample) for sample in raw_samples)

    accl = klv("ACCL", "s", 6, len(raw_samples), accl_payload)
    scal = klv("SCAL", "s", 2, 1, struct.pack(">h", 418))
    strm_payload = scal + accl
    strm = klv("STRM", "\x00", 1, len(strm_payload), strm_payload)
    devc = klv("DEVC", "\x00", 1, len(strm), strm)

    nodes = gpmf.parse(devc)
    check("un nodo DEVC en la raiz", len(nodes) == 1 and nodes[0].key == "DEVC")

    strm_node = nodes[0].find("STRM")
    check("DEVC contiene STRM", strm_node is not None)

    scal_values = strm_node.find("SCAL").values()
    check("SCAL se decodifica", scal_values == [418], f"obtuve {scal_values}")

    accl_values = strm_node.find("ACCL").values()
    check("ACCL da 3 muestras de 3 ejes", len(accl_values) == 3 and len(accl_values[0]) == 3)

    scaled = gpmf.scale_values(accl_values, [float(x) for x in scal_values])
    check(
        "escala aplicada (4180/418 = 10.0)",
        abs(scaled[0][0] - 10.0) < 1e-6,
        f"obtuve {scaled[0][0]}",
    )

    # GPS5 usa cinco divisores distintos, uno por campo.
    gps_row = struct.pack(">iiiii", -334500000, -704000000, 850000, 9500, 9800)
    gps5 = klv("GPS5", "l", 20, 1, gps_row)
    gps_scal = klv("SCAL", "l", 4, 5, struct.pack(">iiiii", 10000000, 10000000, 1000, 1000, 1000))
    gps_strm_payload = gps_scal + gps5
    gps_strm = klv("STRM", "\x00", 1, len(gps_strm_payload), gps_strm_payload)

    strm_node = gpmf.parse(gps_strm)[0]
    divisors = [float(x) for x in strm_node.find("SCAL").values()]
    row = gpmf.scale_values(strm_node.find("GPS5").values(), divisors)[0]
    check("GPS5 latitud", abs(row[0] - (-33.45)) < 1e-6, f"obtuve {row[0]}")
    check("GPS5 velocidad 3D = 9.8 m/s", abs(row[4] - 9.8) < 1e-6, f"obtuve {row[4]}")

    # Un payload cortado a la mitad no debe reventar el parser.
    truncated = gpmf.parse(devc[: len(devc) - 7])
    check("payload truncado no revienta", isinstance(truncated, list))


# --------------------------------------------------------------------------
# 2. telemetria sintetica con eventos conocidos
# --------------------------------------------------------------------------

JUMP_START, JUMP_END = 8.0, 8.6   # 0.6 s de aire
LANDING = 8.75                    # golpe de aterrizaje
CRASH_IMPACT = 24.0               # golpe fuerte seguido de detencion


def synthetic_ride(duration: float = 40.0, with_crash: bool = True) -> Telemetry:
    """Un ride falso: rodando parejo, un salto limpio, y opcionalmente una caida."""
    rate = 200.0
    t = np.arange(0.0, duration, 1.0 / rate)
    rng = np.random.default_rng(7)

    # Base: 1 g mas la vibracion del terreno.
    accel = GRAVITY + rng.normal(0.0, 1.2, t.size)

    # Caida libre durante el salto.
    in_air = (t >= JUMP_START) & (t <= JUMP_END)
    accel[in_air] = rng.normal(1.0, 0.3, int(in_air.sum()))

    # Aterrizaje: pico corto y fuerte.
    landing = (t >= LANDING) & (t <= LANDING + 0.12)
    accel[landing] = 45.0

    gyro = np.abs(rng.normal(0.6, 0.25, t.size))
    gyro[in_air] += 1.5

    gps_t = np.arange(0.0, duration, 1.0 / 18.0)
    speed = np.full(gps_t.size, 8.3)  # ~30 km/h

    if with_crash:
        crash = (t >= CRASH_IMPACT) & (t <= CRASH_IMPACT + 0.15)
        accel[crash] = 60.0
        # La bici se detiene justo despues del golpe.
        speed[gps_t >= CRASH_IMPACT + 0.2] = 0.2

    telemetry = Telemetry(source=Path("sintetico.mp4"), duration=duration)
    telemetry.accel = Series(t, np.abs(accel))
    telemetry.gyro = Series(t, gyro)
    telemetry.speed = Series(gps_t, speed)
    telemetry.gps_fix = 3
    return telemetry


def test_event_detection() -> None:
    print("\n[2] Deteccion de eventos")
    cfg = config_mod.Config()
    telemetry = synthetic_ride()
    curve = signals_mod.build(telemetry, cfg)

    air = [e for e in curve.events if e.kind == "air"]
    check("detecta exactamente un salto", len(air) == 1, f"obtuve {len(air)}")

    if air:
        jump = air[0]
        check(
            f"el salto esta donde debe (esperado {JUMP_START}-{JUMP_END} s)",
            abs(jump.start - JUMP_START) < 0.35 and abs(jump.end - JUMP_END) < 0.35,
            f"obtuve {jump.start:.2f}-{jump.end:.2f}",
        )
        check(
            "mide el airtime correcto (~0.6 s)",
            abs(jump.magnitude - 0.6) < 0.25,
            f"obtuve {jump.magnitude:.2f} s",
        )

    impacts = [e for e in curve.events if e.kind == "impact"]
    check("detecta los impactos", len(impacts) >= 2, f"obtuve {len(impacts)}")

    crashes = [e for e in curve.events if e.kind == "crash"]
    check("detecta la caida", len(crashes) == 1, f"obtuve {len(crashes)}")
    if crashes:
        check(
            f"la caida esta en ~{CRASH_IMPACT} s",
            abs(crashes[0].start - CRASH_IMPACT) < 1.0,
            f"obtuve {crashes[0].start:.2f}",
        )

    # El puntaje tiene que estar alto en el salto y bajo en el tramo parejo.
    at_jump = curve.score[(curve.t >= JUMP_START) & (curve.t <= LANDING + 0.5)].max()
    at_calm = curve.score[(curve.t >= 2.0) & (curve.t <= 6.0)].mean()
    check(
        "el puntaje sube en el salto y no en lo parejo",
        at_jump > at_calm * 2.5,
        f"salto={at_jump:.1f} vs parejo={at_calm:.1f}",
    )


def test_no_telemetry_fallback() -> None:
    print("\n[3] Respaldo sin telemetria")
    cfg = config_mod.Config()

    empty = Telemetry(source=Path("sin_telemetria.mp4"), duration=30.0)
    loud_t = np.arange(0.0, 30.0, 0.05)
    loudness = np.full(loud_t.size, -40.0)
    loudness[(loud_t >= 12.0) & (loud_t <= 16.0)] = -8.0  # tramo ruidoso

    curve = signals_mod.build(empty, cfg, audio=Series(loud_t, loudness))
    check("no revienta sin telemetria", curve.t.size > 0)
    check("marca el archivo como sin telemetria", curve.source_has_telemetry is False)

    loud_section = curve.score[(curve.t >= 12.5) & (curve.t <= 15.5)].mean()
    quiet_section = curve.score[(curve.t >= 2.0) & (curve.t <= 8.0)].mean()
    check(
        "el audio manda cuando no hay sensores",
        loud_section > quiet_section + 20,
        f"ruidoso={loud_section:.1f} vs silencioso={quiet_section:.1f}",
    )


# --------------------------------------------------------------------------
# 3. segmentos
# --------------------------------------------------------------------------

def test_segments() -> None:
    print("\n[4] Construccion de segmentos")
    cfg = config_mod.Config()
    telemetry = synthetic_ride()
    curve = signals_mod.build(telemetry, cfg)
    found = segments_mod.build(Path("sintetico.mp4"), curve, cfg)

    check("encuentra segmentos", len(found) > 0, f"obtuve {len(found)}")
    if not found:
        return

    # Partiendo en los valles la recursion siempre converge, asi que el techo
    # es el maximo de verdad. Con los pasos fijos de antes un sobrante corto se
    # pegaba al pedazo anterior y el techo real era max + min.
    check(
        f"ninguno pasa el largo maximo ({cfg.max_segment_seconds:.0f}s)",
        all(s.duration <= cfg.max_segment_seconds + 0.1 for s in found),
        f"maximo {max(s.duration for s in found):.1f} s",
    )
    check(
        "ninguno baja del largo minimo",
        all(s.duration >= cfg.min_segment_seconds for s in found),
    )

    covering_jump = [s for s in found if s.start <= JUMP_START and s.end >= LANDING]
    check("hay un segmento que cubre el salto", len(covering_jump) >= 1)

    if covering_jump:
        segment = covering_jump[0]
        check(
            "incluye el roll-in antes del salto",
            JUMP_START - segment.start >= cfg.pre_roll - 1.0,
            f"solo {JUMP_START - segment.start:.1f} s de entrada",
        )
        check(
            "la etiqueta describe el evento",
            "AIRE" in segment.headline() or "CAIDA" in segment.headline(),
            f"etiqueta: {segment.headline()}",
        )

    # La seleccion respeta el presupuesto de tiempo y ordena cronologicamente.
    chosen = segments_mod.select(found, target_seconds=20.0)
    total = sum(s.duration for s in chosen)
    check("la seleccion respeta el presupuesto", total <= 20.0 + cfg.max_segment_seconds, f"{total:.1f} s")
    check(
        "la salida queda en orden cronologico",
        all(a.start <= b.start for a, b in zip(chosen, chosen[1:])),
    )
    check("todos quedan rankeados", all(s.rank > 0 for s in chosen))


def test_config_toml() -> None:
    print("\n[5] Lectura de config.toml")
    cfg = config_mod.load()
    check("config.toml se lee", isinstance(cfg, config_mod.Config))
    check(
        "la seccion [pesos] llega a weights",
        cfg.weights.get("speed") == 1.5,
        f"weights={cfg.weights}",
    )
    check(
        "lean sigue en cero (media la cabeza, no la bici)",
        cfg.weights.get("lean") == 0.0,
        f"lean={cfg.weights.get('lean')}",
    )
    # El AGC del microfono aplasta el volumen, asi que el audio no puede pesar
    # como una senal de sensor mientras haya telemetria disponible.
    sensors = [w for name, w in cfg.weights.items() if name not in ("audio", "lean")]
    check(
        "audio pesa menos que cualquier senal de sensor",
        cfg.weights.get("audio", 1.0) < min(sensors),
        f"audio={cfg.weights.get('audio')} vs sensor mas bajo={min(sensors)}",
    )
    check(
        "los pesos son solo senales continuas",
        not ({"air", "impact"} & set(cfg.weights)),
        f"weights={cfg.weights}",
    )
    check("la seccion [puntaje] se lee", cfg.air_gain == 0.62, f"air_gain={cfg.air_gain}")

    # Aplanado de secciones, probado contra un archivo propio en vez de contra
    # un valor afinado: atarlo a un numero del config real hace que el test se
    # caiga cada vez que se recalibra algo, que no es lo que esta verificando.
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        probe = Path(folder) / "probe.toml"
        probe.write_text(
            "[segmentos]\npre_roll = 9.75\n\n[pesos]\ngyro = 0.5\n",
            encoding="utf-8",
        )
        loaded = config_mod.load(probe)
    check(
        "las secciones se aplanan bien",
        loaded.pre_roll == 9.75,
        f"pre_roll={loaded.pre_roll}",
    )
    check(
        "los pesos no se aplanan, se fusionan",
        loaded.weights.get("gyro") == 0.5 and loaded.weights.get("speed") == 1.5,
        f"weights={loaded.weights}",
    )
    check(
        "el presupuesto del reel escala con el largo del ride",
        cfg.reel_budget(2400) > cfg.reel_budget(1200) > 0,
        f"2400s->{cfg.reel_budget(2400):.0f}s  1200s->{cfg.reel_budget(1200):.0f}s",
    )


# --------------------------------------------------------------------------
# regresiones encontradas con material real (GX011110.MP4, ago 2026)
# --------------------------------------------------------------------------

def test_no_air_does_not_crush_scale() -> None:
    """Un archivo sin saltos no puede perder la mitad de su escala.

    Con el modelo viejo 'air' promediaba con el resto conservando su peso,
    asi que en un clip sin saltos el 47% del puntaje era un cero constante y
    el maximo no pasaba de 39/100. Un trail tranquilo quedaba invisible.
    """
    print("\n[6] Regresion: archivo sin saltos")
    cfg = config_mod.Config()

    mellow = synthetic_ride(duration=40.0, with_crash=False)
    # Sacamos la caida libre: queda un ride movido pero sin nada de aire.
    mellow.accel = Series(mellow.accel.t, np.clip(mellow.accel.v, 6.0, None))

    curve = signals_mod.build(mellow, cfg)
    air = [e for e in curve.events if e.kind == "air"]
    check("efectivamente no hay saltos", len(air) == 0, f"obtuve {len(air)}")

    # El golpe de aterrizaje (45 m/s2) sobrevive al recorte, y su aporte al
    # puntaje tiene que ser el completo: no puede diluirse por el hecho de
    # que 'air' este ausente. Ese era exactamente el error.
    impact_g = 45.0 / GRAVITY
    impact_norm = (impact_g - cfg.impact_threshold_g) / (cfg.impact_reference_g - cfg.impact_threshold_g)
    expected_floor = cfg.impact_gain * impact_norm * 100

    check(
        f"el impacto aporta su ganancia completa (>={expected_floor:.0f} pts)",
        curve.score.max() >= expected_floor,
        f"maximo {curve.score.max():.1f}, esperaba al menos {expected_floor:.1f}",
    )
    # Bajo el modelo viejo el mismo impacto solo habria aportado
    # 2.0/6.4 de su valor, porque 'air' en cero se llevaba su parte del peso.
    diluted = (2.0 / 6.4) * impact_norm * 100
    check(
        "y supera lo que daba el modelo viejo diluido",
        curve.score.max() > diluted * 1.3,
        f"{curve.score.max():.1f} vs {diluted:.1f} del modelo viejo",
    )

    # Y con saltos tiene que puntuar todavia mas alto.
    with_air = signals_mod.build(synthetic_ride(duration=40.0, with_crash=False), cfg)
    check(
        "el archivo con salto sigue puntuando mas alto",
        with_air.score.max() > curve.score.max(),
        f"con salto={with_air.score.max():.1f} vs sin salto={curve.score.max():.1f}",
    )


def test_ride_wide_scale() -> None:
    """Los puntajes de archivos distintos tienen que ser comparables.

    Normalizando cada archivo contra si mismo, el momento mas movido del
    camino mas aburrido empataba con el de la mejor bajada, y el ranking
    entre archivos no significaba nada.
    """
    print("\n[7] Regresion: una sola escala para todo el ride")
    cfg = config_mod.Config()
    rng = np.random.default_rng(11)

    def flat_ride(vibration: float, duration: float = 30.0) -> Telemetry:
        rate = 200.0
        t = np.arange(0.0, duration, 1.0 / rate)
        tele = Telemetry(source=Path(f"vib{vibration}.mp4"), duration=duration)
        tele.accel = Series(t, np.abs(GRAVITY + rng.normal(0.0, vibration, t.size)))
        tele.gyro = Series(t, np.abs(rng.normal(vibration * 0.4, 0.2, t.size)))
        return tele

    calm = signals_mod.analyse_file(flat_ride(0.4), cfg)
    wild = signals_mod.analyse_file(flat_ride(4.0), cfg)

    stats = signals_mod.RideStats.collect([calm, wild], cfg)
    calm_curve = signals_mod.finalise(calm, cfg, stats)
    wild_curve = signals_mod.finalise(wild, cfg, stats)

    check(
        "el archivo movido puntua por encima del tranquilo",
        wild_curve.score.mean() > calm_curve.score.mean() * 1.8,
        f"movido={wild_curve.score.mean():.1f} vs tranquilo={calm_curve.score.mean():.1f}",
    )

    # Contraste: normalizado contra si mismo, cada uno se ve igual de bueno.
    alone_calm = signals_mod.finalise(calm, cfg, signals_mod.RideStats.collect([calm], cfg))
    check(
        "aislado, el tranquilo se sobrevalora (por eso hace falta la escala comun)",
        alone_calm.score.mean() > calm_curve.score.mean() * 1.5,
        f"aislado={alone_calm.score.mean():.1f} vs en ride={calm_curve.score.mean():.1f}",
    )


def test_head_trim() -> None:
    """Regresion: el clip abre pegado a la accion, no donde cruza el umbral.

    Las senales continuas suben de a poco, asi que el tramo cruza el umbral
    varios segundos antes de que se vea nada. Medido sobre material real, los
    clips abrian con 5.1 s de mediana antes de su primer evento y hasta 15.3 s,
    contra 1.1 s de los cortes que hizo Chris a mano.
    """
    print("\n[8] Regresion: el clip abre donde arranca la accion")
    cfg = config_mod.Config()

    # Cruza el umbral temprano y se queda apenas encima mucho rato antes de
    # ponerse buena, que es exactamente la forma que causaba el problema. Dura
    # menos que max_segment_seconds a proposito, para aislar el recorte de
    # cabeza del partido en pedazos.
    hz = cfg.analysis_hz
    t = np.arange(0.0, 30.0, 1.0 / hz)
    score = np.interp(
        t,
        [0.0, 5.0, 6.0, 14.0, 16.0, 17.5, 18.0, 22.0, 30.0],
        [5.0, 30.0, 36.0, 36.0, 95.0, 36.0, 29.0, 5.0, 5.0],
    )
    curve = signals_mod.Signals(t=t, score=score, base=score, hz=hz)

    threshold = 30.0
    found = segments_mod.build(Path("rampa.mp4"), curve, cfg, threshold)
    check("encuentra el tramo entero", len(found) == 1, f"obtuve {len(found)}")
    if not found:
        return

    segment = found[0]
    peak_at = 16.0
    check(
        "no abre en el cruce del umbral",
        segment.start > 8.0,
        f"abre en {segment.start:.1f} s (el umbral se cruza cerca de 5 s)",
    )
    check(
        "abre cerca del pico, no diez segundos antes",
        peak_at - segment.start < 6.0,
        f"{peak_at - segment.start:.1f} s de entrada",
    )
    check("conserva algo de entrada", segment.start < peak_at, f"abre en {segment.start:.1f} s")

    # Con commit_fraction en 0 vuelve el comportamiento viejo, y tiene que
    # notarse: si no, el parametro no esta haciendo nada.
    old = segments_mod.build(Path("rampa.mp4"), curve, cfg.merged({"commit_fraction": 0.0}), threshold)
    check(
        "commit_fraction en 0 restaura el arranque temprano",
        old and old[0].start < segment.start - 3.0,
        f"viejo={old[0].start:.1f} s vs nuevo={segment.start:.1f} s" if old else "sin segmento",
    )


def test_split_at_valleys() -> None:
    """Regresion: un tramo largo se parte en el valle, no cada N segundos.

    Partir en pasos fijos de `max_segment_seconds` dejaba el corte donde caiga
    la aritmetica. Sobre material real partio un tramo de 43 s justo por la
    mitad y devolvio dos clips cuya accion empezaba a los 14 y 15 s.
    """
    print("\n[9] Regresion: los tramos largos se parten en el valle")
    cfg = config_mod.Config()

    hz = cfg.analysis_hz
    t = np.arange(0.0, 40.0, 1.0 / hz)
    # Dos picos separados por un valle marcado en el segundo 20. El tramo mide
    # 40 s: pasa el maximo una sola vez, asi que basta un corte y se puede
    # afirmar donde tiene que caer.
    score = np.interp(
        t,
        [0.0, 3.0, 8.0, 14.0, 20.0, 26.0, 32.0, 37.0, 40.0],
        [5.0, 60.0, 95.0, 60.0, 34.0, 60.0, 95.0, 60.0, 5.0],
    )
    curve = signals_mod.Signals(t=t, score=score, base=score, hz=hz)

    found = segments_mod.build(Path("dospicos.mp4"), curve, cfg, 30.0)
    check("parte el tramo largo", len(found) >= 2, f"obtuve {len(found)}")
    check(
        "ninguno pasa el largo maximo",
        all(s.duration <= cfg.max_segment_seconds + 0.1 for s in found),
        f"maximo {max(s.duration for s in found):.1f} s" if found else "",
    )
    if len(found) >= 2:
        boundary = found[0].end
        check(
            "el corte cae en el valle, no en un multiplo del maximo",
            abs(boundary - 20.0) < 4.0,
            f"corta en {boundary:.1f} s, el valle esta en 20.0 s",
        )
        # Lo que de verdad importa: cada pedazo trae su propio pico adelante.
        for index, segment in enumerate(found[:2], start=1):
            window = (t >= segment.start) & (t <= segment.end)
            peak_at = float(t[window][int(np.argmax(score[window]))])
            check(
                f"el pedazo {index} tiene su accion cerca del inicio",
                peak_at - segment.start < 14.0,
                f"{peak_at - segment.start:.1f} s hasta el pico",
            )


def test_chapter_order() -> None:
    """Regresion: el reel sigue el orden de grabacion, no el alfabetico.

    La GoPro pone el capitulo ANTES del numero de grabacion: una toma larga
    sale como GX011134 + GX021134. Ordenar por nombre mete todo lo numerado
    1135 o mas entre las dos mitades, y el reel salta en el tiempo.
    """
    print("\n[10] Regresion: capitulos en orden de grabacion")

    names = ["GX011109.MP4", "GX011134.MP4", "GX021134.MP4", "GX011200.MP4"]
    made = [
        segments_mod.Segment(source=Path(n), start=0.0, end=5.0, score=50.0, peak_time=2.0)
        for n in names
    ]
    order = [s.source.name for s in segments_mod.select(made, target_seconds=100.0)]

    check(
        "el capitulo 02 va justo despues del 01",
        order.index("GX021134.MP4") == order.index("GX011134.MP4") + 1,
        f"orden={order}",
    )
    check(
        "y no al final por orden alfabetico",
        order[-1] == "GX011200.MP4",
        f"orden={order}",
    )

    # Dentro de un mismo archivo manda el tiempo, no el puntaje.
    same = [
        segments_mod.Segment(source=Path("GX011110.MP4"), start=s, end=s + 4.0,
                             score=score, peak_time=s + 2.0)
        for s, score in ((60.0, 90.0), (10.0, 40.0), (35.0, 70.0))
    ]
    starts = [s.start for s in segments_mod.select(same, target_seconds=100.0)]
    check("dentro de un archivo manda el tiempo", starts == sorted(starts), f"{starts}")

    # Un archivo con nombre a mano no se cuela en medio del ride.
    mixed = made + [
        segments_mod.Segment(source=Path("AAA_intro.mp4"), start=0.0, end=5.0,
                             score=50.0, peak_time=2.0)
    ]
    tail = [s.source.name for s in segments_mod.select(mixed, target_seconds=100.0)][-1]
    check("un nombre no-GoPro queda al final", tail == "AAA_intro.mp4", f"ultimo={tail}")


def test_cleanup() -> None:
    """La limpieza libera espacio sin poder borrar algo irrecuperable.

    Chris formatea la SD despues de cada ride, asi que `raw/` es la unica copia
    que existe de ese material. Todo lo demas se regenera renderizando otra vez.
    """
    print("\n[11] Limpieza de espacio")
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        ride = Path(folder) / "2026-08-16_prueba"
        for sub, count in (
            ("raw", 3), ("clips", 4), ("reel", 1), (".huellas", 2),
            ("final", 1), ("shorts", 2),
        ):
            (ride / sub).mkdir(parents=True)
            for i in range(count):
                (ride / sub / f"f{i}.bin").write_bytes(b"x" * 1000)
        (ride / "analysis.json").write_text("{}", encoding="utf-8")
        (ride / "cortes.csv").write_text("a;b", encoding="utf-8")

        items = cleanup_mod.survey(ride)
        labels = {i.path.name for i in items}
        check("por defecto no toca los originales", "raw" not in labels, f"{labels}")
        check(
            "propone clips, reel y cache",
            labels == {"clips", "reel", ".huellas"},
            f"{labels}",
        )
        check("todo lo propuesto se regenera", all(i.regenerable for i in items))
        check("el video final no entra en la barrida", "final" not in labels, f"{labels}")
        check("los shorts tampoco entran en la barrida", "shorts" not in labels, f"{labels}")
        check("ordena por tamano", [i.size for i in items] == sorted((i.size for i in items), reverse=True))

        freed = cleanup_mod.remove(items)
        check("libera lo que dijo", freed == 7000, f"{freed} bytes")
        check("el video final sobrevive", (ride / "final" / "f0.bin").exists())
        check("los shorts sobreviven", (ride / "shorts" / "f0.bin").exists())
        check("los originales siguen ahi", len(list((ride / "raw").iterdir())) == 3)
        check("el analisis sobrevive", (ride / "analysis.json").exists())
        check("la lista de cortes sobrevive", (ride / "cortes.csv").exists())

        # Solo cuando se pide explicitamente aparece raw, y marcado.
        con_final = cleanup_mod.survey(ride, include_final=True)
        check(
            "con --incluir-final si aparece",
            any(i.path.name == "final" for i in con_final),
        )
        check(
            "y los shorts van con la misma bandera (son entregables, no revision)",
            any(i.path.name == "shorts" for i in con_final),
        )

        risky = cleanup_mod.survey(ride, include_raw=True)
        raw_item = [i for i in risky if i.path.name == "raw"]
        check("con --incluir-raw si aparece", len(raw_item) == 1)
        if raw_item:
            check("y va marcado como irrecuperable", raw_item[0].regenerable is False)

        # El reporte nunca puede borrarse, ni estando dentro de una carpeta barrida.
        (ride / "clips").mkdir(exist_ok=True)
        (ride / "clips" / "analysis.json").write_text("{}", encoding="utf-8")
        cleanup_mod.remove(cleanup_mod.survey(ride))
        check(
            "el reporte se protege por nombre",
            (ride / "clips" / "analysis.json").exists(),
        )


def test_tail_trim() -> None:
    """El clip cierra cuando la accion se acaba, no cuando la curva baja del todo.

    Chris lo describio como "empieza con buena accion y despues solo pedaleo sin
    sentido". Medido en Halpatiokee: un clip de 18.6 s con todos sus impactos
    entre el segundo 1.8 y el 8.9 y 8.6 s de rodada intrascendente detras.
    """
    print("\n[13] Regresion: el clip cierra cuando la accion se acaba")
    cfg = config_mod.Config()
    hz = cfg.analysis_hz
    threshold = 30.0

    # Accion fuerte al principio, y despues una meseta larga que se queda justo
    # rondando el umbral. Con un repunte instantaneo al final: la forma exacta
    # que estiraba el clip seis segundos de mas.
    t = np.arange(0.0, 34.0, 1.0 / hz)
    base = np.interp(
        t,
        [0.0, 2.0, 6.0, 9.0, 11.0, 20.0, 20.2, 20.4, 30.0, 34.0],
        [10.0, 60.0, 62.0, 55.0, 31.0, 31.0, 46.0, 31.0, 30.0, 10.0],
    )
    curve = signals_mod.Signals(
        t=t, score=base, base=base, hz=hz,
        events=[signals_mod.Event("impact", 5.0, 5.4, 4.0)],
    )

    end = segments_mod._trim_tail(0.0, 34.0, curve, cfg, threshold)
    check(
        "no se estira hasta el final del tramo",
        end < 20.0,
        f"cierra en {end:.1f} s de 34 s",
    )
    check(
        "un repunte instantaneo no cuenta como accion",
        end < 20.2,
        f"cierra en {end:.1f} s, el repunte esta en 20.2 s",
    )
    check(
        "pero deja el post_roll despues de la accion",
        end >= 9.0,
        f"cierra en {end:.1f} s y la accion llega hasta ~9 s",
    )

    # Nunca por delante del ultimo evento, pase lo que pase con la curva.
    tardio = signals_mod.Signals(
        t=t, score=base, base=base, hz=hz,
        events=[signals_mod.Event("impact", 24.0, 24.5, 8.0)],
    )
    check(
        "nunca corta antes del ultimo evento",
        segments_mod._trim_tail(0.0, 34.0, tardio, cfg, threshold) > 24.5,
        "se comio un impacto",
    )

    # Y una seccion sostenida de verdad no se toca.
    plana = np.full(t.size, 70.0)
    sostenida = signals_mod.Signals(t=t, score=plana, base=plana, hz=hz)
    check(
        "una seccion sostenida sobrevive entera",
        segments_mod._trim_tail(0.0, 34.0, sostenida, cfg, threshold) >= 33.0,
        "recorto una seccion que seguia fuerte",
    )


def test_ajustes() -> None:
    """Las decisiones manuales de Chris sobrevisen a reanalizar y renderizar."""
    print("\n[14] Ajustes manuales por ride")
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "ajustes.toml").write_text(
            'excluir = ["GX011136", "GX011137.MP4"]\n'
            'aperturas = ["GX011145"]\n'
            'descartar = ["GX011146@87.8", "roto"]\n'
            'cierre = "GX011151@14.0"\n'
            "reel_segundos = 290\n"
            "shorts_min_score = 30\n",
            encoding="utf-8",
        )
        aj = ajustes_mod.load(root)

        check("lee la exclusion", aj.excluido(Path("x/GX011136.MP4")))
        check("ignora la extension al comparar", aj.excluido(Path("x/GX011137.MP4")))
        check("no excluye de mas", not aj.excluido(Path("x/GX011138.MP4")))
        check("lee la apertura elegida", aj.aperturas == ["GX011145"])
        check("lee el largo del reel", aj.reel_segundos == 290)
        check("lee el cierre elegido", aj.cierre == ("GX011151", 14.0), f"{aj.cierre}")
        check("lee el piso de puntaje de shorts", aj.shorts_min_score == 30.0)

        check("descarta el corte anotado", aj.descartado(Path("GX011146.MP4"), 87.8))
        check(
            "y tolera que el reanalisis lo mueva un poco",
            aj.descartado(Path("GX011146.MP4"), 89.0),
        )
        check(
            "pero no tumba al vecino",
            not aj.descartado(Path("GX011146.MP4"), 96.0),
        )
        check(
            "una linea mal escrita no rompe el analisis",
            len(aj.descartar) == 1,
            f"{aj.descartar}",
        )

    check("sin archivo, no hay ajustes", not ajustes_mod.load(Path(folder)).activos)


def test_bookends() -> None:
    """La apertura y el cierre salen de la velocidad, no del puntaje de accion.

    El puntaje castiga a proposito todo lo que pase por debajo de
    `moving_speed_kmh`, o sea justo el material que estas dos reglas buscan.
    Y el cierre no se inventa: si la camara dejo de grabar mientras todavia
    rodabas, no hay final que ofrecer.
    """
    print("\n[12] Apertura y cierre del video")
    cfg = config_mod.Config()
    hz = cfg.analysis_hz

    def curve(speed_points: list[tuple[float, float]], duration: float):
        t = np.arange(0.0, duration, 1.0 / hz)
        speed = np.interp(t, [p[0] for p in speed_points], [p[1] for p in speed_points])
        return signals_mod.Signals(
            t=t, score=np.full(t.size, 40.0), base=np.full(t.size, 40.0),
            speed_kmh=speed, hz=hz,
        )

    # Parado 12 s en el parqueadero, despues arranca y rueda.
    salida = curve([(0.0, 0.0), (12.0, 0.0), (14.0, 18.0), (60.0, 22.0)], 60.0)
    intro = bookends_mod.find_intro(Path("GX011136.MP4"), salida, cfg)
    check("encuentra la apertura", intro.segment is not None, intro.note)
    if intro.segment:
        arranque = 12.0 + (cfg.moving_speed_kmh / 18.0) * 2.0  # cruce de 5 km/h
        check(
            "abre con la bici quieta",
            intro.segment.start < arranque - 1.0,
            f"abre en {intro.segment.start:.1f}s, arranca cerca de {arranque:.1f}s",
        )
        check(
            "y no arrastra el parqueadero entero",
            arranque - intro.segment.start <= cfg.intro_lead_seconds + 0.2,
            f"{arranque - intro.segment.start:.1f}s de toma parada",
        )
        check("sigue despues de arrancar", intro.segment.end > arranque)
        check("va etiquetada", intro.segment.headline() == "INTRO")

    # Rueda, frena, y la camara sigue grabando 8 s con la bici quieta.
    llegada = curve([(0.0, 24.0), (40.0, 22.0), (44.0, 0.0), (52.0, 0.0)], 52.0)
    outro = bookends_mod.find_outro(Path("GX011151.MP4"), llegada, cfg)
    check("encuentra el cierre", outro.segment is not None, outro.note)
    if outro.segment:
        parada = 40.0 + (1.0 - cfg.moving_speed_kmh / 22.0) * 4.0
        check(
            "incluye la frenada",
            outro.segment.start < parada - 1.0,
            f"abre en {outro.segment.start:.1f}s, para cerca de {parada:.1f}s",
        )
        check(
            "aguanta con la bici detenida",
            outro.segment.end > parada + 1.0,
            f"cierra en {outro.segment.end:.1f}s",
        )
        check("va etiquetado", outro.segment.headline() == "FINAL")

    # Si la grabacion corta mientras todavia rueda, no hay final: se dice.
    cortado = curve([(0.0, 24.0), (30.0, 24.0)], 30.0)
    sin_final = bookends_mod.find_outro(Path("GX011151.MP4"), cortado, cfg)
    check("no inventa un final", sin_final.segment is None, "devolvio un cierre falso")
    check("y explica por que", "quieta" in sin_final.note, sin_final.note)

    # Sin GPS no hay forma de saber si paro.
    ciego = signals_mod.Signals(
        t=np.arange(0.0, 30.0, 1.0 / hz),
        score=np.full(int(30 * hz), 40.0),
        hz=hz,
    )
    check(
        "sin GPS no arriesga un cierre",
        bookends_mod.find_outro(Path("x.MP4"), ciego, cfg).segment is None,
    )

    # Los extremos no se rankean ni compiten con la accion.
    action = segments_mod.Segment(source=Path("GX011140.MP4"), start=1.0, end=6.0,
                                  score=80.0, peak_time=3.0)
    picked = segments_mod.select([action], target_seconds=100.0)
    check("los candidatos de accion si se rankean", picked[0].rank == 1)
    if intro.segment:
        check("la apertura no", intro.segment.role == "intro" and intro.segment.rank == 0)


def test_clip_reuse() -> None:
    """Un clip solo se reutiliza si es de verdad el corte que pide el analisis.

    Reutilizar por nombre ahorra media hora de render, pero el nombre no lleva
    la duracion: si cambia `post_roll` o el recorte de cola, el corte cambia y
    el nombre no. Y un render interrumpido deja un archivo truncado que pesa
    mas de cero. En los dos casos el clip viejo se colaria al video final sin
    un solo error en pantalla.
    """
    print("\n[15] Reutilizacion de clips ya renderizados")
    import tempfile

    segment = segments_mod.Segment(
        source=Path("GX011147.MP4"), start=10.0, end=25.0, score=80.0, peak_time=14.0
    )
    original = ffmpeg_mod.duration
    try:
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "01_080pts_AIRE_GX011147_00m10s.mp4"
            clip.write_bytes(b"x" * 1024)

            ffmpeg_mod.duration = lambda path: 15.02
            check("acepta el clip que coincide", render_mod.clip_is_current(clip, segment))

            ffmpeg_mod.duration = lambda path: 18.6
            check(
                "rechaza el que quedo de un ajuste anterior",
                not render_mod.clip_is_current(clip, segment),
            )

            ffmpeg_mod.duration = lambda path: 3.4
            check(
                "rechaza el truncado por un render interrumpido",
                not render_mod.clip_is_current(clip, segment),
            )

            def explota(path):
                raise RuntimeError("moov atom not found")

            ffmpeg_mod.duration = explota
            check(
                "un archivo ilegible se rehace, no revienta",
                not render_mod.clip_is_current(clip, segment),
            )
    finally:
        ffmpeg_mod.duration = original


def test_no_repeated_footage() -> None:
    """La accion no repite lo que ya sale en la apertura o en el cierre.

    El cierre arranca en la ultima frenada: si venias rodando fuerte hasta ahi,
    ese tramo ya entro como accion por su cuenta. Pegados uno detras del otro
    serian el mismo metraje dos veces en el video final.
    """
    print("\n[16] Ni la apertura ni el cierre repiten metraje")

    def seg(name, start, end, role=""):
        return segments_mod.Segment(
            source=Path(name), start=start, end=end, score=50.0,
            peak_time=(start + end) / 2, role=role,
        )

    outro = seg("GX011151.MP4", 14.0, 27.0, "outro")
    intro = seg("GX011145.MP4", 0.0, 6.5, "intro-cam")
    selected = [
        seg("GX011147.MP4", 10.0, 25.0),      # otro archivo, no se toca
        seg("GX011151.MP4", 2.0, 13.8),       # termina justo antes del cierre
        seg("GX011151.MP4", 18.0, 26.0),      # dentro del cierre
        seg("GX011145.MP4", 4.0, 12.0),       # pisa la apertura
    ]
    kept, removed = ride_mod.drop_repeated(selected, [intro, outro])

    check("quita el corte que cae dentro del cierre", len(removed) == 2, f"{len(removed)}")
    check("respeta lo que viene de otro archivo", kept[0].source.stem == "GX011147")
    check("un borde que se toca no cuenta como repetido", len(kept) == 2, f"{len(kept)}")
    check(
        "y el que sobrevive es el que termina antes",
        kept[1].start == 2.0 and kept[1].end == 13.8,
    )
    check(
        "sin apertura ni cierre no quita nada",
        ride_mod.drop_repeated(selected, [])[1] == [],
    )


def test_reported_budget() -> None:
    """El reporte dice los segundos que se pidieron de verdad.

    Se recalculaba sobre el ride entero, asi que cuando `ajustes.toml` fija el
    largo o deja archivos fuera -- los dos casos del ride del 16-ago -- el
    numero de `analysis.json` era uno que nunca se uso.
    """
    print("\n[17] El presupuesto que reporta es el que se uso")
    import json
    import tempfile

    cfg = config_mod.load()
    with tempfile.TemporaryDirectory() as folder:
        ride = ride_mod.Ride(root=Path(folder), budget=290.0)
        ride_mod.write_reports(ride, cfg)
        payload = json.loads(ride.analysis_file.read_text(encoding="utf-8"))
        check(
            "reporta el largo fijado a mano",
            payload["config"]["presupuesto_reel_s"] == 290.0,
            f"{payload['config']['presupuesto_reel_s']}",
        )

        sin_ajuste = ride_mod.Ride(root=Path(folder))
        ride_mod.write_reports(sin_ajuste, cfg)
        payload = json.loads(sin_ajuste.analysis_file.read_text(encoding="utf-8"))
        check(
            "sin ajuste sigue cayendo en el calculo por porcentaje",
            payload["config"]["presupuesto_reel_s"]
            == round(cfg.reel_budget(sin_ajuste.total_raw_seconds()), 1),
            f"{payload['config']['presupuesto_reel_s']}",
        )


def test_space_check() -> None:
    """Copiar sin espacio falla al principio, no a los 20 GB."""
    print("\n[18] Espacio antes de copiar")
    import tempfile

    from pov import ingest as ingest_mod

    with tempfile.TemporaryDirectory() as folder:
        destino = Path(folder) / "raw"
        destino.mkdir()

        libre = ingest_mod.shutil.disk_usage(destino).free
        ok = True
        try:
            ingest_mod.check_space(destino, [1024, 2048])
        except RuntimeError:
            ok = False
        check("deja pasar lo que cabe", ok)

        exploto = False
        try:
            ingest_mod.check_space(destino, [libre * 2])
        except RuntimeError as exc:
            exploto = "limpiar --todos" in str(exc)
        check("para lo que no cabe, y dice como liberar", exploto)

        check("sin nada que copiar no molesta", ingest_mod.check_space(destino, []) is None)

        # Lo ya copiado no se vuelve a contar: reintentar una ingesta a medias
        # no puede fallar por espacio que en realidad ya esta ocupado.
        (destino / "GX010001.MP4").write_bytes(b"x" * 100)
        pendiente = ingest_mod._pending(
            {"GX010001.MP4": 100, "GX010002.MP4": 500}, destino
        )
        check("no cuenta lo que ya esta copiado", pendiente == [500], f"{pendiente}")

    # Y la carcasa que deja una ingesta fallida no se convierte en "el ride mas
    # reciente" para todos los comandos que corras sin nombre.
    with tempfile.TemporaryDirectory() as folder:
        library = Path(folder)
        bueno = library / "2026-08-16_trail"
        (bueno / "raw").mkdir(parents=True)
        (bueno / "raw" / "GX010001.MP4").write_bytes(b"x")
        (library / "2026-08-17").mkdir()  # ingesta que fallo: existe y esta vacia

        elegido = ride_mod.resolve(library, None)
        check("ignora el ride vacio de una ingesta fallida", elegido == bueno, f"{elegido.name}")


def test_shorts() -> None:
    """Fase 2: umbral, agrupado, orden descendente y nombre de archivo.

    El agrupado tiene dos topes independientes -- `shorts_max_clips` y
    `shorts_max_seconds` -- y hay que probarlos por separado, porque un short
    que se corta por duracion antes de llegar al tope de clips (o al reves)
    es exactamente el caso que un solo ejemplo no distingue.
    """
    print("\n[19] Fase 2: shorts")

    def seg(name: str, start: float, dur: float, score: float, role: str = "") -> segments_mod.Segment:
        return segments_mod.Segment(
            source=Path(name), start=start, end=start + dur, score=score,
            peak_time=start, role=role,
        )

    def fake_ride(selected: list[segments_mod.Segment], trail: str = "Test Trail") -> ride_mod.Ride:
        return ride_mod.Ride(
            root=Path("fake-ride"),
            selected=selected,
            ajustes=ajustes_mod.Ajustes(nombre_trail=trail),
        )

    cfg = config_mod.load()

    # --- umbral: descarta lo flojo y lo que es apertura/cierre -------------
    # `shorts_min_seconds: 0` en los casos de agrupado: lo que se prueba aca
    # es el umbral de puntaje y los topes, no el piso de duracion (que tiene
    # su propio caso mas abajo). Sin esto, clips de prueba de 3-8 s caerian
    # por el piso y el caso dejaria de medir lo que dice medir.
    cfg = cfg.merged({"shorts_min_seconds": 0.0})
    cfg_umbral = cfg.merged({"shorts_min_score": 50.0})
    ride = fake_ride([
        seg("GX01.MP4", 0.0, 8.0, 60.0),
        seg("GX01.MP4", 20.0, 8.0, 40.0),   # bajo el umbral
        seg("GX01.MP4", 40.0, 6.0, 55.0, role="intro"),  # no es accion
    ])
    cortos = shorts_mod.select_shorts(ride, cfg_umbral)
    check(
        "el umbral descarta lo flojo y lo que no es accion",
        len(cortos) == 1 and len(cortos[0].clips) == 1 and cortos[0].clips[0].score == 60.0,
        f"{[(s.duration, len(s.clips)) for s in cortos]}",
    )

    # --- tope de duracion: cierra el grupo aunque quepan mas clips ---------
    cfg_dur = cfg.merged({"shorts_min_score": 50.0, "shorts_max_clips": 3, "shorts_max_seconds": 20.0})
    ride = fake_ride([
        seg("GX01.MP4", 0.0, 8.0, 60.0),
        seg("GX01.MP4", 10.0, 8.0, 65.0),   # 16s, todavia cabe
        seg("GX01.MP4", 20.0, 8.0, 70.0),   # 24s > 20: no cabe, abre grupo nuevo
    ])
    cortos = shorts_mod.select_shorts(ride, cfg_dur)
    check(
        "el tope de duracion parte el grupo aunque el de clips no se llene",
        [len(s.clips) for s in cortos] == [2, 1],
        f"{[len(s.clips) for s in cortos]}",
    )
    check(
        "el grupo cerrado no supera shorts_max_seconds",
        all(s.duration <= 20.0 for s in cortos),
        f"{[s.duration for s in cortos]}",
    )

    # --- tope de clips: cierra el grupo aunque sobre tiempo -----------------
    cfg_clips = cfg.merged({"shorts_min_score": 50.0, "shorts_max_clips": 2, "shorts_max_seconds": 59.0})
    ride = fake_ride([
        seg("GX01.MP4", 0.0, 3.0, 60.0),
        seg("GX01.MP4", 5.0, 3.0, 61.0),
        seg("GX01.MP4", 10.0, 3.0, 62.0),   # el grupo ya tiene 2: abre uno nuevo
    ])
    cortos = shorts_mod.select_shorts(ride, cfg_clips)
    check(
        "el tope de clips parte el grupo aunque sobre presupuesto de tiempo",
        [len(s.clips) for s in cortos] == [2, 1],
        f"{[len(s.clips) for s in cortos]}",
    )

    # --- orden descendente dentro del grupo: el mas fuerte abre ------------
    ride = fake_ride([
        seg("GX01.MP4", 0.0, 5.0, 70.0),
        seg("GX01.MP4", 10.0, 5.0, 55.0),
        seg("GX01.MP4", 20.0, 5.0, 62.0),
    ])
    cortos = shorts_mod.select_shorts(ride, cfg_dur)
    scores = [c.score for c in cortos[0].clips]
    check(
        "dentro del grupo los clips quedan de mas fuerte a mas flojo",
        scores == sorted(scores, reverse=True),
        f"{scores}",
    )
    check("el climax es el primero del grupo, el de mayor puntaje", cortos[0].climax.score == max(scores))
    check("el puntaje del short es el del climax, no un promedio", cortos[0].score == max(scores))

    # --- piso de duracion: el sobrante corto no llega a ser short ----------
    # Caso real: el short #7 de JD Park era un clip suelto de 7 s. Nadie
    # alcanza a engancharse, asi que se descarta -- pero avisando, porque es
    # accion real que se tira.
    cfg_piso = cfg.merged({
        "shorts_min_score": 50.0, "shorts_max_clips": 2,
        "shorts_max_seconds": 40.0, "shorts_min_seconds": 15.0,
    })
    ride = fake_ride([
        seg("GX01.MP4", 0.0, 10.0, 60.0),
        seg("GX01.MP4", 20.0, 10.0, 65.0),   # grupo 1 = 20s, pasa el piso
        seg("GX01.MP4", 40.0, 7.0, 70.0),    # sobrante de 7s: no llega a 15
    ])
    descartados: list[float] = []
    cortos = shorts_mod.select_shorts(
        ride, cfg_piso, on_discard=lambda members, total: descartados.append(total)
    )
    check(
        "el sobrante corto no se convierte en short",
        len(cortos) == 1 and len(cortos[0].clips) == 2,
        f"{[len(s.clips) for s in cortos]}",
    )
    check(
        "y avisa cuanto descarto en vez de tirarlo en silencio",
        descartados == [7.0],
        f"{descartados}",
    )
    check(
        "ningun short queda bajo el piso",
        all(s.duration >= cfg_piso.shorts_min_seconds for s in cortos),
        f"{[s.duration for s in cortos]}",
    )

    # Renumerar despues de descartar, no antes: `order` es la clave que usa
    # shorts_guion.toml, y un hueco (1, 3, 4...) desalinearia todo el guion.
    ride = fake_ride([
        seg("GX01.MP4", 0.0, 5.0, 60.0),     # grupo 1 = 5s, se cae por corto
        seg("GX01.MP4", 20.0, 20.0, 65.0),   # grupo 2 = 20s, sobrevive
        seg("GX01.MP4", 50.0, 20.0, 70.0),   # grupo 3 = 20s, sobrevive
    ])
    cortos = shorts_mod.select_shorts(ride, cfg_piso)
    check(
        "los orden quedan 1..N sin huecos tras descartar",
        [s.order for s in cortos] == [1, 2],
        f"{[s.order for s in cortos]}",
    )

    # --- piso de puntaje por ride: manda sobre el global -------------------
    # Un ride importado de un MP4 ya editado no trae telemetria, y ahi el
    # techo de puntaje es base_gain*100 = 65, no 100. Con el global en 55 casi
    # nada calificaria, y no seria porque el ride es flojo.
    cfg_score = cfg.merged({"shorts_min_score": 55.0, "shorts_max_seconds": 40.0})
    sin_telemetria = [
        seg("GX01.MP4", 0.0, 10.0, 42.0),
        seg("GX01.MP4", 20.0, 10.0, 38.0),
    ]
    ride = ride_mod.Ride(
        root=Path("fake-ride"), selected=sin_telemetria,
        ajustes=ajustes_mod.Ajustes(nombre_trail="Test"),
    )
    check(
        "con el global de 55 un ride sin telemetria no da nada",
        shorts_mod.select_shorts(ride, cfg_score) == [],
    )
    ride_ajustado = ride_mod.Ride(
        root=Path("fake-ride"), selected=sin_telemetria,
        ajustes=ajustes_mod.Ajustes(nombre_trail="Test", shorts_min_score=30.0),
    )
    cortos_ajustados = shorts_mod.select_shorts(ride_ajustado, cfg_score)
    check(
        "el piso de ajustes.toml rescata ese mismo ride",
        len(cortos_ajustados) == 1 and len(cortos_ajustados[0].clips) == 2,
        f"{[len(s.clips) for s in cortos_ajustados]}",
    )

    # --- nombre de archivo ---------------------------------------------------
    cortos = shorts_mod.select_shorts(fake_ride([
        seg("GX01.MP4", 0.0, 5.0, 70.0),
        seg("GX01.MP4", 10.0, 5.0, 55.0),
        seg("GX01.MP4", 20.0, 5.0, 62.0),
    ]), cfg_dur)
    corto = cortos[0]
    nombre = shorts_mod.short_filename(corto.order, corto)
    check(
        "el nombre lleva orden, puntaje y termina en _short.mp4",
        nombre.startswith(f"{corto.order:02d}_") and nombre.endswith("_short.mp4"),
        nombre,
    )

    # --- el .ass parte las lineas largas en vez de cortarlas ---------------
    # Con WrapStyle 2 libass no ajusta: "how many g's do you think that first
    # one was?" salia como "w many g's ... that first one w", cortada por los
    # dos lados, en el short #2 de JD Park.
    ass = shorts_mod.build_short_labels(cortos[0], cfg)
    check(
        "el .ass pide ajuste automatico de linea (WrapStyle 0)",
        "WrapStyle: 0" in ass,
        [l for l in ass.splitlines() if "WrapStyle" in l],
    )

    # --- cartel de cierre hacia YouTube -------------------------------------
    # Existe porque en TikTok Chris no tiene ningun enlace clickeable
    # disponible (ni bio, ni botones sociales, ni sitio web sin 1.000
    # seguidores): el unico lugar donde el mensaje llega es dentro del video.
    # La linea base tiene que pedir explicitamente "sin cartel": config.toml
    # ya lo trae puesto para todos los rides, asi que `cfg` no sirve de
    # control.
    mismos = [
        seg("GX01.MP4", 0.0, 5.0, 70.0),
        seg("GX01.MP4", 10.0, 5.0, 55.0),
        seg("GX01.MP4", 20.0, 5.0, 62.0),
    ]
    cfg_sin_outro = cfg_dur.merged({"shorts_outro_text": ""})
    corto_sin = shorts_mod.select_shorts(fake_ride(mismos), cfg_sin_outro)[0]
    sin_outro = shorts_mod.build_short_labels(corto_sin, cfg_sin_outro)
    check(
        "sin shorts_outro_text no se quema ningun cartel",
        ",Outro,," not in sin_outro,
        [l for l in sin_outro.splitlines() if "Dialogue" in l],
    )

    cfg_outro = cfg_dur.merged(
        {"shorts_outro_text": "full ride on YouTube @x", "shorts_outro_seconds": 4.0}
    )
    corto_outro = shorts_mod.select_shorts(fake_ride(mismos), cfg_outro)[0]
    check(
        "el cartel sale de la config y queda en el short",
        corto_outro.outro == "full ride on YouTube @x",
        corto_outro.outro,
    )

    ass_outro = shorts_mod.build_short_labels(corto_outro, cfg_outro)
    dialogos = [l for l in ass_outro.splitlines() if l.startswith("Dialogue")]
    outro_lines = [l for l in dialogos if ",Outro,," in l]
    guion_lines = [l for l in dialogos if ",Line,," in l]
    check("el cartel aparece una sola vez", len(outro_lines) == 1, outro_lines)
    check(
        "el .ass declara un estilo Outro al pie (Alignment 2) y Line arriba (8)",
        "Style: Outro," in ass_outro
        and ass_outro.split("Style: Outro,")[1].split("\n")[0].split(",")[-5] == "2",
        [l for l in ass_outro.splitlines() if l.startswith("Style:")],
    )

    # Lo que importa de verdad: el cartel NO le roba tiempo a la pregunta de
    # cierre. Van en posiciones distintas (arriba vs abajo), asi que pueden
    # solaparse en el tiempo -- y de hecho tienen que hacerlo, porque los dos
    # viven en los ultimos segundos. Si alguien alineara los dos estilos al
    # mismo lado, esto seguiria pasando pero se taparian en pantalla; por eso
    # la comprobacion de Alignment de arriba es parte del mismo contrato.
    check(
        "el guion conserva todas sus lineas con el cartel puesto",
        len(guion_lines) == len([l for l in sin_outro.splitlines() if l.startswith("Dialogue")]),
        f"{len(guion_lines)} con cartel vs {len([l for l in sin_outro.splitlines() if l.startswith('Dialogue')])} sin cartel",
    )
    check(
        "el cartel cubre los ultimos segundos y termina con el short",
        _ass_seconds(outro_lines[0].split(",")[2]) >= corto_outro.duration - 0.01,
        outro_lines[0],
    )
    check(
        "el cartel arranca shorts_outro_seconds antes del final",
        abs(_ass_seconds(outro_lines[0].split(",")[1]) - (corto_outro.duration - 4.0)) < 0.05,
        f"arranca en {_ass_seconds(outro_lines[0].split(',')[1]):.2f}, dura {corto_outro.duration:.2f}",
    )

    # Cambiar el cartel tiene que producir un archivo nuevo: si el nombre no
    # cambiara, `clip_is_current` (que solo mira duracion) daria por bueno el
    # render viejo y el cartel nuevo no llegaria nunca al video.
    check(
        "cambiar el cartel invalida el render anterior",
        shorts_mod.short_filename(corto_outro.order, corto_outro)
        != shorts_mod.short_filename(corto_sin.order, corto_sin),
        f"{shorts_mod.short_filename(corto_outro.order, corto_outro)} vs "
        f"{shorts_mod.short_filename(corto_sin.order, corto_sin)}",
    )

    # --- respaldo del nombre del trail sin ajustes.toml ---------------------
    check(
        "sin nombre_trail, prettifica el slug de la carpeta",
        shorts_mod._pretty_trail("2026-08-16_halpatiokee-mtb-trail") == "Halpatiokee Mtb Trail",
    )

    # --- texto: categoria por tipo de evento, prioridad crash > air > impact ---
    base = dict(source=Path("GX01.MP4"), start=0.0, end=10.0, score=60.0, peak_time=1.0)
    solo_impacto = segments_mod.Segment(**base, events=[Event("impact", 1.0, 1.2, 3.5)])
    con_caida = segments_mod.Segment(
        **base,
        events=[Event("impact", 1.0, 1.2, 3.5), Event("crash", 1.0, 4.0, 5.0)],
    )
    impacto_fuerte = segments_mod.Segment(**base, events=[Event("impact", 1.0, 1.2, 7.0)])
    solo_velocidad = segments_mod.Segment(**base, events=[], peak_speed_kmh=25.0)
    sin_nada = segments_mod.Segment(**base, events=[])

    check("impacto moderado -> categoria impact", shorts_textos.category_for(solo_impacto) == "impact")
    check("caida manda sobre impacto", shorts_textos.category_for(con_caida) == "crash")
    check(
        "impacto fuerte (>=6g) -> categoria impact_big",
        shorts_textos.category_for(impacto_fuerte) == "impact_big",
    )
    check("sin eventos pero con velocidad -> categoria speed", shorts_textos.category_for(solo_velocidad) == "speed")
    check("sin nada -> categoria generic", shorts_textos.category_for(sin_nada) == "generic")

    # --- texto: determinista, y {trail} se completa -------------------------
    hook1 = shorts_textos.pick_hook(solo_impacto, "Test Trail")
    hook2 = shorts_textos.pick_hook(solo_impacto, "Test Trail")
    check("el hook es el mismo en dos corridas del mismo clip", hook1 == hook2, f"{hook1!r} vs {hook2!r}")
    check("el hook cae en la lista de plantillas de su categoria", hook1 in [
        t.format(trail="Test Trail") if "{trail}" in t else t
        for t in shorts_textos.HOOK_TEMPLATES["impact"]
    ])

    speed_hook = shorts_textos.pick_hook(solo_velocidad, "Halpatiokee MTB Trail")
    check("el {trail} de la plantilla queda relleno, no literal", "{trail}" not in speed_hook)

    cierre1 = shorts_textos.pick_closing(solo_impacto, "Test Trail")
    cierre2 = shorts_textos.pick_closing(solo_impacto, "Test Trail")
    check("el cierre es el mismo en dos corridas del mismo clip", cierre1 == cierre2)


def test_shorts_guion() -> None:
    """El guion escrito a mano: anclado al climax, nunca aplicado a ciegas.

    El riesgo real es que el agrupado cambie (se toco `shorts_min_score`, se
    reanalizo con otro material) y un guion viejo se pegue al short
    equivocado sin avisar -- exactamente la clase de bug silencioso que
    `render.clip_is_current` existe para evitar en Fase 1. Por eso el guion
    ancla cada entrada a su clip climax, y quien lo aplica valida esa ancla.
    """
    print("\n[20] Guion escrito a mano para los shorts")
    import tempfile

    def seg(name: str, start: float, dur: float, score: float) -> segments_mod.Segment:
        return segments_mod.Segment(
            source=Path(name), start=start, end=start + dur, score=score, peak_time=start,
        )

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / shorts_guion_mod.GUION_FILE).write_text(
            """
[[shorts]]
order = 1
climax = "GX011147@172.9"
alt_hook = "watch what the roots do here"
lineas = [
  { t = 9.8, texto = "did NOT see that root" },
  { t = 0.0, texto = "this section humbled me last time" },
]

[[shorts]]
order = 2
climax = "GX011150@999.0"
lineas = [ { t = 0.0, texto = "texto que no deberia aplicarse" } ]
""",
            encoding="utf-8",
        )

        entries = shorts_guion_mod.load(root)
        check("carga una entrada por short anotado", set(entries.keys()) == {1, 2}, f"{entries.keys()}")
        check(
            "las lineas quedan ordenadas por tiempo aunque el TOML no lo estuviera",
            [t for t, _ in entries[1].lineas] == [0.0, 9.8],
        )

        climax_ok = seg("GX011147.MP4", 172.9, 10.45, 69.6)
        climax_movido = seg("GX011147.MP4", 174.2, 10.45, 69.6)  # reanalisis lo corrio 1.3s
        climax_otro = seg("GX011150.MP4", 41.1, 13.35, 57.4)

        check("el ancla coincide dentro de la tolerancia", shorts_guion_mod.matches(entries[1], climax_ok))
        check(
            "un corrimiento chico por reanalisis sigue contando como el mismo climax",
            shorts_guion_mod.matches(
                shorts_guion_mod.GuionCorto(anchor="GX011147@172.9", lineas=[]), climax_movido
            ),
        )
        check(
            "un climax de otro archivo no coincide",
            not shorts_guion_mod.matches(entries[1], climax_otro),
        )
        check(
            "un ancla vacia nunca coincide",
            not shorts_guion_mod.matches(shorts_guion_mod.GuionCorto(anchor="", lineas=[]), climax_ok),
        )

        cfg = config_mod.load()
        corto1 = shorts_mod.Short(clips=[climax_ok], order=1)
        corto1.lines = shorts_mod._fallback_lines(corto1, "Test Trail", cfg)
        nombre_automatico = shorts_mod.short_filename(1, corto1)

        corto2 = shorts_mod.Short(clips=[climax_otro], order=2)
        corto2.lines = shorts_mod._fallback_lines(corto2, "Test Trail", cfg)

        avisos = shorts_mod.apply_guion(
            ride_mod.Ride(root=root, selected=[]), [corto1, corto2]
        )

        check("el short con climax vigente adopta las lineas del guion", corto1.guion, f"{corto1.guion}")
        check(
            "y las lineas son las del guion, no las automaticas",
            corto1.lines == [(0.0, "this section humbled me last time"), (9.8, "did NOT see that root")],
            f"{corto1.lines}",
        )
        check("se lleva tambien el alt_hook", corto1.alt_hook == "watch what the roots do here")
        check(
            "el short cuyo climax ya no coincide se queda con el texto automatico",
            not corto2.guion,
        )
        check("y avisa en vez de fallar en silencio", len(avisos) == 1 and "#2" in avisos[0], f"{avisos}")

        check(
            "el nombre de archivo cambia cuando el texto cambia (invalida el render viejo)",
            shorts_mod.short_filename(1, corto1) != nombre_automatico,
        )

    # --- write_plan: offsets relativos al inicio del short, no del archivo crudo ---
    with tempfile.TemporaryDirectory() as folder:
        import json

        ride = ride_mod.Ride(root=Path(folder), selected=[])
        clip_a = segments_mod.Segment(
            source=Path("GX01.MP4"), start=10.0, end=18.0, score=69.6, peak_time=14.0,
        )
        clip_b = segments_mod.Segment(
            source=Path("GX02.MP4"), start=100.0, end=110.45, score=50.0, peak_time=100.9,
            events=[Event("impact", 100.9, 101.1, 6.2)],
        )
        corto = shorts_mod.Short(clips=[clip_a, clip_b], order=1)
        plan_path = shorts_guion_mod.write_plan(ride, [corto])
        plan = json.loads(plan_path.read_text(encoding="utf-8"))

        entry = plan["shorts"][0]
        check("el plan trae un short con dos clips", len(entry["clips"]) == 2)
        check(
            "el segundo clip arranca donde termina el primero (8s)",
            entry["clips"][1]["offset_start"] == 8.0,
            f"{entry['clips'][1]['offset_start']}",
        )
        evento = entry["clips"][1]["events"][0]
        check(
            "el evento queda en el eje de tiempo del short, no del archivo crudo",
            abs(evento["offset_start"] - 8.9) < 1e-6,
            f"{evento['offset_start']}",
        )
        check(
            "el ancla del climax es la del clip mas fuerte del grupo",
            entry["climax_anchor"] == "GX01@10.0",
            entry["climax_anchor"],
        )


def test_material_ya_editado() -> None:
    """Material que entra ya editado: no caer encima de sus propios cortes.

    Un MP4 exportado de un editor lleva dentro los cortes de esa edicion. El
    detector de accion no los ve (puntua por audio y acelerometro, no por
    imagen), asi que elige un tramo cualquiera y sus bordes pueden caer a
    medio segundo de uno de ellos. Lo que sobrevive en el short es un plano
    que aparece y se va: Chris cazo tres (0.53, 0.33 y 0.50 s) en el short #1
    del ride del 19-jul-2026.
    """
    print("\n[21] Material que entra ya editado")

    def seg(name: str, start: float, end: float, score: float = 60.0) -> segments_mod.Segment:
        return segments_mod.Segment(
            source=Path(name), start=start, end=end, score=score, peak_time=(start + end) / 2,
        )

    cfg = config_mod.load().merged({"shorts_min_fragmento": 1.2, "min_segment_seconds": 3.1})
    cortes = [10.0, 20.0, 30.0]

    # --- snap: solo los extremos, nunca el medio -------------------------
    check(
        "una migaja al principio se va moviendo el borde al corte",
        escenas_mod.snap(9.5, 25.0, cortes, 1.2) == (10.0, 25.0),
        f"{escenas_mod.snap(9.5, 25.0, cortes, 1.2)}",
    )
    check(
        "una migaja al final tambien",
        escenas_mod.snap(12.0, 20.4, cortes, 1.2) == (12.0, 20.0),
        f"{escenas_mod.snap(12.0, 20.4, cortes, 1.2)}",
    )
    check(
        "un corte a mitad del tramo NO se toca (hay material a los dos lados)",
        escenas_mod.snap(5.0, 25.0, cortes, 1.2) == (5.0, 25.0),
        f"{escenas_mod.snap(5.0, 25.0, cortes, 1.2)}",
    )
    # Dos migajas seguidas: mover el borde una sola vez dejaria la segunda.
    check(
        "dos migajas seguidas se van las dos",
        escenas_mod.snap(9.5, 25.0, [10.0, 10.8, 20.0], 1.2) == (10.8, 25.0),
        f"{escenas_mod.snap(9.5, 25.0, [10.0, 10.8, 20.0], 1.2)}",
    )
    check(
        "un archivo sin cortes propios se queda como estaba",
        escenas_mod.snap(9.5, 25.0, [], 1.2) == (9.5, 25.0),
    )

    # --- apply: ajusta, descarta lo que era casi todo migaja, avisa -------
    notas: list[str] = []
    ajustados = escenas_mod.apply(
        [
            seg("A.mp4", 9.5, 25.0),    # migaja de 0.5s al principio -> se ajusta
            seg("A.mp4", 19.05, 23.05),  # queda en 3.05s, bajo el minimo -> se va
            seg("B.mp4", 0.0, 8.0),      # archivo sin cortes -> intacto
        ],
        {"A.mp4": cortes},
        cfg,
        on_note=notas.append,
    )
    check("el tramo ajustado sobrevive con el borde corrido", ajustados[0].start == 10.0)
    check(
        "el tramo que entre cortes se queda bajo el minimo se descarta entero",
        len(ajustados) == 2 and all(s.start != 19.05 for s in ajustados),
        f"{[(s.source.name, s.start) for s in ajustados]}",
    )
    check(
        "un archivo del que no se detectaron cortes pasa intacto",
        ajustados[1].source.name == "B.mp4" and ajustados[1].start == 0.0,
    )
    check("cada cambio se avisa, no pasa en silencio", len(notas) == 2, f"{notas}")

    # El detector no es perfecto: el sol entre las palmas y el motion blur
    # disparan `scene` igual que un corte. Cuatro falsos positivos seguidos se
    # comieron un clip continuo de 5.85 s del 26-jul antes de existir el tope.
    falsos = [54.87, 55.47, 56.33, 57.40]
    notas_tope: list[str] = []
    intacto = escenas_mod.apply(
        [seg("A.mp4", 52.43, 58.28)], {"A.mp4": falsos}, cfg, on_note=notas_tope.append
    )
    check(
        "un clip que perderia mas del tope NO se recorta: es el detector fallando",
        len(intacto) == 1 and intacto[0].start == 52.43 and intacto[0].end == 58.28,
        f"{[(s.start, s.end) for s in intacto]}",
    )
    check(
        "y se avisa de que no se ajusto, no pasa en silencio",
        len(notas_tope) == 1 and "NO ajuste" in notas_tope[0],
        f"{notas_tope}",
    )

    # Un archivo crudo de GoPro es una toma continua: buscarle cortes seria
    # decodificar 47 min por ride para no encontrar nada.
    check(
        "solo se le buscan cortes al material sin telemetria",
        escenas_mod.necesita_deteccion({"accel_hz": 0, "gps": False})
        and not escenas_mod.necesita_deteccion({"accel_hz": 199, "gps": True}),
    )

    # --- reparto equilibrado: no tirar un huerfano que califico ----------
    # Las duraciones reales del ride del 26-jul: el greedy hace 3+3+1 y tira
    # 6.9 s; 2+2+3 saca tres shorts validos del mismo material.
    duraciones = [11.31, 17.05, 11.08, 8.30, 5.70, 5.78, 6.90]
    eligible = []
    cursor = 0.0
    for d in duraciones:
        eligible.append(seg("A.mp4", cursor, cursor + d))
        cursor += d + 1.0

    cfg_grupos = cfg.merged({"shorts_max_clips": 3, "shorts_max_seconds": 40.0})
    greedy = shorts_mod._greedy_groups(eligible, cfg_grupos)
    check(
        "el greedy deja un huerfano por debajo del piso",
        [len(g) for g in greedy] == [3, 3, 1],
        f"{[len(g) for g in greedy]}",
    )
    balanceado = shorts_mod._balanced_groups(eligible, cfg_grupos, 15.0)
    check(
        "el reparto equilibrado saca un short mas de los mismos clips",
        balanceado is not None and len(balanceado) == 3,
        f"{balanceado and [len(g) for g in balanceado]}",
    )
    check(
        "y ningun grupo queda por debajo del piso",
        all(sum(c.duration for c in g) >= 15.0 for g in balanceado),
        f"{[round(sum(c.duration for c in g), 1) for g in balanceado]}",
    )
    check(
        "el reparto sigue siendo cronologico (no reordena el ride)",
        [c.start for g in balanceado for c in g] == [c.start for c in eligible],
    )
    # Cuando el material no da para que todos lleguen al piso, no se inventa:
    # devuelve None y manda el greedy, que descarta avisando.
    check(
        "sin material suficiente devuelve None en vez de forzar un short flojo",
        shorts_mod._balanced_groups(eligible[:1], cfg_grupos, 15.0) is None,
    )

    # El rebalanceo solo entra si el greedy dejo algo fuera: un ride que ya
    # reparte bien no se toca, para no mover shorts que Chris ya aprobo.
    limpios = [seg("A.mp4", i * 40.0, i * 40.0 + 16.0) for i in range(4)]
    ride = ride_mod.Ride(
        root=Path("fake-ride"),
        selected=limpios,
        ajustes=ajustes_mod.Ajustes(nombre_trail="Test"),
    )
    cortos = shorts_mod.select_shorts(ride, cfg_grupos.merged({"shorts_min_score": 50.0}))
    check(
        "un ride sin huerfanos conserva el reparto greedy",
        [len(s.clips) for s in cortos] == [len(g) for g in shorts_mod._greedy_groups(limpios, cfg_grupos)],
        f"{[len(s.clips) for s in cortos]}",
    )

    # --- piso de duracion por ride ---------------------------------------
    corto_ride = ride_mod.Ride(
        root=Path("fake-ride"),
        selected=[seg("A.mp4", 0.0, 13.0)],
        ajustes=ajustes_mod.Ajustes(nombre_trail="Test", shorts_min_seconds=13.0),
    )
    check(
        "shorts_min_seconds del ride manda sobre el global",
        len(shorts_mod.select_shorts(corto_ride, cfg_grupos.merged({"shorts_min_score": 50.0}))) == 1,
    )


def main() -> int:
    print("Validando el motor con datos sinteticos")
    print("=" * 46)

    test_gpmf_parser()
    test_event_detection()
    test_no_telemetry_fallback()
    test_segments()
    test_config_toml()
    test_no_air_does_not_crush_scale()
    test_ride_wide_scale()
    test_head_trim()
    test_split_at_valleys()
    test_chapter_order()
    test_cleanup()
    test_bookends()
    test_tail_trim()
    test_ajustes()
    test_clip_reuse()
    test_no_repeated_footage()
    test_reported_budget()
    test_space_check()
    test_shorts()
    test_shorts_guion()
    test_material_ya_editado()

    print("\n" + "=" * 46)
    print(f"{len(PASSED)} OK, {len(FAILED)} fallas")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
