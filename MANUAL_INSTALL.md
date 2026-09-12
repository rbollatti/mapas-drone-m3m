🇦🇷 [Versión en español](MANUAL_INSTALL.es.md)

# Manual installation and usage

For those who prefer doing it by hand instead of the assisted setup described
in the `README`. (The full guide the assistant follows, with hardware-based
configuration and troubleshooting, is in `CLAUDE.md` — worth reading even if
you install manually.)

## Validated quality

This is not an experiment: it was compared against the commercial reference
software processing **the same real flight**, and also against chlorophyll
meter readings taken in the field:

- **Vegetation indices: correlation r = 0.93**, with the same value range.
- **Elevation: ±1 cm absolute error** using RTK and the drone's own factory
  lens correction (without that correction, any free software introduces
  errors of tens of centimeters).

## Installation in 5 steps

1. **Install Docker Desktop** (free, docker.com) and enable the WSL2 backend
   (the installer's default option).
2. **Install Miniconda** (free, conda.io) and create the environment:

   ```
   conda env create -f environment.yml
   conda activate m3m
   ```

3. **Install the package** from the repo folder:

   ```
   pip install -e .
   ```

4. **Generate YOUR drone's calibration** from any NIR photo taken by your
   Mavic 3M (each drone leaves the factory with its own lens calibration;
   that's why this step is mandatory and `cameras_fabrica.ejemplo.json` is
   only illustrative):

   ```
   python -m m3m.genera_cameras_fabrica "D:\Drone\MyFlight\DJI_20260101120000_0001_MS_NIR.TIF"
   ```

   That leaves a `cameras_fabrica.json` in the repo folder. This is done
   **once** per drone.

5. **Done.** Process your first flight (see Usage).

## Usage

A single command processes the whole flight — photo scanning, radiometric
calibration, photogrammetry (OpenDroneMap in Docker) and map generation:

```
python -m m3m.pipeline ^
    --misiones "D:\Drone\January flight\DJI_..._031" "D:\Drone\January flight\DJI_..._032" ^
    --productos ndvi,gndvi,ndre,lci,orto,dsm,nube,rgb ^
    --salida "D:\maps\january_flight"
```

- `--misiones`: the mission folders the drone creates on the card (one or
  several from the same flight).
- `--productos`: which maps you want, comma-separated (`ndvi`, `gndvi`,
  `ndre`, `lci`, `orto`, `dsm`, `nube`, `rgb`).
- `--salida`: folder where the final maps end up, plus a `resumen.json`.

A large flight takes several hours; progress is printed stage by stage.
Advanced options: `python -m m3m.pipeline --help`.
