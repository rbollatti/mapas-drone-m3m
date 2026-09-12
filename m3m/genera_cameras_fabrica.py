"""Genera ``cameras_fabrica.json`` desde una foto NIR del propio DJI Mavic 3M.

DJI graba en el XMP de cada foto la calibración geométrica de fábrica de la
lente (campo ``drone-dji:DewarpData``, formato
``fecha;fx,fy,cx,cy,k1,k2,p1,p2,k3``, valores en píxeles, cx/cy respecto del
centro de la imagen). Este script la extrae y la escribe en el formato de
cámaras de ODM/OpenSfM (modelo ``brown``), normalizando focal y centro por el
ancho del sensor multiespectral (2592 px): ``focal_x = fx/2592``,
``c_x = cx/2592``, etc. Los coeficientes de distorsión van tal cual.

La key del JSON es fija: ``"dji m3m 2592 1944 brown 0.8055"`` — 0.8055 es el
focal NOMINAL que declara el EXIF del modelo M3M, y es lo que ODM usa para
matchear la cámara del vuelo con esta entrada. OJO: la calibración es POR
UNIDAD de drone (cada M3M sale de fábrica con su propia DewarpData), por eso
cada usuario genera la suya desde una foto propia; ``cameras_fabrica.ejemplo.json``
del repo es solo ilustrativo.

Uso
---
    python -m m3m.genera_cameras_fabrica <foto_NIR.TIF> [--salida cameras_fabrica.json]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ANCHO = 2592  # sensor multiespectral del M3M (px); normalizador de OpenSfM
ALTO = 1944
KEY = f"dji m3m {ANCHO} {ALTO} brown 0.8055"  # 0.8055 = focal nominal EXIF del modelo


def leer_dewarp(path: Path) -> dict[str, float]:
    """Extrae DewarpData del XMP embebido (primeros 32 KB del TIF).

    Parameters
    ----------
    path : Path
        Foto multiespectral del M3M (idealmente la banda NIR).

    Returns
    -------
    dict
        fx, fy, cx, cy (píxeles), k1, k2, p1, p2, k3, y fecha de calibración.
    """
    with open(path, "rb") as fh:
        raw = fh.read(32768)
    i0 = raw.find(b"<x:xmpmeta")
    i1 = raw.find(b"</x:xmpmeta>")
    if i0 < 0 or i1 < 0:
        raise ValueError(f"{path.name}: no se encontró bloque XMP en los primeros 32 KB")
    xmp = raw[i0:i1].decode("utf-8", errors="replace")

    m = re.search(r'drone-dji:DewarpData="([^"]+)"', xmp)
    if not m:
        m = re.search(r"<drone-dji:DewarpData>([^<]+)</drone-dji:DewarpData>", xmp)
    if not m:
        raise ValueError(f"{path.name}: el XMP no tiene drone-dji:DewarpData")

    fecha, _, numeros = m.group(1).strip().partition(";")
    vals = [float(v) for v in numeros.split(",")]
    if len(vals) != 9:
        raise ValueError(f"DewarpData con {len(vals)} valores (se esperaban 9): {m.group(1)!r}")
    fx, fy, cx, cy, k1, k2, p1, p2, k3 = vals
    return dict(fecha=fecha, fx=fx, fy=fy, cx=cx, cy=cy, k1=k1, k2=k2, p1=p1, p2=p2, k3=k3)


def a_camara_odm(d: dict[str, float]) -> dict:
    """DewarpData (píxeles) -> entrada de cámara ODM/OpenSfM normalizada por el ancho."""
    return {
        "projection_type": "brown",
        "width": ANCHO,
        "height": ALTO,
        "focal_x": d["fx"] / ANCHO,
        "focal_y": d["fy"] / ANCHO,
        "c_x": d["cx"] / ANCHO,
        "c_y": d["cy"] / ANCHO,
        "k1": d["k1"],
        "k2": d["k2"],
        "p1": d["p1"],
        "p2": d["p2"],
        "k3": d["k3"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("foto", type=Path, help="una foto NIR (*.TIF) de TU drone")
    ap.add_argument("--salida", type=Path, default=Path("cameras_fabrica.json"),
                    help="JSON de salida (default: cameras_fabrica.json en el dir actual)")
    a = ap.parse_args()
    if not a.foto.is_file():
        ap.error(f"foto inexistente: {a.foto}")

    d = leer_dewarp(a.foto)
    print(f"DewarpData de {a.foto.name} (calibración de fábrica {d['fecha']}):")
    print(f"  fx={d['fx']:.3f} px  fy={d['fy']:.3f} px  cx={d['cx']:+.3f} px  cy={d['cy']:+.3f} px")
    print(f"  k1={d['k1']:.12g}  k2={d['k2']:.12g}  p1={d['p1']:.12g}  p2={d['p2']:.12g}  k3={d['k3']:.12g}")

    cam = a_camara_odm(d)
    a.salida.write_text(json.dumps({KEY: cam}, indent=2) + "\n", encoding="utf-8")
    print(f"OK -> {a.salida}  (key: \"{KEY}\")")
    print("Recordá: la calibración es POR UNIDAD de drone; este JSON sirve solo para el tuyo.")


if __name__ == "__main__":
    main()
