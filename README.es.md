🇬🇧 [English version](README.md)

# mapas-drone-m3m

Convertí las fotos crudas de tu **DJI Mavic 3M** en mapas listos para usar en
el campo, con software libre.

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

Todo georreferenciado y en formatos estándar (GeoTIFF/COG) que abre
cualquier software GIS.

## Qué necesitás

- **Drone**: DJI Mavic 3M. **RTK muy recomendado** — sin RTK los índices
  sirven igual, pero el mapa de elevación pierde precisión.
- **Computadora**: Windows con **Docker Desktop** (con WSL2, se instala
  gratis).
  - **RAM**: 32 GB alcanzan para vuelos chicos (menos de 500 fotos). Para
    vuelos de un campo entero (2.000+ fotos) hacen falta 64-128 GB, con swap
    configurado.
  - **Disco**: un disco rápido (SSD) con **300-400 GB libres** por vuelo
    grande — son archivos temporales, se liberan al terminar.

## Cómo empezar

Abrí la carpeta del repo con [Claude Code](https://claude.com/claude-code)
(o un asistente similar) y pedile:

> "configurame el pipeline según mi máquina"

El asistente detecta tu hardware, instala y configura todo (Docker, WSL2,
ambiente Python, la calibración de tu drone) y te acompaña para procesar tu
primer vuelo paso a paso. Las instrucciones que sigue están en `CLAUDE.md`.

¿Preferís instalarlo a mano? Los pasos están en
[`MANUAL_INSTALL.es.md`](MANUAL_INSTALL.es.md).

Si te sirvió, dejá una estrella así sé que se usa.

## Interfaz local (sin terminal)

¿Preferís botones antes que comandos? El repo incluye una interfaz web local
mínima:

```
pip install -e ".[ui]"
python -m m3m.servidor
```

Después abrí http://127.0.0.1:8600: elegí las carpetas de misión del vuelo,
tildá los productos que querés y apretá "Generar". El progreso por etapa, el
log y las vistas previas de los productos se ven en la misma página. Todo
corre en tu máquina — no se sube nada. Para probarla sin drone ni Docker,
arrancá el servidor con la variable de entorno `M3M_SIMULACRO=1`: un pipeline
de demo de ~20 segundos genera mapas sintéticos.

Cada producto terminado tiene además un **botón "Ver mapa"**: un visor
interactivo (pan/zoom) renderizado al vuelo desde el GeoTIFF, en la misma
página. Los índices usan escala de colores fija (NDVI 0,15-0,90, GNDVI
0,15-0,80, NDRE 0,05-0,45, LCI 0,05-0,55), así dos vuelos del mismo lote se
comparan directo; el DSM va con paleta de terreno. Sin mapa base y sin
internet — el visor funciona completamente offline.

## Licencia y agradecimientos

Este proyecto es **MIT** (ver `LICENSE`): usalo, modificalo y compartilo
libremente.

La fotogrametría la hace **[OpenDroneMap](https://www.opendronemap.org/)**
(licencia AGPL), que este pipeline ejecuta como programa externo via Docker.
Sin el enorme trabajo de la comunidad de ODM, nada de esto sería posible —
gracias.

El visor de mapas de la interfaz local usa **[Leaflet](https://leafletjs.com/)**
1.9.4 (licencia BSD-2-Clause, (c) Volodymyr Agafonkin y colaboradores),
incluido en `m3m/web/leaflet/` para que la página funcione offline.
