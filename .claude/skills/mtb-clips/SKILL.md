---
name: mtb-clips
description: Arma los shorts verticales 9:16 de un ride de MTB que ya paso por MTB Editor (Fase 1), listos para subir a YouTube/TikTok/Instagram. Usalo cuando Chris diga "mtb clips", pida "los shorts" de un ride, pida que le "actualices los textos" de los shorts, o pregunte por el alcance/formato de los shorts. NO lo uses si el ride todavia no tiene un video largo aprobado por Chris -- para eso esta el skill mtb-editor, separado a proposito.
---

# MTB Clips

Fase 2 del pipeline en `C:\Users\chris\Documents\pov automation`. Convierte los
mejores momentos de un ride (ya detectados por Fase 1) en varios shorts 9:16
armados de punta a punta -- recorte, texto, todo -- listos para subir. A
diferencia del video largo, aca **no hay paso manual en CapCut**: Chris lo
pidio asi el 17-ago-2026 al ver que el pipeline ya podia hacerlo completo.

**Vive separado de `mtb-editor` a proposito** (pedido explicito de Chris, para
no mezclar skills). Comparten el motor de deteccion (`pov/*.py`), pero el
codigo de shorts es aditivo: `pov/shorts.py`, `pov/shorts_guion.py`,
`pov/shorts_textos.py`, un subcomando mas en `cli.py`. No se toca nada de
`mtb-editor/SKILL.md`.

## Orden de trabajo: Fase 1 primero, siempre

**No corras `run.py shorts` sobre un ride cuya Fase 1 (el video largo) Chris
todavia no aprobo.** Los shorts salen exactamente de los mismos segmentos que
el video largo (`ride.selected`, filtrados por puntaje); si la seleccion de
Fase 1 no le gusto, los shorts van a arrastrar el mismo problema. El flujo
real es: ride nuevo -> `mtb-editor` entrega el reel y el video largo -> Chris
dice que esta bien -> recien ahi este skill.

## El comando

```bash
python run.py shorts <ride>
```

Requiere que el ride ya tenga `analysis.json` (lo corre solo si falta). Saca
todo de `ride.selected`: ningun archivo ni parametro de deteccion se toca de
nuevo. Salida en `rides/<ride>/shorts/`:

- un `.mp4` por short (1080x1920, calidad `clip_quality` -- es el entregable,
  no material de revision)
- `shorts.csv`: orden, clips de origen, duracion, puntaje del climax, si el
  texto viene del guion o del respaldo automatico, y el texto linea por
  linea. Revisalo antes de programar la subida en Fase 3.
- `shorts_plan.json`: el timing exacto de cada clip y cada evento dentro de
  la linea de tiempo del short. **Es el insumo para escribir el guion**, ver
  abajo -- no hace falta mostrarselo a Chris.

Se corre **dos veces** la primera vez que un ride pasa por aca: la primera
sin guion (texto de respaldo, generico) para generar `shorts_plan.json`; la
segunda, despues de escribir `shorts_guion.toml` a mano (ver "Como escribir
el guion"), para quemar el texto de verdad. Rides ya guionados solo
necesitan una corrida.

## Como arma cada short

1. Filtra `ride.selected` por `shorts_min_score` (config.toml, seccion
   `[shorts]`). Todo lo que pase el umbral entra, sin techo fijo de cantidad
   -- decision de Chris del 17-ago-2026.
2. Los agrupa de 2 en 2 o 3 en 3 (`shorts_max_clips`), sumando duracion hasta
   `shorts_max_seconds` (59 s, tope de plataforma). Un remanente de un solo
   clip sigue siendo un short valido.
3. Dentro de cada grupo, ordena por **puntaje descendente**: el mas fuerte
   abre como climax, el resto sigue despues. A proposito distinto del video
   largo, que preserva orden cronologico -- el short se juega la retencion
   en los primeros 2-3 segundos, y si el golpe grande queda para el final la
   mayoria ya se fue antes de verlo (correccion de Chris el 17-ago-2026,
   tras ver que el orden ascendente original dejaba la mejor accion al
   final en Halpatiokee y JD Park).
4. Quema las lineas de texto del guion (`shorts_guion.toml`) si existen y su
   ancla sigue vigente; si no, cae al respaldo automatico
   (`pov/shorts_textos.py`). Ver "Texto quemado" abajo -- es la parte que mas
   cuidado necesita.

## El encuadre 9:16 (no lo reinventes)

Probado visualmente con Chris el 17-ago-2026 sobre tres variantes reales de
Halpatiokee antes de decidir, asi que **no asumas recorte central puro para
este tipo de contenido**: pierde sensacion de velocidad, los laterales
importan en MTB. Gano un intermedio: `shorts_crop_width_ratio = 0.5` (se
conserva el 50% del ancho original, centrado, altura intacta) sobre un fondo
desenfocado del cuadro completo que rellena los margenes -- y esos margenes
son donde cae el texto. Es ajustable en `config.toml` si en mas material hace
falta abrir o cerrar el encuadre, pero el punto de partida ya esta validado
por su ojo; no lo cambies sin que lo pida.

## Texto quemado

**En ingles**, no en español (Chris vive en EE.UU. y ya editaba sus shorts en
ingles a mano buscando mas alcance -- ver memoria del proyecto). Es solo el
texto que aparece en el video: el codigo, `config.toml` y la consola del
pipeline siguen en español como el resto del proyecto.

**El texto de verdad lo escribis vos, no una formula** (decision de Chris el
17-ago-2026, al ver que una biblioteca de plantillas genericas sonaba
"basico" y queria conectar mas con la audiencia). `pov/shorts_textos.py` con
sus plantillas por categoria sigue existiendo, pero **solo como respaldo**
para cuando todavia no escribiste el guion de un ride -- no es la version
buena.

### Como escribir el guion

1. Corre `run.py shorts` una vez (produce texto de respaldo, generico, y
   escribe `rides/<ride>/shorts/shorts_plan.json`).
2. Lee ese plan: trae, para cada short, cada clip con su offset dentro de la
   linea de tiempo ya pegada, y cada evento (impacto/aire/caida) con su
   offset exacto. Ahi es donde esta el "cuando pasa cada cosa" que necesitas
   para sincronizar el texto.
3. Escribi `rides/<ride>/shorts_guion.toml` a mano, siguiendo **exactamente**
   las reglas de tono de abajo, con lineas ancladas a esos segundos:

   ```toml
   [[shorts]]
   order = 1
   climax = "GX011147@172.9"   # del climax_anchor del plan -- ver "El ancla"
   alt_hook = "watch what the roots do here"
   lineas = [
     { t = 0.0,  texto = "this section always gets me" },
     { t = 11.3, texto = "yep, there it is" },
     { t = 24.7, texto = "okay THAT one I felt" },
     { t = 29.3, texto = "how bad did that look from there?" },
   ]
   ```

4. Corre `run.py shorts` de nuevo. Si el ancla coincide, quema esas lineas
   (el nombre de archivo cambia solo -- lleva un hash del texto -- asi que
   el render viejo con texto de respaldo no se confunde con el nuevo).

### El ancla (`climax`), y por que no es opcional

Cada entrada del guion se ata a un clip concreto (`archivo@segundo`, el
`climax_anchor` que trae el plan). `apply_guion` en `pov/shorts.py` valida
esa ancla antes de quemar nada: si el agrupado cambio -- tocaste
`shorts_min_score`, reanalizaste con otro ajuste -- y el short #2 de hoy ya
no es el mismo clip que cuando escribiste el guion, la entrada se ignora y
`run.py shorts` avisa en consola en vez de pegar texto en el momento
equivocado. Es la misma logica de fondo que `render.clip_is_current` en Fase
1 (ver memoria: `errores-de-diseno-corregidos`). Si ves ese aviso, hay que
revisar y reescribir esa entrada del guion.

### Las reglas de tono (dadas por Chris el 17-ago-2026, no resumir de mas)

- **Nivel intermedio, nunca "pro".** Cero "full send", "sending it",
  "hucking" -- un rider de verdad lo detecta como falso. La honestidad sobre
  el nivel es la ventaja: la mayoria de la audiencia tampoco es pro.
- **Narracion de companero.** Primera persona, como si le hablaras a un
  amigo al lado. Calido, real, sin guion de anuncio.
- **Pensamiento en vivo, fragmentos.** 3-6 palabras por linea la mayoria de
  las veces, nunca una oracion completa de descripcion.
- **Arco por short: setup -> tension/sorpresa -> resolucion.** La emocion
  tiene que coincidir con lo que pasa de verdad en el clip -- si hay
  sorpresa, sorpresa; si hay susto, susto; si es un tramo rapido, que la
  linea lo diga con un dato real (velocidad, no un adjetivo generico).
- **El hook (primera linea) crea tension o pregunta, nunca es neutro.**
  "this section always gets me" si, "riding some trails today" no.
- **2 a 4 lineas por short, maximo 5.** El texto respira: silencio entre
  lineas para que se oiga el sendero. Nunca antes del momento -- mata la
  sorpresa; sincronizar en el acercamiento, en el momento, o en la reaccion
  de despues.
- **Maximo 1 emoji por short**, y solo si suma emocion real. Por defecto, sin
  emoji.
- **El cierre invita a comentar**, de forma natural: una pregunta directa
  sobre lo que se vio, no un CTA generico tipo "follow for more".
- **`alt_hook`**: una alternativa de gancho distinta a la primera linea, para
  que Chris pueda hacer A/B testing. Se guarda en `shorts.csv` pero no se
  quema en el video.

No hay llamada a ningun modelo dentro de `run.py shorts` -- el pipeline
tiene que poder correr offline y dar el mismo resultado dos veces sobre el
mismo guion. Escribir el guion es un paso tuyo, en la conversacion, no algo
que el script haga solo.

### Si Chris pide "actualiza los textos" o "que este en tendencia"

Dos cosas distintas segun lo que pida:

- **Un ride concreto suena mal o repetitivo**: reescribi ese
  `shorts_guion.toml`, mismas reglas de arriba.
- **Quiere refrescar el respaldo automatico** (`pov/shorts_textos.py`, el que
  se usa solo cuando todavia no hay guion): investiga que esta funcionando en
  shorts de MTB/outdoor y reescribi las plantillas a mano, con el mismo tono
  de esta seccion (nada de "full send" ahi tampoco). Corre
  `python tests/test_pipeline.py` despues (secciones `[19]` y `[20]`) para no
  romper el determinismo.

## Nombre del trail

`ride.ajustes.nombre_trail` (en `ajustes.toml` del ride) es el nombre que
aparece en los textos genericos ("POV: {trail}", etc). Sin el, se usa una
version title-case del nombre de la carpeta, que queda mal con siglas
("Halpatiokee Mtb Trail" en vez de "Halpatiokee MTB Trail"). Si Chris entrega
un ride nuevo, agregale `nombre_trail = "..."` bien escrito antes de correr
`shorts`.

## Duracion: piso, techo y punto comodo

`shorts_min_seconds = 15` / `shorts_max_seconds = 40` (Chris, 17-ago-2026,
viendo los primeros lotes). Lo que el considera comodo esta **entre 20 y 30
segundos**. Los grupos que no llegan al piso se descartan y se avisa en
consola -- pasaba con sobrantes de un solo clip (el short #7 de JD Park
duraba 7 s). Los dos numeros son provisionales: hay que contrastarlos con la
retencion real de YouTube/TikTok/Reels cuando haya metricas.

## Rides sin telemetria (MP4 ya editados)

Un MP4 exportado de un editor **perdio el GPMF**, asi que no hay acelerometro
ni GPS y el motor cae al respaldo por audio. Eso cambia la escala del
puntaje: con sensores un golpe llega a 100, pero sin ellos el techo teorico
es `base_gain * 100 = 65`, y en la practica los mejores momentos rondan
40-55. Con el global en 55 casi nada califica, **y no es que el ride sea
flojo: es otra escala**.

Para eso esta `shorts_min_score` en el `ajustes.toml` **del ride** (0 = usar
el global). Empeza en ~30 para un ride importado y ajusta mirando el
resultado. `run.py shorts` avisa con el mejor puntaje del ride cuando no
sale nada, justo para diagnosticar esto.

**Y ojo con el presupuesto, que es el cuello de botella de verdad.** Medido el
17-ago-2026 sobre los dos rides de julio: con `shorts_min_score = 30` seguian
saliendo *un solo* short por ride. El culpable no era el piso de puntaje sino
`reel_budget` -- 15% del material, con piso de 30 s -- que esta pensado para un
ride crudo de 45 min lleno de pedaleo. En un MP4 ya editado **el recorte duro
ya lo hizo Chris**, asi que quedarse con 30 s de 2:17 tira a la basura la mitad
de lo bueno. Poné `reel_segundos` = el largo completo del archivo en el
`ajustes.toml` del ride y deja que mande el umbral. Con eso los dos rides
pasaron de 1 a 2 shorts cada uno.

**Y el plan no te va a servir para el guion.** Sin GPMF, `shorts_plan.json`
sale con `events: []` y `peak_speed_kmh: 0` en todos los clips: te dice donde
cortan, no que pasa dentro. Escribir el guion desde ahi seria inventar. Lo que
funciono: renderizar una vez con el texto de respaldo y sacar una hoja de
contactos del short ya armado, que ademas te da los segundos en el mismo eje
que usa el guion:

```bash
ffmpeg -v error -y -i <short>.mp4 \
  -vf "fps=1/2,scale=270:480,tile=4x5:padding=6:color=white" \
  -frames:v 1 /tmp/hoja.png
```

Cada casilla es un frame cada 2 s, leyendo por filas desde t=0. (Nada de
`drawtext` para rotular: fontconfig no esta configurado en este equipo y
ffmpeg se cae con segfault.)

**Y el archivo trae sus propios cortes.** Un editado de Chris cambia de plano
cada 2-6 s. El detector de accion no los ve -- puntua por audio, no por imagen
-- asi que los bordes de un tramo elegido caen encima de ellos y sobrevive una
migaja: un plano de medio segundo que aparece y se va. Chris cazo tres (0.53,
0.33 y 0.50 s) en el short #1 del 19-jul. Lo resuelve `pov/escenas.py`, que
corre solo sobre archivos sin telemetria y **mueve el borde hasta el corte**
en vez de acortar a ciegas. No hay nada que configurar por ride, pero si lees
en consola `NO ajuste ...` o `descarto ... entre los cortes`, mira ese tramo
antes de darlo por bueno: el detector confunde sol entre las palmas y motion
blur con cortes, y por eso existe `shorts_snap_max_recorte`.

### El reparto en grupos: el huerfano

El agrupado es goloso -- llena el primer short hasta el tope y deja lo que
sobra en el ultimo -- asi que tiende a terminar con un sobrante demasiado
corto que se tira entero. Cuando eso pasa, `_balanced_groups` busca el reparto
contiguo que saque **mas shorts validos** de los mismos clips: en el 26-jul,
3+3+1 tiraba 6.9 s mientras que 2+2+3 daba tres shorts de 28, 19 y 18 s
(pedido de Chris: "crear un short mas con los clips que califican").

Solo entra si el goloso dejo algo fuera, a proposito: un ride que ya reparte
bien no se toca, para no mover shorts que Chris ya aprobo. Si el material no
da para que todos lleguen al piso, no se fuerza nada -- manda el goloso y lo
que sobra se descarta con aviso.

Si aun asi un sobrante se queda a un pelo del piso y vale la pena, esta
`shorts_min_seconds` en el `ajustes.toml` **del ride** (0 = usar el global).

## Calibracion pendiente

`shorts_min_score = 55.0` esta **sin calibrar contra su ojo**: es el valor
que en Halpatiokee da los 5 mejores momentos del ride, no un numero validado.
Cuando Chris vea el primer lote real de shorts, pedile feedback igual que con
el reel de Fase 1 (numero de short + motivo) y ajusta desde ahi:

- salen muy pocos shorts, o le faltan buenos momentos -> baja `shorts_min_score`
- salen shorts flojos -> subelo
- un short concreto no le gusta pero el puntaje esta bien -> **no hay
  mecanismo de descarte manual todavia** (a diferencia de `descartar` en el
  reel de Fase 1); si lo pide, es la primera extension logica de
  `ajustes.toml` para este skill.

## Notas tecnicas heredadas de Fase 1

- **ffmpeg no esta en el PATH** de la herramienta Bash: exportalo al empezar.
  ```bash
  export PATH="$PATH:/c/Users/chris/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-9.0-full_build/bin"
  ```
- **Render en CPU**: el driver NVIDIA de este equipo no llega a la API NVENC
  que pide ffmpeg 9.0, asi que cae a x264. Un ride con varios shorts puede
  tardar unos minutos -- si es mucho, lanza `run.py shorts` con
  `run_in_background: true`.
- Los shorts se reutilizan si ya existen y coinciden en duracion con el
  analisis actual (`render.clip_is_current`, mismo mecanismo que los clips de
  Fase 1): reanalizar no obliga a rehacer todo desde cero.
- **Ojo con esa reutilizacion**: la comprobacion mira duracion y huella del
  texto, no los parametros del encoder. Si cambias `shorts_max_mbps`,
  `clip_quality` o el filtro de encuadre, los archivos viejos **no** se
  rehacen solos -- borra `rides/<ride>/shorts/*.mp4` para forzarlo.
- `run.py limpiar` no barre `shorts/` por defecto: son entregables, como
  `final/`. Van detras de `--incluir-final`.

## Texto quemado: largo de linea

El `.ass` va con `WrapStyle: 0` para que libass parta las lineas largas. Con
`WrapStyle: 2` (lo que usa el reel de Fase 1, donde las etiquetas son cortas)
no ajusta nada y el texto se sale del cuadro: "how many g's do you think that
first one was?" se veia cortada por los dos lados en el short #2 de JD Park.
Aun asi, las reglas de tono piden fragmentos de 3-6 palabras -- una linea que
necesita dos renglones para entrar probablemente esta de mas.
