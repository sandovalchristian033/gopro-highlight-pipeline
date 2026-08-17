"""Prueba de punta a punta del camino de render.

Genera videos falsos con rafagas de audio en momentos conocidos, corre el
pipeline completo encima, y comprueba que salgan los clips y el reel.

No reemplaza probar con material real: aca no hay telemetria, asi que ejercita
el camino de respaldo por audio. Lo que valida es la parte fragil de ffmpeg
(recorte, concat, quemado de etiquetas, NVENC).

    python tests\\smoke_render.py [carpeta_de_trabajo]
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pov import config as config_mod
from pov import ffmpeg, render, ride as ride_mod
from pov import shorts as shorts_mod

SAMPLE_RATE = 48000
CLIP_SECONDS = 30
# Rafagas fuertes, en segundos, dentro de cada video generado.
BURSTS = [(7.0, 10.0), (18.0, 21.5), (26.0, 29.0)]


def write_envelope_wav(path: Path, seconds: int, shift: float) -> None:
    """Un tono cuya amplitud sube durante las rafagas."""
    t = np.arange(0, seconds * SAMPLE_RATE) / SAMPLE_RATE
    tone = np.sin(2 * np.pi * 300 * t)

    envelope = np.full(t.size, 0.04)
    for start, end in BURSTS:
        window = (t >= start + shift) & (t <= end + shift)
        envelope[window] = 0.9

    samples = np.clip(tone * envelope, -1.0, 1.0)
    pcm = (samples * 32767).astype("<i2")

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())


def make_video(destination: Path, index: int, workdir: Path) -> Path:
    out = destination / f"GX0{index}0001.MP4"
    if out.exists():
        return out

    audio = workdir / f"tone_{index}.wav"
    write_envelope_wav(audio, CLIP_SECONDS, shift=index * 0.7)

    ffmpeg.run(
        [
            "-y",
            "-f", "lavfi",
            "-i", f"testsrc2=size=1920x1080:rate=30:duration={CLIP_SECONDS}",
            "-i", str(audio),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            str(out),
        ]
    )
    return out


def main() -> int:
    if not ffmpeg.available():
        print("Falta ffmpeg en el PATH.")
        return 1

    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="pov_smoke_"))
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    print(f"Carpeta de prueba: {root}")
    print(f"NVENC disponible : {ffmpeg.has_nvenc()}")

    with tempfile.TemporaryDirectory(prefix="pov_tones_") as tmp:
        print("\nGenerando videos de prueba...")
        for index in range(1, 4):
            path = make_video(raw, index, Path(tmp))
            print(f"  {path.name}")

    cfg = config_mod.load()
    # Sin telemetria los segmentos son mas cortos; bajamos el presupuesto.
    cfg = cfg.merged({"target_reel_seconds": 60.0})

    print("\nAnalizando...")
    result = ride_mod.analyse(root, cfg, on_progress=lambda i, n, name: print(f"  [{i}/{n}] {name}"))
    ride_mod.write_reports(result, cfg)

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'OK  ' if condition else 'FALLA'} {name}  {detail}")
        if not condition:
            failures.append(name)

    print("\nComprobaciones del analisis:")
    check("se analizaron los 3 archivos", len(result.files) == 3, f"{len(result.files)}")
    check("se seleccionaron segmentos", len(result.selected) > 0, f"{len(result.selected)}")
    check("existe analysis.json", result.analysis_file.exists())
    check("existe cortes.csv", result.cutlist_file.exists())
    check(
        "los segmentos caen en las rafagas de audio",
        any(
            any(start - 4 <= s.start <= end + 2 for start, end in BURSTS)
            for s in result.selected
        ),
        f"inicios: {[round(s.start, 1) for s in result.selected][:6]}",
    )

    if not result.selected:
        print("\nSin segmentos, no hay nada que renderizar.")
        return 1

    print("\nRenderizando...")
    output = render.render(result, cfg, on_progress=lambda i, n, name: print(f"  [{i}/{n}] {name}"))

    print("\nComprobaciones del render:")
    check("se escribieron los clips", len(output.clips) == len(result.selected))
    check("todos los clips tienen contenido", all(p.exists() and p.stat().st_size > 1000 for p in output.clips))
    check("se genero el reel", output.reel is not None and output.reel.exists())

    if output.reel and output.reel.exists():
        info = ffmpeg.video_info(output.reel)
        expected = sum(s.duration for s in result.selected)
        check("el reel tiene la altura configurada", info["height"] == cfg.reel_height, f"{info['height']}p")
        check(
            "el reel dura lo que suman los clips",
            abs(info["duration"] - expected) < 2.5,
            f"{info['duration']:.1f}s vs {expected:.1f}s esperados",
        )
        check("el reel tiene audio", any(s.get("codec_type") == "audio" for s in ffmpeg.streams(output.reel)))
        check("se escribieron las etiquetas", (result.reel_dir / render.LABELS_NAME).exists())
        print(f"\n  Reel: {output.reel}")

    print("\nFase 2: shorts...")
    # Sin telemetria no se sabe a que escala cae el puntaje por audio; bajar
    # el umbral a 0 aca prueba el camino de ffmpeg (recorte 9:16, fondo
    # desenfocado, concat, texto quemado), no la calibracion del umbral.
    # `shorts_min_seconds` tambien va a 0: los videos de prueba duran 30 s en
    # total, asi que sus segmentos nunca llegarian al piso real de 15 s y no
    # quedaria nada que renderizar. El piso se prueba en test_pipeline.py.
    cfg_shorts = cfg.merged({"shorts_min_score": 0.0, "shorts_min_seconds": 0.0})
    shorts = shorts_mod.select_shorts(result, cfg_shorts)
    check("se armo al menos un short", len(shorts) > 0, f"{len(shorts)}")

    if shorts:
        outputs = shorts_mod.render_shorts(
            result, shorts, cfg_shorts, use_nvenc=False,
            on_progress=lambda i, n, name: print(f"  [{i}/{n}] {name}"),
        )
        check("se escribio un archivo por short", len(outputs) == len(shorts))
        check(
            "todos los shorts tienen contenido",
            all(p.exists() and p.stat().st_size > 1000 for p in outputs),
        )
        if outputs:
            info = ffmpeg.video_info(outputs[0])
            check(
                "el short sale en 1080x1920",
                (info["width"], info["height"]) == (cfg.shorts_width, cfg.shorts_height),
                f"{info['width']}x{info['height']}",
            )
            check(
                "dura lo que suman sus clips",
                abs(info["duration"] - shorts[0].duration) < 2.5,
                f"{info['duration']:.1f}s vs {shorts[0].duration:.1f}s esperados",
            )
            check(
                "el short tiene audio",
                any(s.get("codec_type") == "audio" for s in ffmpeg.streams(outputs[0])),
            )
            check(
                "ningun short supera el tope de plataforma",
                all(s.duration <= cfg_shorts.shorts_max_seconds for s in shorts),
            )
        check(
            "se escribio el manifiesto shorts.csv",
            (result.shorts_dir / shorts_mod.MANIFEST_NAME).exists(),
        )
        check(
            "no quedan .ass sueltos (se borran solos tras cada render)",
            not list(result.shorts_dir.glob("*.ass")),
            f"{list(result.shorts_dir.glob('*.ass'))}",
        )
        print(f"\n  Shorts: {result.shorts_dir}")

    print("\n" + "=" * 46)
    if failures:
        print(f"{len(failures)} fallas:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("Todo OK. El camino de render funciona.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
