# CLAUDE.md — assisted setup guide

**Speak to the user in THEIR language (the repo's docs exist in English and
Spanish); these instructions being in English does not mean the user speaks
English.**

This file is for YOU, the assistant helping the user get this pipeline up and
running. The user probably asked you something like "set up the pipeline for
my machine". Your job is to leave everything working and guide them through
their first flight.

**Golden rule: validate every step by running real commands.** Don't assume
something is installed or configured — verify it. Don't move on to the next
step until the current one is confirmed working.

**Minimal repo context** (read the docstrings in `m3m/` for the details; the
code and its docstrings are in Spanish):

- `m3m/pipeline.py` — end-to-end orchestrator: DJI Mavic 3M raw photos →
  multiband orthomosaic + indices (NDVI/GNDVI/NDRE/LCI) + DSM + point cloud +
  RGB. Uses OpenDroneMap (ODM) in Docker as the photogrammetry engine.
- `m3m/calibracion_m3m.py` — radiometric calibration of the raw TIFFs (the
  official DJI chain), called by the pipeline.
- `m3m/genera_cameras_fabrica.py` — extracts the factory geometric lens
  calibration from a photo taken by the user's drone.
- `m3m/fusion_misiones.py` — alternative method (per-mission orthomosaic
  fusion), normally not needed.

---

## Step 1 — Detect the hardware

Run and note down (PowerShell):

```powershell
# Total RAM and cores
Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory, NumberOfLogicalProcessors
# NVIDIA GPU (if the command doesn't exist or fails, there is no usable NVIDIA GPU)
nvidia-smi
# Drives and free space
Get-PSDrive -PSProvider FileSystem | Select-Object Name, @{n='FreeGB';e={[math]::Round($_.Free/1GB)}}, @{n='TotalGB';e={[math]::Round(($_.Free+$_.Used)/1GB)}}
```

Based on that, decide:

- **RAM < 32 GB**: tell them honestly that only small flights (<500 photos)
  will work, and only with a large swap.
- **RAM 32-64 GB**: small flights run comfortably; whole-field flights
  (2,000+ photos) only with generous swap — slow, but it finishes.
- **RAM 64-128 GB**: whole-field flights with no trouble.
- **Disk**: identify the fastest drive (ideally NVMe) with ≥400 GB free for
  the `--workdir`. The general space rule is **≥3× the size of the flight's
  raw photos**, plus 300-400 GB of temporary space for a large dense run.

## Step 2 — Docker Desktop + WSL2 + .wslconfig

1. Check whether Docker is installed and running: `docker info`. Make sure
   the backend is WSL2 (line `OSType: linux`, and Docker Desktop → Settings →
   General → "Use the WSL 2 based engine").
2. If it isn't there: guide them through installing Docker Desktop
   (docker.com, free) with the WSL2 backend (the installer's default). It may
   require a reboot and enabling virtualization in the BIOS — walk them
   through it.
3. **Write `C:\Users\<user>\.wslconfig`** proportional to the detected
   hardware (without this, WSL keeps only half of the RAM and ODM dies from
   OOM on large flights):

   ```ini
   [wsl2]
   memory=<75-80% of total RAM>GB
   swap=<~= total RAM>GB
   processors=<cores - 4>
   ```

   Example for 128 GB / 32 cores: `memory=100GB`, `swap=128GB`,
   `processors=28`. Leave the swap pointing (default) at a drive with space.
4. Apply with `wsl --shutdown` (with Docker Desktop closed, or restarting it
   afterwards) and verify with `docker run --rm alpine free -g` that the
   visible memory matches what you configured.
5. Pull the ODM image: `docker pull opendronemap/odm` (it's several GB).

## Step 3 — Python environment

1. Is conda available? (`conda --version`). If yes:

   ```
   conda env create -f environment.yml
   conda activate m3m
   pip install -e .
   ```

2. If there is NO conda: offer to install Miniconda (preferred) or, as plan
   B, `python -m venv .venv` + pip. **Warn them that GDAL/rasterio via pip on
   Windows are troublesome** (wheels that fail to build, missing DLLs) —
   conda-forge is the recommended path; the venv only if they insist.
3. Validate with real imports:

   ```
   python -c "import rasterio, numpy, scipy, tifffile, exifread; print('ok')"
   python -c "from osgeo import gdal; print(gdal.__version__)"
   gdal_translate --version
   python -m m3m.pipeline --help
   ```

## Step 4 — Calibrating the user's drone

**This step is mandatory and is PER drone unit**: DJI records in every photo
the factory geometric calibration of THAT lens (XMP field `DewarpData`). The
repo's `cameras_fabrica.ejemplo.json` belongs to a different drone and only
serves as a format reference.

1. Ask the user for **any NIR photo** from their Mavic 3M (a
   `DJI_..._MS_NIR.TIF` file from any flight, from the card or from disk).
2. Run:

   ```
   python -m m3m.genera_cameras_fabrica "<path to the NIR photo>"
   ```

3. Verify that `cameras_fabrica.json` was created at the repo root and that
   the printed values are reasonable (focal length ~2100-2250 px, cx/cy of a
   few px). This file is in `.gitignore` on purpose: it belongs to the user's
   drone.

## Step 5 — Machine-specific configuration

- Calibration `--workers`: `min(8, cores / 2)`.
- ODM `--max-concurrency`: `cores - 4` (this is the pipeline's default, which
  it computes on its own; pass it explicitly if you want a different value).
- `--workdir`: the fast drive detected in step 1 (default `C:\odm_work`;
  change it if drive C: is not the right one). The pipeline aborts if there
  are <400 GB free.
- **If there is an NVIDIA GPU**: offer the `--gpu` variant
  (`opendronemap/odm:gpu` image + `--gpus all`). It is EXPERIMENTAL and not
  validated in this repo: if the user accepts, **validate it first with a
  small flight**, comparing against the same flight processed without GPU,
  before relying on it. Without GPU you lose no quality, only time.
  Key check: in the ODM log, look at the `DensifyPointCloud` command line —
  if it says `--cuda-device -1`, ODM silently fell back to CPU (known with
  very new GPU architectures, e.g. RTX 50-series as of late 2026) and the
  GPU image gains nothing; stay on the standard image.

## Step 6 — Guided first flight

1. Ask the user:
   - Where are the flight's **mission folders**? (the drone creates them on
     the card, like `DJI_202601011200_031`; a long flight spans several).
   - Which **products** do they want? (`ndvi,gndvi,ndre,lci,orto,dsm,nube,rgb`
     — for a first flight suggest `ndvi,ndre,orto,dsm`).
   - **Output folder?** (its basename becomes the project name and the prefix
     of the final files).
2. Launch:

   ```
   python -m m3m.pipeline --misiones "<mission1>" "<mission2>" ^
       --productos ndvi,ndre,orto,dsm --salida "<output_folder>"
   ```

3. The pipeline prints `##ETAPA## <slug> <pct>` lines (stages: escaneo,
   calibracion, odm, productos, rgb, movida, limpieza). Keep telling the user
   which stage it is in and roughly how much is left. The `odm` stage is the
   long one (hours on large flights); the rest take minutes.
4. When it finishes, show them the `resumen.json` in the output folder and
   suggest opening the GeoTIFFs in their GIS software.

## Step 7 — Known problems and their fixes

- **ODM's `--radiometric-calibration camera+sun` is BROKEN for the M3M:
  never use it.** Radiometric calibration is already done by this pipeline
  before ODM (which is why it runs with `none`).
- **Do not use `--ignore-gsd`**: it inflates RAM and disk usage with no real
  improvement.
- **WSL OOM**: if ODM dies suddenly (container killed), it is almost always
  memory. Each crash can leave **~100 GB dumps** in
  `%LOCALAPPDATA%\Temp\wsl-crashes` — check that folder, clean it, and offer
  to create a Windows scheduled task that purges it periodically. Then adjust
  `memory`/`swap` in `.wslconfig` or process a smaller flight.
- **Photos with a corrupt MakerNote** (~1 in every 1,500-2,000): they break
  ODM with an IndexError. The pipeline detects them in the `escaneo` stage
  and moves the whole capture to `<workdir>/cuarentena/` on its own. Nothing
  to do; tell the user if it happened.
- **No RTK**: vegetation indices are still fine, but the elevation model can
  come out with a systematic "dome" of **tens of cm** between the center and
  the edges of the field (lens self-calibration over flat ground is
  ambiguous; the factory correction helps, but fine absolute anchoring
  requires RTK). Explain the limitation honestly: DSM without RTK = relative
  shapes for orientation, not reliable elevations.
- **Disk space**: rule of thumb ≥3× the size of the flight's raw photos, on
  top of the workdir's 300-400 GB of temporaries for large dense runs. The
  pipeline checks the workdir at startup, but the output drive also has to
  hold the final products.

## Step 8 — Offer the local web interface to non-technical users

If the user is not comfortable with the terminal (or just prefers clicking),
offer them the local web interface as an alternative to the CLI of step 6:

1. Install the optional extra: `pip install -e ".[ui]"` (adds
   fastapi/uvicorn; the CLI pipeline keeps working without them).
2. Start it with `python -m m3m.servidor` and have them open
   http://127.0.0.1:8600 (bound to 127.0.0.1 only — nothing is exposed to
   the network, and no data leaves the machine).
3. The page (in Spanish) walks them through the same choices as the CLI:
   mission folders, products, output folder, optional smoothing and the
   experimental GPU switch. It shows per-stage progress, the pipeline log, a
   cancel button, and PNG previews of the products when the job finishes.
   One job at a time.
4. To demo the interface without real data or Docker, set the environment
   variable `M3M_SIMULACRO=1` before starting the server: a ~20-second
   simulated pipeline goes through every stage and writes small synthetic
   GeoTIFFs.

---

If anything in this guide doesn't match what you see on the machine
(versions, paths, new error messages), trust what you observe and solve it
with good judgment — this guide describes the validated path, not the only
possible one.
