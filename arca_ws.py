"""
Cliente oficial de Web Services ARCA (ex-AFIP) — WSAA + Padrón A5.

Autenticación WSAA (protocolo real):
  1. Se genera un TRA (loginTicketRequest XML) con vigencia de ±10 minutos.
  2. El TRA se firma como CMS/PKCS#7 (SignedData con el certificado embebido).
  3. El CMS en base64 se envía como único parámetro de loginCms.
  4. La respuesta trae token + sign válidos 12 horas → se cachean en disco
     (y backup R2) y se renuevan automáticamente al acercarse el vencimiento.

Consulta de constancia (ws_sr_constancia_inscripcion / personaServiceA5):
  - Método SOAP getPersona(token, sign, cuitRepresentada, idPersona).
  - cuitRepresentada = CUIT del titular del certificado (env ARCA_CUIT o
    se extrae del subject del certificado).

Sin dependencias SOAP externas: los envelopes se arman a mano y se parsean
con xml.etree — menos superficie de error que zeep para dos llamadas fijas.

Uso desde consola (primera prueba):
    python arca_ws.py 30710295022            # consulta real (login + padrón)
    python arca_ws.py 30710295022 --dry-run  # solo valida certificado y firma CMS
"""

import base64
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════════════

ARCA_ENV     = os.environ.get('ARCA_ENV', 'produccion')   # 'produccion' | 'homologacion'
ARCA_SERVICE = 'ws_sr_constancia_inscripcion'

_URLS = {
    'produccion': {
        'wsaa':   'https://wsaa.afip.gov.ar/ws/services/LoginCms',
        'padron': 'https://aws.afip.gov.ar/sr-padron/webservices/personaServiceA5',
    },
    'homologacion': {
        'wsaa':   'https://wsaahomo.afip.gov.ar/ws/services/LoginCms',
        'padron': 'https://awshomo.afip.gov.ar/sr-padron/webservices/personaServiceA5',
    },
}

WSAA_URL   = _URLS.get(ARCA_ENV, _URLS['produccion'])['wsaa']
PADRON_URL = _URLS.get(ARCA_ENV, _URLS['produccion'])['padron']

# Margen antes del vencimiento real del TA (12h) para renovar proactivamente
_TA_MARGEN_SEG = 600

# Estado del módulo (se puebla en arca_init)
_data_dir:  str | None = None
_cert_obj             = None   # x509.Certificate
_key_obj              = None   # clave privada cargada
_cuit_rep:  str | None = None  # CUIT del titular del certificado
_ta_cache:  dict       = {}    # {'token','sign','expiration_ts'}
_ta_lock              = threading.Lock()
_r2_upload            = None   # fn(key, bytes, content_type) — opcional
_r2_download          = None   # fn(key) -> bytes|None — opcional

_TA_CACHE_FILE   = 'arca_token_cache.json'
_TA_CACHE_R2_KEY = 'arca_token_cache.json'


# ══════════════════════════════════════════════════════════════════════════════
# INICIALIZACIÓN — carga de credenciales
# ══════════════════════════════════════════════════════════════════════════════

def _leer_pem(env_contenido: str, env_ruta: str, candidatos: list) -> bytes | None:
    """Busca un PEM en orden: contenido en env → ruta en env → archivos candidatos.

    El contenido por env var permite cargar las credenciales en Render sin
    subir archivos al disco (los saltos de línea pueden venir como '\\n').
    """
    contenido = os.environ.get(env_contenido, '').strip()
    if contenido:
        return contenido.replace('\\n', '\n').encode()

    ruta_env = os.environ.get(env_ruta, '').strip()
    if ruta_env and os.path.exists(ruta_env):
        return Path(ruta_env).read_bytes()

    for cand in candidatos:
        if cand.exists():
            return cand.read_bytes()
    return None


def arca_init(data_dir: str, r2_upload_fn=None, r2_download_fn=None) -> None:
    """Carga certificado + clave privada y deja el módulo listo para operar.

    Raises FileNotFoundError si faltan credenciales (el caller decide si es fatal).
    """
    global _data_dir, _cert_obj, _key_obj, _cuit_rep, _r2_upload, _r2_download

    _data_dir    = data_dir
    _r2_upload   = r2_upload_fn
    _r2_download = r2_download_fn

    bases = [Path(data_dir), Path.cwd()]
    # Tolerante a variantes de nombre (ej: descarga del portal como .crt.crt)
    cert_pem = _leer_pem('ARCA_CERT_PEM', 'ARCA_CERT_PATH', [
        b / n for b in bases
        for n in ('arca_certificate.crt', 'arca_certificate.crt.crt',
                  'arca_certificate.pem', 'arca.crt')
    ])
    key_pem = _leer_pem('ARCA_KEY_PEM', 'ARCA_KEY_PATH', [
        b / n for b in bases
        for n in ('arca_private.key', 'arca.key', 'arca_private.pem')
    ])

    if not cert_pem:
        raise FileNotFoundError("certificado ARCA no encontrado (ARCA_CERT_PEM / arca_certificate.crt)")
    if not key_pem:
        raise FileNotFoundError("clave privada ARCA no encontrada (ARCA_KEY_PEM / arca_private.key)")

    _cert_obj = x509.load_pem_x509_certificate(cert_pem)
    _key_obj  = serialization.load_pem_private_key(key_pem, password=None)

    _cuit_rep = _resolver_cuit_representada()
    print(f"[arca] init OK — env={ARCA_ENV} cuit_rep={_cuit_rep} "
          f"cert_vence={_cert_obj.not_valid_after_utc:%Y-%m-%d}", flush=True)


def _resolver_cuit_representada() -> str:
    """CUIT del titular del certificado: env ARCA_CUIT > subject del certificado.

    ARCA emite certificados con serialNumber='CUIT 30xxxxxxxxx'; nuestro CSR
    además puso el CUIT como CN — se intentan ambos campos.
    """
    env_cuit = re.sub(r'\D', '', os.environ.get('ARCA_CUIT', ''))
    if len(env_cuit) == 11:
        return env_cuit

    if _cert_obj is not None:
        for oid in (NameOID.SERIAL_NUMBER, NameOID.COMMON_NAME):
            for attr in _cert_obj.subject.get_attributes_for_oid(oid):
                digits = re.sub(r'\D', '', str(attr.value))
                if len(digits) == 11:
                    return digits

    raise ValueError(
        "no se pudo determinar el CUIT del titular del certificado — "
        "configurar la variable de entorno ARCA_CUIT"
    )


def configurado() -> bool:
    """True si el módulo fue inicializado con credenciales válidas."""
    return _cert_obj is not None and _key_obj is not None


# ══════════════════════════════════════════════════════════════════════════════
# WSAA — login CMS y cache del Ticket de Acceso (TA)
# ══════════════════════════════════════════════════════════════════════════════

def _generar_tra() -> bytes:
    """TRA con generación 5 min hacia atrás (tolerancia a clock skew) y
    vencimiento 10 min hacia adelante, en ISO 8601 con timezone."""
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    gen   = (ahora - timedelta(minutes=5)).isoformat()
    exp   = (ahora + timedelta(minutes=10)).isoformat()
    tra = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<loginTicketRequest version="1.0">'
        '<header>'
        f'<uniqueId>{int(time.time())}</uniqueId>'
        f'<generationTime>{gen}</generationTime>'
        f'<expirationTime>{exp}</expirationTime>'
        '</header>'
        f'<service>{ARCA_SERVICE}</service>'
        '</loginTicketRequest>'
    )
    return tra.encode('utf-8')


def _firmar_cms(tra: bytes) -> str:
    """Firma el TRA como CMS/PKCS#7 attached (DER) y lo devuelve en base64.

    WSAA valida que el SignedData contenga el TRA y el certificado emitido
    por ARCA — por eso NO sirve una firma RSA plana.
    """
    if not configurado():
        raise RuntimeError("módulo ARCA no inicializado")
    firmado = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(tra)
        .add_signer(_cert_obj, _key_obj, hashes.SHA256())
        .sign(serialization.Encoding.DER, [])
    )
    return base64.b64encode(firmado).decode('ascii')


def _local(tag: str) -> str:
    """Nombre local de un tag XML (sin namespace)."""
    return tag.rsplit('}', 1)[-1]


def _buscar_texto(root, nombre: str) -> str | None:
    """Primer texto de un elemento con ese nombre local, en cualquier namespace."""
    for el in root.iter():
        if _local(el.tag) == nombre and el.text is not None:
            return el.text.strip()
    return None


def _wsaa_login() -> dict:
    """loginCms contra WSAA. Retorna {'token','sign','expiration_ts'}."""
    cms_b64 = _firmar_cms(_generar_tra())

    envelope = (
        '<soapenv:Envelope '
        'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:wsaa="http://wsaa.view.sua.dvadac.desein.afip.gov">'
        '<soapenv:Header/><soapenv:Body>'
        f'<wsaa:loginCms><wsaa:in0>{cms_b64}</wsaa:in0></wsaa:loginCms>'
        '</soapenv:Body></soapenv:Envelope>'
    )
    resp = requests.post(
        WSAA_URL, data=envelope.encode('utf-8'),
        headers={'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': '""',
                 'User-Agent': 'VendeSeguro-ARCA/1.0'},
        timeout=20,
    )

    root = ET.fromstring(resp.content)
    fault = _buscar_texto(root, 'faultstring')
    if fault:
        msg = f"WSAA fault: {fault}"
        if 'ya posee un TA valido' in fault or 'alreadyAuthenticated' in fault:
            msg += (" — hay un token vigente emitido antes que se perdió del caché; "
                    "WSAA no re-emite hasta que venza (máx. 12h)")
        raise RuntimeError(msg)
    resp.raise_for_status()

    ta_xml = _buscar_texto(root, 'loginCmsReturn')
    if not ta_xml:
        raise RuntimeError(f"WSAA respuesta sin loginCmsReturn: {resp.text[:300]}")

    ta_root = ET.fromstring(ta_xml)
    token = _buscar_texto(ta_root, 'token')
    sign  = _buscar_texto(ta_root, 'sign')
    expiracion = _buscar_texto(ta_root, 'expirationTime')
    if not token or not sign:
        raise RuntimeError("WSAA: token/sign vacíos en loginTicketResponse")

    try:
        exp_ts = datetime.fromisoformat(expiracion).timestamp()
    except (TypeError, ValueError):
        exp_ts = time.time() + 12 * 3600   # el TA dura 12h por especificación

    print(f"[arca-wsaa] TA emitido OK — vence {expiracion}", flush=True)
    return {'token': token, 'sign': sign, 'expiration_ts': exp_ts}


def _ta_cache_path() -> Path:
    return Path(_data_dir or '.') / _TA_CACHE_FILE


def _ta_valido(ta: dict) -> bool:
    return bool(ta.get('token')) and time.time() < ta.get('expiration_ts', 0) - _TA_MARGEN_SEG


def obtener_ta() -> dict:
    """Ticket de Acceso vigente: memoria → disco → R2 → login WSAA nuevo.

    Thread-safe: un solo login concurrente (WSAA rechaza logins duplicados
    mientras exista un TA vigente).
    """
    global _ta_cache
    if _ta_valido(_ta_cache):
        return _ta_cache

    with _ta_lock:
        if _ta_valido(_ta_cache):   # otro thread pudo renovarlo mientras esperábamos
            return _ta_cache

        # Disco
        try:
            en_disco = json.loads(_ta_cache_path().read_text())
            if _ta_valido(en_disco):
                _ta_cache = en_disco
                return _ta_cache
        except (OSError, ValueError):
            pass

        # R2 (sobrevive redeploys de Render sin disco persistente)
        if _r2_download:
            try:
                raw = _r2_download(_TA_CACHE_R2_KEY)
                if raw:
                    en_r2 = json.loads(raw.decode())
                    if _ta_valido(en_r2):
                        _ta_cache = en_r2
                        _ta_cache_path().write_text(json.dumps(en_r2))
                        return _ta_cache
            except Exception:
                pass

        # Login nuevo
        _ta_cache = _wsaa_login()
        try:
            _ta_cache_path().write_text(json.dumps(_ta_cache))
        except OSError as e:
            print(f"[arca-wsaa] no se pudo guardar TA en disco: {e}", flush=True)
        if _r2_upload:
            def _bg(data=dict(_ta_cache)):
                try:
                    _r2_upload(_TA_CACHE_R2_KEY, json.dumps(data).encode(), 'application/json')
                except Exception:
                    pass
            threading.Thread(target=_bg, daemon=True).start()
        return _ta_cache


# ══════════════════════════════════════════════════════════════════════════════
# PADRÓN A5 — getPersona (constancia de inscripción)
# ══════════════════════════════════════════════════════════════════════════════

def _get_persona_raw(cuit: str) -> ET.Element:
    """Llama getPersona y retorna el XML parseado de la respuesta."""
    ta = obtener_ta()
    envelope = (
        '<soapenv:Envelope '
        'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:a5="http://a5.soap.ws.server.puc.sr/">'
        '<soapenv:Header/><soapenv:Body>'
        '<a5:getPersona>'
        f'<token>{ta["token"]}</token>'
        f'<sign>{ta["sign"]}</sign>'
        f'<cuitRepresentada>{_cuit_rep}</cuitRepresentada>'
        f'<idPersona>{cuit}</idPersona>'
        '</a5:getPersona>'
        '</soapenv:Body></soapenv:Envelope>'
    )
    resp = requests.post(
        PADRON_URL, data=envelope.encode('utf-8'),
        headers={'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': '""',
                 'User-Agent': 'VendeSeguro-ARCA/1.0'},
        timeout=20,
    )
    root = ET.fromstring(resp.content)
    fault = _buscar_texto(root, 'faultstring')
    if fault:
        raise RuntimeError(f"padrón A5 fault: {fault}")
    resp.raise_for_status()
    return root


def _parse_fecha(texto: str) -> datetime | None:
    """Parsea fechas del padrón: ISO (2005-04-12...), YYYYMM o YYYYMMDD."""
    if not texto:
        return None
    t = texto.strip()
    try:
        return datetime.fromisoformat(t.replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        pass
    digits = re.sub(r'\D', '', t)
    try:
        if len(digits) == 6 and '1900' < digits[:4] <= str(datetime.now().year):
            return datetime(int(digits[:4]), max(1, int(digits[4:6])), 1)
        if len(digits) == 8 and '1900' < digits[:4] <= str(datetime.now().year):
            return datetime(int(digits[:4]), max(1, int(digits[4:6])), max(1, int(digits[6:8])))
    except ValueError:
        return None
    return None


# idCategoria numérico del monotributo → letra (fallback si no viene la descripción)
_MONO_ID_A_LETRA = {1: 'A', 2: 'B', 3: 'C', 4: 'D', 5: 'E', 6: 'F',
                    7: 'G', 8: 'H', 9: 'I', 10: 'J', 11: 'K'}


def consultar_constancia(cuit: str) -> dict:
    """Consulta la constancia de inscripción y la mapea al esquema de solvencia.

    Returns dict con: tipo_persona, categoria_monotrib, actividad_principal,
    clae_actividad, es_empleador, antiguedad_anos, estado_afip, razon_social,
    fuente='arca_oficial'.

    Raises RuntimeError/ValueError ante errores (el caller tolerante es
    obtener_datos_fiscales_arca).
    """
    cuit_limpio = re.sub(r'\D', '', str(cuit))
    if len(cuit_limpio) != 11:
        raise ValueError(f"CUIT inválido: {cuit}")

    root = _get_persona_raw(cuit_limpio)

    # El padrón responde errorConstancia cuando el CUIT existe pero no tiene
    # constancia emitible (sin impuestos activos) — no es un error de sistema.
    err = _buscar_texto(root, 'errorConstancia') or _buscar_texto(root, 'errorMonotributo')
    tiene_datos = any(_local(el.tag) in ('datosGenerales', 'persona') for el in root.iter())
    if err and not tiene_datos:
        raise RuntimeError(f"sin constancia: {err}")

    # ── Identidad ──────────────────────────────────────────────────────────
    apellido = _buscar_texto(root, 'apellido') or ''
    nombre   = _buscar_texto(root, 'nombre') or ''
    razon    = (_buscar_texto(root, 'razonSocial')
                or f"{apellido} {nombre}".strip())
    tipo     = (_buscar_texto(root, 'tipoPersona') or '').upper()
    if tipo not in ('FISICA', 'JURIDICA'):
        tipo = 'JURIDICA' if cuit_limpio[:2] in ('30', '33', '34') else 'FISICA'

    # ── Actividad principal (CLAE) — orden 1, o la primera que aparezca ────
    clae = ''
    actividades = [el for el in root.iter() if _local(el.tag) == 'actividad']
    for act in actividades:
        orden = _buscar_texto(act, 'orden')
        if orden == '1':
            clae = _buscar_texto(act, 'idActividad') or ''
            break
    if not clae:
        clae = _buscar_texto(root, 'idActividad') or ''

    # ── Categoría monotributo ──────────────────────────────────────────────
    cat_mono = ''
    desc_cat = _buscar_texto(root, 'descripcionCategoria') or ''
    if len(desc_cat.strip()) == 1 and desc_cat.strip().upper() in 'ABCDEFGHIJK':
        cat_mono = desc_cat.strip().upper()
    else:
        id_cat = _buscar_texto(root, 'idCategoria')
        if id_cat and id_cat.isdigit():
            cat_mono = _MONO_ID_A_LETRA.get(int(id_cat), '')

    # ── Empleador: impuesto/régimen de aportes de seguridad social ─────────
    es_empleador = False
    for el in root.iter():
        if _local(el.tag) in ('descripcionImpuesto', 'descripcionRegimen'):
            if 'EMPLEADOR' in (el.text or '').upper():
                es_empleador = True
                break
        if _local(el.tag) == 'idImpuesto' and (el.text or '').strip() == '301':
            es_empleador = True
            break

    # ── Antigüedad: la fecha más vieja entre inscripciones/períodos ────────
    fechas = []
    for el in root.iter():
        if _local(el.tag) in ('periodo', 'fechaInscripcion', 'fechaContratoSocial'):
            f = _parse_fecha(el.text or '')
            if f:
                fechas.append(f)
    antiguedad = round((datetime.now() - min(fechas)).days / 365.25, 1) if fechas else None

    return {
        'cuit':                cuit_limpio,
        'razon_social':        razon,
        'tipo_persona':        tipo,
        'categoria_monotrib':  cat_mono,
        'actividad_principal': clae,        # código CLAE (6 dígitos)
        'clae_actividad':      clae,
        'es_empleador':        es_empleador,
        'antiguedad_anos':     antiguedad,
        'estado_afip':         (_buscar_texto(root, 'estadoClave') or 'ACTIVO').upper(),
        'fuente':              'arca_oficial',
    }


def obtener_datos_fiscales_arca(cuit: str) -> dict | None:
    """Versión tolerante para el pipeline de solvencia: nunca lanza excepción."""
    if not configurado():
        return None
    try:
        return consultar_constancia(cuit)
    except Exception as e:
        print(f"[arca] {cuit}: {e} — continuando con fuentes fallback", flush=True)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# CLI — primera prueba desde consola
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Uso: python arca_ws.py <cuit> [--dry-run]")
        sys.exit(1)

    arca_init(os.getcwd())

    if '--dry-run' in sys.argv:
        # Valida credenciales + firma CMS sin tocar la red
        tra = _generar_tra()
        cms = _firmar_cms(tra)
        print(f"✓ Certificado cargado (vence {_cert_obj.not_valid_after_utc:%Y-%m-%d})")
        print(f"✓ CUIT representada: {_cuit_rep}")
        print(f"✓ TRA generado ({len(tra)} bytes) y firmado CMS OK ({len(cms)} chars b64)")
        print("Dry-run completo — listo para la consulta real.")
        sys.exit(0)

    resultado = consultar_constancia(sys.argv[1])
    print(json.dumps(resultado, indent=2, ensure_ascii=False))
