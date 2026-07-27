"""
Módulo "Cerebro Fiscal Deductivo" — Scoring basado en datos AFIP/ARCA.

Cuando el BCRA viene vacío, usa:
  - Antigüedad impositiva (años en el padrón)
  - Categoría fiscal (Monotributo, Responsable Inscripto, etc.)
  - Actividad económica (CLAE) → riesgo sectorial
  - Estructura (empleador SÍ/NO)

Retorna puntaje 0-400 que se integra en la Capa B del modelo de scoring.
Todo está diseñado para ser robusto y conservative: sin datos = score neutral degradado.
"""

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# TABLAS DE REFERENCIA — RIESGO SECTORIAL POR CLAE
# ══════════════════════════════════════════════════════════════════════════════

# Rango CLAE: 01.11 a 96.09 (Clasificación Nacional de Actividades Económicas)
# Factor de riesgo: 0.5 a 1.5 (aplica al puntaje base)
#   - 0.5: actividad estable, baja volatilidad (ej: agua, energía, telecomunicaciones)
#   - 1.0: neutral (comercio general, manufactura estándar)
#   - 1.5: alto riesgo (construcción, importación, especulación)

RIESGO_SECTORIAL = {
    # ── Extractivas: relativamente estables pero cíclicas ─────────────────────
    '05.10': 1.1,  # Extracción de carbón
    '06.10': 1.0,  # Extracción de petróleo crudo
    '07.10': 1.1,  # Extracción de gas natural
    '08.11': 1.2,  # Extracción de metales preciosos (volatilidad de precios)
    '08.12': 1.1,  # Extracción de metales comunes
    '09.10': 1.0,  # Actividades de apoyo a la minería

    # ── Suministros: monopolios, estables ────────────────────────────────────
    '35.11': 0.6,  # Generación de energía eléctrica
    '35.13': 0.6,  # Distribución de energía eléctrica
    '36.00': 0.7,  # Captación, tratamiento y distribución de agua
    '37.00': 0.8,  # Tratamiento de aguas residuales
    '38.30': 0.9,  # Gestión de residuos

    # ── Manufactura: diversa, generalmente estable ───────────────────────────
    '10.71': 0.9,  # Panadería (estable, esencial)
    '15.11': 1.0,  # Fabricación de cueros
    '23.11': 1.0,  # Fabricación de vidrio plano
    '27.11': 1.0,  # Generación de maquinaria
    '28.11': 1.1,  # Fabricación de estructuras metálicas (volatilidad de precios)
    '29.10': 1.2,  # Fabricación de vehículos (cíclica, dependiente de importaciones)

    # ── Construcción: altamente cíclica ──────────────────────────────────────
    '41.10': 1.4,  # Construcción de edificios (muy cíclica)
    '41.20': 1.3,  # Ingeniería civil
    '43.21': 1.3,  # Instalaciones eléctricas

    # ── Comercio: volátil, dependiente de demanda ────────────────────────────
    '46.11': 1.1,  # Venta mayorista de cereales (commodities volatilidad)
    '46.21': 1.0,  # Venta mayorista de café, cacao, especias
    '46.30': 1.2,  # Venta mayorista de productos alimenticios diversos
    '46.49': 1.1,  # Venta mayorista de otros bienes
    '47.11': 1.0,  # Comercio minorista de supermercados
    '47.19': 1.2,  # Comercio minorista en almacenes
    '47.25': 1.1,  # Comercio minorista de bebidas
    '47.30': 1.1,  # Comercio minorista de combustibles

    # ── Transporte y Logística: moderado a alto riesgo ──────────────────────
    '49.20': 1.2,  # Transporte terrestre de carga
    '49.30': 1.2,  # Transporte de pasajeros por tubería
    '50.20': 1.3,  # Transporte marítimo (volatilidad de fletes)
    '51.10': 1.2,  # Transporte aéreo (cíclico, vulnerable a shocks)
    '52.10': 1.1,  # Almacenamiento (es logística, medio)

    # ── Alojamiento y Gastronomía: altamente cíclica ──────────────────────────
    '55.10': 1.3,  # Alojamiento en hoteles (turismo, muy cíclico)
    '56.10': 1.2,  # Restaurantes y bares

    # ── Información y Comunicaciones: moderado ────────────────────────────────
    '61.10': 0.8,  # Telecomunicaciones (oligopolio, estables)
    '62.01': 0.9,  # Programación informática (estable si B2B)
    '63.11': 0.9,  # Procesamiento de datos

    # ── Servicios Financieros: regulados, moderado ───────────────────────────
    '64.11': 0.7,  # Intermediación monetaria (bancos, regulados)
    '64.19': 0.8,  # Otros servicios de intermediación monetaria
    '64.20': 0.9,  # Fondos de inversión

    # ── Seguros y Pensiones: regulados ───────────────────────────────────────
    '65.11': 0.7,  # Seguros generales (regulados)
    '65.30': 0.7,  # Fondos de pensión

    # ── Inmobiliario: moderadamente cíclico ──────────────────────────────────
    '68.10': 1.1,  # Compraventa de bienes inmuebles (muy cíclica)
    '68.20': 1.2,  # Alquiler de bienes inmuebles (riesgo de impago de inquilinos)

    # ── Servicios Profesionales: estables ────────────────────────────────────
    '69.11': 0.8,  # Servicios legales
    '70.10': 0.8,  # Actividades de matriz
    '71.11': 0.9,  # Servicios de arquitectura e ingeniería
    '72.19': 0.9,  # Otras actividades de investigación y desarrollo

    # ── Administración Pública: ultra-estable ────────────────────────────────
    '84.11': 0.5,  # Administración pública general
    '84.12': 0.5,  # Relaciones exteriores

    # ── Educación: estable ───────────────────────────────────────────────────
    '85.10': 0.8,  # Educación pre-primaria
    '85.20': 0.8,  # Educación primaria
    '85.30': 0.8,  # Educación secundaria

    # ── Salud: moderadamente estable ─────────────────────────────────────────
    '86.10': 0.8,  # Actividades hospitalarias
    '86.21': 0.9,  # Prácticas médicas

    # ── Actividades Artísticas: altamente volátiles ────────────────────────────
    '90.01': 1.3,  # Artes escénicas
    '90.02': 1.3,  # Actividades de apoyo a las artes escénicas
    '93.11': 1.2,  # Gestión de instalaciones deportivas
}

# Factor por defecto si el CLAE no está en la tabla
RIESGO_SECTORIAL_DEFAULT = 1.0


# ══════════════════════════════════════════════════════════════════════════════
# CURVA DE ANTIGÜEDAD IMPOSITIVA
# ══════════════════════════════════════════════════════════════════════════════

def _puntaje_antiguedad(anos: float | int | None) -> float:
    """
    Pondera antigüedad impositiva en años.

    Lógica:
      - < 1 año: penalización severa (0.4x), muy riesgoso
      - 1-2 años: penalización moderada (0.7x), nuevo
      - 2-3 años: penalización leve (0.85x)
      - 3-10 años: neutral (1.0x), track record establecido
      - 10+ años: bonus moderado (1.15x), estabilidad comprobada

    Returns: factor 0.4 a 1.15
    """
    if anos is None:
        return 0.9  # degradado neutral sin datos

    anos_float = float(anos)
    if anos_float < 1:
        return 0.4
    elif anos_float < 2:
        return 0.7
    elif anos_float < 3:
        return 0.85
    elif anos_float <= 10:
        return 1.0
    else:
        return 1.15


def _puntaje_estructura(es_empleador: bool, categoria_mipyme: str = '') -> float:
    """
    Bonus por estructura: empleadores y PyMEs tienen capacidad declarada mayor.

    Returns: factor 1.0 a 1.3
    """
    if es_empleador:
        return 1.25  # empleador = nómina, más robusto

    categoria_map = {
        'Mediana_T2': 1.2,
        'Mediana_T1': 1.15,
        'Pequeña': 1.1,
        'Micro': 1.05,
    }

    if categoria_mipyme in categoria_map:
        return categoria_map[categoria_mipyme]

    return 1.0  # sin estructura, neutral


def _puntaje_riesgo_sectorial(clae: str | None) -> float:
    """
    Busca factor de riesgo por CLAE (actividad económica).

    Formatos aceptados: 'XX.XX', 'XXXX', o el código de 6 dígitos del padrón
    A5 de ARCA ('461039' → grupo '46.10' → sector '46').

    Returns: factor 0.5 a 1.5 (mayor = más riesgoso; DIVIDE el puntaje)
    """
    if not clae:
        return RIESGO_SECTORIAL_DEFAULT

    clae_norm = ''.join(c for c in str(clae).strip() if c.isdigit() or c == '.')

    # Normalizar códigos numéricos a 'XX.XX' (4711 → 47.11; 461039 → 46.10)
    if '.' not in clae_norm and len(clae_norm) >= 4:
        clae_norm = clae_norm[:2] + '.' + clae_norm[2:4]

    # Búsqueda exacta primero
    if clae_norm in RIESGO_SECTORIAL:
        return RIESGO_SECTORIAL[clae_norm]

    # Búsqueda por sector (2 dígitos): promedio de las actividades del sector
    sector_prefix = clae_norm.split('.')[0][:2]
    sector_factors = [
        v for k, v in RIESGO_SECTORIAL.items()
        if k.startswith(sector_prefix + '.')
    ]
    if sector_factors:
        avg_factor = sum(sector_factors) / len(sector_factors)
        return round(avg_factor, 2)

    # Fallback
    return RIESGO_SECTORIAL_DEFAULT


# ══════════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL: "CEREBRO DEDUCTIVO"
# ══════════════════════════════════════════════════════════════════════════════

def puntaje_perfil_fiscal(
    antiguedad_anos: float | None = None,
    categoria_mipyme: str = '',
    categoria_monotrib: str = '',
    clae_actividad: str = '',
    es_empleador: bool = False,
    tipo_persona: str = '',
) -> tuple:
    """
    Cerebro Fiscal Deductivo: calcula puntaje 0-400 combinando:
      - Antigüedad impositiva
      - Estructura (empleador, MiPyME)
      - Riesgo sectorial (CLAE)
      - Categoría fiscal

    Este puntaje se usa como "respaldo" cuando BCRA viene vacío, para llenar
    la Capa B (Conducta Interna) del modelo de scoring.

    Args:
        antiguedad_anos: años inscripto en padrón fiscal
        categoria_mipyme: 'Micro', 'Pequeña', 'Mediana_T1', 'Mediana_T2'
        categoria_monotrib: 'A', 'B', 'C', ..., 'K'
        clae_actividad: código CLAE (ej: '46.11')
        es_empleador: declaró empleados ante AFIP
        tipo_persona: 'FISICA' o 'JURIDICA'

    Returns:
        (puntaje_0_400: int, debug_dict: dict)

        debug_dict = {
            'factor_antiguedad': float,
            'factor_estructura': float,
            'factor_riesgo_sectorial': float,
            'puntaje_bruto': float,
            'puntaje_final': int,
            'componentes': str (para logs)
        }
    """

    # ── Conversiones y validaciones ────────────────────────────────────────────
    anos_imputables = float(antiguedad_anos) if antiguedad_anos else None
    es_empl_bool = bool(es_empleador)
    cat_mip = (categoria_mipyme or '').strip()
    cat_mono = (categoria_monotrib or '').strip().upper()
    clae = (clae_actividad or '').strip()

    # ── Cálculo de factores ────────────────────────────────────────────────────
    factor_antiguedad = _puntaje_antiguedad(anos_imputables)
    factor_estructura = _puntaje_estructura(es_empl_bool, cat_mip)
    factor_riesgo = _puntaje_riesgo_sectorial(clae)

    # ── Puntaje base según tipo persona y categoría fiscal ────────────────────
    # Responsable Inscripto o Empleador → base 280
    # Monotributo A-K → base 120-220 según categoría
    # Otro → base 120 (neutral degradado)
    if es_empl_bool or tipo_persona.upper() == 'JURIDICA':
        pts_base = 280
    elif cat_mono:
        # Categoría monotributo: A es la más baja, K la más alta
        cat_mono_pts = {
            'A': 120, 'B': 130, 'C': 140, 'D': 150,
            'E': 160, 'F': 170, 'G': 180, 'H': 190,
            'I': 200, 'J': 210, 'K': 220,
        }
        pts_base = cat_mono_pts.get(cat_mono, 150)
    else:
        pts_base = 120  # conservador, sin información

    # ── Aplicar factores ───────────────────────────────────────────────────────
    # Antigüedad y estructura multiplican (más años / más nómina = mejor).
    # El riesgo sectorial DIVIDE: a mayor riesgo del rubro, menor puntaje.
    # Ej: construcción (1.4) reduce; servicios públicos (0.6) aumenta.
    puntaje_bruto = pts_base * factor_antiguedad * factor_estructura / factor_riesgo

    # ── Caps y normalización a 0-400 ──────────────────────────────────────────
    puntaje_final = max(40, min(400, int(round(puntaje_bruto))))

    # ── Debug para logs ────────────────────────────────────────────────────────
    debug = {
        'factor_antiguedad': round(factor_antiguedad, 3),
        'factor_estructura': round(factor_estructura, 3),
        'factor_riesgo_sectorial': round(factor_riesgo, 3),
        'puntaje_bruto': round(puntaje_bruto, 1),
        'puntaje_final': puntaje_final,
        'componentes': (
            f"base={pts_base} × "
            f"antig={factor_antiguedad:.2f} × "
            f"estr={factor_estructura:.2f} ÷ "
            f"riesgo={factor_riesgo:.2f} "
            f"→ {puntaje_final}"
        ),
    }

    logger.debug(
        f"[cerebro-fiscal] {debug['componentes']}"
    )

    return puntaje_final, debug


# Nota de integración: arca_ws.consultar_constancia ya mapea la respuesta del
# padrón A5 directamente al esquema de solvencia (antiguedad_anos incluida),
# por lo que este módulo solo aporta la ponderación (puntaje_perfil_fiscal).
