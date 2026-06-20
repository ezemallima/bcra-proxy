#!/usr/bin/env python3
"""
bcra_import_local.py — Importación de archivos bulk del BCRA.

Tablas generadas en bcra_nomdeu.db:
  denominaciones  — Nomdeu.txt      : CUIT → nombre oficial
  entidades       — Maeent.txt      : código banco → nombre
  deudas_resumen  — PADRON / DEUDORES : situación ACTUAL por CUIT (snapshot mensual)
  historial_bulk  — 24DSF            : evolución mes a mes 24 meses por CUIT+periodo

Detección automática de modo por nombre de archivo:
  *24DSF*   → historial_bulk  (también actualiza deudas_resumen si no existe)
  *PADRON* / *DEUDORES* / *DSF* → deudas_resumen (snapshot actual)

Formatos soportados: .zip, .7z
  Para .7z en Google Colab ejecutar primero:
    !apt-get install -q p7zip-full

Uso:
    # 1. Importar padrón actual (mayo 2026) — reemplaza deudas_resumen
    python bcra_import_local.py 20260531PADRON.7Z bcra_nomdeu.db

    # 2. Importar 24 meses de historial (abril 2026) — agrega historial_bulk
    python bcra_import_local.py 24DSF202604.7Z bcra_nomdeu.db

    # 3. Subir a Cloudflare R2
    # rclone copy bcra_nomdeu.db r2:tu-bucket/
"""

import os
import sys
import time
import sqlite3
import subprocess
import tempfile
import shutil
from pathlib import Path

if len(sys.argv) < 2:
    print("Uso: python bcra_import_local.py <archivo.[zip|7z]> [salida.db]")
    sys.exit(1)

ZIP_PATH = sys.argv[1]
DB_PATH  = sys.argv[2] if len(sys.argv) > 2 else "bcra_nomdeu.db"

_CUIT_PREFIJOS = {"20", "23", "24", "27", "30", "33", "34"}


# ─── helpers ──────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _monto(s: str) -> float:
    s = s.strip()
    if not s:
        return 0.0
    if s.startswith(","):
        s = "0" + s
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


def _upsert_batch(conn: sqlite3.Connection, tabla: str, batch: list, n_cols: int) -> None:
    placeholders = ",".join(["?"] * n_cols)
    conn.executemany(f"INSERT OR REPLACE INTO {tabla} VALUES ({placeholders})", batch)
    conn.commit()


def _detectar_modo(path: str) -> str:
    """Retorna '24dsf' o 'padron' según el nombre del archivo."""
    nombre = Path(path).stem.upper()
    if "24DSF" in nombre:
        return "24dsf"
    return "padron"


def _extraer_a_tmpdir(path: str) -> str:
    """Extrae el archivo comprimido a un directorio temporal y retorna su ruta."""
    tmpdir = tempfile.mkdtemp(prefix="bcra_import_")
    suffix = Path(path).suffix.lower()
    _log(f"Extrayendo {Path(path).name} ({Path(path).stat().st_size / 1e6:.0f} MB)...")

    if suffix == ".zip":
        import zipfile
        with zipfile.ZipFile(path, "r") as z:
            z.extractall(tmpdir)
    elif suffix == ".7z":
        result = subprocess.run(
            ["7z", "x", os.path.abspath(path), f"-o{tmpdir}", "-y"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            _log(f"ERROR 7z: {result.stderr.strip()}")
            _log("Tip Colab: !apt-get install -q p7zip-full")
            shutil.rmtree(tmpdir, ignore_errors=True)
            sys.exit(1)
    else:
        _log(f"ERROR: formato no soportado: {suffix}. Usar .zip o .7z")
        sys.exit(1)

    archivos = [f.name for f in Path(tmpdir).rglob("*") if f.is_file()]
    _log(f"Contenido extraído: {archivos}")
    return tmpdir


def _encontrar_archivo(tmpdir: str, patrones: list) -> str | None:
    """Busca el primer archivo .txt que contenga alguno de los patrones en su nombre."""
    candidatos = sorted(
        [f for f in Path(tmpdir).rglob("*") if f.is_file() and f.suffix.lower() == ".txt"],
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    for patron in patrones:
        for f in candidatos:
            if patron.lower() in f.name.lower():
                return str(f)
    # Fallback: el .txt más grande (probablemente el de deudores)
    if candidatos:
        _log(f"Patrón no encontrado — usando mayor: {candidatos[0].name}")
        return str(candidatos[0])
    return None


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS denominaciones (
            cuit   TEXT PRIMARY KEY,
            nombre TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entidades (
            codigo TEXT PRIMARY KEY,
            nombre TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deudas_resumen (
            cuit          TEXT PRIMARY KEY,
            sit_max       INTEGER NOT NULL,
            monto_total   REAL    NOT NULL,
            entidades_cod TEXT    NOT NULL,
            periodo       TEXT    NOT NULL
        );
        -- Resumen 24 meses del 24DSF: 1 fila por CUIT (no 24 filas).
        -- ~3M filas total — misma escala que deudas_resumen, descargable en Render.
        CREATE TABLE IF NOT EXISTS historial_bulk (
            cuit          TEXT    PRIMARY KEY,
            sit_max_24m   INTEGER NOT NULL,   -- peor sit en 24 meses
            meses_en_mora INTEGER NOT NULL,   -- meses con sit >= 3
            meses_critico INTEGER NOT NULL,   -- meses con sit >= 5 (irrecuperable)
            periodo_inicio TEXT    NOT NULL,  -- primer período con datos
            periodo_fin    TEXT    NOT NULL,  -- último período con datos
            monto_max      REAL    NOT NULL   -- monto máximo registrado
        );
        CREATE INDEX IF NOT EXISTS idx_den_cuit  ON denominaciones(cuit);
        CREATE INDEX IF NOT EXISTS idx_deu_cuit  ON deudas_resumen(cuit);
        CREATE INDEX IF NOT EXISTS idx_hist_cuit ON historial_bulk(cuit);
    """)
    conn.commit()


# ─── procesadores ──────────────────────────────────────────────────────────────

def _proc_nomdeu(conn: sqlite3.Connection, path: str) -> None:
    _log("Procesando Nomdeu.txt (denominaciones)...")
    count, batch = 0, []
    with open(path, "r", encoding="latin-1") as f:
        for linea in f:
            if len(linea) < 12:
                continue
            cuit   = linea[0:11]
            nombre = linea[11:].strip()
            if cuit.isdigit() and nombre:
                batch.append((cuit, nombre))
                count += 1
                if len(batch) >= 50_000:
                    _upsert_batch(conn, "denominaciones", batch, 2)
                    batch = []
    if batch:
        _upsert_batch(conn, "denominaciones", batch, 2)
    _log(f"Denominaciones: {count:,} registros")


def _proc_maeent(conn: sqlite3.Connection, path: str) -> None:
    _log("Procesando Maeent.txt (entidades)...")
    batch = []
    with open(path, "r", encoding="latin-1") as f:
        for linea in f:
            if len(linea) < 6:
                continue
            codigo = linea[0:5].strip()
            nombre = linea[5:].strip()
            if codigo and nombre:
                batch.append((codigo, nombre))
    _upsert_batch(conn, "entidades", batch, 2)
    _log(f"Entidades: {len(batch):,} registros")


def _parse_linea_deudor(linea: str) -> tuple | None:
    """
    Parsea una línea del formato fijo BCRA deudores/padrón.
    Retorna (entidad, periodo, cuit, sit, monto) o None si inválida.

    Formato (posiciones 0-indexed):
      0-4   código entidad    (5 chars)
      5-12  fecha YYYYMMDD    (8 chars) → periodo = s[5:11]
      13-23 CUIT deudor       (11 chars)
      24-27 situación         (4 chars, 0-padded)
      28+   10 columnas monto (12 chars c/u, decimal con coma)
    """
    if len(linea) < 30:
        return None
    entidad = linea[0:5]
    periodo = linea[5:11]
    if not periodo.isdigit():
        return None
    cuit = linea[13:24]
    if not (cuit.isdigit() and cuit[:2] in _CUIT_PREFIJOS):
        return None
    sit_s = linea[24:28].lstrip("0") or "0"
    if not sit_s.isdigit():
        return None
    sit = int(sit_s)
    if not (1 <= sit <= 6):
        return None
    monto = max(
        (_monto(linea[28 + i * 12 : 40 + i * 12])
         for i in range(10) if 40 + i * 12 <= len(linea)),
        default=0.0,
    )
    if monto <= 0:
        return None
    return entidad, periodo, cuit, sit, monto


def _proc_padron(conn: sqlite3.Connection, path: str) -> None:
    """
    Importa PADRON / DEUDORES → deudas_resumen.
    Trunca la tabla y reconstruye completamente (snapshot mensual).
    Agrega: sit_max y monto_total por CUIT a través de todas sus entidades.
    """
    _log("Truncando deudas_resumen para snapshot fresco...")
    conn.execute("DELETE FROM deudas_resumen")
    conn.commit()

    # Acumula en RAM: {cuit: [sit_max, monto_acum, {entidades}, periodo_max]}
    por_cuit: dict = {}
    count = skip = 0
    t0 = time.time()

    with open(path, "r", encoding="latin-1") as f:
        for linea in f:
            parsed = _parse_linea_deudor(linea)
            if not parsed:
                skip += 1
                continue
            entidad, periodo, cuit, sit, monto = parsed
            entry = por_cuit.get(cuit)
            if entry is None:
                por_cuit[cuit] = [sit, monto, {entidad}, periodo]
            else:
                if sit > entry[0]:
                    entry[0] = sit
                entry[1] += monto
                entry[2].add(entidad)
                if periodo > entry[3]:
                    entry[3] = periodo
            count += 1
            if count % 5_000_000 == 0:
                elapsed = time.time() - t0
                _log(f"  {count / 1e6:.0f}M líneas | {len(por_cuit):,} CUITs | {elapsed:.0f}s")

    elapsed = time.time() - t0
    _log(f"Streaming: {count:,} válidos | {skip:,} saltados | {elapsed:.0f}s")
    _log(f"Volcando {len(por_cuit):,} CUITs → deudas_resumen...")

    batch = []
    for cuit, (sit_max, monto_total, ents, periodo) in por_cuit.items():
        batch.append((cuit, sit_max, round(monto_total, 1), ",".join(sorted(ents)), periodo))
        if len(batch) >= 50_000:
            _upsert_batch(conn, "deudas_resumen", batch, 5)
            batch = []
    if batch:
        _upsert_batch(conn, "deudas_resumen", batch, 5)
    del por_cuit
    _log("deudas_resumen: completo")


def _proc_24dsf(conn: sqlite3.Connection, path: str) -> None:
    """
    Importa 24DSF → historial_bulk (1 fila por CUIT, resumen de 24 meses).

    Agrega en RAM: sit_max, meses_en_mora, meses_critico, periodo_inicio/fin, monto_max.
    ~3M entradas en el dict ≈ 600 MB RAM — manejable en Colab (12 GB disponibles).
    La DB resultante tiene la misma escala que deudas_resumen: ~150-250 MB en SQLite.

    NO almacena 24 filas por CUIT (hubiera sido 72M filas / ~4 GB en Render).
    """
    _log("Preparando historial_bulk (resumen 24 meses, 1 fila por CUIT)...")
    conn.execute("DELETE FROM historial_bulk")
    conn.commit()

    # {cuit: [sit_max, meses_mora, meses_critico, periodo_min, periodo_max, monto_max, periodos_set]}
    # periodos_set: para contar meses únicos con mora (evitar doble-contar por entidad)
    por_cuit: dict = {}
    count = skip = 0
    t0 = time.time()

    with open(path, "r", encoding="latin-1") as f:
        for linea in f:
            parsed = _parse_linea_deudor(linea)
            if not parsed:
                skip += 1
                continue
            _, periodo, cuit, sit, monto = parsed

            entry = por_cuit.get(cuit)
            if entry is None:
                por_cuit[cuit] = [
                    sit,                        # sit_max
                    set(),                      # periodos_mora (sit >= 3)
                    set(),                      # periodos_critico (sit >= 5)
                    periodo,                    # periodo_min
                    periodo,                    # periodo_max
                    monto,                      # monto_max
                ]
            else:
                if sit > entry[0]:
                    entry[0] = sit
                if sit >= 3:
                    entry[1].add(periodo)
                if sit >= 5:
                    entry[2].add(periodo)
                if periodo < entry[3]:
                    entry[3] = periodo
                if periodo > entry[4]:
                    entry[4] = periodo
                if monto > entry[5]:
                    entry[5] = monto

            # Registrar período mora/crítico en la entrada inicial también
            if entry is None:
                entry = por_cuit[cuit]
                if sit >= 3:
                    entry[1].add(periodo)
                if sit >= 5:
                    entry[2].add(periodo)

            count += 1
            if count % 5_000_000 == 0:
                elapsed = time.time() - t0
                _log(f"  {count / 1e6:.0f}M líneas | {len(por_cuit):,} CUITs | {elapsed:.0f}s")

    elapsed = time.time() - t0
    _log(f"Streaming: {count:,} válidos | {skip:,} saltados | {elapsed:.0f}s")
    _log(f"Volcando {len(por_cuit):,} CUITs → historial_bulk...")

    batch = []
    for cuit, (sit_max, periodos_mora, periodos_crit, p_ini, p_fin, monto_max) in por_cuit.items():
        batch.append((
            cuit,
            sit_max,
            len(periodos_mora),   # meses_en_mora
            len(periodos_crit),   # meses_critico
            p_ini,
            p_fin,
            round(monto_max, 1),
        ))
        if len(batch) >= 50_000:
            _upsert_batch(conn, "historial_bulk", batch, 7)
            batch = []
    if batch:
        _upsert_batch(conn, "historial_bulk", batch, 7)
    del por_cuit

    n_hist = conn.execute("SELECT COUNT(*) FROM historial_bulk").fetchone()[0]
    _log(f"historial_bulk: {n_hist:,} CUITs")

    # Completar deudas_resumen con CUITs que solo aparecen en 24DSF (no en padrón actual)
    _log("Completando deudas_resumen con CUITs del 24DSF no presentes en padrón...")
    conn.execute("""
        INSERT OR IGNORE INTO deudas_resumen (cuit, sit_max, monto_total, entidades_cod, periodo)
        SELECT cuit, sit_max_24m, monto_max, '24DSF', periodo_fin
        FROM historial_bulk
    """)
    conn.commit()
    _log("historial_bulk + deudas_resumen: completo")


# ─── entrada principal ────────────────────────────────────────────────────────

def importar(zip_path: str, db_path: str) -> None:
    if not Path(zip_path).exists():
        _log(f"ERROR: no se encontró {zip_path}")
        sys.exit(1)

    modo = _detectar_modo(zip_path)
    _log(f"{'='*60}")
    _log(f"Archivo : {Path(zip_path).name}")
    _log(f"Base    : {db_path}")
    _log(f"Modo    : {modo.upper()} ({'historial 24 meses' if modo=='24dsf' else 'snapshot actual'})")
    _log(f"{'='*60}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous   = NORMAL")
    conn.execute("PRAGMA cache_size    = -131072")   # 128 MB caché
    conn.execute("PRAGMA temp_store    = MEMORY")
    conn.execute("PRAGMA locking_mode  = EXCLUSIVE")

    _init_db(conn)

    tmpdir = _extraer_a_tmpdir(zip_path)
    try:
        # Nomdeu.txt — denominaciones (presente en ambos tipos de archivo)
        nomdeu = _encontrar_archivo(tmpdir, ["nomdeu"])
        if nomdeu:
            _proc_nomdeu(conn, nomdeu)
        else:
            _log("Nomdeu.txt no encontrado — saltando denominaciones")

        # Maeent.txt — entidades
        maeent = _encontrar_archivo(tmpdir, ["maeent"])
        if maeent:
            _proc_maeent(conn, maeent)
        else:
            _log("Maeent.txt no encontrado — saltando entidades")

        # Archivo principal de deudores
        deudores = _encontrar_archivo(tmpdir, ["deudor", "padron", "dsf", "1dsf", "24dsf"])
        if not deudores:
            _log("ERROR: No se encontró el archivo de deudores dentro del comprimido")
            sys.exit(1)

        _log(f"Archivo de deudores: {Path(deudores).name}")
        if modo == "24dsf":
            _proc_24dsf(conn, deudores)
        else:
            _proc_padron(conn, deudores)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    _log("VACUUM (compactando DB final)...")
    conn.execute("VACUUM")
    conn.close()

    size_mb = Path(db_path).stat().st_size / 1_048_576
    _log(f"{'='*60}")
    _log(f"✓ Listo: {db_path} ({size_mb:.1f} MB)")
    _log(f"Próximo paso: subir a Cloudflare R2")
    _log(f"  rclone copy {db_path} r2:tu-bucket/")
    _log(f"{'='*60}")


if __name__ == "__main__":
    importar(ZIP_PATH, DB_PATH)
