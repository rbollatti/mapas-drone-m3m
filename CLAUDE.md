# CLAUDE.md — guía de configuración asistida

Este archivo es para VOS, el asistente que está ayudando al usuario a poner en
marcha este pipeline. El usuario probablemente te pidió algo como "configurame
el pipeline según mi máquina". Tu trabajo es dejarle todo funcionando y
acompañarlo en su primer vuelo.

**Regla de oro: validá cada paso ejecutando comandos reales.** No asumas que
algo está instalado o configurado — verificalo. No avances al paso siguiente
si el actual no quedó comprobado.

**Contexto mínimo del repo** (leé los docstrings de `m3m/` para el detalle):

- `m3m/pipeline.py` — orquestador end-to-end: crudos del DJI Mavic 3M →
  ortomosaico multibanda + índices (NDVI/GNDVI/NDRE/LCI) + DSM + nube + RGB.
  Usa OpenDroneMap (ODM) en Docker como motor fotogramétrico.
- `m3m/calibracion_m3m.py` — calibración radiométrica de los TIFF crudos
  (cadena oficial DJI), la llama el pipeline.
- `m3m/genera_cameras_fabrica.py` — extrae la calibración geométrica de
  fábrica de la lente desde una foto del drone del usuario.
- `m3m/fusion_misiones.py` — método alternativo (fusión de ortos por misión),
  normalmente no hace falta.

---

## Paso 1 — Detectar el hardware

Ejecutá y anotá (PowerShell):

```powershell
# RAM total y cores
Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory, NumberOfLogicalProcessors
# GPU NVIDIA (si el comando no existe o falla, no hay GPU NVIDIA usable)
nvidia-smi
# Discos y espacio libre
Get-PSDrive -PSProvider FileSystem | Select-Object Name, @{n='LibreGB';e={[math]::Round($_.Free/1GB)}}, @{n='TotalGB';e={[math]::Round(($_.Free+$_.Used)/1GB)}}
```

Con eso decidí:

- **RAM < 32 GB**: avisale honestamente que solo van a andar vuelos chicos
  (<500 fotos) y con swap grande.
- **RAM 32-64 GB**: vuelos chicos cómodos; vuelos de campo entero (2.000+
  fotos) solo con swap generoso, va a ser lento pero termina.
- **RAM 64-128 GB**: vuelos de campo entero sin drama.
- **Disco**: identificá el disco más rápido (ideal NVMe) con ≥400 GB libres
  para el `--workdir`. La regla general de espacio es **≥3× el tamaño del
  vuelo** en crudos, y 300-400 GB temporales para un run denso grande.

## Paso 2 — Docker Desktop + WSL2 + .wslconfig

1. Verificá si Docker está instalado y andando: `docker info`. Fijate que el
   backend sea WSL2 (línea `OSType: linux` y en Docker Desktop → Settings →
   General → "Use the WSL 2 based engine").
2. Si no está: guialo para instalar Docker Desktop (docker.com, gratis) con
   el backend WSL2 (default del instalador). Puede requerir reinicio y
   habilitar virtualización en BIOS — acompañalo.
3. **Escribí `C:\Users\<usuario>\.wslconfig`** proporcional al hardware
   detectado (sin esto, WSL se queda con la mitad de la RAM y ODM muere por
   OOM en vuelos grandes):

   ```ini
   [wsl2]
   memory=<75-80% de la RAM total>GB
   swap=<~= RAM total>GB
   processors=<cores - 4>
   ```

   Ejemplo para 128 GB / 32 cores: `memory=100GB`, `swap=128GB`,
   `processors=28`. El swap dejalo apuntando (default) a un disco con espacio.
4. Aplicá con `wsl --shutdown` (con Docker Desktop cerrado o reiniciándolo
   después) y verificá con `docker run --rm alpine free -g` que la memoria
   visible sea la configurada.
5. Bajá la imagen de ODM: `docker pull opendronemap/odm` (son varios GB).

## Paso 3 — Ambiente Python

1. ¿Hay conda? (`conda --version`). Si sí:

   ```
   conda env create -f environment.yml
   conda activate m3m
   pip install -e .
   ```

2. Si NO hay conda: ofrecé instalar Miniconda (preferido) o, como plan B,
   `python -m venv .venv` + pip. **Avisale que GDAL/rasterio por pip en
   Windows son problemáticos** (ruedas que no compilan, DLLs que faltan) —
   conda-forge es el camino recomendado; el venv solo si insiste.
3. Validá con imports reales:

   ```
   python -c "import rasterio, numpy, scipy, tifffile, exifread; print('ok')"
   python -c "from osgeo import gdal; print(gdal.__version__)"
   gdal_translate --version
   python -m m3m.pipeline --help
   ```

## Paso 4 — Calibración del drone del usuario

**Este paso es obligatorio y es POR UNIDAD de drone**: DJI graba en cada foto
la calibración geométrica de fábrica de ESA lente (campo XMP `DewarpData`).
El `cameras_fabrica.ejemplo.json` del repo es de otro drone y solo sirve de
referencia de formato.

1. Pedile al usuario **una foto NIR cualquiera** de su Mavic 3M (un archivo
   `DJI_..._MS_NIR.TIF` de cualquier vuelo, de la tarjeta o del disco).
2. Corré:

   ```
   python -m m3m.genera_cameras_fabrica "<ruta a la foto NIR>"
   ```

3. Verificá que quedó `cameras_fabrica.json` en la raíz del repo y que los
   valores impresos son razonables (focal ~2100-2250 px, cx/cy de pocos px).
   Este archivo está en `.gitignore` a propósito: es del drone del usuario.

## Paso 5 — Configuración según la máquina

- `--workers` de la calibración: `min(8, cores / 2)`.
- `--max-concurrency` de ODM: `cores - 4` (es el default del pipeline, que lo
  calcula solo; pasalo explícito si querés otro valor).
- `--workdir`: el disco rápido detectado en el paso 1 (default `C:\odm_work`;
  cambialo si el disco C: no es el indicado). El pipeline aborta si hay
  <400 GB libres.
- **Si hay GPU NVIDIA**: ofrecé la variante `--gpu` (imagen
  `opendronemap/odm:gpu` + `--gpus all`). Es EXPERIMENTAL y no está validada
  en este repo: si el usuario acepta, **validala primero con un vuelo chico**
  y comparando contra el mismo vuelo procesado sin GPU antes de usarla en
  serio. Sin GPU no se pierde calidad, solo tiempo.

## Paso 6 — Primer vuelo guiado

1. Preguntale al usuario:
   - ¿Dónde están las **carpetas de misión** del vuelo? (las crea el drone en
     la tarjeta, tipo `DJI_202601011200_031`; un vuelo largo son varias).
   - ¿Qué **productos** quiere? (`ndvi,gndvi,ndre,lci,orto,dsm,nube,rgb` —
     para un primer vuelo sugerí `ndvi,ndre,orto,dsm`).
   - ¿**Carpeta de salida**? (su basename es el nombre del proyecto y el
     prefijo de los archivos finales).
2. Lanzá:

   ```
   python -m m3m.pipeline --misiones "<mision1>" "<mision2>" ^
       --productos ndvi,ndre,orto,dsm --salida "<carpeta_salida>"
   ```

3. El pipeline imprime líneas `##ETAPA## <slug> <pct>` (etapas: escaneo,
   calibracion, odm, productos, rgb, movida, limpieza). Andá contándole al
   usuario en qué etapa va y cuánto falta aproximadamente. La etapa `odm` es
   la larga (horas en vuelos grandes); el resto son minutos.
4. Al terminar, mostrale el `resumen.json` de la salida y sugerile abrir los
   GeoTIFF en QGIS.

## Paso 7 — Problemas conocidos y sus fixes

- **`--radiometric-calibration camera+sun` de ODM está ROTO para el M3M:
  jamás usarlo.** La calibración radiométrica ya la hace este pipeline antes
  de ODM (por eso corre con `none`).
- **No usar `--ignore-gsd`**: infla RAM y disco sin mejora real.
- **OOM de WSL**: si ODM muere de golpe (contenedor killed), casi siempre es
  memoria. Cada crash puede dejar **volcados de ~100 GB** en
  `%LOCALAPPDATA%\Temp\wsl-crashes` — revisá esa carpeta, limpiala, y ofrecé
  crear una tarea programada de Windows que la purgue periódicamente. Después
  ajustá `memory`/`swap` en `.wslconfig` o procesá un vuelo más chico.
- **Fotos con MakerNote corrupto** (~1 cada 1.500-2.000): rompen ODM con
  IndexError. El pipeline las detecta en la etapa `escaneo` y mueve la
  captura completa a `<workdir>/cuarentena/` solo. No hay que hacer nada;
  contale al usuario si pasó.
- **Sin RTK**: los índices de vegetación sirven igual, pero el modelo de
  elevación puede salir con un "domo" sistemático de **decenas de cm** entre
  el centro y los bordes del lote (la autocalibración de lente en terreno
  plano es ambigua; la corrección de fábrica ayuda pero el anclaje absoluto
  fino requiere RTK). Explicale el límite honestamente: DSM sin RTK = formas
  relativas orientativas, no cotas confiables.
- **Espacio en disco**: regla práctica ≥3× el tamaño del vuelo en crudos,
  además de los 300-400 GB temporales del workdir para runs densos grandes.
  El pipeline verifica el workdir al arrancar, pero el disco de salida
  también tiene que aguantar los productos finales.

---

Si algo de esta guía no coincide con lo que ves en la máquina (versiones,
rutas, mensajes de error nuevos), priorizá lo que observás y resolvé con
criterio — esta guía describe el camino validado, no el único posible.
