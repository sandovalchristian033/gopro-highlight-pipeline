# POV Automation

Pipeline para pasar de "20 archivos crudos de la GoPro" a "clips listos para editar"
sin tener que mirar el material completo.

## La idea

Tu GoPro Hero 9 no solo graba video: graba **telemetria incrustada dentro del MP4**
(acelerometro y giroscopio a ~200 Hz, GPS a ~18 Hz). Esa telemetria dice
exactamente donde estuvo la accion, y se puede leer en segundos sin decodificar
un solo frame de video.

| Lo que se ve en el video | Lo que mide el sensor |
|---|---|
| Salto o drop | el acelerometro cae a ~0 g: estas en el aire |
| Aterrizaje o golpe | pico de aceleracion de 3 a 8 g |
| Caida | golpe fuerte + la velocidad se va a cero justo despues |
| Tramo rapido | velocidad GPS, y el medidor de viento de la camara |
| Rock garden, raices | varianza de alta frecuencia del acelerometro |
| Curvas, whips | energia del giroscopio |

El sistema combina todo eso en una curva de "puntaje de accion" por instante,
encuentra los picos, y arma segmentos con contexto antes y despues.

### Donde abre cada clip

Un clip no abre donde el puntaje cruza el umbral, sino donde la accion
**arranca de verdad**. La diferencia no es teorica: las senales continuas suben
de a poco, asi que el cruce ocurre segundos antes de que se vea nada. Medido
sobre un ride real, los clips abrian con una mediana de 5.1 s de nada por
delante, y uno llegaba a 15.3 s.

| | mediana | maximo |
|---|---|---|
| cortes hechos a mano por Chris | 1.1 s | 6.3 s |
| sistema antes | 5.1 s | 15.3 s |
| **sistema ahora** | **2.3 s** | **7.3 s** |

Dos arreglos, no uno:

1. **Ancla de arranque** (`commit_fraction`). El clip abre donde la intensidad
   sube una fraccion real del camino entre el umbral y el pico del tramo, y de
   ahi se cuenta el `pre_roll`. Se mide sobre la senal continua y **nunca**
   sobre el puntaje total: el bono de un golpe de 8 g levanta tanto el pico que
   la aproximacion rapida hacia el golpe cae por debajo del corte. Probado,
   con el puntaje total un corte que abria 6.4 s antes pasaba a abrir 5.1 s
   **tarde**, o sea el error contrario y peor.
2. **Corte en el valle**. Un tramo mas largo que `max_segment_seconds` se parte
   en su momento mas tranquilo, no cada N segundos. Partir en pasos fijos habia
   rebanado un tramo de 43 s justo por la mitad, dejando dos clips cuya accion
   empezaba a los 14 y 15 s.
3. **Ancla de cierre** (`tail_hold_seconds`). El espejo del ancla de arranque:
   el clip cierra donde la intensidad deja de sostenerse, mas el `post_roll`.
   La diferencia con la cabeza es que ahi basta con tocar el nivel una vez
   (la senal viene subiendo), y en la cola hay que **sostenerlo**: un repunte
   de medio segundo mientras la rodada se apaga estaba estirando un clip seis
   segundos. Medido: un corte de 18.6 s con todos sus impactos entre el segundo
   1.8 y el 8.9 quedo en 13.3 s. La cola mediana quedo en 3.60 s, o sea
   exactamente el `post_roll` y nada mas.

Sobre el ground truth los dos cambios no costaron nada: **los mismos 8 clips
aciertan** y la cobertura queda igual en 82.2%.

### Como abre y como cierra el video

Aparte de los candidatos de accion, el pipeline busca dos clips mas que no
salen del puntaje: la **apertura** (parado antes de arrancar) y el **cierre**
(frenando hasta quedar quieto). Un video POV que entra a mitad de trail y sale
a mitad de trail no tiene forma.

No pueden salir del detector normal: el puntaje multiplica por
`stopped_penalty` todo lo que pase por debajo de `moving_speed_kmh`, o sea que
esta disenado para tirar justo ese material. Van por reglas propias, en
`pov/bookends.py`, y **necesitan el GPS encendido**.

| | de donde sale | como se ancla |
|---|---|---|
| apertura | primer archivo del ride | el primer tramo sostenido por encima de la velocidad de marcha; se cuenta hacia atras desde ahi |
| apertura alternativa | cualquier archivo que arranque desde parado y tenga un tramo rodado de verdad | igual que la anterior. Sirve cuando el ride se parte en dos, por ejemplo al cambiar la camara de casco a pecho: ese segundo arranque puede ser mejor apertura |
| apertura con la camara en la mano | archivos cortos y casi todos detenidos | el principio del archivo. Son las paradas para cambiar el montaje o arreglar algo, y **el unico momento del ride en que la camara apunta al rider**: todo lo demas, casco o pecho, mira hacia adelante |
| cierre | ultimo archivo del ride | el final del ultimo tramo en movimiento, siempre que la camara haya seguido grabando quieta despues |

Si la grabacion corta mientras todavia ruedas, **no hay cierre y se dice**.
Rellenarlo con los ultimos segundos del archivo reproduce exactamente el
problema que esto viene a resolver. Para que exista un final, hay que dejar la
camara grabando hasta estar detenido del todo.

Los dos aparecen en el reel como `#01 INTRO` y el ultimo como `FINAL`, y en
`clips/` con esos mismos nombres. No compiten por el presupuesto del reel ni se
rankean contra la accion.

### Que tan bien funciona

Medido contra un video que Chris edito el mismo (`JD park.mp4`), recuperando
por imagen que trozos de los crudos sobrevivieron al montaje. Los pesos se
ajustaron sobre el 60% de los archivos y se midieron sobre el 40% restante,
que la busqueda **nunca vio**:

| | precision | cobertura |
|---|---|---|
| antes de calibrar | 38.2% | 15.1% |
| **calibrado** | **85.5%** | **54.1%** |

Precision = de lo que propongo, cuanto querias de verdad.
Cobertura = de lo que querias, cuanto alcance a proponer.

Ambas cifras son un **piso**: solo se pudieron emparejar 45 s de los 208 s del
editado, asi que parte de lo que cuenta como "no lo querias" en realidad no
tiene con que compararse.

### Que senal sirve de verdad

Los pesos de `config.toml` no son inventados. Salen de medir material real,
comparando un tramo bueno contra uno de puro pedaleo:

| senal | separacion | veredicto |
|---|---|---|
| velocidad GPS | — | la respuesta real, **necesita el GPS encendido** |
| **viento (WNDM)** | **2.51x** | el medidor interno de la camara. Funciona sin GPS |
| chatter | 2.04x | bueno, pero mide "rugoso", no "rapido" |
| giroscopio | 1.75x | aceptable |
| volumen | 1.27x | inutil: el AGC del microfono lo aplana |
| inclinacion | 0.71x | **invertido**, desactivado. Mide la cabeza, no la bici |

El hallazgo util es **WNDM**: la Hero 9 guarda su propio medidor de ruido de
viento para decidir cuando activar la reduccion, y lo mide *antes* del control
automatico de ganancia. Es el unico proxy de velocidad que funciona con el GPS
apagado, o sea que sirve para material ya grabado.

### Limite conocido

Terreno **rugoso pero lento** (pedalear sobre grava) se parece a rugoso y rapido
para el acelerometro. El viento ayuda bastante, pero la solucion completa es
grabar con **GPS encendido**.

## Que te entrega

Despues de correr el pipeline, cada ride queda asi:

```
rides/2026-08-16_nombre-del-trail/
  raw/                    los archivos originales de la camara
  clips/                  clips limpios, resolucion nativa, sin overlays
                          <- por si quieres uno suelto
  reel/
    reel_candidatos.mp4   TODO junto en 1080p con etiquetas quemadas
                          <- esto es lo unico que tienes que mirar
  final/
    video_completo.mp4    los mismos clips pegados, resolucion nativa, sin
                          etiquetas  <- esto es lo que subes
  analysis.json           todo el detalle: eventos, puntajes, telemetria
  cortes.csv              la lista de cortes, abrible en Excel
```

El **reel de candidatos** muestra en pantalla, para cada clip: numero, ranking,
archivo de origen, timecode exacto, por que fue elegido (AIRE 0.64s, IMPACTO 5.2g,
CAIDA, VELOCIDAD 48 km/h) y su puntaje.

Tu flujo pasa a ser: mirar el reel, anotar los numeros que **sobran**, y correr
`run.py completo`. Nunca mas abrir los 20 originales.

## Instalacion

Requiere **Python 3.11+** y **ffmpeg**.

```bash
winget install Gyan.FFmpeg
```

Cierra y vuelve a abrir la terminal para que tome el PATH, y despues:

```bash
pip install -r requirements.txt
```

## Uso

Todo en un comando, despues de un ride:

```bash
python run.py ingesta --nombre "nombre del trail" --seguir
```

Eso crea la carpeta con la fecha de hoy, copia los archivos desde la camara
(por lector SD si hay tarjeta montada, o por USB/MTP si la camara esta conectada),
analiza todo y renderiza clips y reel.

### Paso a paso

```bash
python run.py nuevo --nombre "cerro-san-cristobal"   # crear carpeta del ride
python run.py ingesta                                # copiar desde la camara
python run.py analizar                               # detectar la accion
python run.py reel                                   # renderizar clips y reel
```

### Otros comandos

```bash
python run.py completo                               # pegar los clips en un solo video
python run.py listar                                 # ver todos los rides
python run.py revisar                                # ver la lista de cortes
python run.py analizar 2026-08-16_nombre-del-trail   # apuntar a un ride puntual
python run.py limpiar                                # liberar espacio del ride
```

### Liberar espacio

Un ride de ~45 min ocupa unos **37 GB**: 30 de originales, 3.4 de clips, 3.4 del
video final y 0.3 del reel. `limpiar` borra solo material de revision:

```bash
python run.py limpiar                    # el ride mas reciente
python run.py limpiar --todos            # todos los rides de golpe
```

Muestra cuanto va a liberar y pide confirmacion antes de tocar nada. Fuera de la
barrida quedan tres cosas:

| queda intacto | por que |
|---|---|
| `raw/` | si ya formateaste la tarjeta, es la unica copia que existe |
| `final/` | es el video que subes. Vuelve con `run.py completo`, pero eso necesita los clips |
| `analysis.json`, `cortes.csv` | pesan 26 KB y son el unico registro de que encontro el detector |

Lo que si barre — clips y reel — vuelve corriendo `run.py reel`.

`final/` esta en su propia carpeta justamente por esto: cuando el video final
vivia dentro de `reel/`, el comando cuyo trabajo es liberar espacio al terminar
habria borrado el entregable.

Para borrar tambien los originales hay que pedirlo por su nombre, y la
confirmacion es distinta a proposito — hay que escribir `BORRAR ORIGINALES`:

```bash
python run.py limpiar 2026-08-16_trail --incluir-raw
```

### El video entero, sin etiquetas

El reel lleva las etiquetas quemadas y esta comprimido para revisar. Cuando la
seleccion ya te gusta como esta, esto te da los mismos clips pegados en un solo
archivo en resolucion nativa:

```bash
python run.py completo
```

Sale en `reel/video_completo.mp4`. Va con `-c copy`, o sea copiando bytes: los
clips vienen todos del mismo render y comparten codec, resolucion y fps, asi
que **tarda segundos y no pierde nada de calidad**. Sirve para subirlo tal cual
o para meterlo en CapCut como una sola pista en vez de arrastrar treinta
archivos.

### Decisiones manuales sobre un ride

Cuando miras el reel y decides cosas que no son un parametro — "estos archivos
no los quiero", "la apertura es esta", "este corte sobra" — van en un
`ajustes.toml` **dentro de la carpeta del ride**, para que sobrevivan a volver
a analizar y renderizar sin contaminar la configuracion global:

```toml
# rides/2026-08-16_halpatiokee-mtb-trail/ajustes.toml

excluir   = ["GX011136", "GX011137"]        # no aportan candidatos de accion
aperturas = ["GX011145", "GX011141"]        # que abre el video, en este orden
descartar = ["GX011146@87.8"]               # cortes concretos que no quieres
reel_segundos = 290                         # largo fijo en vez del 15%
```

`excluir` deja los archivos fuera de los candidatos de accion pero **no** del
ride: siguen pudiendo aportar apertura, que es justo lo que se necesita cuando
un angulo no te gusta para rodar pero su arranque si sirve. Un corte anotado en
`descartar` se reconoce aunque un reanalisis lo mueva un par de segundos.

Al excluir archivos, el presupuesto del reel pasa a medirse contra el material
que **queda**, no contra el ride entero: pedirle los mismos segundos a la mitad
del material solo obliga a bajar el liston.

### Calibrar contra un video que ya editaste

El comando mas util para mejorar el detector. Le pasas un video que editaste tu
y los archivos crudos de los que salio, y te dice **que tan de acuerdo esta el
sistema contigo**:

```bash
python run.py comparar "JD park.mp4" 2026-08-10_jd-park
```

Un export de CapCut no conserva la telemetria, asi que el emparejamiento es por
imagen: huellas perceptuales de cada fotograma, normalizadas para que el color
grading no importe. Un corte real correlaciona por encima de 0.95 *y* avanza con
offset estable entre las dos lineas de tiempo; coincidencias sueltas se
descartan porque el material POV se parece mucho a si mismo.

Reporta precision (de lo que propongo, cuanto querias) y cobertura (de lo que
querias, cuanto propuse). La primera corrida tarda porque hay que decodificar
todo; despues queda cacheado en `.huellas` dentro del ride.

Si ya tienes los archivos en el disco:

```bash
python run.py ingesta --desde "C:\ruta\a\los\videos" --seguir
```

Sin argumento de ride, todos los comandos usan el ride mas reciente.

## Ajustes

Todo se tunea en [`config.toml`](config.toml). Los que mas vas a querer tocar
despues del primer ride real:

- `target_reel_seconds` — cuanto material entra al reel (por defecto 6 min).
- `peak_percentile` — subelo a 85-90 si quieres menos candidatos y mas selectivos.
- `air_threshold_g` — si marca saltos donde no los hubo, subelo un poco.
- `pre_roll` / `post_roll` — cuanto contexto antes y despues de cada momento.
- `commit_fraction` — cuanto tiempo muerto se permite al principio de cada
  clip. Subelo si todavia arrancan flojos, bajalo si se pierde la entrada de
  los saltos. En 0 vuelve al comportamiento viejo.
- `[pesos]` — cuanto pesa cada senal. Si quieres un reel mas de velocidad que de
  saltos, sube `speed` y baja `air`.
- `intro_lead_seconds` / `outro_lead_seconds` — cuanto dura la toma quieta del
  principio y cuanto de la frenada final entra en el cierre.

## Aceleracion por GPU

El render usa el encoder NVENC de la tarjeta NVIDIA si funciona. El sistema no
se conforma con preguntar si el encoder existe: **encodea un frame de prueba**,
porque ffmpeg puede traer NVENC compilado y aun asi fallar si el driver es mas
viejo que la API contra la que se compilo. Si falla, cae solo a CPU y dice por que.

> **En este equipo hoy:** la RTX 3050 esta presente, pero ffmpeg 9.0 pide la API
> NVENC 13.1 y el driver instalado entrega la 12.2. Se renderiza por CPU (x264).
> Para recuperar la GPU hay que **actualizar el driver NVIDIA a 610.00 o mas nuevo**.
> Vale la pena: la diferencia en tiempo de render es de varias veces.

## Si un archivo no trae telemetria

Pasa si el GPS estaba apagado o si el archivo viene ya reexportado de otro
programa. El sistema no se cae: usa el **audio** como senal de respaldo
(el ruido del viento escala con la velocidad y los golpes suenan fuerte).
Los resultados son mas gruesos, y el analisis lo avisa en pantalla.

Para que funcione bien de verdad, en la GoPro: **Preferencias > Regional > GPS: ON**.

## Estructura del codigo

| Archivo | Que hace |
|---|---|
| `pov/gpmf.py` | parser del formato GPMF de GoPro (KLV binario, sin dependencias) |
| `pov/telemetry.py` | extrae el track de telemetria del MP4 y lo vuelve series de tiempo |
| `pov/audio.py` | envolvente de volumen, la senal de respaldo |
| `pov/signals.py` | detecta aire, impactos y caidas; arma la curva de puntaje |
| `pov/segments.py` | convierte la curva en segmentos cortables y los rankea |
| `pov/ride.py` | orquesta un ride completo y escribe los reportes |
| `pov/render.py` | renderiza los clips limpios y el reel etiquetado |
| `pov/ingest.py` | copia desde tarjeta SD, desde la camara por USB, o desde carpeta |
| `pov/ffmpeg.py` | wrappers de ffmpeg/ffprobe, deteccion de NVENC |
| `pov/bookends.py` | la apertura y el cierre, por reglas de velocidad propias |
| `pov/ajustes.py` | las decisiones manuales sobre un ride concreto |
| `pov/cleanup.py` | libera espacio sin poder borrar lo irrecuperable |
| `pov/naming.py` | como nombra la GoPro, y que implica sobre el orden real |
| `pov/config.py` | valores por defecto y lectura de config.toml |

## Pendiente

- Generacion de shorts en 9:16 con texto quemado.
- Titulos, descripciones y hashtags por clip.
- Subida y programacion automatica a YouTube (Data API v3).
