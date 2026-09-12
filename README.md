# mapas-drone-m3m

Convertí las fotos crudas de tu **DJI Mavic 3M** en mapas listos para usar en
el campo, **sin pagar licencias** (DJI Terra cuesta miles de euros; esto usa
software libre).

## Qué te da

De un vuelo del Mavic 3M (las fotos tal cual salen de la tarjeta), el pipeline
genera con un solo comando:

- **Ortomosaico multibanda**: el "mapa foto" de todo el lote, con las 4 bandas
  espectrales (verde, rojo, borde rojo e infrarrojo cercano).
- **Índices de vegetación**: NDVI, GNDVI, NDRE y LCI — los mapas de vigor y
  clorofila que se usan para ver dónde el cultivo está mejor o peor.
- **Modelo de elevación (DSM)**: el relieve del lote — lomas, bajos y
  microrelieve.
- **Nube de puntos** 3D del lote.
- **Ortomosaico RGB**: la foto aérea "normal", en color real, de alta
  resolución.

Todo georreferenciado y en formatos estándar (GeoTIFF/COG) que abren QGIS,
Auravant o cualquier software GIS.

## Calidad validada

No es un experimento: se comparó contra **DJI Terra** (el software oficial
pago de DJI) procesando **el mismo vuelo real**, y también contra mediciones
de clorofilómetro a campo:

- **Índices de vegetación: correlación r = 0,93** con Terra, con el mismo
  rango de valores.
- **Elevación: ±1 cm de error absoluto** usando RTK y la corrección de lente
  de fábrica del propio drone (sin esa corrección, cualquier software libre
  mete errores de decenas de centímetros).

## Qué necesitás

- **Drone**: DJI Mavic 3M. **RTK muy recomendado** — sin RTK los índices
  sirven igual, pero el mapa de elevación pierde precisión (ver CLAUDE.md).
- **Computadora**: Windows con **Docker Desktop** (con WSL2, se instala
  gratis).
  - **RAM**: 32 GB alcanzan para vuelos chicos (menos de 500 fotos). Para
    vuelos de un campo entero (2.000+ fotos) hacen falta 64-128 GB, con swap
    configurado.
  - **Disco**: un disco rápido (SSD) con **300-400 GB libres** por vuelo
    grande — son archivos temporales, se liberan al terminar.

## Instalación en 5 pasos

1. **Instalá Docker Desktop** (gratis, docker.com) y activá el backend WSL2
   (es la opción default del instalador).
2. **Instalá Miniconda** (gratis, conda.io) y creá el ambiente:

   ```
   conda env create -f environment.yml
   conda activate m3m
   ```

3. **Instalá el paquete** desde la carpeta del repo:

   ```
   pip install -e .
   ```

4. **Generá la calibración de TU drone** desde una foto NIR cualquiera de tu
   Mavic 3M (cada drone sale de fábrica con su propia calibración de lente;
   por eso este paso es obligatorio y `cameras_fabrica.ejemplo.json` es solo
   ilustrativo):

   ```
   python -m m3m.genera_cameras_fabrica "D:\Drone\MiVuelo\DJI_20260101120000_0001_MS_NIR.TIF"
   ```

   Eso deja un `cameras_fabrica.json` en la carpeta del repo. Se hace **una
   sola vez** por drone.

5. **Listo.** Procesá tu primer vuelo (ver Uso).

## Uso

Un solo comando procesa el vuelo completo — escaneo de fotos, calibración
radiométrica, fotogrametría (OpenDroneMap en Docker) y generación de mapas:

```
python -m m3m.pipeline ^
    --misiones "D:\Drone\Vuelo enero\DJI_..._031" "D:\Drone\Vuelo enero\DJI_..._032" ^
    --productos ndvi,gndvi,ndre,lci,orto,dsm,nube,rgb ^
    --salida "D:\mapas\vuelo_enero"
```

- `--misiones`: las carpetas de misión que crea el drone en la tarjeta (una o
  varias del mismo vuelo).
- `--productos`: qué mapas querés, separados por coma (`ndvi`, `gndvi`,
  `ndre`, `lci`, `orto`, `dsm`, `nube`, `rgb`).
- `--salida`: carpeta donde quedan los mapas finales más un `resumen.json`.

Un vuelo grande tarda varias horas; el progreso se va imprimiendo por etapa.
Opciones avanzadas: `python -m m3m.pipeline --help`.

## Instalación asistida con Claude Code

Si nada de lo anterior te suena, hay un camino más fácil: abrí la carpeta del
repo con [Claude Code](https://claude.com/claude-code) y pedile:

> "configurame el pipeline según mi máquina"

El repo incluye un `CLAUDE.md` con instrucciones detalladas para que el
asistente detecte tu hardware, instale y configure todo (Docker, WSL2,
ambiente Python, calibración de tu drone), y te acompañe en el primer vuelo
paso a paso.

## Licencia y agradecimientos

Este proyecto es **MIT** (ver `LICENSE`): usalo, modificalo y compartilo
libremente.

La fotogrametría la hace **[OpenDroneMap](https://www.opendronemap.org/)**
(licencia AGPL), que este pipeline ejecuta como programa externo via Docker.
Sin el enorme trabajo de la comunidad de ODM, nada de esto sería posible —
gracias.
