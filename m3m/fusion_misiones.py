"""Fusión armonizada de ortomosaicos ODM procesados por misión (método alternativo).

Cuando cada misión de un vuelo largo se procesa por separado en ODM (con
``--radiometric-calibration camera``: iluminación constante dentro de la
misión), este script fusiona los NDRE en un mosaico único del lote:

1. Recorta ``--recorte-m`` metros del borde de cada orto (los bordes tienen
   NDRE inflado por geometría de vista; este paso subió r de 0,64 a 0,85 en
   la validación contra Terra).
2. Armoniza por offset de mediana en el solape contra la primera misión
   (offsets típicos <0,003: el cociente NDRE cancela casi toda la deriva de
   sol entre misiones; NO usar ajuste lineal con pendiente, los solapes de
   borde dan ajustes basura).
3. Fusiona con feathering (peso = distancia al borde válido de cada misión).

Las bandas del orto multibanda de ODM son: 1=Red, 2=Green, 3=NIR, 4=RedEdge,
5=alpha. Para ortos de imágenes pre-calibradas con brillo por banda, pasar
``--k-nir/--k-re`` (de ``k_bandas.json``) para des-escalar antes del índice.

Uso
---
    python -m m3m.fusion_misiones --ortos m025.tif m027.tif ... --salida ndre.tif
"""
from __future__ import annotations

import argparse

import numpy as np
import rasterio
from affine import Affine
from rasterio.warp import Resampling, reproject
from scipy.ndimage import binary_erosion, distance_transform_edt


def cargar_ndre(path: str, res: float, k_nir: float, k_re: float):
    """Lee un orto ODM y devuelve (ndre, transform, crs) a resolución ``res``."""
    with rasterio.open(path) as ds:
        f = max(1, int(round(res / ds.res[0])))
        oh, ow = ds.height // f, ds.width // f
        nir = ds.read(3, out_shape=(oh, ow)).astype(np.float32) / k_nir
        re = ds.read(4, out_shape=(oh, ow)).astype(np.float32) / k_re
        al = ds.read(5, out_shape=(oh, ow)).astype(np.float32)
        tr = ds.transform * Affine.scale(ds.width / ow, ds.height / oh)
        crs = ds.crs
    ok = (al > 0) & ((nir + re) > 1e-6)
    ndre = np.where(ok, (nir - re) / np.maximum(nir + re, 1e-9), np.nan)
    return ndre, tr, crs


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ortos", nargs="+", required=True, help="ortos ODM por misión, en orden de vuelo")
    ap.add_argument("--salida", required=True)
    ap.add_argument("--res", type=float, default=0.5, help="resolución del mosaico (m)")
    ap.add_argument("--recorte-m", type=float, default=20.0, help="borde a descartar por misión (m)")
    ap.add_argument("--k-nir", type=float, default=1.0)
    ap.add_argument("--k-re", type=float, default=1.0)
    a = ap.parse_args()

    datos = [cargar_ndre(p, a.res, a.k_nir, a.k_re) for p in a.ortos]
    crs = datos[0][2]

    xs0 = min(tr.c for _, tr, _ in datos)
    ys1 = max(tr.f for _, tr, _ in datos)
    xs1 = max(tr.c + tr.a * nd.shape[1] for nd, tr, _ in datos)
    ys0 = min(tr.f + tr.e * nd.shape[0] for nd, tr, _ in datos)
    ancho, alto = int((xs1 - xs0) / a.res), int((ys1 - ys0) / a.res)
    tr_out = Affine(a.res, 0, xs0, 0, -a.res, ys1)

    capas = []
    for nd, tr, _ in datos:
        dst = np.full((alto, ancho), np.nan, dtype=np.float32)
        reproject(nd, dst, src_transform=tr, src_crs=crs, dst_transform=tr_out, dst_crs=crs,
                  resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan)
        capas.append(dst)

    it = int(a.recorte_m / a.res)
    for i, c in enumerate(capas):
        v = binary_erosion(np.isfinite(c), iterations=it)
        capas[i] = np.where(v, c, np.nan)

    ref = capas[0]
    for i, c in enumerate(capas[1:], 1):
        ov = np.isfinite(ref) & np.isfinite(c)
        b = float(np.nanmedian(ref[ov]) - np.nanmedian(c[ov])) if ov.sum() > 5000 else 0.0
        capas[i] = c + b
        print(f"orto {i}: solape {ov.sum()} px, offset {b:+.4f}")

    num = np.zeros((alto, ancho))
    den = np.zeros((alto, ancho))
    for c in capas:
        v = np.isfinite(c)
        w = np.clip(distance_transform_edt(v), 0, 100 / a.res)
        num += np.where(v, c * w, 0.0)
        den += np.where(v, w, 0.0)
    mosaico = np.where(den > 0, num / np.maximum(den, 1e-9), np.nan).astype(np.float32)

    prof = dict(driver="GTiff", height=alto, width=ancho, count=1, dtype="float32",
                crs=crs, transform=tr_out, nodata=np.nan, compress="deflate")
    with rasterio.open(a.salida, "w", **prof) as ds:
        ds.write(mosaico, 1)
    print(f"OK {a.salida} · válidos {np.isfinite(mosaico).mean()*100:.0f}%")


if __name__ == "__main__":
    main()
