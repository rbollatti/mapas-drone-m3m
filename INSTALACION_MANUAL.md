# Instalación manual y uso

Para el que prefiere hacerlo a mano en vez de la instalación asistida del
`README`. (La guía completa que sigue el asistente, con configuración según
hardware y solución de problemas, está en `CLAUDE.md` — vale la pena leerla
aunque instales a mano.)

## Calidad validada

No es un experimento: se comparó contra el software comercial de referencia procesando **el mismo vuelo real**, y también contra mediciones
de clorofilómetro a campo:

- **Índices de vegetación: correlación r = 0,93**, con el mismo
  rango de valores.
- **Elevación: ±1 cm de error absoluto** usando RTK y la corrección de lente
  de fábrica del propio drone (sin esa corrección, cualquier software libre
  mete errores de decenas de centímetros).

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
