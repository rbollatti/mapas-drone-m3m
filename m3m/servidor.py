"""Interfaz web LOCAL del pipeline M3M: ``python -m m3m.servidor``.

Levanta FastAPI en http://127.0.0.1:8600 (solo loopback, nunca expuesto a la
red) y abre el navegador. Desde la página (``m3m/web/index.html``) el usuario
lista sus misiones DJI_*, tilda productos, elige salida y lanza el pipeline
como subprocess — UN trabajo a la vez.

API (JSON, prefijo /api)
------------------------
- ``GET  /api/misiones?raiz=<path>`` — subcarpetas ``DJI_*`` de la raíz, con
  cantidad de fotos (TIF/JPG).
- ``POST /api/trabajo`` — lanza el pipeline; 409 si ya hay uno corriendo.
- ``GET  /api/trabajo`` — estado + etapa/pct (parseados de las líneas
  ``##ETAPA##`` del log), tail del log y productos al terminar.
- ``POST /api/trabajo/cancelar`` — mata el árbol de procesos y hace
  ``docker rm -f`` del contenedor del trabajo.
- ``GET  /api/vista?archivo=<path>`` — miniatura PNG de un GeoTIFF, SOLO si
  está dentro de la carpeta de salida del último trabajo.
- ``GET  /api/tiles/{z}/{x}/{y}.png?archivo=<rel>&tipo=<producto>`` — tile XYZ
  (WebMercator, 256 px) renderizado al vuelo desde el GeoTIFF con rio-tiler,
  para el visor Leaflet de la página. ``archivo`` es SIEMPRE relativo a la
  carpeta de salida del último trabajo (misma validación que /api/vista).
  Índices → paleta RdYlGn con escala fija por índice; dsm → paleta terrain
  con p2-p98; rgb → render directo. Cache LRU chico en memoria.
- ``GET  /api/tiles/bounds?archivo=<rel>`` — bounds del raster en EPSG:4326
  (``[oeste, sur, este, norte]``) para centrar el mapa.
- ``GET  /api/tiles/stats?archivo=<rel>`` — min/max/p2/p50/p98 por banda.

El estado se persiste en ``<repo>/trabajos/ultimo.json`` y el log del trabajo
en ``<repo>/trabajos/ultimo.log``: si el servidor se reinicia con un trabajo
corriendo, al arrancar re-engancha el PID vivo o lo marca como interrumpido.

Modo demo: con la variable de entorno ``M3M_SIMULACRO=1`` el servidor corre
``m3m.simulacro_pipeline`` (mismo protocolo, ~20 s, GeoTIFFs sintéticos) en
lugar del pipeline real — sirve para probar la interfaz sin drone ni Docker.
"""
from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import webbrowser
import zlib
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse, Response
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError as e:  # el pipeline CLI no necesita fastapi: solo avisa acá
    raise SystemExit(
        "Falta FastAPI para la interfaz web. Instalá con:\n"
        "    pip install fastapi uvicorn        (o: pip install -e .[ui])\n"
        "El pipeline por consola (python -m m3m.pipeline) no los necesita."
    ) from e

from .pipeline import INDICES, PRODUCTOS_VALIDOS

REPO = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
TRABAJOS_DIR = REPO / "trabajos"
ESTADO_FILE = TRABAJOS_DIR / "ultimo.json"
LOG_FILE = TRABAJOS_DIR / "ultimo.log"

HOST = "127.0.0.1"
PUERTO = 8600
EXT_FOTO = {".tif", ".tiff", ".jpg", ".jpeg"}
MINIATURA_PX = 1200  # lado máximo de la miniatura PNG

# --- visor de mapas (tiles dinámicos con rio-tiler) ---
TILE_PX = 256
# Escala fija de colores por índice (misma en la leyenda del visor): rangos
# agronómicos típicos para que dos vuelos sean comparables a simple vista.
ESCALA_FIJA: dict[str, tuple[float, float]] = {
    "ndvi": (0.15, 0.90),
    "gndvi": (0.15, 0.80),
    "ndre": (0.05, 0.45),
    "lci": (0.05, 0.55),
}
TIPOS_VISOR = ("ndvi", "gndvi", "ndre", "lci", "dsm", "rgb")

# Cache LRU de tiles renderizados (PNG): el pan/zoom del visor repite tiles y
# lo caro es el primer render. ~256 tiles × ~50-100 KB ≈ 25 MB máx.
_TILES_CACHE_MAX = 256
_tiles_cache: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
_tiles_lock = threading.Lock()

# Cache de estadísticas por (path, mtime): alimenta la escala del DSM (p2-p98)
# y el estirado de ortos float.
_STATS_CACHE_MAX = 32
_stats_cache: OrderedDict[tuple[str, int], dict[str, Any]] = OrderedDict()
_stats_lock = threading.Lock()

_RE_ETAPA = re.compile(rb"##ETAPA## ([a-z]+) (\d+)")

# Estado del ÚNICO trabajo (el último): mutar siempre bajo _lock y persistir.
_lock = threading.Lock()
_trabajo: dict = {"estado": "inactivo"}
_proc: subprocess.Popen | None = None


# ---------------------------------------------------------------- estado ----

def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _persistir() -> None:
    """Escribe _trabajo a trabajos/ultimo.json (llamar con _lock tomado)."""
    TRABAJOS_DIR.mkdir(exist_ok=True)
    ESTADO_FILE.write_text(
        json.dumps(_trabajo, indent=2, ensure_ascii=False), encoding="utf-8")


def _pid_vivo(pid: int) -> bool:
    """¿Existe el proceso? En Windows NO usar os.kill(pid, 0): mata el proceso."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            codigo = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(codigo)):
                return False
            return codigo.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _matar_arbol(pid: int) -> None:
    """Mata el proceso del pipeline y todos sus hijos (calibración, docker cli)."""
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    else:
        import signal

        try:  # el trabajo se lanza con start_new_session=True -> tiene su pgid
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _tail_log(max_bytes: int = 8000, max_lineas: int = 80) -> str:
    """Últimas líneas del log del trabajo (para mostrar en la página)."""
    try:
        with open(LOG_FILE, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            datos = fh.read()
    except OSError:
        return ""
    texto = datos.decode("utf-8", errors="replace")
    lineas = texto.splitlines()[-max_lineas:]
    return "\n".join(lineas)


def _etapa_del_log() -> tuple[str | None, int]:
    """Última línea ``##ETAPA## <slug> <pct>`` del log (progreso del trabajo)."""
    try:
        with open(LOG_FILE, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            datos = fh.read()
    except OSError:
        return None, 0
    hits = _RE_ETAPA.findall(datos)
    if not hits:
        return None, 0
    slug, pct = hits[-1]
    return slug.decode(), min(100, int(pct))


def _leer_productos(salida: str) -> list[dict]:
    """Lee resumen.json de la salida -> [{tipo, archivo(path absoluto)}]."""
    try:
        resumen = json.loads((Path(salida) / "resumen.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [{"tipo": p.get("tipo"), "archivo": str(Path(salida) / p.get("archivo", ""))}
            for p in resumen.get("productos", [])]


def _terminar(rc: int | None) -> None:
    """Cierra el estado del trabajo cuando el proceso terminó (rc None = sin código)."""
    with _lock:
        if _trabajo.get("estado") != "corriendo":  # ya cancelado desde la API
            return
        _trabajo["fin"] = _ahora()
        salida = _trabajo.get("salida", "")
        productos = _leer_productos(salida)
        if rc == 0 or (rc is None and productos):
            _trabajo["estado"] = "hecho"
            _trabajo["etapa"], _trabajo["pct"] = "limpieza", 100
            _trabajo["productos"] = productos
        else:
            _trabajo["estado"] = "error"
            _trabajo["error"] = (
                f"el pipeline terminó con código {rc} (ver log)" if rc is not None
                else "el pipeline terminó sin dejar resumen.json (ver log)")
        _persistir()


def _vigilar(proc: subprocess.Popen) -> None:
    """Thread que espera el fin del subprocess y cierra el estado."""
    rc = proc.wait()
    _terminar(rc)


def _revigilar(pid: int) -> None:
    """Tras un reinicio del servidor con el PID todavía vivo: sondear hasta que muera."""
    while _pid_vivo(pid):
        time.sleep(2)
        with _lock:
            if _trabajo.get("estado") != "corriendo":
                return
    _terminar(None)  # sin código de salida: decide por resumen.json


def _recuperar() -> None:
    """Al arrancar: levanta trabajos/ultimo.json y resuelve un 'corriendo' huérfano."""
    try:
        datos = json.loads(ESTADO_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    with _lock:
        _trabajo.clear()
        _trabajo.update(datos)
        if _trabajo.get("estado") != "corriendo":
            return
        pid = int(_trabajo.get("pid") or 0)
        if _pid_vivo(pid):
            threading.Thread(target=_revigilar, args=(pid,), daemon=True).start()
        else:
            _trabajo["estado"] = "error"
            _trabajo["error"] = ("interrumpido: el servidor se reinició y el proceso "
                                 "del pipeline ya no existe")
            _trabajo["fin"] = _ahora()
            _persistir()


# ------------------------------------------------------------- miniatura ----

# RdYlGn de ColorBrewer (11 clases), 0 = rojo (peor) .. 1 = verde (mejor)
_RDYLGN = [(165, 0, 38), (215, 48, 39), (244, 109, 67), (253, 174, 97),
           (254, 224, 139), (255, 255, 191), (217, 239, 139), (166, 217, 106),
           (102, 189, 99), (26, 152, 80), (0, 104, 55)]
_RDYLGN_POS = [i / 10 for i in range(11)]

# 'terrain' de matplotlib (anclas), para el DSM
_TERRAIN = [(51, 51, 153), (0, 153, 255), (0, 204, 102),
            (255, 255, 153), (128, 92, 84), (255, 255, 255)]
_TERRAIN_POS = [0.0, 0.15, 0.25, 0.5, 0.75, 1.0]


def _png_bytes(rgba) -> bytes:
    """PNG RGBA 8 bits con zlib de la stdlib (sin dependencia de imágenes)."""
    import numpy as np

    rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
    alto, ancho = rgba.shape[:2]
    crudo = b"".join(b"\x00" + rgba[i].tobytes() for i in range(alto))

    def chunk(tag: bytes, datos: bytes) -> bytes:
        return (struct.pack(">I", len(datos)) + tag + datos
                + struct.pack(">I", zlib.crc32(tag + datos) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(crudo, 6))
            + chunk(b"IEND", b""))


def _aplicar_cmap(norm, posiciones, colores):
    """norm en [0,1] con NaN = sin dato -> RGBA uint8 (alpha 0 donde NaN)."""
    import numpy as np

    valido = np.isfinite(norm)
    n = np.where(valido, norm, 0.0)
    rgba = np.zeros(norm.shape + (4,), np.uint8)
    for c in range(3):
        rgba[..., c] = np.clip(
            np.interp(n, posiciones, [col[c] for col in colores]), 0, 255
        ).astype(np.uint8)
    rgba[..., 3] = np.where(valido, 255, 0).astype(np.uint8)
    return rgba


def _normalizar(arr, valido):
    """Estira percentiles 2-98 de los píxeles válidos a [0,1]; NaN fuera."""
    import numpy as np

    salida = np.full(arr.shape, np.nan, np.float64)
    if not valido.any():
        return salida
    p2, p98 = np.percentile(arr[valido], [2, 98])
    salida[valido] = np.clip((arr[valido] - p2) / max(p98 - p2, 1e-9), 0, 1)
    return salida


def _miniatura(archivo: Path) -> bytes:
    """GeoTIFF -> PNG ~1200 px: RdYlGn para índices, terrain para dsm, RGB directo."""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling

    tipo = archivo.stem.rsplit("_", 1)[-1].lower()
    with rasterio.open(archivo) as ds:
        esc = max(1.0, max(ds.width, ds.height) / MINIATURA_PX)
        ancho, alto = max(1, int(ds.width / esc)), max(1, int(ds.height / esc))

        if tipo in INDICES or tipo == "dsm" or ds.count < 3:
            banda = ds.read(1, out_shape=(alto, ancho),
                            resampling=Resampling.nearest, masked=True)
            arr = banda.filled(np.nan).astype(np.float64)
            valido = np.isfinite(arr)
            norm = _normalizar(arr, valido)
            if tipo == "dsm":
                rgba = _aplicar_cmap(norm, _TERRAIN_POS, _TERRAIN)
            elif tipo in INDICES:
                rgba = _aplicar_cmap(norm, _RDYLGN_POS, _RDYLGN)
            else:  # banda única desconocida: escala de grises
                gris = np.where(valido, norm * 255, 0).astype(np.uint8)
                rgba = np.dstack([gris, gris, gris,
                                  np.where(valido, 255, 0).astype(np.uint8)])
        else:
            # orto/rgb: bandas 1-3 directas + alpha (última banda si es 4 o 5)
            datos = ds.read([1, 2, 3], out_shape=(3, alto, ancho),
                            resampling=Resampling.nearest).astype(np.float64)
            if ds.count in (4, 5):
                a = ds.read(ds.count, out_shape=(alto, ancho),
                            resampling=Resampling.nearest)
                valido = a > 0
            else:
                valido = np.ones((alto, ancho), bool)
            rgba = np.zeros((alto, ancho, 4), np.uint8)
            for c in range(3):
                norm = _normalizar(datos[c], valido)
                rgba[..., c] = np.where(valido, norm * 255, 0).astype(np.uint8)
            rgba[..., 3] = np.where(valido, 255, 0).astype(np.uint8)

    return _png_bytes(rgba)


# ------------------------------------------------------------------- API ----

@asynccontextmanager
async def _ciclo_vida(app: FastAPI):
    _recuperar()
    yield


app = FastAPI(title="m3m — interfaz web local", lifespan=_ciclo_vida)

# Estáticos de la página: Leaflet vendoreado en m3m/web/leaflet/ (licencia
# BSD-2, ver m3m/web/leaflet/LICENSE) — sin CDN, la página funciona offline.
app.mount("/web", StaticFiles(directory=str(WEB)), name="web")


class TrabajoNuevo(BaseModel):
    misiones: list[str]
    productos: list[str]
    salida: str
    suavizado_m: float = 0.0
    gpu: bool = False


@app.get("/")
def raiz():
    index = WEB / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html")
    return HTMLResponse(
        "<h1>m3m</h1><p>Falta <code>m3m/web/index.html</code>. La API igual "
        "está viva en <code>/api/trabajo</code>.</p>", status_code=200)


@app.get("/api/misiones")
def listar_misiones(raiz: str = ""):
    base = Path(raiz.strip()) if raiz.strip() else None
    if base is None or not base.is_dir():
        raise HTTPException(400, f"la carpeta raíz no existe: {raiz!r}")
    misiones = []
    for d in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not (d.is_dir() and d.name.upper().startswith("DJI_")):
            continue
        try:
            fotos = sum(1 for f in d.iterdir()
                        if f.is_file() and f.suffix.lower() in EXT_FOTO)
        except OSError:
            fotos = 0
        misiones.append({"dir": str(d), "nombre": d.name, "fotos": fotos})
    return {"misiones": misiones}


@app.post("/api/trabajo")
def crear_trabajo(t: TrabajoNuevo):
    global _proc
    misiones = [Path(m.strip()) for m in t.misiones if m.strip()]
    if not misiones:
        raise HTTPException(400, "elegí al menos una misión")
    faltan = [str(m) for m in misiones if not m.is_dir()]
    if faltan:
        raise HTTPException(400, f"misiones inexistentes: {faltan}")
    productos = [p.strip().lower() for p in t.productos if p.strip()]
    if not productos:
        raise HTTPException(400, "elegí al menos un producto")
    invalidos = [p for p in productos if p not in PRODUCTOS_VALIDOS]
    if invalidos:
        raise HTTPException(
            400, f"productos inválidos: {invalidos}; válidos: {', '.join(PRODUCTOS_VALIDOS)}")
    if not t.salida.strip():
        raise HTTPException(400, "falta la carpeta de salida")
    if t.suavizado_m < 0:
        raise HTTPException(400, "el suavizado no puede ser negativo")
    salida = Path(t.salida.strip()).resolve()

    simulacro = os.environ.get("M3M_SIMULACRO") == "1"
    modulo = "m3m.simulacro_pipeline" if simulacro else "m3m.pipeline"
    contenedor = f"m3m_web_{salida.name}"
    cmd = [sys.executable, "-u", "-m", modulo,
           "--misiones", *[str(m) for m in misiones],
           "--productos", ",".join(productos),
           "--salida", str(salida),
           "--contenedor", contenedor]
    if t.suavizado_m > 0:
        cmd += ["--suavizado-m", str(t.suavizado_m)]
    if t.gpu:
        cmd += ["--gpu"]

    with _lock:
        if _trabajo.get("estado") == "corriendo":
            raise HTTPException(409, "ya hay un trabajo corriendo; cancelalo o esperá a que termine")
        TRABAJOS_DIR.mkdir(exist_ok=True)
        entorno = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        # stdout del trabajo directo al archivo de log: sobrevive reinicios del servidor
        with open(LOG_FILE, "wb") as logf:
            logf.write(("$ " + " ".join(cmd) + "\n").encode("utf-8"))
            proc = subprocess.Popen(
                cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=str(REPO),
                env=entorno, start_new_session=(os.name != "nt"))
        _proc = proc
        _trabajo.clear()
        _trabajo.update({
            "estado": "corriendo", "etapa": None, "pct": 0,
            "inicio": _ahora(), "fin": None, "error": None,
            "pid": proc.pid, "contenedor": contenedor,
            "salida": str(salida), "productos": [], "simulacro": simulacro,
        })
        _persistir()
    threading.Thread(target=_vigilar, args=(proc,), daemon=True).start()
    return {"ok": True}


@app.get("/api/trabajo")
def ver_trabajo():
    with _lock:
        est = dict(_trabajo)
    if est.get("estado") == "corriendo":
        etapa, pct = _etapa_del_log()
        if etapa:
            est["etapa"], est["pct"] = etapa, pct
    resp = {clave: est.get(clave) for clave in
            ("estado", "etapa", "pct", "inicio", "fin", "error",
             "productos", "salida", "simulacro")}
    resp["productos"] = resp["productos"] or []
    resp["log_tail"] = "" if est.get("estado") == "inactivo" else _tail_log()
    return resp


@app.post("/api/trabajo/cancelar")
def cancelar_trabajo():
    with _lock:
        if _trabajo.get("estado") != "corriendo":
            raise HTTPException(409, "no hay ningún trabajo corriendo")
        pid = int(_trabajo.get("pid") or 0)
        contenedor = _trabajo.get("contenedor")
        _trabajo["estado"] = "cancelado"
        _trabajo["fin"] = _ahora()
        _persistir()
    _matar_arbol(pid)
    if contenedor:  # el contenedor docker no es hijo del pipeline: borrarlo aparte
        subprocess.run(["docker", "rm", "-f", contenedor], capture_output=True)
    return {"ok": True}


@app.get("/api/vista")
def vista(archivo: str):
    with _lock:
        salida = _trabajo.get("salida")
    if not salida:
        raise HTTPException(404, "todavía no hay ningún trabajo con salida conocida")
    try:
        f = Path(archivo).resolve(strict=True)
    except OSError:
        raise HTTPException(404, f"no existe: {archivo}")
    # SOLO archivos dentro de la salida del último trabajo (normcase: Windows
    # es case-insensitive y resolve() puede devolver otra capitalización)
    base = os.path.normcase(str(Path(salida).resolve()))
    objetivo = os.path.normcase(str(f))
    if not (objetivo == base or objetivo.startswith(base + os.sep)):
        raise HTTPException(403, "solo se sirven archivos de la carpeta de salida del último trabajo")
    if not f.is_file():
        raise HTTPException(404, f"no es un archivo: {archivo}")
    if f.suffix.lower() not in (".tif", ".tiff"):
        raise HTTPException(415, "solo hay miniatura para GeoTIFF (.tif)")
    try:
        png = _miniatura(f)
    except Exception as e:  # rasterio/numpy: error legible en vez de 500 pelado
        raise HTTPException(500, f"no pude renderizar {f.name}: {type(e).__name__}: {e}")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


# ------------------------------------------- visor de mapas (tiles XYZ) ----

def _resolver_en_salida(archivo: str) -> Path:
    """Path RELATIVO bajo la salida del último trabajo -> Path real validado.

    Misma política que /api/vista (solo archivos de esa carpeta), pero acá el
    cliente manda el path relativo: nada absoluto, nada con ``..``, solo .tif.
    """
    with _lock:
        salida = _trabajo.get("salida")
    if not salida:
        raise HTTPException(404, "todavía no hay ningún trabajo con salida conocida")
    archivo = (archivo or "").strip().replace("\\", "/")
    if not archivo:
        raise HTTPException(400, "falta el parámetro 'archivo'")
    p = Path(archivo)
    if p.is_absolute() or p.drive or ".." in p.parts:
        raise HTTPException(400, "'archivo' debe ser un path relativo a la carpeta "
                                 "de salida del último trabajo, sin '..'")
    if p.suffix.lower() not in (".tif", ".tiff"):
        raise HTTPException(415, "el visor solo sirve GeoTIFF (.tif)")
    base = Path(salida).resolve()
    real = (base / p).resolve()
    # normcase: Windows es case-insensitive y resolve() puede cambiar mayúsculas
    b, o = os.path.normcase(str(base)), os.path.normcase(str(real))
    if not (o == b or o.startswith(b + os.sep)):
        raise HTTPException(403, "solo se sirven archivos de la carpeta de salida del último trabajo")
    if not real.is_file():
        raise HTTPException(404, f"no existe {p.as_posix()} en la salida del último trabajo")
    return real


def _rio_tiler_disponible() -> None:
    """El visor necesita rio-tiler (extra [ui]); el resto del servidor no."""
    try:
        import rio_tiler  # noqa: F401
    except ImportError:
        raise HTTPException(
            500, "falta rio-tiler para el visor de mapas: pip install -e .[ui]")


def _tipo_visor(path: Path, tipo: str) -> str:
    """Valida ``tipo`` o lo infiere del sufijo del nombre (<nombre>_<tipo>.tif)."""
    t = (tipo or "").strip().lower()
    if t:
        if t not in TIPOS_VISOR:
            raise HTTPException(
                400, f"tipo '{tipo}' inválido; válidos: {', '.join(TIPOS_VISOR)}")
        return t
    t = path.stem.rsplit("_", 1)[-1].lower()
    if t == "orto":  # multibanda: se muestra como RGB con las bandas 1-3
        return "rgb"
    if t in TIPOS_VISOR:
        return t
    raise HTTPException(400, "no pude inferir el tipo de producto; pasá ?tipo=")


def _stats_de(path: Path) -> dict[str, Any]:
    """min/max/p2/p50/p98 por banda (lectura decimada vía overviews), cacheado
    por (path, mtime)."""
    clave = (str(path), int(path.stat().st_mtime))
    with _stats_lock:
        cached = _stats_cache.get(clave)
        if cached is not None:
            _stats_cache.move_to_end(clave)
            return cached

    from rio_tiler.io import Reader  # import pesado, adentro

    with Reader(str(path)) as r:
        st = r.statistics(percentiles=[2, 98])
    bandas = {
        b: {"min": round(float(s.min), 4), "max": round(float(s.max), 4),
            "p2": round(float(s.percentile_2), 4), "p50": round(float(s.median), 4),
            "p98": round(float(s.percentile_98), 4)}
        for b, s in st.items()
    }
    with _stats_lock:
        _stats_cache[clave] = bandas
        while len(_stats_cache) > _STATS_CACHE_MAX:
            _stats_cache.popitem(last=False)
    return bandas


def _escala_de(path: Path, tipo: str) -> tuple[float, float]:
    """Rango de colores: la escala fija del índice (comparable entre vuelos);
    para el DSM, p2-p98 del raster (el rango depende de cada terreno)."""
    if tipo in ESCALA_FIJA:
        return ESCALA_FIJA[tipo]
    b1 = _stats_de(path)["b1"]
    return float(b1["p2"]), float(b1["p98"])


def _render_tile(path: Path, tipo: str, z: int, x: int, y: int) -> bytes:
    """PNG 256 px del tile XYZ WebMercator ``z/x/y`` del GeoTIFF.

    - índices / DSM (1 banda): reescala a la escala del tipo y colorea
      (RdYlGn los índices, terrain el DSM); nodata queda transparente.
    - rgb / orto (>=3 bandas): bandas 1-3; uint8 directo, float estirado p2-p98.

    Deja propagar ``rio_tiler.errors.TileOutsideBounds`` (→ 404 en el endpoint).
    """
    clave = (str(path), int(path.stat().st_mtime), tipo, z, x, y)
    with _tiles_lock:
        png = _tiles_cache.get(clave)
        if png is not None:
            _tiles_cache.move_to_end(clave)
            return png

    from rio_tiler.colormap import cmap  # imports pesados, adentro
    from rio_tiler.io import Reader

    with Reader(str(path)) as r:
        multibanda = tipo == "rgb" and r.dataset.count >= 3
        img = r.tile(x, y, z, tilesize=TILE_PX,
                     indexes=(1, 2, 3) if multibanda else None,
                     resampling_method="nearest", reproject_method="nearest")
        if multibanda:
            if img.data.dtype != "uint8":  # orto float (reflectancia): estirar
                st = _stats_de(path)
                img.rescale(in_range=tuple(
                    (st[f"b{i}"]["p2"], st[f"b{i}"]["p98"]) for i in (1, 2, 3)))
            png = img.render(img_format="PNG")
        else:
            vmin, vmax = _escala_de(path, tipo)
            img.rescale(in_range=((vmin, vmax),))
            png = img.render(
                img_format="PNG",
                colormap=cmap.get("terrain" if tipo == "dsm" else "rdylgn"))

    with _tiles_lock:
        _tiles_cache[clave] = png
        while len(_tiles_cache) > _TILES_CACHE_MAX:
            _tiles_cache.popitem(last=False)
    return png


@app.get("/api/tiles/bounds")
def tiles_bounds(archivo: str = ""):
    f = _resolver_en_salida(archivo)
    import rasterio
    from rasterio.warp import transform_bounds

    with rasterio.open(f) as ds:
        if ds.crs is None:
            raise HTTPException(422, f"{f.name} no tiene CRS: no se puede ubicar en el mapa")
        oeste, sur, este, norte = transform_bounds(
            ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
    return {"archivo": archivo, "bounds": [oeste, sur, este, norte]}


@app.get("/api/tiles/stats")
def tiles_stats(archivo: str = "", tipo: str = ""):
    f = _resolver_en_salida(archivo)
    _rio_tiler_disponible()
    t = _tipo_visor(f, tipo)
    try:
        bandas = _stats_de(f)
        escala = None if t == "rgb" else list(_escala_de(f, t))
    except HTTPException:
        raise
    except Exception as e:  # error legible en vez de 500 pelado
        raise HTTPException(
            500, f"no pude calcular estadísticas de {f.name}: {type(e).__name__}: {e}")
    return {"archivo": archivo, "tipo": t, "bandas": bandas, "escala": escala}


@app.get("/api/tiles/{z}/{x}/{y}.png")
def tiles_png(z: int, x: int, y: int, archivo: str = "", tipo: str = ""):
    if not (0 <= z <= 24):
        raise HTTPException(400, "zoom fuera de rango (0-24)")
    f = _resolver_en_salida(archivo)
    _rio_tiler_disponible()
    t = _tipo_visor(f, tipo)

    from rio_tiler.errors import TileOutsideBounds

    try:
        png = _render_tile(f, t, z, x, y)
    except TileOutsideBounds:
        raise HTTPException(404, "tile fuera del área del raster")
    except HTTPException:
        raise
    except Exception as e:  # rio-tiler/rasterio: error legible en vez de 500 pelado
        raise HTTPException(
            500, f"no pude renderizar el tile de {f.name}: {type(e).__name__}: {e}")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ main ----

def main() -> None:
    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "Falta uvicorn para la interfaz web. Instalá con:\n"
            "    pip install fastapi uvicorn        (o: pip install -e .[ui])")
    url = f"http://{HOST}:{PUERTO}"
    print(f"Interfaz web m3m en {url}  (Ctrl+C para cortar)")
    if os.environ.get("M3M_SIMULACRO") == "1":
        print("MODO SIMULACRO activo (M3M_SIMULACRO=1): los trabajos corren "
              "m3m.simulacro_pipeline, ~20 s, con GeoTIFFs sintéticos.")
    navegador = threading.Timer(1.0, webbrowser.open, (url,))
    navegador.daemon = True
    navegador.start()
    uvicorn.run(app, host=HOST, port=PUERTO, log_level="warning")


if __name__ == "__main__":
    main()
