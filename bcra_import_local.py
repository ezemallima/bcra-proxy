#!/usr/bin/env python3
"""
bcra_import_local.py — Importación mensual del ZIP de deudores BCRA.

Genera bcra_nomdeu.db listo para subir a Cloudflare R2. Render lo descarga
al arrancar y lo usa como fuente offline para denominaciones y deuda.

Tablas generadas:
  denominaciones  — Nomdeu.txt  : CUIT → nombre oficial (13.9 MB → ~50 MB SQLite)
  entidades       — Maeent.txt  : código banco → nombre del banco
  deudas_resumen  — deudores.txt: CUIT → situacion máxima, monto total, periodo
                    (6.7 GB streaming, agrupado en RAM por CUIT, ~150 MB SQLite)

Uso:
    python bcra_import_local.py /ruta/al/archivo.zip [salida.db]

Requisitos:
    pip install tqdm   (opcional, para barra de progreso)
"""

import zipfile
import sqlite3
import os
import sys
import time
from pathlib import Path

ZIP_PATH = sys.argv[1] if len(sys.argv) > 1 else "deudores_bcra.zip"
DB_PATH  = sys.argv[2] if len(sys.argv) > 2 else "bcra_nomdeu.db"

# Prefijos CUIT válidos en Argentina
_CUIT_PREFIJOS = {"20", "23", "24", "27", "30", "33", "34"}


# ─── helpers ─────────────────────────────────────────────────────────────────

def _monto(s: str) -> float:
    """'1.234,56' → 1234.56  |  ',0' → 0.0  |  '' → 0.0"""
    s = s.strip()
    if not s:
        return 0.0
    if s.startswith(","):
        s = "0" + s
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _upsert_batch(conn, tabla, batch, n_cols):
    placeholders = ",".join(["?"] * n_cols)
    conn.executemany(
        f"INSERT OR REPLACE INTO {tabla} VALUES ({placeholders})", batch
    )
    conn.commit()


# ─── import principal ────────────────────────────────────────────────────────

def importar(zip_path: str, db_path: str) -> None:

    if not Path(zip_path).exists():
        _log(f"ERROR: no se encontró {zip_path}")
        sys.exit(1)

    _log(f"Abriendo {zip_path} ({Path(zip_path).stat().st_size / 1e6:.0f} MB)...")

    conn = sqlite3.connect(db_path)
    # Pragmas para escritura masiva eficiente
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous   = NORMAL")
    conn.execute("PRAGMA cache_size    = -131072")   # 128 MB en caché
    conn.execute("PRAGMA temp_store    = MEMORY")
    conn.execute("PRAGMA locking_mode  = EXCLUSIVE")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS denominaciones (
            cuit   TEXT PRIMARY KEY,
            nombre TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entidades (
            codigo TEXT PRIMARY KEY,
            nombre TEXT NOT NULL
        );

        -- Resumen por CUIT: peor situación, monto total acumulado,
        -- códigos de entidades acreedoras y período más reciente.
        CREATE TABLE IF NOT EXISTS deudas_resumen (
            cuit          TEXT    PRIMARY KEY,
            sit_max       INTEGER NOT NULL,
            monto_total   REAL    NOT NULL,
            entidades_cod TEXT    NOT NULL,
            periodo       TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_den_cuit ON denominaciones(cuit);
        CREATE INDEX IF NOT EXISTS idx_deu_cuit ON deudas_resumen(cuit);
    """)
    conn.commit()

    with zipfile.ZipFile(zip_path, "r") as z:
        nombres_zip = {i.filename for i in z.infolist()}

        # ── 1. Maeent.txt — maestro de entidades financieras ─────────────────
        # Formato: código (5) + nombre (resto, strip)
        if "Maeent.txt" in nombres_zip:
            _log("Procesando Maeent.txt...")
            batch = []
            with z.open("Maeent.txt") as f:
                for linea in f:
                    s = linea.decode("latin-1")
                    if len(s) < 6:
                        continue
                    codigo = s[0:5].strip()
                    nombre = s[5:].strip()
                    if codigo and nombre:
                        batch.append((codigo, nombre))
            _upsert_batch(conn, "entidades", batch, 2)
            _log(f"Entidades: {len(batch)} registros")

        # ── 2. Nomdeu.txt — denominaciones oficiales ─────────────────────────
        # Formato: CUIT (pos 0-10, 11 chars fijos) + nombre (pos 11+, strip)
        if "Nomdeu.txt" in nombres_zip:
            _log("Procesando Nomdeu.txt...")
            count, batch = 0, []
            with z.open("Nomdeu.txt") as f:
                for linea in f:
                    s = linea.decode("latin-1")
                    if len(s) < 12:
                        continue
                    cuit   = s[0:11]          # 11 chars exactos, ya sin dashes
                    nombre = s[11:].strip()
                    if cuit.isdigit() and nombre:
                        batch.append((cuit, nombre))
                        count += 1
                        if len(batch) >= 50_000:
                            _upsert_batch(conn, "denominaciones", batch, 2)
                            batch = []
            if batch:
                _upsert_batch(conn, "denominaciones", batch, 2)
            _log(f"Denominaciones: {count:,} registros")

        # ── 3. deudores.txt — streaming 6.7 GB, agregado en RAM por CUIT ─────
        #
        # Formato de cada línea (ancho fijo, posiciones 0-indexed):
        #   0-4   : código entidad financiera    (5 chars)
        #   5-12  : fecha proceso YYYYMMDD        (8 chars) → periodo = s[5:11]
        #   13-23 : CUIT deudor                  (11 chars)
        #   24-27 : situación 0-padded (1-6)     (4 chars)
        #   28+   : 10 columnas de monto         (12 chars c/u, decimal con coma)
        #
        # ⚠ Verificar posiciones contra LEAME DEUDORES.pdf si los datos no
        #   coinciden. Los campos se validaron contra 3 líneas de muestra.
        #
        # Estrategia de memoria: agrupamos en RAM por CUIT (no línea a línea),
        # mantenemos solo [sit_max, monto_total, set(entidades), periodo_max].
        # Con ~3M CUITs únicos el dict ocupa ~250 MB — seguro para 4 GB RAM.
        if "deudores.txt" in nombres_zip:
            _log("Procesando deudores.txt en streaming (5-15 min según hardware)...")
            # por_cuit[cuit] = [sit_max, monto_acum, {entidades}, periodo_max]
            por_cuit: dict = {}
            count = skip = 0
            t0 = time.time()

            with z.open("deudores.txt") as f:
                for linea in f:
                    try:
                        s = linea.decode("latin-1")
                        if len(s) < 30:
                            skip += 1
                            continue

                        entidad = s[0:5]
                        periodo = s[5:11]
                        if not periodo.isdigit():
                            skip += 1
                            continue

                        cuit = s[13:24]
                        if not (cuit.isdigit() and cuit[:2] in _CUIT_PREFIJOS):
                            skip += 1
                            continue

                        sit_s = s[24:28].lstrip("0") or "0"
                        if not sit_s.isdigit():
                            skip += 1
                            continue
                        sit = int(sit_s)
                        if not (1 <= sit <= 6):
                            skip += 1
                            continue

                        # Monto: máximo de las 10 columnas de 12 chars
                        # (algunas columnas son subtotales — el máximo es el total)
                        monto = max(
                            (_monto(s[28 + i * 12 : 40 + i * 12])
                             for i in range(10) if 40 + i * 12 <= len(s)),
                            default=0.0,
                        )
                        if monto <= 0:
                            skip += 1
                            continue

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
                            _log(
                                f"  {count / 1e6:.0f}M líneas | "
                                f"{len(por_cuit):,} CUITs únicos | "
                                f"{elapsed:.0f}s"
                            )

                    except Exception:
                        skip += 1

            elapsed = time.time() - t0
            _log(
                f"Streaming completo: {count:,} válidos | "
                f"{skip:,} saltados | {elapsed:.0f}s"
            )
            _log(f"Volcando {len(por_cuit):,} CUITs únicos a SQLite...")

            batch = []
            for cuit, (sit_max, monto_total, ents, periodo) in por_cuit.items():
                batch.append((
                    cuit,
                    sit_max,
                    round(monto_total, 1),
                    ",".join(sorted(ents)),
                    periodo,
                ))
                if len(batch) >= 50_000:
                    _upsert_batch(conn, "deudas_resumen", batch, 5)
                    batch = []
            if batch:
                _upsert_batch(conn, "deudas_resumen", batch, 5)
            del por_cuit  # liberar RAM

            _log("deudas_resumen: volcado completo")

    _log("VACUUM (compactando archivo final)...")
    conn.execute("VACUUM")
    conn.close()

    size_mb = Path(db_path).stat().st_size / 1_048_576
    _log(f"✓ Listo: {db_path} ({size_mb:.1f} MB)")
    _log("Próximo paso: subir bcra_nomdeu.db a Cloudflare R2 y configurar")
    _log("  BCRA_NOMDEU_URL=https://<tu-bucket>.r2.dev/bcra_nomdeu.db en Render")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python bcra_import_local.py <ruta_zip> [salida.db]")
        sys.exit(1)
    importar(ZIP_PATH, DB_PATH)
