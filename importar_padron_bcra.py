#!/usr/bin/env python3
"""
Importador del Padrón Mensual BCRA — Vende Seguro
==================================================
Uso:
    python importar_padron_bcra.py <archivo_padron.txt>

El script lee el archivo línea a línea (streaming, <120 MB RAM),
inserta en SQLite en bloques de 5.000 filas y borra el archivo
original al finalizar para liberar disco en Render.

Compatible con los formatos del portal oficial BCRA:
  - Delimitadores: ; | , \\t (auto-detección por cabecera)
  - Encodings:     UTF-8, UTF-8-BOM, Latin-1
  - Con o sin fila de cabecera

Argumento opcional --no-borrar: conserva el archivo fuente.
"""

import os
import sys
import sqlite3
import json
import time

# ── Ruta del archivo de base de datos ────────────────────────────────────────
DATA_DIR      = '/data' if os.path.exists('/data') else os.path.dirname(os.path.abspath(__file__))
PADRON_DB_PATH = os.path.join(DATA_DIR, 'bcra_padron.db')

# ── Mapa flexible de nombres de columnas ─────────────────────────────────────
_ALIAS = {
    'cuit':              'cuit',
    'identificacion':    'cuit',
    'id':                'cuit',
    'denominacion':      'denominacion',
    'razon_social':      'denominacion',
    'nombre':            'denominacion',
    'denom':             'denominacion',
    'entidad':           'entidad',
    'nom_entidad':       'entidad',
    'nombre_entidad':    'entidad',
    'banco':             'entidad',
    'cod_entidad':       'entidad',
    'situacion':         'situacion',
    'sit':               'situacion',
    'calificacion':      'situacion',
    'monto':             'monto',
    'monto_miles':       'monto',
    'saldo':             'monto',
    'periodo':           'periodo',
    'periodo_informado': 'periodo',
    'periodoInformado':  'periodo',
    'fecha':             'periodo',
    'dias_atraso':       'dias_atraso',
    'diasatraso':        'dias_atraso',
    'dias':              'dias_atraso',
}


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bcra_padron_local (
            cuit          TEXT PRIMARY KEY,
            denominacion  TEXT,
            periodo       TEXT,
            sit_max       INTEGER,
            monto_total   INTEGER,
            num_entidades INTEGER,
            detalle       TEXT,
            importado_en  TEXT
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_padron_cuit ON bcra_padron_local(cuit)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _padron_staging (
            cuit TEXT, denominacion TEXT, periodo TEXT,
            entidad TEXT, situacion INTEGER, monto INTEGER, dias_atraso INTEGER
        )
    """)
    conn.commit()


def detectar_formato(ruta: str) -> tuple[str, str, dict]:
    """Devuelve (encoding, delimitador, idx_map)."""
    # Detectar encoding
    for enc in ('utf-8-sig', 'utf-8', 'latin-1'):
        try:
            with open(ruta, 'r', encoding=enc, errors='strict') as f:
                primera = f.readline().strip()
            encoding = enc
            break
        except UnicodeDecodeError:
            encoding = 'latin-1'
            with open(ruta, 'r', encoding='latin-1') as f:
                primera = f.readline().strip()

    # Detectar delimitador
    if '|' in primera:
        delimitador = '|'
    elif ';' in primera:
        delimitador = ';'
    elif '\t' in primera:
        delimitador = '\t'
    else:
        delimitador = ','

    tokens = [t.strip().lower() for t in primera.split(delimitador)]
    tiene_header = any(t in _ALIAS for t in tokens)

    if tiene_header:
        idx_map = {}
        for i, t in enumerate(tokens):
            campo = _ALIAS.get(t)
            if campo and campo not in idx_map:
                idx_map[campo] = i
    else:
        # Formato posicional estándar BCRA: CUIT|DENOM|PERIODO|ENTIDAD|SIT|MONTO
        idx_map = {
            'cuit': 0, 'denominacion': 1, 'periodo': 2,
            'entidad': 3, 'situacion': 4, 'monto': 5,
        }

    return encoding, delimitador, idx_map, tiene_header


def importar(ruta: str, borrar_fuente: bool = True):
    if not os.path.exists(ruta):
        print(f"[ERROR] Archivo no encontrado: {ruta}")
        sys.exit(1)

    tam_mb = os.path.getsize(ruta) / (1024 * 1024)
    print(f"[padron] Archivo: {ruta} ({tam_mb:.1f} MB)")

    encoding, delimitador, idx_map, tiene_header = detectar_formato(ruta)
    print(f"[padron] Formato: encoding={encoding}, delim='{delimitador}', cabecera={tiene_header}")
    print(f"[padron] Columnas detectadas: {idx_map}")

    conn = sqlite3.connect(PADRON_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_db(conn)

    # Limpiar staging de importaciones anteriores
    conn.execute("DELETE FROM _padron_staging")
    conn.commit()

    def _get(partes, campo, default=''):
        idx = idx_map.get(campo)
        if idx is None or idx >= len(partes):
            return default
        return partes[idx].strip()

    lote   = []
    lineas = 0
    t0     = time.time()

    print("[padron] Leyendo y cargando staging...")
    with open(ruta, 'r', encoding=encoding, errors='replace') as f:
        if tiene_header:
            next(f)

        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            partes = linea.split(delimitador)

            cuit_raw = _get(partes, 'cuit').replace('-', '').replace(' ', '')
            if len(cuit_raw) != 11 or not cuit_raw.isdigit():
                continue

            try:
                sit   = int(_get(partes, 'situacion', '0') or '0')
                monto = int(float(_get(partes, 'monto', '0') or '0'))
                dias  = int(_get(partes, 'dias_atraso', '0') or '0')
            except (ValueError, TypeError):
                sit, monto, dias = 0, 0, 0

            lote.append((
                cuit_raw,
                _get(partes, 'denominacion')[:120],
                _get(partes, 'periodo')[:6],
                _get(partes, 'entidad')[:100],
                sit,
                monto,
                dias,
            ))
            lineas += 1

            if len(lote) >= 5000:
                conn.executemany(
                    "INSERT INTO _padron_staging VALUES (?,?,?,?,?,?,?)", lote
                )
                conn.commit()
                lote.clear()
                if lineas % 50_000 == 0:
                    elapsed = time.time() - t0
                    print(f"[padron]   {lineas:,} líneas — {elapsed:.0f}s", flush=True)

    if lote:
        conn.executemany("INSERT INTO _padron_staging VALUES (?,?,?,?,?,?,?)", lote)
        conn.commit()

    print(f"[padron] Staging cargado: {lineas:,} filas en {time.time()-t0:.1f}s")
    print("[padron] Agregando por CUIT → tabla final...")

    conn.execute("DELETE FROM bcra_padron_local")
    conn.execute("""
        INSERT INTO bcra_padron_local
            (cuit, denominacion, periodo, sit_max, monto_total, num_entidades, detalle, importado_en)
        SELECT
            cuit,
            MAX(denominacion),
            MAX(periodo),
            MAX(situacion),
            SUM(monto),
            COUNT(*),
            json_group_array(
                json_object(
                    'entidad',    entidad,
                    'situacion',  situacion,
                    'monto',      monto,
                    'diasAtraso', dias_atraso
                )
            ),
            strftime('%Y-%m-%dT%H:%M:%S', 'now')
        FROM _padron_staging
        GROUP BY cuit
    """)
    conn.execute("DELETE FROM _padron_staging")
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM bcra_padron_local").fetchone()[0]
    periodo = conn.execute(
        "SELECT MAX(periodo) FROM bcra_padron_local"
    ).fetchone()[0]
    conn.close()

    print(f"[padron] IMPORTACIÓN COMPLETA:")
    print(f"         CUITs únicos:  {total:,}")
    print(f"         Período:       {periodo}")
    print(f"         Tiempo total:  {time.time()-t0:.1f}s")
    print(f"         DB:            {PADRON_DB_PATH}")

    if borrar_fuente:
        try:
            os.remove(ruta)
            print(f"[padron] Archivo fuente borrado: {ruta}")
        except Exception as e:
            print(f"[padron] No se pudo borrar el archivo fuente: {e}")


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = [a for a in sys.argv[1:] if a.startswith('--')]
    borrar = '--no-borrar' not in flags

    if not args:
        print(__doc__)
        sys.exit(1)

    importar(args[0], borrar_fuente=borrar)
