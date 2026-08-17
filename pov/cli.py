"""Command line interface. See `run.py --help`."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import config as config_mod
from . import cleanup, ffmpeg, ingest, matching, render, ride as ride_mod
from . import shorts as shorts_mod, shorts_guion as shorts_guion_mod

BAR_WIDTH = 26


# --------------------------------------------------------------------------
# console helpers
# --------------------------------------------------------------------------

def say(text: str = "") -> None:
    print(text, flush=True)


def title(text: str) -> None:
    say()
    say(text)
    say("-" * len(text))


def progress(done: int, total: int, label: str) -> None:
    filled = int(BAR_WIDTH * done / total) if total else 0
    bar = "#" * filled + "." * (BAR_WIDTH - filled)
    sys.stdout.write(f"\r  [{bar}] {done}/{total}  {label[:44]:<44}")
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def human_time(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def require_ffmpeg() -> None:
    if not ffmpeg.available():
        say()
        say("Falta ffmpeg. Instalalo con:")
        say("    winget install Gyan.FFmpeg")
        say("Cierra y abre la terminal despues de instalar.")
        raise SystemExit(1)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_nuevo(args, cfg) -> int:
    library = config_mod.library_path(cfg)
    library.mkdir(parents=True, exist_ok=True)
    root = ride_mod.create(library, args.nombre)
    say(f"Ride creado: {root}")
    say(f"Copia los .mp4 a: {root / 'raw'}")
    return 0


def cmd_ingesta(args, cfg) -> int:
    library = config_mod.library_path(cfg)
    library.mkdir(parents=True, exist_ok=True)

    if args.ride:
        root = ride_mod.resolve(library, args.ride)
    else:
        root = ride_mod.create(library, args.nombre)

    destination = root / "raw"
    title(f"Ingesta -> {root.name}")

    def on_file(index, total, name, size):
        progress(index, total, f"{name} ({human_size(size)})")

    source = Path(args.desde) if args.desde else None
    result = ingest.run(destination, from_folder=source, on_progress=on_file)

    say(f"  origen        : {result.source}")
    say(f"  copiados      : {len(result.copied)} archivos, {human_size(result.total_bytes)}")
    if result.skipped:
        say(f"  ya existian   : {len(result.skipped)}")
    say(f"  tiempo        : {human_time(result.seconds)}")
    say(f"  carpeta       : {destination}")

    if args.seguir:
        args.ride = str(root)
        return cmd_analizar(args, cfg) or cmd_reel(args, cfg)
    return 0


def cmd_analizar(args, cfg) -> int:
    require_ffmpeg()
    library = config_mod.library_path(cfg)
    root = ride_mod.resolve(library, args.ride)

    title(f"Analizando {root.name}")
    started = time.monotonic()

    def on_file(index, total, name):
        progress(index, total, name)

    result = ride_mod.analyse(root, cfg, on_progress=on_file)
    ride_mod.write_reports(result, cfg)

    with_telemetry = sum(1 for f in result.files if f.summary.get("accel_hz"))
    speeds = [f.summary.get("vel_max_kmh", 0) for f in result.files]
    airs = sum(len([e for e in s.events if e.kind == "air"]) for s in result.selected)
    crashes = sum(len([e for e in s.events if e.kind == "crash"]) for s in result.selected)

    say()
    say(f"  archivos          : {len(result.files)} ({with_telemetry} con telemetria)")
    say(f"  material bruto    : {result.total_raw_seconds() / 60:.1f} min")
    say(f"  candidatos totales: {sum(len(f.segments) for f in result.files)}")
    say(f"  seleccionados     : {len(result.selected)} segmentos, {result.total_selected_seconds() / 60:.1f} min")
    if any(speeds):
        say(f"  velocidad maxima  : {max(speeds):.0f} km/h")
    say(f"  saltos detectados : {airs}")
    if crashes:
        say(f"  caidas detectadas : {crashes}")
    say(f"  tiempo de analisis: {human_time(time.monotonic() - started)}")

    if result.ajustes.activos:
        say()
        say("  Ajustes manuales de este ride (ajustes.toml):")
        for line in result.ajustes.resumen():
            say(f"    {line}")

    say()
    say("  Apertura y cierre:")
    for note in result.bookend_notes:
        say(f"    {note}")

    say()
    say("  Top 5 momentos:")
    action = [s for s in result.selected if not s.role]
    for segment in sorted(action, key=lambda s: s.rank)[:5]:
        say(
            f"    {segment.rank:2d}. {segment.source.name} @ {segment.timecode()}"
            f"  {segment.headline():<20} {segment.score:5.1f} pts"
        )

    say()
    say(f"  Detalle : {result.analysis_file}")
    say(f"  Cortes  : {result.cutlist_file}")

    for source in result.files:
        if source.note:
            say(f"  aviso: {source.path.name}: {source.note}")

    if args.seguir:
        return cmd_reel(args, cfg)
    return 0


def cmd_reel(args, cfg) -> int:
    require_ffmpeg()
    library = config_mod.library_path(cfg)
    root = ride_mod.resolve(library, args.ride)

    if not (root / "analysis.json").exists():
        say("No hay analisis todavia; lo corro primero.")
        analysis_args = argparse.Namespace(**{**vars(args), "seguir": False})
        cmd_analizar(analysis_args, cfg)

    title(f"Renderizando {root.name}")
    result = ride_mod.analyse(root, cfg)

    if not result.selected:
        say("  No hay segmentos que renderizar.")
        return 1

    if cfg.use_nvenc and ffmpeg.has_nvenc():
        say("  encoder: NVENC (GPU)")
    else:
        reason = "desactivado en config.toml" if not cfg.use_nvenc else ffmpeg.nvenc_problem()
        say(f"  encoder: x264 (CPU, preset {cfg.x264_preset})")
        say(f"           sin GPU porque: {reason}")
    started = time.monotonic()

    def on_clip(index, total, name):
        progress(index, total, name)

    output = render.render(result, cfg, on_progress=on_clip)

    say()
    say(f"  clips limpios : {len(output.clips)} en {result.clips_dir}")
    if output.reel:
        say(f"  reel revision : {output.reel}")
    say(f"  tiempo        : {human_time(time.monotonic() - started)}")
    say()
    say("  Siguiente paso: mira el reel y anota los numeros que sobren.")
    say("  Si ya esta bien asi:  python run.py completo")
    return 0


def cmd_shorts(args, cfg) -> int:
    """Fase 2: armar los shorts verticales listos para subir."""
    require_ffmpeg()
    library = config_mod.library_path(cfg)
    root = ride_mod.resolve(library, args.ride)

    if not (root / "analysis.json").exists():
        say("No hay analisis todavia; lo corro primero.")
        analysis_args = argparse.Namespace(**{**vars(args), "seguir": False})
        cmd_analizar(analysis_args, cfg)

    title(f"Shorts de {root.name}")
    result = ride_mod.analyse(root, cfg)

    def on_discard(members, total: float) -> None:
        cuales = ", ".join(f"{c.source.stem}@{c.start:.0f}s" for c in members)
        say(f"  descarto {total:.0f}s ({cuales}): no llega a shorts_min_seconds "
            f"({cfg.shorts_min_seconds:.0f}s).")

    shorts = shorts_mod.select_shorts(result, cfg, on_discard=on_discard)

    min_score = result.ajustes.shorts_min_score or cfg.shorts_min_score
    if result.ajustes.shorts_min_score:
        say(f"  piso de puntaje: {min_score:.0f} pts (fijado en ajustes.toml de este ride)")

    if not shorts:
        say(f"  Ningun short quedo en pie: nada paso el piso de puntaje "
            f"({min_score:.0f} pts) o todo quedo bajo shorts_min_seconds "
            f"({cfg.shorts_min_seconds:.0f}s).")
        mejor = max((s.score for s in result.selected if not s.role), default=0.0)
        say(f"  El mejor momento del ride puntuo {mejor:.0f}.")
        if mejor < min_score:
            say("  Si este ride viene de un MP4 ya editado no trae telemetria, y ahi el")
            say("  techo de puntaje es 65, no 100: fija shorts_min_score en su ajustes.toml.")
        return 1

    plan_path = shorts_guion_mod.write_plan(result, shorts)
    warnings = shorts_mod.apply_guion(result, shorts)
    for warning in warnings:
        say(f"  aviso: {warning}")

    if cfg.use_nvenc and ffmpeg.has_nvenc():
        say("  encoder: NVENC (GPU)")
    else:
        reason = "desactivado en config.toml" if not cfg.use_nvenc else ffmpeg.nvenc_problem()
        say(f"  encoder: x264 (CPU, preset {cfg.x264_preset})")
        say(f"           sin GPU porque: {reason}")
    started = time.monotonic()

    def on_clip(index, total, name):
        progress(index, total, name)

    outputs = shorts_mod.render_shorts(
        result, shorts, cfg, cfg.use_nvenc and ffmpeg.has_nvenc(), on_progress=on_clip
    )

    say()
    say(f"  shorts        : {len(outputs)} en {result.shorts_dir}")
    say(f"  manifiesto    : {result.shorts_dir / shorts_mod.MANIFEST_NAME}")
    say(f"  plan (timing) : {plan_path}")
    say(f"  tiempo        : {human_time(time.monotonic() - started)}")
    say()
    for short in shorts:
        clips = ", ".join(f"{c.source.stem}@{c.start:.0f}s" for c in short.clips)
        fuente = "guion" if short.guion else "texto automatico"
        say(f"  #{short.order} ({short.duration:.0f}s, {short.score:.0f} pts, {fuente}) {clips}")
        for t, text in sorted(short.lines, key=lambda i: i[0]):
            say(f"      [{t:5.1f}s] {text}")
    return 0


def cmd_completo(args, cfg) -> int:
    """Pegar los clips ya renderizados en un solo archivo, sin etiquetas."""
    require_ffmpeg()
    library = config_mod.library_path(cfg)
    root = ride_mod.resolve(library, args.ride)
    result = ride_mod.analyse(root, cfg)

    pairs = [
        (result.clips_dir / render.clip_filename(order, segment), segment)
        for order, segment in enumerate(result.selected, start=1)
    ]
    clips = [path for path, _ in pairs]
    missing = [path for path, _ in pairs if not path.exists()]
    if missing:
        say(f"Faltan {len(missing)} clips. Corre primero: python run.py reel")
        return 1

    # Pegar es copiar bytes, asi que lo que este mal en la carpeta sale tal cual
    # en el video final y sin avisar. Si un clip no dura lo que dice el analisis
    # -- render interrumpido, o un parametro que cambio despues de renderizar --
    # mejor pararse aqui que subir un video con un corte viejo dentro.
    stale = [path for path, segment in pairs if not render.clip_is_current(path, segment)]
    if stale:
        say(f"Hay {len(stale)} clips que no coinciden con el analisis de ahora:")
        for path in stale[:5]:
            say(f"  {path.name}")
        if len(stale) > 5:
            say(f"  ... y {len(stale) - 5} mas")
        say("Vuelve a renderizarlos primero: python run.py reel")
        return 1

    title(f"Pegando {len(clips)} clips de {root.name}")
    say("  sin recomprimir: resolucion nativa, sin etiquetas")
    started = time.monotonic()
    out = render.render_master(result, clips)
    if out is None:
        say("  No hay clips que pegar.")
        return 1

    say(f"  archivo : {out}")
    say(f"  tamano  : {human_size(out.stat().st_size)}")
    say(f"  tiempo  : {human_time(time.monotonic() - started)}")
    return 0


def cmd_comparar(args, cfg) -> int:
    """Check the detector against a video you actually edited yourself."""
    require_ffmpeg()
    library = config_mod.library_path(cfg)
    root = ride_mod.resolve(library, args.ride)

    edited = Path(args.editado)
    if not edited.is_file():
        say(f"No encontre el video editado: {edited}")
        return 1

    raws = ride_mod.source_files(root)
    if not raws:
        say(f"No hay archivos crudos en {root}.")
        return 1

    title(f"Comparando {edited.name} contra {root.name}")
    say(f"  Buscando en {len(raws)} archivos crudos que trozos sobrevivieron al editado.")
    say("  (la primera vez tarda: hay que decodificar todo el material)")
    say()

    cache = root / ".huellas"
    kept = matching.find_kept(edited, raws, cache_dir=cache)

    if not kept:
        say("  No encontre nada de este ride dentro del editado.")
        say("  Puede que el editado venga de otros archivos, o que tenga zoom")
        say("  o recortes que rompen el emparejamiento visual.")
        return 0

    say(f"  Trozos que conservaste ({sum(k.duration for k in kept):.1f}s en total):")
    for item in kept:
        say(
            f"    {item.source.name} {item.start:7.1f}-{item.end:6.1f}s "
            f"({item.duration:4.1f}s)  -> editado {item.edit_time:6.1f}s  "
            f"conf={item.confidence:.3f}"
        )

    result = ride_mod.analyse(root, cfg)
    report = matching.compare(kept, result.selected)

    say()
    say("  Por archivo:")
    say(f"    {'archivo':<18} {'conservaste':>12} {'yo propuse':>12} {'acerte':>9} {'perdi':>8}")
    for name, row in report.per_file.items():
        say(
            f"    {name:<18} {row['conservado_s']:>11.1f}s {row['propuesto_s']:>11.1f}s "
            f"{row['acertado_s']:>8.1f}s {row['perdido_s']:>7.1f}s"
        )

    say()
    say(f"  Precision : {report.precision * 100:5.1f}%  "
        f"(de lo que propongo, cuanto querias de verdad)")
    say(f"  Cobertura : {report.recall * 100:5.1f}%  "
        f"(de lo que querias, cuanto alcance a proponer)")

    return 0


def cmd_limpiar(args, cfg) -> int:
    """Free space from rides you have already finished editing."""
    library = config_mod.library_path(cfg)

    if args.todos:
        roots = sorted((p for p in library.iterdir() if p.is_dir()), reverse=True)
    else:
        roots = [ride_mod.resolve(library, args.ride)]
    if not roots:
        say("No hay rides que limpiar.")
        return 0

    plan = [
        (
            root,
            cleanup.survey(
                root, include_raw=args.incluir_raw, include_final=args.incluir_final
            ),
        )
        for root in roots
    ]
    plan = [(root, items) for root, items in plan if items]
    if not plan:
        say("No hay nada que borrar: estos rides ya estan limpios.")
        return 0

    total = sum(item.size for _, items in plan for item in items)
    risky = [item for _, items in plan for item in items if not item.regenerable]

    title("Limpiar" + (" (TODOS los rides)" if args.todos else ""))
    for root, items in plan:
        say(f"  {root.name}")
        for item in items:
            mark = "  " if item.regenerable else "!!"
            say(
                f"   {mark} {item.label:<22} {item.files:>4} arch  "
                f"{human_size(item.size):>10}   {item.note}"
            )
    say()
    say(f"  Se liberan: {human_size(total)}")

    if risky:
        say()
        say("  ATENCION: vas a borrar los archivos ORIGINALES.")
        say("  Si ya formateaste la SD de la camara, esta es la unica copia")
        say("  que existe de ese material y no hay forma de recuperarlo.")

    if not args.si:
        say()
        expected = "BORRAR ORIGINALES" if risky else "si"
        try:
            answer = input(f"  Escribe '{expected}' para continuar: ").strip()
        except EOFError:
            # Sin a quien preguntar (una tuberia, una tarea programada) la unica
            # respuesta segura es no borrar. Para eso esta --si.
            say()
            say("  Sin confirmacion posible aqui. Si es a proposito, usa --si.")
            return 0
        if answer != expected:
            say("  Cancelado, no se borro nada.")
            return 0

    freed = sum(cleanup.remove(items) for _, items in plan)
    say()
    say(f"  Liberados {human_size(freed)}.")
    if not args.incluir_raw:
        say("  Los originales y el analisis siguen intactos.")
    if not args.incluir_final:
        say("  El video final y los shorts tampoco se tocan.")
    return 0


def cmd_listar(args, cfg) -> int:
    library = config_mod.library_path(cfg)
    if not library.is_dir():
        say(f"Todavia no existe {library}.")
        return 0

    rides = sorted((p for p in library.iterdir() if p.is_dir()), reverse=True)
    if not rides:
        say("No hay rides todavia.")
        return 0

    title(f"Rides en {library}")
    for root in rides:
        raw = len(list((root / "raw").glob("*.mp4"))) if (root / "raw").is_dir() else 0
        clips = len(list((root / "clips").glob("*.mp4"))) if (root / "clips").is_dir() else 0
        analysed = "si" if (root / "analysis.json").exists() else "no"
        say(f"  {root.name:<28} raw:{raw:<4} clips:{clips:<4} analizado:{analysed}")
    return 0


def cmd_revisar(args, cfg) -> int:
    """Print the cut list of an already-analysed ride."""
    library = config_mod.library_path(cfg)
    root = ride_mod.resolve(library, args.ride)
    cutlist = root / "cortes.csv"
    if not cutlist.exists():
        say(f"No hay cortes.csv en {root}. Corre primero: run.py analizar")
        return 1

    title(f"Cortes de {root.name}")
    for line in cutlist.read_text(encoding="utf-8-sig").splitlines():
        say("  " + line.replace(";", " | "))
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Pipeline de edicion para videos POV de MTB grabados con GoPro.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Flujo tipico despues de un ride:\n"
            "  python run.py ingesta --nombre \"nombre del trail\" --seguir\n"
            "  python run.py completo          # el video largo que subes\n"
            "  python run.py shorts            # los shorts 9:16, ya despues de aprobar el largo\n"
            "\n"
            "O paso a paso:\n"
            "  python run.py nuevo --nombre \"cerro-san-cristobal\"\n"
            "  python run.py ingesta\n"
            "  python run.py analizar\n"
            "  python run.py reel\n"
            "  python run.py completo\n"
            "  python run.py shorts\n"
        ),
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    new = sub.add_parser("nuevo", help="crear una carpeta de ride vacia")
    new.add_argument("--nombre", help="nombre del trail")
    new.set_defaults(func=cmd_nuevo)

    ing = sub.add_parser("ingesta", help="copiar los archivos de la GoPro")
    ing.add_argument("--ride", help="ride destino (por defecto crea uno nuevo)")
    ing.add_argument("--nombre", help="nombre del trail para el ride nuevo")
    ing.add_argument("--desde", help="copiar desde esta carpeta en vez de buscar la camara")
    ing.add_argument("--seguir", action="store_true", help="analizar y renderizar al terminar")
    ing.set_defaults(func=cmd_ingesta)

    ana = sub.add_parser("analizar", help="detectar la accion y elegir los segmentos")
    ana.add_argument("ride", nargs="?", help="nombre o ruta del ride (por defecto el mas reciente)")
    ana.add_argument("--seguir", action="store_true", help="renderizar al terminar")
    ana.set_defaults(func=cmd_analizar)

    ree = sub.add_parser("reel", help="renderizar los clips limpios y el reel de revision")
    ree.add_argument("ride", nargs="?", help="nombre o ruta del ride")
    ree.set_defaults(func=cmd_reel, seguir=False)

    sho = sub.add_parser(
        "shorts", help="armar los shorts verticales 9:16, listos para subir"
    )
    sho.add_argument("ride", nargs="?", help="nombre o ruta del ride")
    sho.set_defaults(func=cmd_shorts)

    com = sub.add_parser(
        "completo",
        help="pegar los clips en un solo video, sin etiquetas y sin recomprimir",
    )
    com.add_argument("ride", nargs="?", help="nombre o ruta del ride")
    com.set_defaults(func=cmd_completo)

    cmp_ = sub.add_parser(
        "comparar",
        help="medir el detector contra un video que ya editaste tu",
    )
    cmp_.add_argument("editado", help="ruta al video ya editado (el exportado de CapCut)")
    cmp_.add_argument("ride", nargs="?", help="ride con los archivos crudos de ese editado")
    cmp_.set_defaults(func=cmd_comparar)

    lim = sub.add_parser(
        "limpiar",
        help="liberar espacio de un ride que ya editaste",
    )
    lim.add_argument("ride", nargs="?", help="nombre o ruta del ride")
    lim.add_argument("--todos", action="store_true", help="limpiar todos los rides")
    lim.add_argument(
        "--incluir-raw",
        dest="incluir_raw",
        action="store_true",
        help="borrar tambien los archivos originales (NO se pueden recuperar)",
    )
    lim.add_argument(
        "--incluir-final",
        dest="incluir_final",
        action="store_true",
        help="borrar tambien el video final y los shorts (se regeneran con: "
             "run.py completo / run.py shorts)",
    )
    lim.add_argument("--si", action="store_true", help="no preguntar antes de borrar")
    lim.set_defaults(func=cmd_limpiar)

    lst = sub.add_parser("listar", help="ver todos los rides")
    lst.set_defaults(func=cmd_listar)

    rev = sub.add_parser("revisar", help="mostrar la lista de cortes de un ride")
    rev.add_argument("ride", nargs="?", help="nombre o ruta del ride")
    rev.set_defaults(func=cmd_revisar)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = config_mod.load()

    try:
        return args.func(args, cfg) or 0
    except KeyboardInterrupt:
        say("\nCancelado.")
        return 130
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        say()
        say(f"Error: {exc}")
        return 1
