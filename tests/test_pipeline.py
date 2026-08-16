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
from pov import gpmf, segments as segments_mod, signals as signals_mod
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
        for sub, count in (("raw", 3), ("clips", 4), ("reel", 1), (".huellas", 2), ("final", 1)):
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
        check("ordena por tamano", [i.size for i in items] == sorted((i.size for i in items), reverse=True))

        freed = cleanup_mod.remove(items)
        check("libera lo que dijo", freed == 7000, f"{freed} bytes")
        check("el video final sobrevive", (ride / "final" / "f0.bin").exists())
        check("los originales siguen ahi", len(list((ride / "raw").iterdir())) == 3)
        check("el analisis sobrevive", (ride / "analysis.json").exists())
        check("la lista de cortes sobrevive", (ride / "cortes.csv").exists())

        # Solo cuando se pide explicitamente aparece raw, y marcado.
        con_final = cleanup_mod.survey(ride, include_final=True)
        check(
            "con --incluir-final si aparece",
            any(i.path.name == "final" for i in con_final),
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
            "reel_segundos = 290\n",
            encoding="utf-8",
        )
        aj = ajustes_mod.load(root)

        check("lee la exclusion", aj.excluido(Path("x/GX011136.MP4")))
        check("ignora la extension al comparar", aj.excluido(Path("x/GX011137.MP4")))
        check("no excluye de mas", not aj.excluido(Path("x/GX011138.MP4")))
        check("lee la apertura elegida", aj.aperturas == ["GX011145"])
        check("lee el largo del reel", aj.reel_segundos == 290)
        check("lee el cierre elegido", aj.cierre == ("GX011151", 14.0), f"{aj.cierre}")

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

    print("\n" + "=" * 46)
    print(f"{len(PASSED)} OK, {len(FAILED)} fallas")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
