"""Pipeline SIMULADO para demo y test de la interfaz web (sin drone ni Docker).

Se activa arrancando el servidor con la variable de entorno ``M3M_SIMULACRO=1``
(el servidor corre este módulo en lugar de ``m3m.pipeline``). Acepta los
mismos flags que el pipeline real, emite el mismo protocolo de progreso
``##ETAPA## <slug> <pct>`` por stdout, tarda ~20 segundos y escribe en
``--salida`` GeoTIFFs sintéticos chicos (200x200, EPSG:32720) con rasterio,
más un ``resumen.json`` con el mismo contrato que el pipeline real.

Uso directo (mismos flags que m3m.pipeline)::

    python -m m3m.simulacro_pipeline --misiones <dir...> \\
        --productos ndvi,dsm --salida <dir> [--suavizado-m 0] [--gpu] ...
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .pipeline import INDICES, PRODUCTOS_VALIDOS, etapa

LADO = 200  # px de los rasters sintéticos
RES_M = 1.0  # m/px
ORIGEN = (439_000.0, 6_377_000.0)  # UTM 20S (EPSG:32720), zona Las Rosas-ish
CRS = "EPSG:32720"
BORDE_PX = 12  # anillo NaN alrededor, simula el recorte de borde del real


def _perfil(dtype: str, bandas: int, nodata=None) -> dict:
    from rasterio.transform import from_origin

    p = dict(driver="GTiff", height=LADO, width=LADO, count=bandas, dtype=dtype,
             crs=CRS, transform=from_origin(ORIGEN[0], ORIGEN[1], RES_M, RES_M),
             compress="deflate")
    if nodata is not None:
        p["nodata"] = nodata
    return p


def _mascara_valida():
    """True adentro del lote sintético (todo menos un anillo de BORDE_PX)."""
    import numpy as np

    m = np.zeros((LADO, LADO), bool)
    m[BORDE_PX:-BORDE_PX, BORDE_PX:-BORDE_PX] = True
    return m


def _escribir_indice(archivo: Path, semilla: int) -> None:
    """Índice sintético: gradiente + ondas, con un manchón bajo y borde NaN."""
    import numpy as np
    import rasterio

    yy, xx = np.mgrid[0:LADO, 0:LADO] / LADO
    arr = 0.25 + 0.55 * xx + 0.08 * np.sin(2 * np.pi * (3 * yy + 0.37 * semilla))
    # manchón de bajo vigor (posición distinta por índice)
    cx, cy = 0.3 + 0.1 * semilla, 0.6 - 0.08 * semilla
    arr -= 0.25 * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / 0.02)
    arr = np.clip(arr, -0.1, 0.95).astype(np.float32)
    arr[~_mascara_valida()] = np.nan
    with rasterio.open(archivo, "w", **_perfil("float32", 1, nodata=float("nan"))) as ds:
        ds.write(arr, 1)


def _escribir_dsm(archivo: Path) -> None:
    """DSM sintético: plano suave + dos lomas gaussianas."""
    import numpy as np
    import rasterio

    yy, xx = np.mgrid[0:LADO, 0:LADO] / LADO
    arr = (98.0 + 2.5 * xx + 1.5 * yy
           + 3.0 * np.exp(-((xx - 0.35) ** 2 + (yy - 0.4) ** 2) / 0.03)
           + 1.8 * np.exp(-((xx - 0.75) ** 2 + (yy - 0.7) ** 2) / 0.02)
           ).astype(np.float32)
    arr[~_mascara_valida()] = np.nan
    with rasterio.open(archivo, "w", **_perfil("float32", 1, nodata=float("nan"))) as ds:
        ds.write(arr, 1)


def _escribir_rgb(archivo: Path, semilla: int) -> None:
    """Orto RGB sintético uint8 de 4 bandas (RGB + alpha), verde con textura."""
    import numpy as np
    import rasterio

    rng = np.random.default_rng(semilla)
    yy, xx = np.mgrid[0:LADO, 0:LADO] / LADO
    verde = 90 + 90 * xx + 12 * np.sin(2 * np.pi * 8 * yy) + rng.normal(0, 6, (LADO, LADO))
    rojo = 0.55 * verde + rng.normal(0, 5, (LADO, LADO))
    azul = 0.45 * verde + rng.normal(0, 5, (LADO, LADO))
    valida = _mascara_valida()
    alpha = np.where(valida, 255, 0).astype(np.uint8)
    with rasterio.open(archivo, "w", **_perfil("uint8", 4)) as ds:
        for i, canal in enumerate((rojo, verde, azul), start=1):
            banda = np.clip(canal, 0, 255).astype(np.uint8)
            banda[~valida] = 0
            ds.write(banda, i)
        ds.write(alpha, 4)


def _correr_etapa(slug: str, segundos: float, pasos: int = 4) -> None:
    """Emite ##ETAPA## 0..100 durante `segundos`, como haría el pipeline real."""
    etapa(slug, 0)
    for i in range(1, pasos + 1):
        time.sleep(segundos / pasos)
        etapa(slug, 100 * i / pasos)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--misiones", nargs="+", required=True, type=Path)
    ap.add_argument("--productos", required=True)
    ap.add_argument("--salida", required=True, type=Path)
    # flags del pipeline real: se aceptan y se ignoran (el simulacro no los usa)
    ap.add_argument("--workdir", type=Path)
    ap.add_argument("--resolucion-cm", type=float)
    ap.add_argument("--suavizado-m", type=float, default=0.0)
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--boundary", type=Path)
    ap.add_argument("--contenedor")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--max-concurrency", type=int)
    a, _ = ap.parse_known_args()

    productos = [p.strip().lower() for p in a.productos.split(",") if p.strip()]
    invalidos = [p for p in productos if p not in PRODUCTOS_VALIDOS]
    if invalidos:
        ap.error(f"productos inválidos: {invalidos}; válidos: {', '.join(PRODUCTOS_VALIDOS)}")

    nombre = a.salida.name
    a.salida.mkdir(parents=True, exist_ok=True)
    print(f"SIMULACRO: {len(a.misiones)} misiones, productos {productos} -> {a.salida}",
          flush=True)

    tiempos: dict[str, float] = {}
    generados: list[tuple[str, Path]] = []

    t0 = time.monotonic()
    print("simulacro: escaneo EXIF de mentira", flush=True)
    _correr_etapa("escaneo", 2.0)
    tiempos["escaneo"] = round(time.monotonic() - t0, 1)

    t0 = time.monotonic()
    print("simulacro: calibración radiométrica de mentira", flush=True)
    _correr_etapa("calibracion", 3.0)
    tiempos["calibracion"] = round(time.monotonic() - t0, 1)

    t0 = time.monotonic()
    print("simulacro: ODM de mentira (acá el real tarda horas)", flush=True)
    _correr_etapa("odm", 8.0, pasos=8)
    tiempos["odm"] = round(time.monotonic() - t0, 1)

    t0 = time.monotonic()
    etapa("productos", 0)
    pedidos_idx = [p for p in productos if p in INDICES]
    for j, idx in enumerate(pedidos_idx):
        f = a.salida / f"{nombre}_{idx}.tif"
        print(f"simulacro: escribiendo {f.name}", flush=True)
        _escribir_indice(f, semilla=j)
        generados.append((idx, f))
        etapa("productos", 60 * (j + 1) / max(len(pedidos_idx), 1))
    if "dsm" in productos:
        f = a.salida / f"{nombre}_dsm.tif"
        print(f"simulacro: escribiendo {f.name}", flush=True)
        _escribir_dsm(f)
        generados.append(("dsm", f))
        etapa("productos", 80)
    if "orto" in productos:
        f = a.salida / f"{nombre}_orto.tif"
        print(f"simulacro: escribiendo {f.name}", flush=True)
        _escribir_rgb(f, semilla=7)
        generados.append(("orto", f))
        etapa("productos", 90)
    if "nube" in productos:
        f = a.salida / f"{nombre}_nube.laz"
        f.write_bytes(b"LASF" + b"\x00" * 128)  # stub: solo para que figure en la lista
        generados.append(("nube", f))
    time.sleep(1.0)
    etapa("productos", 100)
    tiempos["productos"] = round(time.monotonic() - t0, 1)

    if "rgb" in productos:
        t0 = time.monotonic()
        _correr_etapa("rgb", 2.0)
        f = a.salida / f"{nombre}_rgb.tif"
        print(f"simulacro: escribiendo {f.name}", flush=True)
        _escribir_rgb(f, semilla=11)
        generados.append(("rgb", f))
        tiempos["rgb"] = round(time.monotonic() - t0, 1)

    t0 = time.monotonic()
    etapa("movida", 0)
    lista = [{"tipo": tipo, "archivo": f.name, "bytes": f.stat().st_size}
             for tipo, f in generados]
    tiempos["movida"] = round(time.monotonic() - t0, 1)
    resumen = {"productos": lista, "tiempos_por_etapa": tiempos,
               "fotos": 123, "cuarentena": [], "simulacro": True}
    (a.salida / "resumen.json").write_text(
        json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")
    etapa("movida", 100)

    _correr_etapa("limpieza", 1.0, pasos=2)
    print(f"OK simulacro completo -> {a.salida} ({len(lista)} productos)", flush=True)


if __name__ == "__main__":
    main()
