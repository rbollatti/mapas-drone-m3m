"""Calibración radiométrica de TIFFs DJI Mavic 3M (estilo DJI Terra).

Aplica la cadena oficial de DJI (Mavic 3M Image Processing Guide, Eq. 7-9)
a cada TIFF multiespectral crudo:

    X = (DN - BlackLevel) * V(r) / (SensorGain * ExposureTime_us / 1e6)
        * SensorGainAdjustment / Irradiance

con V(r) = 1 + k0*r + k1*r^2 + ... + k5*r^6 (coeficientes ``VignettingData``
del XMP, r en píxeles desde el centro óptico calibrado), CAPEADO a
``VIG_MAX`` porque el polinomio sobrecorrige en las esquinas extremas.
``Irradiance`` es la señal del sensor de sol de la banda, ya compensada por
el algoritmo interno de DJI: se usa tal cual (NO aplicar ángulo solar).

La salida es uint16 con **brillo igualado por banda** (k_banda = objetivo/p99,
guardados en ``k_bandas.json``): imprescindible para que SfM y texturizado de
ODM funcionen (con escala global única las bandas G/R quedan oscuras, la
reconstrucción mete puntos espurios y el mosaico se agujerea). Los índices se
calculan des-escalando: NDRE = (NIR/k_NIR - RE/k_RE) / (NIR/k_NIR + RE/k_RE).

Los TIFF de salida son copias byte a byte del original con SOLO los píxeles
reescritos in-place (``tifffile.memmap``, los TIF del M3M no van comprimidos):
todo el XMP/EXIF (GPS RTK, CaptureUUID, banda) queda intacto, así ODM agrupa
bandas y georreferencia igual que con los crudos.

Uso
---
    python -m m3m.calibracion_m3m escala --src <dir_crudos>
    python -m m3m.calibracion_m3m todo   --src <dir_crudos> --dst <dir_salida>

Luego ODM sobre <dir_salida> con la receta validada (denso + lente de
fábrica + RTK apretado; r=0,93 vs DJI Terra)::

    docker run --rm -v <proyecto>:/datasets/code opendronemap/odm \\
        --project-path /datasets --radiometric-calibration none \\
        --primary-band NIR --pc-filter 2 --boundary /datasets/code/boundary.geojson \\
        --texturing-skip-global-seam-leveling --skip-3dmodel \\
        --orthophoto-resolution 7.4 --min-num-features 12000 --mesh-size 200000 \\
        --camera-lens brown --cameras /datasets/code/cameras_fabrica.json \\
        --gps-accuracy 0.05 --dsm --dem-resolution 10

o directamente ``python -m m3m.pipeline``, que orquesta todo el vuelo.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import tifffile

EXPOSURE_UNIT = 1e6  # ExposureTime del XMP en microsegundos (guía DJI + ODM)
VIG_MAX = 1.5  # cap del factor de viñeteo (validado hasta r~1100 px; sin cap hay lunares)
VIG_MIN = 0.8
OBJETIVO_P99 = 50000.0  # brillo objetivo por banda en el uint16 de salida
BANDAS = ("G", "R", "RE", "NIR")

_vig_cache: dict = {}


def leer_params(path: Path) -> dict:
    """Extrae los tags radiométricos del XMP embebido (primeros 16 KB del TIF).

    Parameters
    ----------
    path : Path
        TIFF multiespectral del M3M (una banda).

    Returns
    -------
    dict
        black, gain, texp, gain_adj, irr, cx, cy, vig (lista de 6), band.
    """
    with open(path, "rb") as fh:
        raw = fh.read(16384)
    i0 = raw.find(b"<x:xmpmeta")
    i1 = raw.find(b"</x:xmpmeta>")
    xmp = raw[i0:i1].decode("utf-8", errors="replace")

    def f(tag, cast=float):
        m = re.search(rf'drone-dji:{tag}="([^"]+)"', xmp)
        if not m:
            m = re.search(rf"<drone-dji:{tag}>([^<]+)</drone-dji:{tag}>", xmp)
        return cast(m.group(1)) if m else None

    vig = f("VignettingData", str)
    return dict(
        black=f("BlackLevel"), gain=f("SensorGain"), texp=f("ExposureTime"),
        gain_adj=f("SensorGainAdjustment"), irr=f("Irradiance"),
        cx=f("CalibratedOpticalCenterX"), cy=f("CalibratedOpticalCenterY"),
        vig=[float(v) for v in vig.split(",")] if vig else None,
        band=f("BandName", str),
    )


def factor_vineteo(p: dict, shape: tuple) -> np.ndarray:
    """Mapa multiplicativo de compensación de viñeteo, cacheado por banda."""
    key = (tuple(p["vig"]), p["cx"], p["cy"], shape)
    if key not in _vig_cache:
        h, w = shape
        yy, xx = np.mgrid[0:h, 0:w]
        r = np.sqrt((xx - p["cx"]) ** 2 + (yy - p["cy"]) ** 2)
        vc = np.ones_like(r)
        rr = r.copy()
        for ki in p["vig"]:
            vc += ki * rr
            rr *= r
        np.clip(vc, VIG_MIN, VIG_MAX, out=vc)
        _vig_cache[key] = vc.astype(np.float32)
    return _vig_cache[key]


def senal_calibrada(dn: np.ndarray, p: dict) -> np.ndarray:
    """DN uint16 -> señal proporcional a reflectancia (float32).

    Orden validado (ODM/MicaSense): black level -> viñeteo -> ganancia y
    exposición -> ajuste inter-banda -> irradiancia. La división por 2^16 de
    la guía DJI es una constante global y queda absorbida por k_banda.
    """
    x = dn.astype(np.float32) - p["black"]
    np.clip(x, 0, None, out=x)
    x *= factor_vineteo(p, dn.shape)
    x /= p["gain"] * (p["texp"] / EXPOSURE_UNIT)
    x *= p["gain_adj"]
    x /= p["irr"]
    return x


def _init_worker(dst: str):
    global _DST
    _DST = Path(dst)


def procesar_uno(args) -> str:
    """Copia el TIF y reescribe sus píxeles calibrados in-place (metadata intacta)."""
    src, k_bandas = args
    dst = _DST / src.name
    if not dst.exists():
        shutil.copy2(src, dst)
    p = leer_params(src)
    with tifffile.TiffFile(str(src)) as tf:
        dn = tf.pages[0].asarray()
    x = senal_calibrada(dn, p) * k_bandas[p["band"]]
    out = np.clip(x, 0, 65535).astype(np.uint16)
    mm = tifffile.memmap(str(dst))
    mm[:] = out
    mm.flush()
    del mm
    return src.name


def muestra_archivos(src: Path, n_capturas: int = 25) -> list[Path]:
    """n capturas repartidas a lo largo del vuelo, las 4 bandas de cada una."""
    todos = sorted(src.glob("*_MS_NIR.TIF"))
    paso = max(1, len(todos) // n_capturas)
    files = []
    for f in todos[::paso][:n_capturas]:
        for b in BANDAS:
            g = src / f.name.replace("_MS_NIR", f"_MS_{b}")
            if g.exists():
                files.append(g)
    return files


def calcular_escala(src: Path, k_file: Path) -> dict:
    """k por banda tal que p99 de la señal calibrada ~= OBJETIVO_P99."""
    por_banda: dict[str, list] = {}
    for f in muestra_archivos(src, 20):
        p = leer_params(f)
        with tifffile.TiffFile(str(f)) as tf:
            dn = tf.pages[0].asarray()[::8, ::8]
        por_banda.setdefault(p["band"], []).append(np.percentile(senal_calibrada(dn, p), 99))
    ks = {b: OBJETIVO_P99 / float(np.percentile(v, 95)) for b, v in por_banda.items()}
    k_file.write_text(json.dumps(ks, indent=1))
    print("k por banda:", {b: round(k, 2) for b, k in ks.items()}, "->", k_file)
    return ks


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("modo", choices=["escala", "todo", "muestra"])
    ap.add_argument("--src", required=True, type=Path, help="dir con los TIFF crudos (bandas juntas)")
    ap.add_argument("--dst", type=Path, help="dir de salida (requerido para todo/muestra)")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    k_file = a.src.parent / "k_bandas.json"

    if a.modo == "escala":
        calcular_escala(a.src, k_file)
        return
    if not a.dst:
        ap.error("--dst es requerido para modo todo/muestra")
    if not k_file.exists():
        calcular_escala(a.src, k_file)
    k_bandas = json.loads(k_file.read_text())
    a.dst.mkdir(parents=True, exist_ok=True)
    files = muestra_archivos(a.src, 10) if a.modo == "muestra" else sorted(a.src.glob("*_MS_*.TIF"))
    print(f"{len(files)} archivos, k={k_bandas}")
    with Pool(a.workers, initializer=_init_worker, initargs=(str(a.dst),)) as pool:
        for i, _ in enumerate(pool.imap_unordered(
                procesar_uno, [(f, k_bandas) for f in files], chunksize=16)):
            if i % 500 == 0:
                print(f"  {i}/{len(files)}")
    shutil.copy2(k_file, a.dst.parent / "k_bandas.json") if k_file.parent != a.dst.parent else None
    print("OK")


if __name__ == "__main__":
    main()
