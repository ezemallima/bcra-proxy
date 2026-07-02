#!/usr/bin/env python3
"""
bcra_import_local.py — Importación de archivos bulk del BCRA.

Tablas generadas en bcra_nomdeu.db:
  denominaciones    — Nomdeu.txt       : CUIT → nombre oficial
  entidades         — Maeent.txt       : código banco → nombre
  deudas_resumen    — PADRON/DEUDORES  : situación actual por CUIT (snapshot mensual)
  historial_detalle — 24DSF            : sit+monto real por CUIT+entidad, N meses
                      (1 fila por par CUIT+entidad; columnas sit_01..sit_N, monto_01..monto_N
                       donde 01 = período más reciente. Montos guardados × 10 para
                       compatibilidad con main.py que los divide por 10.0 al leer.)

Detección automática de modo por nombre de archivo:
  *24DSF*            → historial_detalle + deudas_resumen
  *PADRON* / *DSF*   → deudas_resumen (snapshot actual)

Formatos soportados: .zip, .7z
  Para .7z en Google Colab ejecutar primero:
    !apt-get install -q p7zip-full

Uso:
    # 1. Importar 24 meses de historial — genera historial_detalle + sube a R2
    R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_ENDPOINT_URL=... R2_BUCKET_NAME=... \\
        python bcra_import_local.py 24DSF202605.7Z bcra_nomdeu.db

    # 2. Sin upload automático (omitir vars R2)
    python bcra_import_local.py 24DSF202605.7Z bcra_nomdeu.db

    # 3. Solo snapshot actual (padrón mensual)
    python bcra_import_local.py 20260531PADRON.7Z bcra_nomdeu.db
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

# Meses a guardar en historial_detalle.
# main.py lee hasta _HIST_DETALLE_MESES (actualmente 12); poner 24 guarda todo el 24DSF
# sin cambios en main.py (los meses 13-24 quedan en la DB para uso futuro).
N_MESES_HISTORIAL = 24

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
    nombre = Path(path).stem.upper()
    if "24DSF" in nombre:
        return "24dsf"
    return "padron"


def _extraer_a_tmpdir(path: str) -> str:
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
    candidatos = sorted(
        [f for f in Path(tmpdir).rglob("*") if f.is_file() and f.suffix.lower() == ".txt"],
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    for patron in patrones:
        for f in candidatos:
            if patron.lower() in f.name.lower():
                return str(f)
    if candidatos:
        _log(f"Patrón no encontrado — usando mayor: {candidatos[0].name}")
        return str(candidatos[0])
    return None


def _init_db(conn: sqlite3.Connection) -> None:
    """Crea las tablas base (historial_detalle se crea en _proc_24dsf con columnas dinámicas)."""
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
        CREATE INDEX IF NOT EXISTS idx_den_cuit ON denominaciones(cuit);
        CREATE INDEX IF NOT EXISTS idx_deu_cuit ON deudas_resumen(cuit);
    """)
    conn.commit()


# ─── procesadores ─────────────────────────────────────────────────────────────

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
    Parsea una línea del formato fijo BCRA.
    Retorna (entidad, periodo, cuit, sit, monto) o None si inválida.
    Posiciones 0-indexed:
      0-4   código entidad (5 chars)
      5-12  fecha YYYYMMDD (8 chars) → periodo = s[5:11]
      13-23 CUIT deudor   (11 chars)
      24-27 situación      (4 chars, 0-padded)
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


def _detectar_periodos(path: str, max_lineas: int = 300_000) -> list[str]:
    """
    Escanea las primeras max_lineas para encontrar todos los períodos YYYYMM.
    El 24DSF tiene ~24 períodos distintos, que aparecen en los primeros miles de líneas.
    Retorna lista ordenada descendente (más reciente primero).
    """
    periodos: set[str] = set()
    with open(path, "r", encoding="latin-1") as f:
        for i, linea in enumerate(f):
            if i >= max_lineas:
                break
            if len(linea) >= 11:
                p = linea[5:11]
                if p.isdigit() and len(p) == 6 and "200" <= p[:3] <= "202":
                    periodos.add(p)
    return sorted(periodos, reverse=True)


def _proc_padron(conn: sqlite3.Connection, path: str) -> None:
    """Importa PADRON/DEUDORES → deudas_resumen (snapshot mensual completo)."""
    _log("Truncando deudas_resumen para snapshot fresco...")
    conn.execute("DELETE FROM deudas_resumen")
    conn.commit()

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


def _proc_24dsf(conn: sqlite3.Connection, path: str, n_meses: int = N_MESES_HISTORIAL) -> None:
    """
    Importa 24DSF → historial_detalle.

    Una fila por (cuit, entidad) con columnas sit_01..sit_N y monto_01..monto_N:
      - sit_01 / monto_01 = período más reciente
      - Montos guardados como int(monto * 10) → main.py los divide por 10.0 al leer
      - None donde no hay dato para ese mes en esa entidad

    También completa deudas_resumen con CUITs que solo aparecen en 24DSF.
    """
    # 1. Detectar períodos disponibles (escaneo rápido de las primeras líneas)
    _log("Detectando períodos disponibles en 24DSF (escaneo rápido)...")
    todos_periodos = _detectar_periodos(path)
    if not todos_periodos:
        _log("ERROR: No se encontraron períodos válidos en el archivo")
        sys.exit(1)

    periodos_a_usar = todos_periodos[:n_meses]
    periodo_a_col   = {p: i + 1 for i, p in enumerate(periodos_a_usar)}
    periodos_set    = set(periodo_a_col)
    periodo_reciente = periodos_a_usar[0]

    _log(f"Períodos disponibles ({len(todos_periodos)}): {todos_periodos}")
    _log(f"Guardando {len(periodos_a_usar)} meses: {periodo_reciente} → {periodos_a_usar[-1]}")

    # 2. Crear/recrear historial_detalle con columnas para los N meses
    col_defs = ", ".join(
        f"sit_{i:02d} INTEGER, monto_{i:02d} INTEGER"
        for i in range(1, n_meses + 1)
    )
    conn.executescript(f"""
        DROP TABLE IF EXISTS historial_detalle;
        CREATE TABLE historial_detalle (
            cuit    TEXT NOT NULL,
            entidad TEXT NOT NULL,
            {col_defs},
            PRIMARY KEY (cuit, entidad)
        );
        CREATE INDEX IF NOT EXISTS idx_hist_det_cuit ON historial_detalle(cuit);
    """)
    conn.commit()

    # 3. Leer 24DSF y acumular en RAM: {{(cuit, entidad): {{col: [sit, monto_int]}}}}
    # Para deudas_resumen usamos solo el período más reciente (col=1).
    datos: dict         = {}
    resumen: dict       = {}   # {cuit: [sit_max, monto_total, ents_set, periodo]}
    count = skip = 0
    t0 = time.time()

    with open(path, "r", encoding="latin-1") as f:
        for linea in f:
            parsed = _parse_linea_deudor(linea)
            if not parsed:
                skip += 1
                continue
            entidad, periodo, cuit, sit, monto = parsed
            col = periodo_a_col.get(periodo)
            if col is None:
                skip += 1
                continue

            monto_int = int(monto * 10)
            key = (cuit, entidad)
            if key not in datos:
                datos[key] = {}
            entry = datos[key].get(col)
            if entry is None:
                datos[key][col] = [sit, monto_int]
            else:
                if sit > entry[0]:
                    entry[0] = sit
                if monto_int > entry[1]:
                    entry[1] = monto_int

            # Acumular resumen del período más reciente para deudas_resumen
            if col == 1:
                r = resumen.get(cuit)
                if r is None:
                    resumen[cuit] = [sit, monto, {entidad}, periodo_reciente]
                else:
                    if sit > r[0]:
                        r[0] = sit
                    r[1] += monto
                    r[2].add(entidad)

            count += 1
            if count % 5_000_000 == 0:
                elapsed = time.time() - t0
                _log(f"  {count / 1e6:.0f}M líneas | {len(datos):,} pares CUIT+entidad | {elapsed:.0f}s")

    elapsed = time.time() - t0
    _log(f"Streaming: {count:,} válidos | {skip:,} saltados | {elapsed:.0f}s")

    # 4. Volcar historial_detalle
    _log(f"Volcando {len(datos):,} filas → historial_detalle...")
    col_names   = "cuit, entidad, " + ", ".join(
        f"sit_{i:02d}, monto_{i:02d}" for i in range(1, n_meses + 1)
    )
    n_total_cols  = 2 + n_meses * 2
    placeholders  = ", ".join(["?"] * n_total_cols)
    sql_insert    = f"INSERT OR REPLACE INTO historial_detalle ({col_names}) VALUES ({placeholders})"

    batch = []
    for (cuit, entidad), cols_data in datos.items():
        row: list = [cuit, entidad]
        for i in range(1, n_meses + 1):
            v = cols_data.get(i)
            row.extend(v if v else [None, None])
        batch.append(tuple(row))
        if len(batch) >= 50_000:
            conn.executemany(sql_insert, batch)
            conn.commit()
            batch = []
    if batch:
        conn.executemany(sql_insert, batch)
        conn.commit()
    del datos

    n_hist = conn.execute("SELECT COUNT(*) FROM historial_detalle").fetchone()[0]
    _log(f"historial_detalle: {n_hist:,} filas (pares CUIT+entidad)")

    # 5. Completar deudas_resumen con CUITs del 24DSF no presentes en padrón
    _log(f"Completando deudas_resumen con {len(resumen):,} CUITs del período {periodo_reciente}...")
    batch = []
    for cuit, (sit_max, monto_total, ents, periodo) in resumen.items():
        batch.append((cuit, sit_max, round(monto_total, 1), ",".join(sorted(ents)), periodo))
        if len(batch) >= 50_000:
            conn.executemany(
                "INSERT OR IGNORE INTO deudas_resumen "
                "(cuit, sit_max, monto_total, entidades_cod, periodo) VALUES (?,?,?,?,?)",
                batch,
            )
            conn.commit()
            batch = []
    if batch:
        conn.executemany(
            "INSERT OR IGNORE INTO deudas_resumen "
            "(cuit, sit_max, monto_total, entidades_cod, periodo) VALUES (?,?,?,?,?)",
            batch,
        )
        conn.commit()
    del resumen
    _log("historial_detalle + deudas_resumen: completo")


# ─── upload R2 ────────────────────────────────────────────────────────────────

def _upload_r2(db_path: str) -> None:
    """
    Sube db_path a Cloudflare R2 como 'bcra_nomdeu.db'.
    Requiere variables de entorno: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
    R2_ENDPOINT_URL, R2_BUCKET_NAME.
    """
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_key  = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    endpoint    = os.environ.get("R2_ENDPOINT_URL", "").strip()
    bucket      = os.environ.get("R2_BUCKET_NAME", "").strip()

    if not all([access_key, secret_key, endpoint, bucket]):
        _log("R2 no configurado — omitiendo upload automático")
        _log("Para subir, exportar: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT_URL, R2_BUCKET_NAME")
        return

    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        _log("boto3 no instalado — ejecutar: pip install boto3")
        return

    size_mb = Path(db_path).stat().st_size / 1_048_576
    _log(f"Subiendo {db_path} → R2 bucket={bucket} ({size_mb:.0f} MB)...")

    s3 = boto3.client(
        service_name="s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
    )

    s3.upload_file(
        db_path, bucket, "bcra_nomdeu.db",
        ExtraArgs={"ContentType": "application/octet-stream"},
        Callback=lambda bytes_transferred: None,
    )
    _log(f"✓ Upload R2 completo: bcra_nomdeu.db ({size_mb:.0f} MB)")


# ─── entrada principal ─────────────────────────────────────────────────────────

def importar(zip_path: str, db_path: str) -> None:
    if not Path(zip_path).exists():
        _log(f"ERROR: no se encontró {zip_path}")
        sys.exit(1)

    modo = _detectar_modo(zip_path)
    _log(f"{'='*60}")
    _log(f"Archivo : {Path(zip_path).name}")
    _log(f"Base    : {db_path}")
    _log(f"Modo    : {modo.upper()} ({'historial_detalle ' + str(N_MESES_HISTORIAL) + 'm' if modo=='24dsf' else 'snapshot actual'})")
    _log(f"{'='*60}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous   = NORMAL")
    conn.execute("PRAGMA cache_size    = -131072")   # 128 MB caché escritura
    conn.execute("PRAGMA temp_store    = MEMORY")
    conn.execute("PRAGMA locking_mode  = EXCLUSIVE")

    _init_db(conn)

    tmpdir = _extraer_a_tmpdir(zip_path)
    try:
        nomdeu = _encontrar_archivo(tmpdir, ["nomdeu"])
        if nomdeu:
            _proc_nomdeu(conn, nomdeu)
        else:
            _log("Nomdeu.txt no encontrado — saltando denominaciones")

        maeent = _encontrar_archivo(tmpdir, ["maeent"])
        if maeent:
            _proc_maeent(conn, maeent)
        else:
            _log("Maeent.txt no encontrado — saltando entidades")

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
    _log(f"{'='*60}")

    _upload_r2(db_path)


if __name__ == "__main__":
    importar(ZIP_PATH, DB_PATH)
