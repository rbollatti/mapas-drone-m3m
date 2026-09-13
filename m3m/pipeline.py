"""Orquestador end-to-end de un vuelo M3M: crudos -> ortomosaico + índices COG.

Encadena todas las piezas del paquete en un solo comando:

1. ``escaneo``     — exifread sobre todos los TIF/JPG de las misiones. Una
   captura con EXIF corrupto (MakerNote roto, ~1 archivo cada 1.500-2.000,
   rompe ODM con IndexError) se mueve COMPLETA (las 4 bandas + JPG del mismo
   prefijo ``DJI_..._NNNN_``) a ``<workdir>/cuarentena/<mision>/``.
2. ``calibracion`` — ``m3m.calibracion_m3m todo`` por misión, todas al
   mismo ``<proy>/images``; un solo ``k_bandas.json`` para el vuelo, copiado a
   ``<proy>/`` y a ``<salida>/``. Copia además ``cameras_fabrica.json``
   de la raíz del repo (OJO: es POR UNIDAD de drone — generarlo con
   ``python -m m3m.genera_cameras_fabrica``) y el boundary si vino.
3. ``odm``         — receta validada (denso, lente brown + calibración de
   fábrica, gps-accuracy 0.05, DSM) en Docker con contenedor de nombre
   determinístico (sin ``--rm``: un proceso externo puede inspeccionarlo por
   nombre para cancelar/re-attachear; acá se borra recién al terminar bien).
   Variante ``--gpu`` EXPERIMENTAL (imagen ``opendronemap/odm:gpu`` +
   ``--gpus all``): NO validada en este repo.
4. ``productos``   — índices NDVI/GNDVI/NDRE/LCI desde el orto multibanda
   des-escalando con k_bandas.json, recorte de borde de 15 m, suavizado
   gaussiano opcional; DSM y orto multibanda a COG; nube LAZ copiada.
5. ``rgb``         — segundo run ODM con las ``*_D.JPG`` (solo si se pidió),
   con geometría CURADA (lente de fábrica RGB extraída del vuelo + RTK 0,05
   + DSM): validado r=0,998 y p50 4 cm vs el software comercial de referencia.
6. ``control``     — con ``rgb`` + ``dsm``, el DSM OFICIAL (``_dsm``) pasa a
   ser el del bloque RGB (no sufre el hundimiento del multiespectral en
   canopeo abierto); el multiespectral queda como ``_dsm_ms``. Además compara
   ambos bloques y advierte en ``resumen.json`` (``control_bloques``) dónde
   el ``_dsm_ms`` no es confiable para alturas.
7. ``movida``      — productos a ``<salida>/<nombre>_<producto>.*`` (el
   ``<nombre>`` del proyecto es el basename de ``--salida``) + ``resumen.json``.
8. ``limpieza``    — borra los working dirs del proyecto.

Protocolo de progreso (parseable por cualquier wrapper que lance esto por
subprocess): imprime en stdout líneas ``##ETAPA## <slug> <pct entero 0-100>``
al entrar a cada etapa y periódicamente durante la misma. El pct es relativo
a la etapa.

Uso
---
    python -m m3m.pipeline \\
        --misiones "D:\\Drone\\Vuelo 08012027\\DJI_..._001" "D:\\...\\_002" \\
        --productos ndvi,ndre,dsm,orto --salida "D:\\odm\\vuelo_0801" \\
        [--workdir C:\\odm_work] [--resolucion-cm 7.4] [--suavizado-m 0] \\
        [--gpu] [--boundary lote.geojson] [--contenedor m3m_job_x] \\
        [--workers 8] [--max-concurrency 12]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Defaults operativos (ver README: NVMe para trabajar, disco grande para salida)
WORKDIR_DEFAULT = Path("C:/odm_work")  # SSD NVMe: ODM es I/O-intensivo (en HDD pierde 20-40%)
ESPACIO_MIN_GB = 400  # un run denso ocupa 300-400 GB temporales
RECORTE_BORDE_M = 15.0  # índices inflados en el perímetro (recortar subió r de 0,64 a 0,85)
RESOLUCION_CM_DEFAULT = 7.4  # receta validada (r=0,93 vs referencia comercial)
ODM_IMAGEN = "opendronemap/odm"
ODM_IMAGEN_GPU = "opendronemap/odm:gpu"  # EXPERIMENTAL: no validada en este repo

PRODUCTOS_VALIDOS = ("ndvi", "gndvi", "ndre", "lci", "orto", "dsm", "nube", "rgb")
INDICES = ("ndvi", "gndvi", "ndre", "lci")

# Bandas del orto multibanda de ODM (OJO: distinto del orden G/R/RE/NIR de los crudos)
BANDA_ORTO = {"R": 1, "G": 2, "NIR": 3, "RE": 4}  # 5 = alpha

# k_bandas.json usa los BandName del XMP DJI ("Green"/"Red"/"RedEdge"/"NIR");
# se aceptan también los sufijos de archivo por si el JSON fue editado a mano.
ALIAS_K = {"G": ("Green", "G"), "R": ("Red", "R"), "RE": ("RedEdge", "RE", "Red Edge"), "NIR": ("NIR",)}

# (a - b) / (c + d), cada banda YA des-escalada por su k antes del cociente
FORMULAS = {
    "ndvi": (("NIR", "R"), ("NIR", "R")),
    "gndvi": (("NIR", "G"), ("NIR", "G")),
    "ndre": (("NIR", "RE"), ("NIR", "RE")),
    "lci": (("NIR", "RE"), ("NIR", "R")),
}

# Hitos del log de ODM -> % dentro de la etapa (aprox. por tiempos observados:
# opensfm 10-45, openmvs 45-75, meshing/texturing 75-90, dem/ortho 90-100)
ODM_HITOS = (
    ("running dataset stage", 2),
    ("running opensfm stage", 10),
    ("running openmvs stage", 45),
    ("running odm_filterpoints stage", 70),
    ("running odm_meshing stage", 75),
    ("running mvs_texturing stage", 80),
    ("running odm_georeferencing stage", 87),
    ("running odm_dem stage", 90),
    ("running odm_orthophoto stage", 95),
    ("running odm_postprocess stage", 99),
)


def etapa(slug: str, pct: float) -> None:
    """Línea de progreso parseable por un wrapper que lea el stdout del pipeline."""
    print(f"##ETAPA## {slug} {int(round(pct))}", flush=True)


def verificar_espacio(workdir: Path) -> None:
    """Aborta con mensaje claro si el disco del workdir no tiene ESPACIO_MIN_GB libres."""
    libre_gb = shutil.disk_usage(workdir).free / 1e9
    if libre_gb < ESPACIO_MIN_GB:
        sys.exit(
            f"Espacio insuficiente en {workdir}: {libre_gb:.0f} GB libres, se necesitan "
            f">={ESPACIO_MIN_GB} GB para un run denso de ODM. Liberá espacio o pasá otro --workdir."
        )


def _rmtree(path: Path) -> None:
    """rmtree tolerante a atributos read-only que deja el mount de docker en Windows."""
    import stat

    def quitar_ro(func, p, exc_info):  # noqa: ARG001 - firma que exige shutil
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=quitar_ro)


def escanear_misiones(misiones: list[Path], cuarentena: Path) -> tuple[int, list[str]]:
    """exifread sobre todos los TIF/JPG; capturas con EXIF corrupto van a cuarentena.

    Se cuarentena la CAPTURA COMPLETA (todas las bandas y el JPG del mismo
    prefijo ``DJI_..._NNNN_``), no solo el archivo roto, para no romper el
    agrupado de bandas de ODM.

    Returns
    -------
    tuple[int, list[str]]
        (cantidad de fotos OK, nombres de archivos movidos a cuarentena).
    """
    import exifread  # import pesado adentro: --help y el resto de etapas no lo pagan

    archivos: list[Path] = []
    for m in misiones:
        vistos: set[Path] = set()
        for pat in ("*.TIF", "*.JPG"):  # Windows es case-insensitive: dedupe por si acaso
            for f in sorted(m.glob(pat)):
                if f not in vistos:
                    vistos.add(f)
                    archivos.append(f)
    print(f"escaneo EXIF de {len(archivos)} archivos en {len(misiones)} misiones", flush=True)

    corruptos: list[Path] = []
    for i, f in enumerate(archivos):
        if i % 500 == 0:
            etapa("escaneo", 100 * i / max(len(archivos), 1))
        try:
            with open(f, "rb") as fh:
                # details=True OBLIGATORIO: es el modo con que ODM parsea (entra al
                # MakerNote); con False el escaneo deja pasar corruptos que ODM no tolera
                exifread.process_file(fh, details=True)
        except Exception as e:  # cualquier excepción de parseo = EXIF corrupto -> cuarentena
            print(f"EXIF corrupto en {f.name}: {type(e).__name__}: {e}", flush=True)
            corruptos.append(f)

    movidos: list[str] = []
    for f in corruptos:
        # prefijo de captura DJI_<timestamp>_<NNNN>: cubre las *_MS_*.TIF y la *_D.JPG
        m = re.match(r"^(DJI_\d+_\d+)_", f.name)
        grupo = sorted(f.parent.glob(m.group(1) + "_*")) if m else [f]
        destino = cuarentena / f.parent.name
        destino.mkdir(parents=True, exist_ok=True)
        for g in grupo:
            if not g.exists():  # ya movido por otro archivo corrupto de la misma captura
                continue
            tgt = destino / g.name
            if tgt.exists():
                tgt.unlink()  # re-run: pisar la copia vieja en cuarentena
            shutil.move(str(g), str(tgt))
            movidos.append(g.name)
    if movidos:
        print(f"cuarentena: {len(movidos)} archivos movidos a {cuarentena}", flush=True)
    return len(archivos) - len(movidos), movidos


def calibrar(misiones: list[Path], proy: Path, salida: Path, workers: int) -> Path:
    """Corre ``calibracion_m3m todo`` por misión, todas al mismo ``<proy>/images``.

    k_bandas.json queda en el padre del primer --src (calibracion_m3m lo
    calcula si falta); a las misiones con otro padre se les copia ANTES para
    que todo el vuelo use el mismo k por banda. Al final se copia a
    ``<proy>/`` y a ``<salida>/``.
    """
    dst = proy / "images"
    dst.mkdir(parents=True, exist_ok=True)
    k_ref: Path | None = None
    for i, m in enumerate(misiones):
        k_local = m.parent / "k_bandas.json"
        if k_ref is not None and k_local != k_ref and not k_local.exists():
            shutil.copy2(k_ref, k_local)
        # stdout/stderr heredan los del pipeline -> van al mismo log del job
        subprocess.run(
            [sys.executable, "-m", "m3m.calibracion_m3m", "todo",
             "--src", str(m), "--dst", str(dst), "--workers", str(workers)],
            cwd=str(REPO_ROOT), check=True,
        )
        if k_ref is None:
            k_ref = k_local
        etapa("calibracion", 100 * (i + 1) / len(misiones))
    assert k_ref is not None
    k_proy = proy / "k_bandas.json"
    shutil.copy2(k_ref, k_proy)
    salida.mkdir(parents=True, exist_ok=True)
    shutil.copy2(k_ref, salida / "k_bandas.json")
    return k_proy


def preparar_proyecto(proy: Path, boundary: Path | None) -> None:
    """Copia la calibración de fábrica del drone y el boundary al proyecto ODM.

    ``cameras_fabrica.json`` es POR UNIDAD de drone (sale del campo
    ``drone-dji:DewarpData`` del XMP de las fotos): cada usuario genera el
    suyo desde una foto propia con ``python -m m3m.genera_cameras_fabrica``.
    """
    cam = REPO_ROOT / "cameras_fabrica.json"
    if not cam.is_file():
        sys.exit(
            f"Falta {cam}. La calibración de lente es POR UNIDAD de drone: generala desde "
            "una foto NIR de TU drone con "
            "'python -m m3m.genera_cameras_fabrica <foto_NIR.TIF>' (ver README)."
        )
    shutil.copy2(cam, proy / "cameras_fabrica.json")
    if boundary is not None:
        shutil.copy2(boundary, proy / "boundary.geojson")


def args_odm_ms(resolucion_cm: float, con_boundary: bool, max_concurrency: int) -> list[str]:
    """Flags de la receta validada (denso + lente de fábrica + RTK apretado, r=0,93 vs referencia comercial)."""
    a = ["--radiometric-calibration", "none", "--primary-band", "NIR", "--pc-filter", "2"]
    a += ["--boundary", "/datasets/code/boundary.geojson"] if con_boundary else ["--auto-boundary"]
    a += [
        "--texturing-skip-global-seam-leveling", "--skip-3dmodel",
        "--orthophoto-resolution", str(resolucion_cm),
        "--min-num-features", "12000", "--mesh-size", "200000",
        "--camera-lens", "brown", "--cameras", "/datasets/code/cameras_fabrica.json",
        "--gps-accuracy", "0.05", "--dsm", "--dem-resolution", "10",
        "--matcher-neighbors", "8", "--skip-report",
        "--max-concurrency", str(max_concurrency),
    ]
    return a


def args_odm_rgb(con_boundary: bool, max_concurrency: int) -> list[str]:
    """Run RGB con geometría CURADA: lente de fábrica RGB + RTK apretado + DSM.

    Validado contra el software comercial de referencia (mismo vuelo): con
    estos flags el DSM del bloque RGB da r=0,998 y |dif| p50 4 cm, con MAD
    5 cm contra la franja RTK — igual que la referencia comercial y mejor que
    el multiespectral (MAD 8, y sin su hundimiento en canopeo abierto). Por
    eso, cuando el trabajo pide ``rgb`` + ``dsm``, el DSM OFICIAL del vuelo
    es el del bloque RGB (el multiespectral queda como ``_dsm_ms`` secundario).
    La calibración de fábrica de la lente RGB (distinta de la multiespectral,
    k1 −0,11) se extrae del XMP de la primera JPG en :func:`cameras_rgb_de`.
    """
    a = ["--boundary", "/datasets/code/boundary.geojson"] if con_boundary else ["--auto-boundary"]
    a += [
        "--orthophoto-resolution", "3", "--dsm", "--dem-resolution", "20",
        "--camera-lens", "brown", "--cameras", "/datasets/code/cameras_fabrica.json",
        "--gps-accuracy", "0.05", "--pc-filter", "2",
        "--skip-report", "--max-concurrency", str(max_concurrency),
    ]
    return a


def cameras_rgb_de(jpg: Path, salida: Path) -> None:
    """Extrae la calibración de fábrica de la lente RGB (XMP ``DewarpData``)
    de una ``*_D.JPG`` y escribe el cameras.json de ODM (modelo brown).

    Es POR UNIDAD de drone, igual que la multiespectral — por eso se genera
    del propio vuelo en cada corrida en vez de usar una plantilla.
    """
    raw = jpg.read_bytes()[:65536]
    m = re.search(rb'drone-dji:DewarpData="\s*([^"]+)"', raw)
    if not m:
        raise RuntimeError(f"la JPG {jpg.name} no trae DewarpData en el XMP")
    nums = m.group(1).decode().split(";")[1]
    fx, fy, cx, cy, k1, k2, p1, p2, k3 = (float(x) for x in nums.split(","))
    mw = re.search(rb'exif:PixelXDimension="(\d+)"', raw)
    mh = re.search(rb'exif:PixelYDimension="(\d+)"', raw)
    w = float(mw.group(1)) if mw else 5280.0
    h = float(mh.group(1)) if mh else 3956.0
    cam = {
        f"dji m3m {w:.0f} {h:.0f} brown {fx / w:.4f}": {
            "projection_type": "brown", "width": int(w), "height": int(h),
            "focal_x": fx / w, "focal_y": fy / w, "c_x": cx / w, "c_y": cy / w,
            "k1": k1, "k2": k2, "p1": p1, "p2": p2, "k3": k3,
        }
    }
    salida.write_text(json.dumps(cam, indent=2), encoding="utf-8")


def control_bloques(dsm_ms: Path, dsm_rgb: Path) -> dict:
    """Compara el DSM multiespectral contra el del bloque RGB (independiente).

    Globos de desacuerdo >5 cm y >0,5 ha = zonas donde el multiespectral se
    hunde en el canopeo (efecto de sensor, no de configuración — investigado
    con tests controlados: matching, lente y RTK exonerados). El DSM oficial
    ya es el del bloque RGB; esto queda como advertencia de dónde NO usar el
    ``_dsm_ms``.

    Returns
    -------
    dict con ``veredicto`` ("ok" | "revisar") y ``blobs``.
    """
    import numpy as np
    import rasterio
    from affine import Affine
    from rasterio.warp import Resampling, reproject
    from scipy import ndimage

    res_m = 1.0
    with rasterio.open(dsm_ms) as ds:
        fac = max(1, round(res_m / ds.res[0]))
        oh, ow = ds.height // fac, ds.width // fac
        ms = ds.read(1, out_shape=(oh, ow)).astype("float32")
        if ds.nodata is not None:
            ms[ms == ds.nodata] = np.nan
        t_ms = ds.transform * Affine.scale(ds.width / ow, ds.height / oh)
        crs = ds.crs
    with rasterio.open(dsm_rgb) as ds:
        rgb_r = ds.read(1).astype("float32")
        if ds.nodata is not None:
            rgb_r[rgb_r == ds.nodata] = np.nan
        rgb = np.full_like(ms, np.nan)
        reproject(rgb_r, rgb, src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=t_ms, dst_crs=crs, resampling=Resampling.bilinear,
                  src_nodata=np.nan, dst_nodata=np.nan)

    ok = np.isfinite(ms) & np.isfinite(rgb)
    if ok.sum() < 10000:
        return {"veredicto": "sin_datos", "blobs": []}
    d = ms - rgb
    med = float(np.nanmedian(d[ok]))
    resid = np.where(ok, d - med, np.nan)
    # escala de bloque (~15 m): suavizado que ignora NaN
    peso = ndimage.uniform_filter(ok.astype("float32"), size=15)
    suma = ndimage.uniform_filter(np.nan_to_num(resid), size=15)
    resid_s = np.where(peso > 0.3, suma / np.maximum(peso, 1e-6), np.nan)

    mal = np.isfinite(resid_s) & (np.abs(resid_s) > 0.05)
    mal = ndimage.binary_opening(mal, iterations=3)
    lab, n = ndimage.label(mal)
    blobs = []
    for i in range(1, n + 1):
        m = lab == i
        ha = float(m.sum() * res_m * res_m / 1e4)
        if ha < 0.5:
            continue
        cy, cx = ndimage.center_of_mass(m)
        blobs.append({
            "ha": round(ha, 1),
            "dz_cm": round(float(np.nanmedian(resid_s[m])) * 100, 1),
            "centro_utm": [round(t_ms.c + t_ms.a * cx, 0), round(t_ms.f + t_ms.e * cy, 0)],
        })
    blobs.sort(key=lambda b: -b["ha"])

    for b in blobs:
        print(f"CONTROL: el DSM multiespectral se hunde {b['dz_cm']:+.0f} cm en "
              f"{b['ha']} ha (UTM {b['centro_utm'][0]:.0f},{b['centro_utm'][1]:.0f}) — "
              "usar el _dsm oficial (bloque RGB) para alturas ahí", flush=True)
    return {"veredicto": "revisar" if blobs else "ok", "blobs": blobs}


def correr_odm(proy: Path, contenedor: str, gpu: bool, flags: list[str], slug: str) -> None:
    """Corre ODM en docker con nombre determinístico, mapeando el log a progreso.

    Sin ``--rm``: un proceso externo puede inspeccionar el contenedor por
    nombre para cancelar (``docker rm -f``) o re-attachear tras un reinicio;
    acá se borra recién cuando terminó bien. Si ODM falla, el contenedor queda
    vivo para inspección con ``docker logs``.
    """
    # Limpia un contenedor viejo con el mismo nombre (idempotente: no falla si no existe)
    subprocess.run(["docker", "rm", "-f", contenedor], capture_output=True, text=True)

    cmd = ["docker", "run", "--name", contenedor]
    if gpu:
        cmd += ["--gpus", "all"]
    cmd += ["-v", f"{proy}:/datasets/code", ODM_IMAGEN_GPU if gpu else ODM_IMAGEN]
    cmd += ["--project-path", "/datasets"] + flags

    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout is not None
    pct = 0
    for i, linea in enumerate(proc.stdout, start=1):
        print(linea, end="", flush=True)
        low = linea.lower()
        for hito, p in ODM_HITOS:
            if hito in low and p > pct:
                pct = p
                etapa(slug, pct)
        if i % 500 == 0:  # latido periódico para quien siga el log
            etapa(slug, pct)
    ret = proc.wait()
    if ret != 0:
        raise RuntimeError(
            f"ODM terminó con código {ret} (contenedor {contenedor}); "
            "queda vivo para inspección con 'docker logs'."
        )
    subprocess.run(["docker", "rm", contenedor], capture_output=True, text=True)


def leer_k(k_file: Path) -> dict[str, float]:
    """k por banda con claves canónicas R/G/RE/NIR (acepta los BandName del XMP)."""
    crudo = json.loads(k_file.read_text())
    k: dict[str, float] = {}
    for canon, alias in ALIAS_K.items():
        for a in alias:
            if a in crudo:
                k[canon] = float(crudo[a])
                break
    faltan = [b for b in BANDA_ORTO if b not in k]
    if faltan:
        raise KeyError(f"k_bandas.json sin k para {faltan}; claves presentes: {list(crudo)}")
    return k


def _suavizar_nan(arr, sigma_px: float):
    """Gaussiano que ignora NaN (normaliza por la masa válida) y conserva la huella."""
    import numpy as np
    from scipy.ndimage import gaussian_filter

    v = np.isfinite(arr)
    num = gaussian_filter(np.where(v, arr, 0.0), sigma_px)
    den = gaussian_filter(v.astype(np.float32), sigma_px)
    return np.where(v & (den > 1e-6), num / den, np.nan).astype(np.float32)


def generar_indices(
    orto: Path, indices: list[str], k: dict[str, float], suavizado_m: float,
    destino_dir: Path, nombre: str, pct0: float = 5, pct1: float = 70,
) -> list[tuple[str, Path]]:
    """Índices des-escalados desde el orto multibanda ODM, salida COG float32 NaN.

    Cada banda se divide por su k de k_bandas.json ANTES del cociente (el
    brillo igualado por banda es solo para que SfM/texturizado no se rompan).
    Recorta ``RECORTE_BORDE_M`` del borde válido y suaviza opcionalmente
    (sigma_px = suavizado_m / res, solo índices).
    """
    import numpy as np
    import rasterio
    from scipy.ndimage import binary_erosion

    with rasterio.open(orto) as ds:
        res = float(ds.res[0])
        alpha = ds.read(5)
        necesarias = sorted({b for idx in indices for par in FORMULAS[idx] for b in par})
        bandas = {b: ds.read(BANDA_ORTO[b]).astype(np.float32) / k[b] for b in necesarias}
        # perfil COG estilo destripe.py: el driver COG no acepta blockxsize/tiled/etc.,
        # así que se arma un perfil limpio en lugar de heredar el del orto
        prof = dict(
            driver="COG", height=ds.height, width=ds.width, count=1, dtype="float32",
            crs=ds.crs, transform=ds.transform, nodata=np.nan,
            compress="DEFLATE", BIGTIFF="IF_SAFER",
        )

    # recorte de borde: índices inflados por geometría de vista en el perímetro
    it = max(1, int(RECORTE_BORDE_M / res))
    valido = binary_erosion(alpha > 0, iterations=it)
    del alpha

    out: list[tuple[str, Path]] = []
    for j, idx in enumerate(indices):
        (a, b), (c, d) = FORMULAS[idx]
        num = bandas[a] - bandas[b]
        den = bandas[c] + bandas[d]
        ok = valido & (den > 1e-6)
        arr = np.where(ok, num / np.maximum(den, 1e-9), np.nan).astype(np.float32)
        del num, den, ok
        if suavizado_m > 0:
            arr = _suavizar_nan(arr, suavizado_m / res)
        f = destino_dir / f"{nombre}_{idx}.tif"
        with rasterio.open(f, "w", **prof) as dso:
            dso.write(arr, 1)
        out.append((idx, f))
        etapa("productos", pct0 + (pct1 - pct0) * (j + 1) / len(indices))
    return out


def a_cog(src: Path, dst: Path, overview_resampling: str | None = None) -> None:
    """GeoTIFF -> COG vía gdal_translate (streaming: no carga el raster en RAM).

    ``overview_resampling`` pasa OVERVIEW_RESAMPLING al driver COG. Para el
    ortho RGB usar LANCZOS: +45-59 % de detalle medido en los zooms medios
    del visor, sin artefactos (validado con varianza de laplaciano).
    """
    if not src.exists():
        raise FileNotFoundError(f"falta el producto ODM esperado: {src}")
    subprocess.run(
        ["gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE"]
        + (["-co", f"OVERVIEW_RESAMPLING={overview_resampling}"] if overview_resampling else [])
        + [
         "-co", "BIGTIFF=IF_SAFER", str(src), str(dst)],
        check=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--misiones", nargs="+", required=True, type=Path,
                    help="dirs de misiones DJI con los crudos (TIF/JPG)")
    ap.add_argument("--productos", required=True,
                    help="lista separada por comas: " + ",".join(PRODUCTOS_VALIDOS))
    ap.add_argument("--salida", required=True, type=Path,
                    help=r"dir final (ej. G:\odm\<nombre>); el nombre del proyecto es su basename")
    ap.add_argument("--workdir", type=Path, default=WORKDIR_DEFAULT,
                    help=f"working dir NVMe (default {WORKDIR_DEFAULT})")
    ap.add_argument("--resolucion-cm", type=float, default=RESOLUCION_CM_DEFAULT,
                    help=f"resolución del orto en cm (default {RESOLUCION_CM_DEFAULT})")
    ap.add_argument("--suavizado-m", type=float, default=0.0,
                    help="sigma gaussiano en metros, solo índices (0 = apagado)")
    ap.add_argument("--gpu", action="store_true",
                    help="EXPERIMENTAL: opendronemap/odm:gpu + --gpus all (no validado en este repo)")
    ap.add_argument("--boundary", type=Path,
                    help="GeoJSON EPSG:4326 con ~40 m de buffer; sin él se usa --auto-boundary")
    ap.add_argument("--contenedor", help="nombre del contenedor docker (default m3m_odm_<nombre>)")
    ap.add_argument("--workers", type=int, default=8, help="workers de la calibración")
    ap.add_argument("--max-concurrency", type=int, default=max(2, (os.cpu_count() or 8) - 4),
                    help="hilos de ODM (default: cores del host - 4)")
    a = ap.parse_args()

    productos = [p.strip().lower() for p in a.productos.split(",") if p.strip()]
    if not productos:
        ap.error("--productos no puede estar vacío")
    invalidos = [p for p in productos if p not in PRODUCTOS_VALIDOS]
    if invalidos:
        ap.error(f"productos inválidos: {invalidos}; válidos: {', '.join(PRODUCTOS_VALIDOS)}")
    misiones = list(dict.fromkeys(a.misiones))  # dedupe preservando orden
    inexistentes = [str(m) for m in misiones if not m.is_dir()]
    if inexistentes:
        ap.error(f"misiones inexistentes: {inexistentes}")
    if a.boundary is not None and not a.boundary.is_file():
        ap.error(f"boundary inexistente: {a.boundary}")

    nombre = a.salida.name
    proy = a.workdir / nombre
    proy_rgb = a.workdir / f"{nombre}_rgb"
    contenedor = a.contenedor or f"m3m_odm_{nombre}"

    a.workdir.mkdir(parents=True, exist_ok=True)
    verificar_espacio(a.workdir)
    for d in (proy, proy_rgb):  # restos de un run anterior con el mismo nombre
        if d.exists():
            print(f"borrando working dir viejo {d}", flush=True)
            _rmtree(d)
    proy.mkdir(parents=True)

    tiempos: dict[str, float] = {}
    generados: list[tuple[str, Path]] = []

    # --- escaneo -------------------------------------------------------------
    t = time.monotonic()
    etapa("escaneo", 0)
    fotos, cuarentena = escanear_misiones(misiones, a.workdir / "cuarentena")
    etapa("escaneo", 100)
    tiempos["escaneo"] = round(time.monotonic() - t, 1)

    # --- calibracion ---------------------------------------------------------
    t = time.monotonic()
    etapa("calibracion", 0)
    k_file = calibrar(misiones, proy, a.salida, a.workers)
    preparar_proyecto(proy, a.boundary)
    etapa("calibracion", 100)
    tiempos["calibracion"] = round(time.monotonic() - t, 1)

    # --- odm -----------------------------------------------------------------
    t = time.monotonic()
    etapa("odm", 0)
    correr_odm(proy, contenedor, a.gpu,
               args_odm_ms(a.resolucion_cm, a.boundary is not None, a.max_concurrency), "odm")
    etapa("odm", 100)
    tiempos["odm"] = round(time.monotonic() - t, 1)

    # --- productos -----------------------------------------------------------
    t = time.monotonic()
    etapa("productos", 0)
    stage_dir = proy / "productos"
    stage_dir.mkdir(exist_ok=True)
    orto = proy / "odm_orthophoto" / "odm_orthophoto.tif"
    indices_pedidos = [p for p in productos if p in INDICES]
    if indices_pedidos:
        if not orto.exists():
            raise FileNotFoundError(f"ODM no dejó el orto esperado: {orto}")
        generados += generar_indices(orto, indices_pedidos, leer_k(k_file), a.suavizado_m,
                                     stage_dir, nombre)
    if "dsm" in productos:
        f = stage_dir / f"{nombre}_dsm.tif"
        a_cog(proy / "odm_dem" / "dsm.tif", f)
        generados.append(("dsm", f))
        etapa("productos", 80)
    if "orto" in productos:
        f = stage_dir / f"{nombre}_orto.tif"
        a_cog(orto, f)
        generados.append(("orto", f))
        etapa("productos", 90)
    if "nube" in productos:
        laz = proy / "odm_georeferencing" / "odm_georeferenced_model.laz"
        if not laz.exists():
            raise FileNotFoundError(f"ODM no dejó la nube esperada: {laz}")
        f = stage_dir / f"{nombre}_nube.laz"
        shutil.copy2(laz, f)
        generados.append(("nube", f))
    etapa("productos", 100)
    tiempos["productos"] = round(time.monotonic() - t, 1)

    # --- rgb (segundo run ODM con las JPG, aparte del multiespectral) --------
    hay_rgb = False
    if "rgb" in productos:
        jpgs = [f for m in misiones for f in sorted(m.glob("*_D.JPG"))]
        if not jpgs:
            print("rgb pedido pero no hay *_D.JPG en las misiones: se saltea", flush=True)
        else:
            t = time.monotonic()
            etapa("rgb", 0)
            img_rgb = proy_rgb / "images"
            img_rgb.mkdir(parents=True, exist_ok=True)
            print(f"copiando {len(jpgs)} JPG al proyecto rgb", flush=True)
            for f in jpgs:
                shutil.copy2(f, img_rgb / f.name)
            if a.boundary is not None:
                shutil.copy2(a.boundary, proy_rgb / "boundary.geojson")
            cameras_rgb_de(jpgs[0], proy_rgb / "cameras_fabrica.json")
            correr_odm(proy_rgb, contenedor, a.gpu,
                       args_odm_rgb(a.boundary is not None, a.max_concurrency), "rgb")
            f = stage_dir / f"{nombre}_rgb.tif"
            a_cog(proy_rgb / "odm_orthophoto" / "odm_orthophoto.tif", f, overview_resampling="LANCZOS")
            generados.append(("rgb", f))
            hay_rgb = True
            etapa("rgb", 100)
            tiempos["rgb"] = round(time.monotonic() - t, 1)

    # --- control de bloques + DSM oficial = bloque RGB ------------------------
    # El DSM del bloque RGB (geometría curada) es equivalente al del software
    # comercial de referencia y no sufre el hundimiento del multiespectral en
    # canopeo abierto: cuando hay rgb, pasa a ser el _dsm oficial y el MS
    # queda como _dsm_ms.
    control: dict | None = None
    dsm_ms = next((f for tipo, f in generados if tipo == "dsm"), None)
    dsm_rgb = proy_rgb / "odm_dem" / "dsm.tif"
    if hay_rgb and dsm_ms is not None and dsm_rgb.exists():
        t = time.monotonic()
        etapa("control", 0)
        dsm_ms_final = stage_dir / f"{nombre}_dsm_ms.tif"
        dsm_ms.rename(dsm_ms_final)
        dsm_oficial = stage_dir / f"{nombre}_dsm.tif"
        a_cog(dsm_rgb, dsm_oficial)
        generados[:] = [("dsm_ms", dsm_ms_final) if tipo == "dsm" else (tipo, f)
                        for tipo, f in generados]
        generados.append(("dsm", dsm_oficial))
        control = control_bloques(dsm_ms_final, dsm_oficial)
        print(f"control de bloques: {control['veredicto']} "
              f"({len(control['blobs'])} zonas de desacuerdo MS vs RGB; "
              f"el _dsm oficial es el del bloque RGB)", flush=True)
        etapa("control", 100)
        tiempos["control"] = round(time.monotonic() - t, 1)

    # --- movida --------------------------------------------------------------
    t = time.monotonic()
    etapa("movida", 0)
    a.salida.mkdir(parents=True, exist_ok=True)
    lista: list[dict] = []
    for i, (tipo, f) in enumerate(generados):
        destino = a.salida / f.name
        if destino.exists():
            destino.unlink()  # re-run del mismo proyecto: pisar el producto viejo
        shutil.move(str(f), str(destino))
        lista.append({"tipo": tipo, "archivo": destino.name, "bytes": destino.stat().st_size})
        etapa("movida", 100 * (i + 1) / len(generados))
    tiempos["movida"] = round(time.monotonic() - t, 1)
    # limpieza no entra en tiempos_por_etapa: el resumen se escribe antes (contrato de la etapa)
    resumen = {"productos": lista, "tiempos_por_etapa": tiempos, "fotos": fotos,
               "cuarentena": cuarentena}
    if control is not None:
        resumen["control_bloques"] = control
    (a.salida / "resumen.json").write_text(
        json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")
    etapa("movida", 100)

    # --- limpieza ------------------------------------------------------------
    etapa("limpieza", 0)
    _rmtree(proy)
    if hay_rgb:
        _rmtree(proy_rgb)
    etapa("limpieza", 100)
    print(f"OK pipeline completo -> {a.salida} ({len(lista)} productos)", flush=True)


if __name__ == "__main__":
    main()
