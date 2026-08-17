---
name: mtb-editor
description: Procesa un ride nuevo de MTB grabado con GoPro y devuelve el video listo para subir, mas un reel etiquetado para revisarlo y los clips sueltos. Usalo siempre que Chris diga "mtb editor", o cuando entregue archivos nuevos de la GoPro, mencione que grabo un ride, hable de procesar/analizar material nuevo, pida "el reel" o "el video" de un ride, o pregunte que quedo de una grabacion — aunque no nombre la herramienta. Tambien cuando pida ajustes sobre un reel que acaba de ver, o cuando entregue un video ya editado por el para recalibrar el detector.
---

# MTB Editor

Convierte 40-60 min de material crudo de GoPro en **el video que sube**, mas un
reel etiquetado para revisarlo antes. El pipeline vive en
`C:\Users\chris\Documents\pov automation`.

## Que entrega, y en que orden

1. `reel/reel_candidatos.mp4` — todo junto en 1080p con etiquetas quemadas.
   **Esto es lo unico que tiene que mirar.** Existe para que descarte, asi que
   lleva a proposito mas material del que va a quedar.
2. `clips/` — los mismos cortes sueltos, resolucion nativa, sin overlays.
3. `final/video_completo.mp4` — los clips pegados sin recomprimir, con
   `run.py completo`. **Este es el entregable.**

El flujo real es: renderizas el reel, el lo mira, te dice que sobra, lo anotas
en `ajustes.toml`, reanalizas, y recien ahi corres `completo`.

## Lo que cambio y hay que tener presente

Al principio Chris cortaba el master el mismo en CapCut y esto solo proponia
candidatos. **Eso ya no es asi** (16-ago-2026): la seleccion le quedo lo
bastante buena como para publicar directo, y como sus videos van con **audio
crudo del trail y nunca musica**, no hay nada que un editor tenga que aportar.
No le propongas pasar por CapCut, ni musica, ni corte a tiempo con el beat.

Lo que sigue siendo decision suya es **que sobra**: el detector sabe donde hubo
accion, no sabe que tres curvas seguidas se ven iguales en camara.

## El flujo

Todo en un comando cuando la camara esta conectada por USB:

```bash
python run.py ingesta --nombre "<nombre del trail>" --seguir
```

Si los archivos ya estan en una carpeta del disco:

```bash
python run.py ingesta --desde "<ruta>" --nombre "<nombre del trail>" --seguir
```

`--seguir` encadena analisis y render. Paso a paso, si algo falla:
`nuevo` → `ingesta` → `analizar` → `reel` → `completo`.

Si no te dio el nombre del trail, preguntaselo antes de empezar: es lo unico que
no puedes deducir, y el nombre queda en la carpeta del ride para siempre.

### La copia por USB (MTP) miente de dos formas

Costo un ride descubrirlas, asi que no las vuelvas a descubrir tu:

1. **Los nombres vienen sin extension.** Sobre MTP el shell de Windows entrega
   el nombre *para mostrar*, y los cuatro archivos que la GoPro genera por
   grabacion (`.MP4`, `.WAV`, `.LRV`, `.THM`) comparten nombre visible. El
   filtro se hace con `System.FileName`, que trae el nombre real. Ya esta
   arreglado en `scripts/gopro_mtp.ps1`.
2. **`CopyHere` puede reservar el tamano final antes de escribir los datos.**
   O sea que "el archivo ya pesa lo que debe" no significa que este completo.

Por eso, **despues de la ingesta, verifica siempre**: compara el numero de
archivos y el tamano de cada uno contra lo que reporta la camara.

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gopro_mtp.ps1 -List
```

Y si algo no cuadra, vuelve a correr `ingesta` sobre el mismo ride: los que ya
estan completos se saltan por tamano.

### ffmpeg no esta en el PATH

El proyecto necesita `ffmpeg` y `ffprobe`, y **no estan en el PATH** de la
herramienta Bash. Exportalo al principio de cada comando o falla con
`FFmpegMissing`:

```bash
export PATH="$PATH:/c/Users/chris/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-9.0-full_build/bin"
```

### El render es lento, lanzalo en segundo plano

El analisis tarda segundos, pero el render corre en CPU porque el driver NVIDIA
de este equipo entrega la API NVENC 12.2 y ffmpeg 9.0 pide la 13.1. Tarda unas
3.5 veces la duracion del reel: 18 min para un reel de 5 min. Lanza `run.py reel` con `run_in_background: true` y
sigue con otra cosa; te avisan cuando termina.

Mientras renderiza hay trabajo util que hacer: leer `analysis.json` y preparar
el reporte de abajo. No te quedes esperando.

## Que revisar antes de entregar

**1. GPS.** Los 19 archivos del primer ride se grabaron con el GPS apagado, y
`speed` — el peso mas importante del sistema — quedo **sin calibrar**. Chris lo
activo el 15-ago-2026. Revisa los avisos del analisis:

- Si siguen apareciendo `sin GPS: sin velocidad ni deteccion de caidas`,
  **diselo**. Es la senal que mas mejoraria el detector.
- Si por fin hay GPS, **avisale que ahora si se puede calibrar `speed`** y
  ofrecele hacerlo. No lo cambies por tu cuenta.

**2. Tiempo muerto al inicio de los clips.** La metrica que mas le importa, y
por la que ya hubo una ronda de correcciones. Sacala de `analysis.json`: para
cada segmento, `min(evento.inicio) - segmento.inicio`. Referencias medidas:

| | mediana | maximo |
|---|---|---|
| cortes que hace Chris a mano | 1.1 s | 6.3 s |
| el sistema hoy | 2.3 s | 7.3 s |

Si la mediana se dispara por encima de ~3.5 s en un ride nuevo, algo cambio y
vale la pena mirarlo antes de entregar.

**Con GPS esta metrica exagera, y hay que cruzarla con la velocidad.** Mide la
distancia hasta el primer *evento* discreto, pero un clip puede abrir en rodada
genuinamente rapida que no dispara ningun evento. Medido en Halpatiokee: las
ocho cabezas mas largas (hasta 14.2 s) iban todas a 16-21 km/h, por encima de
la mediana del ride. Eso no es relleno, es la aproximacion. Antes de reportar
un maximo alto como problema, mira la velocidad dentro de esa cabeza.

**3. Apertura y cierre.** El analisis imprime una linea por cada uno con lo que
encontro, o con la razon por la que no. El caso que hay que decirle siempre es
el cierre ausente: significa que dejo de grabar mientras todavia rodaba, y la
solucion es suya, no del codigo — seguir grabando hasta parar del todo.

Lo que quiere de la apertura es **que se le vea la cara**, como en JD Park. Ni
el casco ni el pecho lo enfocan a el: los dos miran adelante. La unica toma
donde aparece es la de los archivos cortos y detenidos, cuando esta acomodando
la camara — por eso existe la regla de "camara en la mano". Si un ride no tiene
ninguna parada asi, **no hay toma de su cara y hay que decirselo**: la solucion
es grabarse a proposito unos segundos antes de montar la camara.

**4. Si cambio de montaje a mitad de ride** (casco a pecho, por ejemplo): esto
sesga la seleccion, y esta medido sobre Halpatiokee (16-ago-2026). A la **misma
velocidad mediana** (15.2 km/h en las dos mitades), el pecho lee:

| senal | casco | pecho |
|---|---|---|
| gyro | 0.58 | 0.84 (+45%) |
| viento | 41.9 | 58.2 (+39%) |
| chatter | 1.09 | 1.20 (+10%) |

Como `RideStats` normaliza contra el ride entero, la mitad de pecho puntua mas
alto sin rodar mas rapido: se llevo el **64% de los clips con el 25% del
material**. No lo arregles por tu cuenta — reportalo con los numeros y deja que
el ojo de Chris decida si esa mitad era de verdad la buena.

El viento tambien **deja de servir como proxy de velocidad en el pecho**:
correlacion con la velocidad GPS de +0.72 en casco contra +0.32 en pecho. En el
casco el microfono queda expuesto al aire limpio; en el pecho queda en una
bolsa de turbulencia y se satura. Si un ride entero es de pecho, el peso de
`wind` esta comprando menos de lo que dice la calibracion original.

## Como entregar

Manda el reel con la herramienta de envio de archivos, no solo la ruta. Despues
un reporte corto — Chris lee rapido y odia el relleno:

- cuantos clips, cuanto dura el reel, de cuanto material salio
- el tiempo muerto medio, contra la referencia de arriba
- **cualquier clip raro y por que**: el impacto mas fuerte del ride, o un clip
  etiquetado `ACCION` (entro solo por senal continua, sin ningun evento — util
  para saber si el detector de velocidad funciona sin GPS)
- si hubo avisos de archivos sin telemetria

Los clips limpios quedan en `rides/<ride>/clips/`. La numeracion de la carpeta
**coincide** con la del reel; el pipeline borra los clips de corridas anteriores
justamente para que siga coincidiendo.

Cuando la seleccion ya le guste, `python run.py completo` pega los clips en
`final/video_completo.mp4` en resolucion nativa, con `-c copy`, en segundos.
**Ese archivo es el video final, no un paso intermedio.**

Si `completo` dice que hay clips que **no coinciden con el analisis**, no
insistas ni lo fuerces: significa que el clip de disco ya no es el corte que
pide el analisis de ahora — un render que se interrumpio, o un parametro que
cambio despues de renderizar. Corre `run.py reel` otra vez y solo se rehacen
los que hagan falta. Pegar es copiar bytes: lo que este mal en la carpeta sale
tal cual en el video final, y sin avisar.

## Espacio en disco

Un ride de ~45 min ocupa **~37 GB** (30-33 de originales, 3.4 de clips, 3.4 del
video final, 0.3 del reel). Al 16-ago-2026 habia 223 GB libres, o sea unos 5
rides. Vale la pena avisarle cuando quede poco.

`ingesta` mide antes de copiar y se planta si no cabe, en vez de reventar a los
20 GB: si eso pasa, `run.py limpiar --todos` libera clips y reel de los rides
viejos sin tocar nada irrecuperable.

**Chris formatea la SD de 64 GB despues de cada ride**, asi que `raw/` en el
computador es la **unica copia** que existe de ese material. Nunca propongas
borrarlo a la ligera, y nunca lo borres tu.

**Su politica de retencion, decidida el 16-ago-2026:** al procesar el ride N,
borra a mano los originales del ride **N-2**, dejando siempre dos de colchon.
Al terminar un ride nuevo, **recordarselo con los GB que libera**. La decision y
el borrado son suyos.

Cuando ya haya subido el video, recuerdale:

```bash
python run.py limpiar
```

Libera clips, reel y cache (~4 GB por ride) y deja intactos **los originales, el
video final (`final/`), `analysis.json` y `cortes.csv`**. Los clips se regeneran
con `run.py reel`.

`final/` esta separado de `reel/` a proposito: cuando el video final vivia
dentro de `reel/`, `limpiar` habria borrado el entregable. Si alguna vez hay que
liberar tambien eso, es `--incluir-final`, y vuelve en 9 s con `run.py completo`
mientras los clips existan.

## Como pedirle feedback

Cerrar el ciclo es lo que hace que esto mejore. Pidele **numero de clip + motivo
en una palabra**, y explicale que el motivo importa mas que el numero porque
cada uno apunta a un parametro distinto:

| lo que dice | que se toca |
|---|---|
| "pedaleo", "aburrido" | los pesos de `[pesos]` |
| "bueno pero muy largo" | `max_segment_seconds`, `post_roll` |
| "empieza antes de la accion" | `commit_fraction`, `pre_roll` |
| "termina en pedaleo sin sentido" | `tail_hold_seconds`, `post_roll` |
| "el salto no existe" | `air_threshold_g` |
| "repetido" | `merge_gap_seconds` |
| "estos archivos no los quiero" | `ajustes.toml` del ride, no un parametro |

**Ojo con la ultima fila.** Cuando lo que dice es una decision sobre *este*
material y no sobre el detector — un angulo que no le gusto, la apertura que
eligio, un corte puntual que sobra — eso va en `ajustes.toml` dentro de la
carpeta del ride (`excluir`, `aperturas`, `descartar`, `reel_segundos`), no en
`config.toml`. Asi sobrevive a reanalizar sin arrastrar la decision a los rides
siguientes. Ver el README.

Preguntale tambien **si falto algo bueno** que tuvo que ir a buscar a los
originales. Los descartes miden precision; lo que falta mide cobertura, y eso es
mucho mas caro de conseguir.

## Cuando te de un video ya editado

Es la calibracion mas valiosa que existe. Un export de CapCut no conserva
telemetria, asi que el emparejamiento es por imagen:

```bash
python run.py comparar "<video editado>" <ride>
```

Reporta precision y cobertura. **Ojo con la precision por segundos**: si
recortas tiempo muerto, sobra presupuesto, entran mas clips, y la cifra baja sin
que nada haya empeorado. Mira siempre el **conteo de clips que aciertan** antes
de creerle a esa metrica.

## Antes de tocar cualquier parametro

Los valores actuales estan ajustados contra 45 s de ground truth recuperados de
un video que Chris edito el mismo, validados sobre archivos que la busqueda
nunca vio, y **aprobados por su ojo** sobre el reel v2. No los muevas por
iniciativa propia.

Cuando si haya que moverlos, la leccion de las dos rondas anteriores es:
**traduce el sintoma a una medicion antes de tocar nada**. La segunda ronda
arranco midiendo el lead-in real de Chris sobre el ground truth en vez de elegir
un `pre_roll` a ojo, y eso destapo que habia dos causas y no una — una de ellas
un bug de verdad en el corte de tramos largos, que se habria quedado escondido
si el numero se hubiera elegido a dedo.

Corre `python tests/test_pipeline.py` despues de cualquier cambio al motor.
Son 116 comprobaciones y varias son regresiones de bugs que ya costaron caro.
