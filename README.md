🇦🇷 [Versión en español](README.es.md)

# mapas-drone-m3m

Turn the raw photos from your **DJI Mavic 3M** into maps ready to use in the
field, with free and open-source software.

## What you get

From one Mavic 3M flight (the photos exactly as they come off the card), the
pipeline generates with a single command:

- **Multiband orthomosaic**: the "photo map" of the whole field, with the 4
  spectral bands (green, red, red edge and near infrared).
- **Vegetation indices**: NDVI, GNDVI, NDRE and LCI — the vigor and
  chlorophyll maps used to see where the crop is doing better or worse.
- **Elevation model (DSM)**: the field's relief — high spots, low spots and
  microrelief.
- **3D point cloud** of the field.
- **RGB orthomosaic**: the "normal" true-color aerial photo, in high
  resolution.

Everything georeferenced, in standard formats (GeoTIFF/COG) that any GIS
software can open.

## What you need

- **Drone**: DJI Mavic 3M. **RTK strongly recommended** — without RTK the
  indices are still useful, but the elevation map loses accuracy.
- **Computer**: Windows with **Docker Desktop** (WSL2 backend, free to
  install).
  - **RAM**: 32 GB is enough for small flights (under 500 photos). Flights
    covering a whole field (2,000+ photos) need 64-128 GB, with swap
    configured.
  - **Disk**: a fast drive (SSD) with **300-400 GB free** per large flight —
    these are temporary files, freed when the run finishes.

## How to start

Open the repo folder with [Claude Code](https://claude.com/claude-code)
(or a similar assistant) and ask it:

> "set up the pipeline for my machine"

The assistant detects your hardware, installs and configures everything
(Docker, WSL2, the Python environment, your drone's calibration) and walks
you through processing your first flight step by step. The instructions it
follows are in `CLAUDE.md`.

Prefer to install by hand? The steps are in
[`MANUAL_INSTALL.md`](MANUAL_INSTALL.md).

If this tool helped you, star the repo so I know it's being used.

## License and acknowledgements

This project is **MIT** (see `LICENSE`): use it, modify it and share it
freely.

The photogrammetry is done by
**[OpenDroneMap](https://www.opendronemap.org/)** (AGPL license), which this
pipeline runs as an external program via Docker. Without the huge amount of
work by the ODM community, none of this would be possible — thank you.
