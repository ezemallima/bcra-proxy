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

ARCA_ENV = os.environ.get('ARCA_ENV', 'produccion')   # 'produccion' | 'homologacion'

# Servicios de negocio habilitados para el certificado (Administrador de Relaciones).
# WSAA emite un Ticket de Acceso POR SERVICIO: cada uno tiene su propio TRA y su
# propia entrada de caché.
SERVICIO_CONSTANCIA = 'ws_sr_constancia_inscripcion'
SERVICIO_PADRON_A13 = 'ws_sr_padron_a13'

# Compatibilidad con el nombre anterior del módulo
ARCA_SERVICE = SERVICIO_CONSTANCIA

_URLS = {
    'produccion': {
        'wsaa':       'https://wsaa.afip.gov.ar/ws/services/LoginCms',
        'padron':     'https://aws.afip.gov.ar/sr-padron/webservices/personaServiceA5',
        'padron_a13': 'https://aws.afip.gov.ar/sr-padron/webservices/personaServiceA13',
    },
    'homologacion': {
        'wsaa':       'https://wsaahomo.afip.gov.ar/ws/services/LoginCms',
        'padron':     'https://awshomo.afip.gov.ar/sr-padron/webservices/personaServiceA5',
        'padron_a13': 'https://awshomo.afip.gov.ar/sr-padron/webservices/personaServiceA13',
    },
}

_ENTORNO       = _URLS.get(ARCA_ENV, _URLS['produccion'])
WSAA_URL       = _ENTORNO['wsaa']
PADRON_URL     = _ENTORNO['padron']
PADRON_A13_URL = _ENTORNO['padron_a13']

# Namespace SOAP del servicio de padrón según alcance
_NS_PADRON = {
    SERVICIO_CONSTANCIA: 'http://a5.soap.ws.server.puc.sr/',
    SERVICIO_PADRON_A13: 'http://a13.soap.ws.server.puc.sr/',
}

# Margen antes del vencimiento real del TA (12h) para renovar proactivamente
_TA_MARGEN_SEG = 600

# Estado del módulo (se puebla en arca_init)
_data_dir:  str | None = None
_cert_obj             = None   # x509.Certificate
_key_obj              = None   # clave privada cargada
_cuit_rep:  str | None = None  # CUIT del titular del certificado
_ta_cache:  dict       = {}    # {servicio: {'token','sign','expiration_ts'}}
_ta_lock              = threading.Lock()
_r2_upload            = None   # fn(key, bytes, content_type) — opcional
_r2_download          = None   # fn(key) -> bytes|None — opcional

_TA_CACHE_FILE   = 'arca_token_cache.json'
_TA_CACHE_R2_KEY = 'arca_token_cache.json'


# ══════════════════════════════════════════════════════════════════════════════
# INICIALIZACIÓN — carga de credenciales
# ══════════════════════════════════════════════════════════════════════════════

_PEM_BLOQUE_RE = re.compile(
    r'-----BEGIN ([A-Z0-9 ]+?)-----(.*?)-----END \1-----',
    re.DOTALL,
)


def normalizar_pem(texto: str | bytes) -> bytes:
    """Reconstruye un PEM canónico a partir de texto en cualquier formato.

    Los paneles de variables de entorno (Render entre ellos) suelen destruir el
    formato PEM: pegan todo en una línea, convierten los saltos en espacios, o
    los guardan como la secuencia literal '\\n'. OpenSSL rechaza esos textos
    aunque el material criptográfico esté intacto.

    La normalización extrae el cuerpo base64 de cada bloque BEGIN/END, le quita
    todo el espacio en blanco y lo re-envuelve a 64 caracteres por línea, que es
    el ancho canónico del formato PEM (RFC 7468).

    Si el texto no contiene ningún bloque BEGIN/END reconocible se devuelve tal
    cual: puede ser un DER binario u otro formato que el caller sepa manejar.
    """
    if isinstance(texto, bytes):
        try:
            texto = texto.decode('utf-8')
        except UnicodeDecodeError:
            return texto   # binario (DER/PKCS12) — no es PEM, se devuelve intacto

    # Secuencias literales que llegan desde JSON/env mal escapado
    crudo = texto.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\\r', '\n')
    crudo = crudo.replace('\r\n', '\n').replace('\r', '\n')

    bloques = []
    for match in _PEM_BLOQUE_RE.finditer(crudo):
        etiqueta = ' '.join(match.group(1).split())      # colapsa espacios internos
        cuerpo   = re.sub(r'\s+', '', match.group(2))    # base64 sin espacios ni saltos
        if not cuerpo:
            continue
        lineas = [cuerpo[i:i + 64] for i in range(0, len(cuerpo), 64)]
        bloques.append(
            f"-----BEGIN {etiqueta}-----\n" + "\n".join(lineas) + f"\n-----END {etiqueta}-----\n"
        )

    if not bloques:
        return crudo.encode('utf-8')
    return "".join(bloques).encode('ascii')


def _leer_pem(env_contenido: str, env_ruta: str, candidatos: list) -> bytes | None:
    """Busca un PEM en orden: contenido en env → ruta en env → archivos candidatos.

    Todo lo que se devuelve pasa por normalizar_pem, así que da igual si el
    panel de Render aplastó los saltos de línea o si el archivo en disco tiene
    finales de línea de Windows.
    """
    contenido = os.environ.get(env_contenido, '').strip()
    if contenido:
        return normalizar_pem(contenido)

    ruta_env = os.environ.get(env_ruta, '').strip()
    if ruta_env and os.path.exists(ruta_env):
        return normalizar_pem(Path(ruta_env).read_bytes())

    for cand in candidatos:
        if cand.exists():
            return normalizar_pem(cand.read_bytes())
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

    # Errores explícitos: distinguir "no está" de "está pero es ilegible" ahorra
    # horas de diagnóstico cuando el panel de Render mutila el PEM.
    try:
        _cert_obj = x509.load_pem_x509_certificate(cert_pem)
    except Exception as e:
        raise ValueError(
            f"certificado ARCA ilegible tras normalizar ({type(e).__name__}: {e}). "
            f"Debe incluir las líneas -----BEGIN/END CERTIFICATE-----"
        ) from e
    try:
        _key_obj = serialization.load_pem_private_key(key_pem, password=None)
    except Exception as e:
        raise ValueError(
            f"clave privada ARCA ilegible tras normalizar ({type(e).__name__}: {e}). "
            f"Debe ser PEM sin contraseña, con las líneas -----BEGIN/END PRIVATE KEY-----"
        ) from e

    # El par debe corresponderse: un certificado de otro trámite con esta clave
    # produce un CMS que WSAA rechaza con un error genérico difícil de rastrear.
    if _cert_obj.public_key().public_numbers() != _key_obj.public_key().public_numbers():
        raise ValueError(
            "el certificado ARCA y la clave privada no son del mismo par "
            "(la clave no corresponde al certificado emitido)"
        )

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


def diagnostico(data_dir: str | None = None) -> dict:
    """Informe de por qué ARCA está (o no) operativo, sin exponer secretos.

    Nunca devuelve material criptográfico: de la clave privada solo se reporta
    presencia, longitud y si tiene cabecera PEM reconocible. Del certificado se
    exponen únicamente datos públicos (sujeto, vigencia).
    """
    base_dir = data_dir or _data_dir or os.getcwd()
    info: dict = {
        'env': ARCA_ENV,
        'configurado': configurado(),
        'fuentes': {},
        'archivos_en_disco': {},
        'error': None,
    }

    # ── Qué llegó por variables de entorno (sin volcar el contenido) ────────
    for etiqueta, var in (('certificado', 'ARCA_CERT_PEM'), ('clave_privada', 'ARCA_KEY_PEM')):
        crudo = os.environ.get(var, '')
        detalle = {
            'env_var': var,
            'presente': bool(crudo.strip()),
            'longitud': len(crudo),
        }
        if crudo.strip():
            normalizado = normalizar_pem(crudo)
            try:
                texto = normalizado.decode('ascii')
                cabecera = next((l for l in texto.splitlines() if l.startswith('-----BEGIN')), None)
            except UnicodeDecodeError:
                cabecera = None
            detalle['cabecera_detectada'] = cabecera or 'NINGUNA (no se reconoce un bloque PEM)'
            detalle['saltos_de_linea_originales'] = crudo.count('\n')
            detalle['secuencia_backslash_n']      = '\\n' in crudo
        info['fuentes'][etiqueta] = detalle

    # ── Qué hay en disco (Render no tiene los .crt/.key: están gitignoreados) ──
    for nombre in ('arca_certificate.crt', 'arca_certificate.crt.crt',
                   'arca_certificate.pem', 'arca_private.key', 'arca_private.pem'):
        for base in {str(base_dir), os.getcwd()}:
            ruta = Path(base) / nombre
            if ruta.exists():
                info['archivos_en_disco'][str(ruta)] = ruta.stat().st_size

    # ── Datos públicos del certificado ya cargado ──────────────────────────
    if _cert_obj is not None:
        info['certificado'] = {
            'sujeto':       _cert_obj.subject.rfc4514_string(),
            'emisor':       _cert_obj.issuer.rfc4514_string(),
            'vence':        _cert_obj.not_valid_after_utc.isoformat(),
            'vencido':      _cert_obj.not_valid_after_utc < datetime.now(timezone.utc),
            'cuit_titular': _cuit_rep,
        }

    # ── Si no está configurado, reproducir el fallo para reportar la causa ──
    if not configurado():
        try:
            arca_init(base_dir, _r2_upload, _r2_download)
            info['configurado'] = configurado()
            info['error'] = None
        except Exception as e:
            info['error'] = f"{type(e).__name__}: {e}"

    # ── Estado de los tickets de acceso cacheados (uno por servicio) ───────
    info['servicios'] = {}
    for servicio in (SERVICIO_CONSTANCIA, SERVICIO_PADRON_A13):
        ta = _ta_cache.get(servicio) or {}
        info['servicios'][servicio] = {
            'ticket_vigente': _ta_valido(ta),
            'vence_en_segundos': (
                int(ta.get('expiration_ts', 0) - time.time()) if _ta_valido(ta) else None
            ),
        }

    return info


# ══════════════════════════════════════════════════════════════════════════════
# WSAA — login CMS y cache del Ticket de Acceso (TA)
# ══════════════════════════════════════════════════════════════════════════════

def _generar_tra(servicio: str = SERVICIO_CONSTANCIA) -> bytes:
    """TRA con generación 5 min hacia atrás (tolerancia a clock skew) y
    vencimiento 10 min hacia adelante, en ISO 8601 con timezone.

    El TA que emite WSAA sirve únicamente para el servicio declarado acá.
    """
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
        f'<service>{servicio}</service>'
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


def _wsaa_login(servicio: str = SERVICIO_CONSTANCIA) -> dict:
    """loginCms contra WSAA para un servicio. Retorna {'token','sign','expiration_ts'}."""
    cms_b64 = _firmar_cms(_generar_tra(servicio))

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

    print(f"[arca-wsaa] TA emitido OK para {servicio} — vence {expiracion}", flush=True)
    return {'token': token, 'sign': sign, 'expiration_ts': exp_ts}


def _ta_cache_path() -> Path:
    return Path(_data_dir or '.') / _TA_CACHE_FILE


def _ta_valido(ta: dict | None) -> bool:
    """True si el TA existe y le queda margen antes de vencer.

    Tolera None: con el caché multi-servicio, pedir el TA de un servicio que
    todavía no se usó devuelve None y eso no es un error.
    """
    if not isinstance(ta, dict):
        return False
    return bool(ta.get('token')) and time.time() < ta.get('expiration_ts', 0) - _TA_MARGEN_SEG


def _ta_cache_leer_todos() -> dict:
    """Caché completo {servicio: TA} desde disco, con R2 como respaldo.

    Migra en silencio el formato viejo (un único TA plano en la raíz del JSON,
    anterior al soporte multi-servicio) a la estructura por servicio.
    """
    def _normalizar(datos: dict) -> dict:
        if not isinstance(datos, dict):
            return {}
        if 'token' in datos and 'sign' in datos:     # formato legacy monoservicio
            return {SERVICIO_CONSTANCIA: datos}
        return {k: v for k, v in datos.items() if isinstance(v, dict)}

    try:
        return _normalizar(json.loads(_ta_cache_path().read_text()))
    except (OSError, ValueError):
        pass

    if _r2_download:   # sobrevive redeploys de Render sin disco persistente
        try:
            raw = _r2_download(_TA_CACHE_R2_KEY)
            if raw:
                return _normalizar(json.loads(raw.decode()))
        except Exception:
            pass
    return {}


def obtener_ta(servicio: str = SERVICIO_CONSTANCIA) -> dict:
    """Ticket de Acceso vigente para un servicio: memoria → disco → R2 → login.

    Thread-safe: un solo login concurrente (WSAA rechaza logins duplicados
    mientras exista un TA vigente para el mismo servicio).
    """
    global _ta_cache
    vigente = _ta_cache.get(servicio)
    if _ta_valido(vigente):
        return vigente

    with _ta_lock:
        vigente = _ta_cache.get(servicio)
        if _ta_valido(vigente):   # otro thread pudo renovarlo mientras esperábamos
            return vigente

        # Disco / R2 — se conservan los TA de los demás servicios
        persistido = _ta_cache_leer_todos()
        for srv, ta in persistido.items():
            if srv not in _ta_cache or not _ta_valido(_ta_cache.get(srv)):
                _ta_cache[srv] = ta
        if _ta_valido(_ta_cache.get(servicio)):
            return _ta_cache[servicio]

        # Login nuevo solo para el servicio pedido
        _ta_cache[servicio] = _wsaa_login(servicio)

        # Purgar vencidos antes de persistir para que el JSON no crezca
        snapshot = {s: t for s, t in _ta_cache.items()
                    if t.get('expiration_ts', 0) > time.time()}
        try:
            _ta_cache_path().write_text(json.dumps(snapshot))
        except OSError as e:
            print(f"[arca-wsaa] no se pudo guardar TA en disco: {e}", flush=True)
        if _r2_upload:
            def _bg(data=snapshot):
                try:
                    _r2_upload(_TA_CACHE_R2_KEY, json.dumps(data).encode(), 'application/json')
                except Exception:
                    pass
            threading.Thread(target=_bg, daemon=True).start()
        return _ta_cache[servicio]


# ══════════════════════════════════════════════════════════════════════════════
# PADRÓN A5 — getPersona (constancia de inscripción)
# ══════════════════════════════════════════════════════════════════════════════

def _get_persona_raw(cuit: str, servicio: str = SERVICIO_CONSTANCIA) -> ET.Element:
    """Llama getPersona en el alcance indicado y retorna el XML de la respuesta.

    A5 y A13 comparten la firma del método (token, sign, cuitRepresentada,
    idPersona) y solo difieren en el endpoint y el namespace.
    """
    ta  = obtener_ta(servicio)
    url = PADRON_A13_URL if servicio == SERVICIO_PADRON_A13 else PADRON_URL
    ns  = _NS_PADRON.get(servicio, _NS_PADRON[SERVICIO_CONSTANCIA])

    envelope = (
        '<soapenv:Envelope '
        'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:pad="{ns}">'
        '<soapenv:Header/><soapenv:Body>'
        '<pad:getPersona>'
        f'<token>{ta["token"]}</token>'
        f'<sign>{ta["sign"]}</sign>'
        f'<cuitRepresentada>{_cuit_rep}</cuitRepresentada>'
        f'<idPersona>{cuit}</idPersona>'
        '</pad:getPersona>'
        '</soapenv:Body></soapenv:Envelope>'
    )
    resp = requests.post(
        url, data=envelope.encode('utf-8'),
        headers={'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': '""',
                 'User-Agent': 'VendeSeguro-ARCA/1.0'},
        timeout=20,
    )
    root = ET.fromstring(resp.content)
    fault = _buscar_texto(root, 'faultstring')
    if fault:
        raise RuntimeError(f"padrón {servicio} fault: {fault}")
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

    # ── Impuestos y regímenes (solo el A5 los devuelve) ────────────────────
    impuestos   = _parsear_impuestos(root)
    activos     = [i for i in impuestos if i['activo']]
    ids_activos = {i['id'] for i in activos}

    es_empleador = bool(ids_activos & _IMP_EMPLEADOR) or any(
        'EMPLEADOR' in (i['descripcion'] or '').upper() for i in activos
    )

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
        'estado_clave':        (_buscar_texto(root, 'estadoClave') or '').upper(),
        'domicilios':          _parsear_domicilios(root),
        'impuestos':           impuestos,
        'n_impuestos_activos': len(activos),
        'tiene_iva':           bool(ids_activos & _IMP_IVA),
        'tiene_ganancias':     bool(ids_activos & _IMP_GANANCIAS),
        'tiene_monotributo':   bool(ids_activos & _IMP_MONOTRIBUTO),
        'fuente':              'arca_oficial',
    }


# ══════════════════════════════════════════════════════════════════════════════
# PADRÓN ALCANCE 13 — datos ampliados (estado de clave, domicilios, impuestos)
# ══════════════════════════════════════════════════════════════════════════════

# Identificadores de impuesto del padrón que señalan operación comercial real.
_IMP_IVA        = {'30'}                   # IVA
_IMP_GANANCIAS  = {'10', '11'}             # Ganancias (sociedades / personas)
_IMP_EMPLEADOR  = {'301'}                  # Aportes de seguridad social
_IMP_MONOTRIBUTO = {'20', '21', '24'}      # Monotributo y componentes


def _texto_hijo(el, nombre: str) -> str:
    """Texto de un hijo directo o descendiente por nombre local, o ''."""
    return (_buscar_texto(el, nombre) or '').strip()


def _parsear_domicilios(root) -> list:
    """Domicilios declarados con su estado.

    Cubre las dos formas del padrón: A13 devuelve varios <domicilio> (FISCAL y
    LEGAL/REAL) con calle/numero/codigoPostal; A5 devuelve un único
    <domicilioFiscal> con codPostal.
    """
    domicilios = []
    for el in root.iter():
        if _local(el.tag) not in ('domicilio', 'domicilioFiscal'):
            continue
        dom = {
            'tipo':         _texto_hijo(el, 'tipoDomicilio') or _local(el.tag),
            'estado':       _texto_hijo(el, 'estadoDomicilio'),
            'direccion':    _texto_hijo(el, 'direccion'),
            'localidad':    _texto_hijo(el, 'localidad'),
            'provincia':    _texto_hijo(el, 'descripcionProvincia'),
            'id_provincia': _texto_hijo(el, 'idProvincia'),
            'cod_postal':   _texto_hijo(el, 'codigoPostal') or _texto_hijo(el, 'codPostal'),
            'adicional':    _texto_hijo(el, 'datoAdicional'),
        }
        if any(dom[k] for k in ('direccion', 'localidad', 'provincia')):
            domicilios.append(dom)
    return domicilios


# estadoImpuesto del padrón: 'AC' = activo. Cualquier otro valor implica baja
# o inscripción no vigente.
_ESTADO_IMPUESTO_ACTIVO = {'AC', 'ACTIVO'}


def _parsear_impuestos(root) -> list:
    """Impuestos y regímenes en los que el CUIT está inscripto.

    Solo el alcance A5 (constancia) devuelve estos bloques; A13 no los incluye.
    Una lista vacía por lo tanto NO significa "sin impuestos": el caller debe
    distinguir "no consultado" de "consultado y sin resultados".
    """
    items = []
    for el in root.iter():
        if _local(el.tag) not in ('impuesto', 'regimen'):
            continue
        ident = _texto_hijo(el, 'idImpuesto') or _texto_hijo(el, 'idRegimen')
        desc  = _texto_hijo(el, 'descripcionImpuesto') or _texto_hijo(el, 'descripcionRegimen')
        if not ident and not desc:
            continue
        estado = (_texto_hijo(el, 'estadoImpuesto') or _texto_hijo(el, 'estado')).upper()
        items.append({
            'tipo':        _local(el.tag),
            'id':          ident,
            'descripcion': desc,
            'estado':      estado,
            'periodo':     _texto_hijo(el, 'periodo'),
            # Los <regimen> no traen estado: estar listados ya implica vigencia.
            'activo':      (estado in _ESTADO_IMPUESTO_ACTIVO) if estado else True,
        })
    return items


def consultar_padron_a13(cuit: str) -> dict:
    """Consulta el Padrón Alcance 13 y lo mapea al esquema interno de solvencia.

    Sobre lo que ya devuelve la constancia (A5), agrega las señales que el
    scoring necesita para distinguir una empresa consolidada de un CUIT
    fantasma: estado de la clave fiscal, domicilios con su estado, y el set
    completo de impuestos y regímenes activos.
    """
    cuit_limpio = re.sub(r'\D', '', str(cuit))
    if len(cuit_limpio) != 11:
        raise ValueError(f"CUIT inválido: {cuit}")

    root = _get_persona_raw(cuit_limpio, SERVICIO_PADRON_A13)

    tiene_datos = any(
        _local(el.tag) in ('datosGenerales', 'persona', 'datosRegimenGeneral', 'datosMonotributo')
        for el in root.iter()
    )
    if not tiene_datos:
        err = _buscar_texto(root, 'error') or _buscar_texto(root, 'errorConstancia')
        raise RuntimeError(f"padrón A13 sin datos para {cuit_limpio}: {err or 'respuesta vacía'}")

    # ── Identidad ──────────────────────────────────────────────────────────
    apellido = _buscar_texto(root, 'apellido') or ''
    nombre   = _buscar_texto(root, 'nombre') or ''
    razon    = (_buscar_texto(root, 'razonSocial') or f"{apellido} {nombre}".strip())
    tipo     = (_buscar_texto(root, 'tipoPersona') or '').upper()
    if tipo not in ('FISICA', 'JURIDICA'):
        tipo = 'JURIDICA' if cuit_limpio[:2] in ('30', '33', '34') else 'FISICA'

    estado_clave = (_buscar_texto(root, 'estadoClave') or '').upper().strip()

    # ── Actividad principal (CLAE) ─────────────────────────────────────────
    # A13 expone directamente idActividadPrincipal, que es la actividad
    # declarada como principal. El listado <actividad> del A5, en cambio, viene
    # con un 'orden' que no identifica a la principal (para Cencosud la primera
    # del listado es "matanza de ganado bovino", no el hipermercado), así que
    # este campo del A13 es la única fuente confiable del CLAE real.
    clae = _buscar_texto(root, 'idActividadPrincipal') or ''
    desc_actividad = _buscar_texto(root, 'descripcionActividadPrincipal') or ''

    # ── Domicilios (A13 trae fiscal + legal/real) ──────────────────────────
    domicilios = _parsear_domicilios(root)

    # ── Antigüedad: la fecha más vieja declarada ───────────────────────────
    fechas = []
    for el in root.iter():
        if _local(el.tag) in ('fechaInscripcion', 'fechaContratoSocial', 'fechaNacimiento'):
            f = _parse_fecha(el.text or '')
            if f:
                fechas.append(f)
    antiguedad = round((datetime.now() - min(fechas)).days / 365.25, 1) if fechas else None

    return {
        'cuit':                cuit_limpio,
        'razon_social':        razon,
        'tipo_persona':        tipo,
        'forma_juridica':      _buscar_texto(root, 'formaJuridica') or '',
        'categoria_monotrib':  '',      # el A13 no informa categoría; la aporta el A5
        'actividad_principal': clae,
        'clae_actividad':      clae,
        'descripcion_actividad': desc_actividad,
        'antiguedad_anos':     antiguedad,
        'estado_afip':         estado_clave or 'ACTIVO',
        'estado_clave':        estado_clave,
        'domicilios':          domicilios,
        # El alcance 13 NO devuelve impuestos ni regímenes: se dejan en None
        # (desconocido). Ponerlos en 0 haría que el scoring marcara como "CUIT
        # fantasma" a cualquier empresa consultada por esta vía.
        'impuestos':           None,
        'n_impuestos_activos': None,
        'tiene_iva':           None,
        'tiene_ganancias':     None,
        'tiene_monotributo':   None,
        'es_empleador':        None,
        'fuente':              'arca_padron_a13',
    }


def obtener_datos_fiscales_arca(cuit: str) -> dict | None:
    """Perfil fiscal oficial combinando ambos alcances del padrón.

    Los dos alcances son complementarios, no redundantes:
      - A13 aporta la actividad principal real (idActividadPrincipal), el
        domicilio fiscal y el legal/real, y la forma jurídica.
      - A5  aporta el set de impuestos y regímenes activos, la categoría de
        monotributo y la condición de empleador.

    Se consultan los dos y se fusionan; si uno falla se devuelve el otro. Nunca
    lanza excepción: ante fallo total retorna None y la cadena de solvencia
    sigue con sus fuentes de respaldo.
    """
    if not configurado():
        return None

    a13 = a5 = None
    try:
        a13 = consultar_padron_a13(cuit)
    except Exception as e:
        print(f"[arca] {cuit} A13 no disponible: {e}", flush=True)
    try:
        a5 = consultar_constancia(cuit)
    except Exception as e:
        print(f"[arca] {cuit} A5 no disponible: {e}", flush=True)

    if a13 is None and a5 is None:
        print(f"[arca] {cuit} sin datos en ningún alcance — se usan fuentes fallback", flush=True)
        return None
    if a13 is None:
        return a5
    if a5 is None:
        return a13

    # Fusión: A13 manda en identidad y actividad; A5 completa lo impositivo.
    combinado = dict(a5)
    combinado.update({k: v for k, v in a13.items() if v not in (None, '', [])})
    # El CLAE del A13 es el autoritativo (ver nota en consultar_padron_a13)
    if a13.get('clae_actividad'):
        combinado['clae_actividad']      = a13['clae_actividad']
        combinado['actividad_principal'] = a13['clae_actividad']
    # Lo impositivo solo puede venir del A5
    for campo in ('impuestos', 'n_impuestos_activos', 'tiene_iva',
                  'tiene_ganancias', 'tiene_monotributo', 'es_empleador',
                  'categoria_monotrib'):
        combinado[campo] = a5.get(campo)
    combinado['fuente'] = 'arca_a13+a5'
    return combinado


# ══════════════════════════════════════════════════════════════════════════════
# CLI — primera prueba desde consola
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Uso: python arca_ws.py <cuit> [--dry-run] [--a5]")
        print("  (por defecto consulta Padrón A13; --a5 fuerza la constancia)")
        sys.exit(1)

    arca_init(os.getcwd())

    if '--dry-run' in sys.argv:
        # Valida credenciales + firma CMS sin tocar la red
        print(f"✓ Certificado cargado (vence {_cert_obj.not_valid_after_utc:%Y-%m-%d})")
        print(f"✓ CUIT representada: {_cuit_rep}")
        for srv in (SERVICIO_CONSTANCIA, SERVICIO_PADRON_A13):
            tra = _generar_tra(srv)
            cms = _firmar_cms(tra)
            print(f"✓ TRA de {srv}: {len(tra)} bytes, CMS {len(cms)} chars b64")
        print("Dry-run completo — listo para la consulta real.")
        sys.exit(0)

    cuit_cli = sys.argv[1]
    if '--a5' in sys.argv:
        resultado = consultar_constancia(cuit_cli)
    else:
        resultado = consultar_padron_a13(cuit_cli)
    print(json.dumps(resultado, indent=2, ensure_ascii=False))
