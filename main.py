from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import urllib3
import os
import json
import time
import threading
import random
import traceback
import gc
try:
    import boto3
    BOTO3_OK = True
    from botocore.config import Config as _BotoCfg
    _LAMBDA_CFG = _BotoCfg(connect_timeout=10, read_timeout=30, retries={'max_attempts': 1})
except ImportError:
    BOTO3_OK = False
    _LAMBDA_CFG = None
    print("[aws] boto3 no instalado — usando Workers", flush=True)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__, static_folder='static')
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024

GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')
OPENAI_KEY = os.environ.get('OPENAI_API_KEY', '')
CUIT_API_KEY = os.environ.get('API_KEY_CUIT', '')
CUIT_API_URL = os.environ.get('API_SOLVENCY_URL', '')

_MONOTRIB_INGRESOS = {
    'A': 1_500_000, 'B': 3_000_000, 'C': 5_000_000, 'D': 8_000_000,
    'E': 12_000_000, 'F': 18_000_000, 'G': 26_000_000, 'H': 36_000_000,
    'I': 48_000_000, 'J': 62_000_000, 'K': 82_000_000,
}
GEMINI_MODEL = "gemini-1.5-flash"
DATA_DIR = '/data' if os.path.exists('/data') else os.getcwd()
ALERTAS_FILE      = os.path.join(DATA_DIR, 'alertas_cartera.json')
ALERTAS_BCRA_FILE = os.path.join(DATA_DIR, 'alertas_bcra.json')
DATOS_FILE        = os.path.join(DATA_DIR, 'datos_bodega.json')
print(f"[init] Almacenamiento en: {DATA_DIR}", flush=True)
WSP_FILE = os.path.join(os.getcwd(), 'whatsapp_index.json')

bcra_cache = {}
CACHE_TTL = 60 * 60 * 2
CACHE_TTL_ERROR = 300
BCRA_VACIO = {"results": None, "sin_deudas": None, "error_bcra": None}

# ── Cartera comercial ──
_cartera_comercial = []
_CC_FILE = os.path.join(DATA_DIR, 'cartera_comercial.json')
try:
    # DATA_DIR first (persistent on Render), fallback to repo copy
    _cc_path = _CC_FILE if os.path.exists(_CC_FILE) else os.path.join(os.getcwd(), 'cartera_comercial.json')
    with open(_cc_path, encoding='utf-8') as f:
        _cartera_comercial = json.load(f)
    print(f"[comercial] {len(_cartera_comercial)} clientes cargados desde {_cc_path}", flush=True)
except Exception as e:
    print(f"[comercial] Error cargando cartera: {e}", flush=True)

def cache_get(cuit):
    try:
        cf = os.path.join(DATA_DIR, 'bcra_cache.json') if os.path.exists(DATA_DIR) else '/tmp/bcra_cache.json'
        if os.path.exists(cf):
            with open(cf, 'r') as f:
                cache = json.load(f)
            if cuit in cache:
                entry = cache[cuit]
                ttl = 300 if entry.get('error') else CACHE_TTL
                if time.time() - entry.get('ts', 0) < ttl:
                    return entry.get('data'), entry.get('error')
    except: pass
    return None, None

def cache_set(cuit, data, error=None):
    try:
        cf = os.path.join(DATA_DIR, 'bcra_cache.json') if os.path.exists(DATA_DIR) else '/tmp/bcra_cache.json'
        cache = {}
        if os.path.exists(cf):
            with open(cf, 'r') as f:
                cache = json.load(f)
        cache[cuit] = {'data': data, 'error': error, 'ts': time.time()}
        ahora = time.time()
        cache = {k: v for k, v in cache.items() if ahora - v.get('ts', 0) < CACHE_TTL * 2}
        with open(cf, 'w') as f:
            json.dump(cache, f)
    except: pass

def consultar_bcra_cached(cuit):
    print(f"[bcra] {cuit} consultando BCRA...", flush=True)
    cached_data, cached_error = cache_get(cuit)
    if cached_data is not None:
        origen = "cache-error" if cached_error else "caché"
        print(f"[bcra] {cuit} desde {origen}", flush=True)
        return cached_data, cached_error
    data, error = consultar_bcra(cuit)
    if error or not data:
        data_cache = {"results": None, "sin_deudas": None, "error_bcra": str(error or "sin_respuesta")}
        cache_set(cuit, data_cache, error)
        print(f"[bcra] {cuit} error: {error}", flush=True)
        return data_cache, error
    cache_set(cuit, data)
    print(f"[bcra] {cuit} OK desde BCRA", flush=True)
    return data, None

verificacion_estado = {
    "corriendo": False,
    "progreso": 0,
    "total": 0,
    "cliente_actual": "",
    "mensaje": ""
}

def gemini_request(payload, timeout=250):
    if GEMINI_KEY:
        url = "https://generativelanguage.googleapis.com/v1beta/models/" + GEMINI_MODEL + ":generateContent?key=" + GEMINI_KEY
        for intento in range(2):
            try:
                r = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=timeout)
                print(f"[gemini] Intento {intento+1} status {r.status_code}", flush=True)
                if r.status_code == 200:
                    data = r.json()
                    if 'candidates' in data:
                        print("[gemini] OK", flush=True)
                        return data['candidates'][0]['content']['parts'][0]['text'], None
                    if 'error' in data:
                        msg = data['error'].get('message', 'Error')
                        print(f"[gemini] Error: {msg[:80]}", flush=True)
                        if 'demand' in msg.lower() or 'demanda' in msg.lower():
                            if intento < 1:
                                time.sleep(20)
                                continue
                        break
                else:
                    print(f"[gemini] HTTP {r.status_code}", flush=True)
                    break
            except Exception as e:
                print(f"[gemini] Excepcion: {e}", flush=True)
                if intento < 1:
                    time.sleep(10)
        print("[gemini] Fallando a OpenAI...", flush=True)

    if OPENAI_KEY:
        try:
            partes = payload.get('contents', [{}])[0].get('parts', [])
            prompt_text = ''
            for parte in partes:
                if 'text' in parte:
                    prompt_text += parte['text']
            headers_oai = {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + OPENAI_KEY
            }
            body_oai = {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt_text}],
                "max_tokens": 2000,
                "temperature": 0.3
            }
            r2 = requests.post("https://api.openai.com/v1/chat/completions",
                headers=headers_oai, json=body_oai, timeout=60)
            print(f"[openai] Status {r2.status_code}", flush=True)
            if r2.status_code == 200:
                data2 = r2.json()
                texto = data2['choices'][0]['message']['content']
                print("[openai] OK", flush=True)
                return texto, None
            else:
                msg = f"OpenAI HTTP {r2.status_code}: {r2.text[:100]}"
                print(f"[openai] {msg}", flush=True)
                return None, msg
        except Exception as e:
            print(f"[openai] Excepcion: {e}", flush=True)
            return None, str(e)

    return None, "No hay APIs de IA disponibles."

BCRA_WORKER   = "https://orange-recipe-3bb1.ezetombacapo.workers.dev"
BCRA_WORKER_2 = "https://little-feather-5b68.ezequielmallima.workers.dev"
BCRA_WORKER_3 = "https://square-pine-e6b4.ezequielmallima.workers.dev"
BCRA_WORKER_4 = "https://fancy-feather-7ead.ezequielmallima.workers.dev"
BCRA_WORKER_5 = "https://summer-wood-9639.ezequielmallima.workers.dev"
BCRA_WORKERS  = [BCRA_WORKER, BCRA_WORKER_2, BCRA_WORKER_3, BCRA_WORKER_4, BCRA_WORKER_5]

AWS_LAMBDA_FUNCTION = os.environ.get('AWS_LAMBDA_FUNCTION', 'vende-seguro-bcra')
AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')

def consultar_bcra_lambda(cuit):
    """Consulta deudas + historial + cheques via AWS Lambda en una sola llamada.
    Devuelve (deudas, historial, cheques) o None si falla."""
    if not BOTO3_OK:
        return None
    aws_key = os.environ.get('AWS_ACCESS_KEY_ID')
    aws_secret = os.environ.get('AWS_SECRET_ACCESS_KEY')
    if not aws_key or not aws_secret:
        return None
    try:
        client = boto3.client(
            'lambda',
            region_name=AWS_REGION,
            aws_access_key_id=aws_key,
            aws_secret_access_key=aws_secret,
            **({'config': _LAMBDA_CFG} if _LAMBDA_CFG else {})
        )
        payload = json.dumps({'cuit': cuit}).encode('utf-8')
        response = client.invoke(
            FunctionName=AWS_LAMBDA_FUNCTION,
            InvocationType='RequestResponse',
            Payload=payload
        )
        result = json.loads(response['Payload'].read())
        if response.get('FunctionError') or result.get('statusCode') != 200:
            print(f"[aws] Error Lambda {cuit}: {result}", flush=True)
            return None
        body = json.loads(result['body'])
        # Validar que tenga datos reales
        deudas = body.get('deudas')
        historial = body.get('historial')
        cheques = body.get('cheques')
        if not deudas or deudas.get('error'):
            print(f"[aws] Sin datos BCRA para {cuit}", flush=True)
            return None
        print(f"[aws] {cuit} OK via Lambda", flush=True)
        return deudas, historial, cheques
    except Exception as e:
        print(f"[aws] Fallo Lambda {cuit}: {e}", flush=True)
        return None

def consultar_bcra(cuit, reintentos=3):
    # Rotar entre los 5 workers + BCRA directo
    endpoints = [(w + "/deudas/" + cuit, f"Worker{i+1}") for i, w in enumerate(BCRA_WORKERS)]
    endpoints.append(("https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/" + cuit, "directo"))
    for ep_url, via in endpoints:
        try:
            print(f"[bcra] {cuit} consultando via {via}...", flush=True)
            r = requests.get(ep_url, timeout=15, verify=False)
            if r.status_code == 200:
                text = r.text.strip()
                if not text or len(text) < 10:
                    print(f"[bcra] Vacío via {via} para {cuit}", flush=True)
                    continue
                data = r.json()
                if data.get('error'):
                    continue
                results = data.get('results') or {}
                periodos = results.get('periodos') or []
                data['sin_deudas'] = len(periodos) == 0
                print(f"[bcra] {cuit} OK via {via}", flush=True)
                return data, None
            elif r.status_code == 404:
                return {"results": {"denominacion": "", "periodos": []}, "sin_deudas": True}, None
            else:
                print(f"[bcra] HTTP {r.status_code} via {via} para {cuit}", flush=True)
        except Exception as e:
            print(f"[bcra] Error via {via} para {cuit}: {e}", flush=True)
            continue
    return None, "sin_respuesta"

def analizar_bodegas_server(cuit, nombre, mensajes):
    if not mensajes:
        return False, ""
    try:
        mensajes_texto = "\n".join(["- " + m for m in mensajes[:20]])
        prompt = (
            "Sos un Analista de Riesgo Crediticio experto en el sector vitinicola argentino.\n"
            "Analiza estos mensajes del grupo de bodegas sobre " + nombre + " (CUIT: " + cuit + ").\n\n"
            "DICCIONARIO DE TERMINOS (OBLIGATORIO USAR):\n"
            "- LC: Limite de Credito\n- MM: Millones de pesos\n- s/ CP: Segun condiciones de pago pactadas\n"
            "- fct: Facturas\n- opera con...: Relacion comercial activa\n"
            "- contado anticipado: Paga antes de recibir mercaderia (mejor escenario)\n"
            "- pagar con +X dias: Cliente se financia con la bodega (riesgo de flujo)\n"
            "- cheque reemplazado / repuesto: Problema resuelto, NO es negativo\n\n"
            "REGLAS:\n"
            "- Priorizá el chat sobre el reporte financiero. El chat es la realidad operativa.\n"
            "- Solo negativo si hay deudas impagas NO resueltas, estafas o desaparicion.\n"
            "- Si distintas bodegas dicen cosas contradictorias, marcalo como comportamiento_inconsistente=true.\n"
            "- Cheques rechazados pero reemplazados = NO negativo.\n"
            "- Mensaje sobre OTRO CUIT diferente = NO negativo para este cliente.\n"
            "- NUNCA digas que no hay antecedentes si el chat tiene mensajes.\n\n"
            "MENSAJES:\n" + mensajes_texto + "\n\n"
            'Responde SOLO con este JSON sin markdown: {"es_negativo": false, "motivo": "texto descriptivo", "comportamiento_inconsistente": false}'
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        texto, error = gemini_request(payload, timeout=30)
        if error or not texto:
            return False, ""
        texto_limpio = texto.strip().replace("```json", "").replace("```", "").strip()
        import re as re_mod
        match = re_mod.search(r'\{[\s\S]+\}', texto_limpio)
        if match:
            texto_limpio = match.group(0)
        resultado = json.loads(texto_limpio)
        motivo = resultado.get("motivo", "")
        if resultado.get("comportamiento_inconsistente"):
            motivo = "⚠ Comportamiento Inconsistente: " + motivo
        return resultado.get("es_negativo", False), motivo
    except Exception:
        return False, ""

def get_solvency_data(cuit):
    """Ingresos estimados vía API externa + AFIP pública. Caché 24h. Fail-safe: retorna None."""
    cuit_limpio = str(cuit).replace('-', '').replace(' ', '').strip()
    cache_path = os.path.join(DATA_DIR, f'solvency_{cuit_limpio}.json')
    try:
        if os.path.exists(cache_path):
            with open(cache_path, 'r') as f:
                cached = json.load(f)
            if time.time() - cached.get('ts', 0) < 86400:
                return cached.get('data')
    except: pass

    data = None
    if CUIT_API_URL and CUIT_API_KEY:
        try:
            url = f"{CUIT_API_URL.rstrip('/')}/{cuit_limpio}"
            r = requests.get(url,
                headers={'Authorization': f'Bearer {CUIT_API_KEY}', 'x-api-key': CUIT_API_KEY},
                timeout=8, verify=False)
            if r.status_code == 200:
                data = r.json()
                print(f"[solvency] {cuit_limpio} OK vía API configurada", flush=True)
        except Exception as e:
            print(f"[solvency] API externa error: {e}", flush=True)

    if data is None:
        try:
            r = requests.get(
                f"https://afip.tangofactura.com/Rest/GetContribuyenteFull?cuitContribuyente={cuit_limpio}",
                timeout=8, verify=False)
            if r.status_code == 200:
                contrib = (r.json().get('Contribuyente') or {})
                cat = (contrib.get('categMonotrib') or '').strip().upper()
                ingresos = _MONOTRIB_INGRESOS.get(cat, 0)
                if not ingresos and contrib.get('tipoPersona') == 'JURIDICA':
                    ingresos = 100_000_000
                data = {'ingresos_anuales': ingresos, 'categoria_monotrib': cat,
                        'tipo_persona': contrib.get('tipoPersona', ''), 'fuente': 'afip_tang'}
                print(f"[solvency] {cuit_limpio} AFIP cat={cat} ingresos≈{ingresos}", flush=True)
        except Exception as e:
            print(f"[solvency] AFIP fallback error: {e}", flush=True)

    try:
        with open(cache_path, 'w') as f:
            json.dump({'data': data, 'ts': time.time()}, f)
    except: pass
    return data


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  MODELO NACIONAL DE RIESGO VENDE SEGURO v9.0                            ║
# ║  Capa 1 (40%): Estabilidad Bancaria  | Capa 2 (30%): Solvencia Federal  ║
# ║  Capa 3 (30%): Comportamiento Interno | Liquidez: bonus cheques          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

_SCORE_VERSION = "9.0"

# Session-level cache — se limpia al inicio de cada verificación
_score_session_cache: dict = {}

# Mapa de riesgo logístico por zona geográfica
_GEO_RIESGO_LOGISTICO: dict = {
    'salta': 'medio', 'jujuy': 'medio', 'tucuman': 'medio', 'catamarca': 'medio',
    'la rioja': 'medio', 'santiago del estero': 'medio', 'formosa': 'medio',
    'chaco': 'medio', 'misiones': 'medio', 'corrientes': 'medio',
    'mendoza': 'medio', 'san juan': 'medio', 'san luis': 'medio',
    'neuquen': 'alto', 'neuquén': 'alto', 'rio negro': 'alto', 'río negro': 'alto',
    'chubut': 'alto', 'santa cruz': 'alto', 'tierra del fuego': 'alto',
    'la pampa': 'medio',
}

def _alerta_logistica(ciudad: str) -> str:
    """Devuelve '' | 'medio' | 'alto' según riesgo logístico de incobrabilidad."""
    c = str(ciudad or '').lower()
    for zona, nivel in _GEO_RIESGO_LOGISTICO.items():
        if zona in c:
            return nivel
    return ''


def _layer1_estabilidad_bancaria(
    max_sit: int,
    n_periodos_h: int,
    monto_real: float,
    periodos_hist: list,
    periodos_curr: list,
) -> tuple:
    """
    Capa 1: Estabilidad Bancaria — 40% del score (0-400 pts).
    Situación BCRA (0-280) + Tendencia 24m (0-120).
    Penalización doble en pts_sit si tendencia es negativa (Sit 1→2 reciente).
    Returns (pts: int, tendencia: str, alerta_deuda_creciente: bool)
    """
    if max_sit == 1:
        if   monto_real == 0:         pts_sit = 160
        elif monto_real < 500_000:    pts_sit = 200
        elif monto_real < 2_500_000:  pts_sit = 240
        elif n_periodos_h >= 12:      pts_sit = 280
        elif n_periodos_h >= 6:       pts_sit = 260
        elif n_periodos_h >= 2:       pts_sit = 240
        else:                         pts_sit = 210
    elif max_sit == 2:  pts_sit = 100
    elif max_sit == 3:  pts_sit = 20
    elif max_sit == 4:  pts_sit = 5
    else:               pts_sit = 0

    pts_tend = 55
    tendencia = 'neutral'
    pool = periodos_hist if periodos_hist else periodos_curr
    if pool and len(pool) >= 4:
        def _ms(p):
            return max(((e.get('situacion') or 1) for e in p.get('entidades', [])), default=1)
        r3 = [_ms(p) for p in pool[:3]]
        a9 = [_ms(p) for p in pool[3:12]]
        pr = sum(r3) / len(r3)
        pa = sum(a9) / len(a9) if a9 else pr
        if   pr < pa - 0.3:  pts_tend = 120; tendencia = 'mejorando'
        elif pr <= pa + 0.1: pts_tend = 65;  tendencia = 'estable'
        elif pr <= pa + 0.5: pts_tend = 10;  tendencia = 'deteriorando_leve'
        else:                pts_tend = 0;   tendencia = 'deteriorando'

    if tendencia in ('deteriorando_leve', 'deteriorando') and max_sit <= 2:
        pts_sit = pts_sit // 2   # penalización doble

    alerta_creciente = False
    pool_a = periodos_curr if periodos_curr else periodos_hist
    if pool_a and len(pool_a) >= 2:
        def _m(p):
            return sum((e.get('monto') or 0) for e in p.get('entidades', []))
        m0, m1 = _m(pool_a[0]), _m(pool_a[1])
        if m1 > 0 and m0 / m1 > 1.30:
            alerta_creciente = True

    return (min(400, pts_sit + pts_tend), tendencia, alerta_creciente)


def _layer2_solvencia_federal(solvency_data: dict) -> tuple:
    """
    Capa 2: Solvencia Federal — 30% del score (0-300 pts).
    AFIP: tipo persona + categoría Monotributo.
    Empleador/Jurídica → 300 pts. Monotrib A/B → flag cap externo 600.
    Returns (pts: int, es_empleador: bool, es_monotrib_bajo: bool, indice: float)
    """
    if not solvency_data:
        return 120, False, False, 0.40   # graceful degradation: neutral degradado

    cat  = (solvency_data.get('categoria_monotrib') or '').strip().upper()
    tipo = (solvency_data.get('tipo_persona') or '').strip().upper()

    es_empleador     = 'JURIDICA' in tipo or 'EMPLEADOR' in tipo
    es_monotrib_bajo = cat in ('A', 'B')

    if es_empleador:
        pts_base = 300
    elif 'INSCRIPTO' in tipo or tipo in ('RI', 'IVA'):
        pts_base = 220
    elif cat:
        CAT_PTS = {
            'K': 200, 'J': 175, 'I': 150, 'H': 130,
            'G': 110, 'F': 90,  'E': 70,  'D': 50,
            'C': 35,  'B': 20,  'A': 10,
        }
        pts_base = CAT_PTS.get(cat, 80)
    else:
        pts_base = 120

    indice = round(pts_base / 300, 3)
    return (min(300, pts_base), es_empleador, es_monotrib_bajo, indice)


def _layer3_comportamiento_interno(
    cuit_limpio: str,
    en_mora: bool,
    moras_norm: dict,
    wsp_index: dict,
    en_cartera: bool,
) -> tuple:
    """
    Capa 3: Comportamiento Interno — 30% del score (0-300 pts).
    Mora Piattelli (Hard Block, 0-150) + Chat Bodegas (0-100) + Relación (0-50).
    hard_block=True → score final no puede superar 400 pts.
    Returns (pts: int, hard_block: bool)
    """
    if en_mora:
        pts_mora   = 0
        hard_block = True
    else:
        pts_mora   = 150
        hard_block = False

    wsp_entry = wsp_index.get(cuit_limpio, {})
    if not wsp_entry:
        pts_wsp = 50
    elif wsp_entry.get('es_negativo'):
        pts_wsp = 0
    else:
        pts_wsp = 100

    pts_rel = 50 if en_cartera else 0
    return (pts_mora + pts_wsp + pts_rel, hard_block)


def _layer_liquidez(cheq_data: dict, max_sit: int, n_periodos_h: int) -> tuple:
    """
    Liquidez (cheques rechazados) — bonus/penalidad (0-100 pts).
    rechazar=True → score = 1 (rechazo definitivo por cheques críticos).
    Returns (pts: int, rechazar: bool)
    """
    if not cheq_data:
        return 0, False
    if cheq_data.get('sin_deudas'):
        if   n_periodos_h >= 12: pts = 100
        elif n_periodos_h >= 6:  pts = 80
        elif n_periodos_h >= 2:  pts = 60
        elif n_periodos_h >= 1:  pts = 40
        else:                    pts = 20
    else:
        res_c    = cheq_data.get('results') or {}
        causales = res_c.get('causales', []) if isinstance(res_c, dict) else []
        detalles: list = []
        for causal in causales:
            for ent in causal.get('entidades', []):
                detalles.extend(ent.get('detalle', []))
        total_ch   = len(detalles)
        activos_ch = sum(1 for d in detalles
                         if not d.get('fechaPago') or d.get('estadoMulta') == 'IMPAGA')
        if activos_ch > 5 or total_ch >= 113:
            return 0, True
        if   total_ch == 0:   pts = 100 if n_periodos_h > 6 else (50 if n_periodos_h >= 1 else 20)
        elif activos_ch == 0: pts = 50
        else:                 pts = 0
    if   max_sit >= 3:               pts = 0
    elif max_sit == 2 and pts > 50:  pts = 50
    return pts, False


def calcular_rating_predictivo(
    cuit: str,
    bcra_data: dict,
    hist_data: dict     = None,
    cheq_data: dict     = None,
    en_mora: bool       = None,
    solvency_data: dict = None,
    ciudad: str         = '',
) -> dict:
    """
    Modelo Nacional de Riesgo Vende Seguro v9.0

    Capa 1 — Estabilidad Bancaria   40%  (0-400 pts): BCRA + Tendencia 24m
    Capa 2 — Solvencia Federal      30%  (0-300 pts): AFIP tipo/categoría
    Capa 3 — Comportamiento Interno 30%  (0-300 pts): Mora + Chat + Relación
    Liquidez                              (0-100 pts): Cheques bonus

    Caps: Sit5+→1 | Sit4→250 | Sit3→400 | Mora interna→400 | Monotrib A/B→600
    Alerta Temprana: deuda +30% último mes | Monotrib bajo | solvencia < 40%
    Alerta Logística: cliente en zona de riesgo geográfico
    """
    cuit_limpio = str(cuit).replace('-', '').replace(' ', '').strip()

    if cuit_limpio in _score_session_cache:
        return _score_session_cache[cuit_limpio]

    # ── Parsear BCRA ──────────────────────────────────────────────────────
    sin_deudas_real = bcra_data.get('sin_deudas', False)
    periodos_curr   = (bcra_data.get('results') or {}).get('periodos') or []
    max_sit = 1; nro_entidades = 0; monto_total_m = 0.0
    if periodos_curr:
        ents = periodos_curr[0].get('entidades', [])
        nro_entidades = len(ents)
        if ents:
            max_sit       = max((e.get('situacion', 1) or 1) for e in ents)
            monto_total_m = sum((e.get('monto', 0) or 0) for e in ents) / 1000
    elif sin_deudas_real:
        max_sit = 1
    monto_real = monto_total_m * 1000

    # ── Historial 24m ─────────────────────────────────────────────────────
    periodos_hist = (hist_data.get('results') or {}).get('periodos') or [] if hist_data else []
    if not periodos_hist:
        periodos_hist = periodos_curr
    n_periodos_h = n_periodos_recientes = meses_malos = 0
    sit_grave_6m = False
    for idx_p, p in enumerate(periodos_hist[:24]):
        smax        = max(((e.get('situacion') or 1) for e in p.get('entidades', [])), default=1)
        tiene_deuda = any((e.get('monto') or 0) > 0 for e in p.get('entidades', []))
        if tiene_deuda:
            n_periodos_h += 1
            if idx_p < 6: n_periodos_recientes += 1
        if smax > 1: meses_malos += 1
        if idx_p < 6 and smax >= 3: sit_grave_6m = True
    n_periodos_h = min(n_periodos_h, n_periodos_recientes * 4)

    # ── Mora Piattelli ────────────────────────────────────────────────────
    moras_norm: dict = {}
    _mp = os.path.join(DATA_DIR, 'moras_piattelli.json')
    if not os.path.exists(_mp):
        _mp = os.path.join(os.getcwd(), 'moras_piattelli.json')
    try:
        with open(_mp, 'r', encoding='utf-8') as _mf:
            _md = json.load(_mf)
        moras_norm = {str(k).replace('-','').replace(' ','').strip(): v for k, v in _md.items()}
    except: pass
    if en_mora is None:
        en_mora = cuit_limpio in moras_norm

    en_cartera = any(
        str(c.get('cuit', '')).replace('-', '').replace(' ', '').strip() == cuit_limpio
        for c in _cartera_comercial
    )

    # ── WhatsApp Bodegas ──────────────────────────────────────────────────
    wsp_index: dict = {}
    try:
        with open(WSP_FILE, 'r', encoding='utf-8') as _wf:
            wsp_index = json.load(_wf)
    except: pass

    # ── Solvencia AFIP (graceful degradation via get_solvency_data) ───────
    if solvency_data is None:
        solvency_data = get_solvency_data(cuit_limpio)

    # ═══ CAPA 1: Estabilidad Bancaria (0-400 pts) ════════════════════════
    pts_c1, tendencia, alerta_creciente = _layer1_estabilidad_bancaria(
        max_sit, n_periodos_h, monto_real, periodos_hist, periodos_curr
    )

    # ═══ CAPA 2: Solvencia Federal (0-300 pts) ═══════════════════════════
    pts_c2, es_empleador, es_monotrib_bajo, indice_solv = _layer2_solvencia_federal(solvency_data)

    # ═══ CAPA 3: Comportamiento Interno (0-300 pts) ══════════════════════
    pts_c3, hard_block_mora = _layer3_comportamiento_interno(
        cuit_limpio, en_mora, moras_norm, wsp_index, en_cartera
    )

    # ═══ LIQUIDEZ: Cheques (0-100 pts) ═══════════════════════════════════
    pts_liq, rechazar = _layer_liquidez(cheq_data or {}, max_sit, n_periodos_h)
    if rechazar:
        resultado = {
            'score': 1, 'rango': 'Rechazar', 'color': '#7f1d1d', 'emoji': '⛔',
            'alerta_temprana': False, 'bloquear_oportunidad': True,
            'alerta_logistica': _alerta_logistica(ciudad),
            'componentes': {'capa1': pts_c1, 'capa2': pts_c2, 'capa3': pts_c3, 'liquidez': 0},
            'tendencia': tendencia, 'es_empleador': es_empleador,
            'indice_solvencia': indice_solv, 'version': _SCORE_VERSION,
        }
        _score_session_cache[cuit_limpio] = resultado
        print(f"[score v{_SCORE_VERSION}] {cuit_limpio} RECHAZAR — cheques críticos", flush=True)
        return resultado

    # ── Suma bruta ────────────────────────────────────────────────────────
    puntos = pts_c1 + pts_c2 + pts_c3 + pts_liq

    # ── Ajuste: concentración de deuda ────────────────────────────────────
    if   nro_entidades == 0 or sin_deudas_real:     puntos += 25
    elif nro_entidades == 1 and monto_total_m < 50: puntos += 18
    elif nro_entidades <= 2 and monto_total_m < 100:puntos += 12

    # ── Ajuste: ratio de apalancamiento BCRA/AFIP ─────────────────────────
    if solvency_data:
        _ing = float(solvency_data.get('ingresos_anuales') or 0)
        if _ing > 0 and (monto_total_m * 1000) / _ing > 0.5:
            puntos -= 200
            print(f"[score v{_SCORE_VERSION}] {cuit_limpio} apalancamiento alto → -200", flush=True)

    # ── Penalidades históricas ────────────────────────────────────────────
    if 2 <= meses_malos <= 5:
        puntos = round(puntos * 0.75)
    if sit_grave_6m:
        puntos = min(puntos, 150)

    # ── Techos duros por situación BCRA ───────────────────────────────────
    if   max_sit >= 5:  puntos = min(puntos, 1)
    elif max_sit >= 4:  puntos = min(puntos, 250)
    elif max_sit == 3:  puntos = min(puntos, 400)

    # ── Hard Block: mora interna → score ≤ 400 ────────────────────────────
    if hard_block_mora:
        puntos = min(puntos, 400)

    # ── Cap: Monotrib A/B → score ≤ 600 ──────────────────────────────────
    if es_monotrib_bajo:
        puntos = min(puntos, 600)

    score = max(1, min(999, round(puntos)))

    if   score >= 750: rango, color, emoji = 'Excelente',   '#16a34a', '🟢'
    elif score >= 600: rango, color, emoji = 'Bueno',       '#ca8a04', '🟡'
    elif score >= 400: rango, color, emoji = 'Revisar',     '#ea580c', '🟠'
    elif score >= 200: rango, color, emoji = 'Alto riesgo', '#dc2626', '🔴'
    else:              rango, color, emoji = 'Rechazar',    '#7f1d1d', '⛔'

    alerta_temprana      = alerta_creciente or es_monotrib_bajo or indice_solv < 0.40
    bloquear_oportunidad = hard_block_mora or (en_mora and score > 700)
    alerta_log           = _alerta_logistica(ciudad)

    print(
        f"[score v{_SCORE_VERSION}] {cuit_limpio} sit={max_sit} tend={tendencia} "
        f"c1={pts_c1} c2={pts_c2} c3={pts_c3} liq={pts_liq} "
        f"→ {score} {rango} | at={alerta_temprana} bloq={bloquear_oportunidad} geo={alerta_log or '-'}",
        flush=True
    )

    resultado = {
        'score':                score,
        'rango':                rango,
        'color':                color,
        'emoji':                emoji,
        'alerta_temprana':      alerta_temprana,
        'bloquear_oportunidad': bloquear_oportunidad,
        'alerta_logistica':     alerta_log,
        'componentes': {
            'capa1': pts_c1, 'capa2': pts_c2,
            'capa3': pts_c3, 'liquidez': pts_liq,
        },
        'tendencia':            tendencia,
        'es_empleador':         es_empleador,
        'indice_solvencia':     indice_solv,
        'version':              _SCORE_VERSION,
    }
    _score_session_cache[cuit_limpio] = resultado
    return resultado


def calcular_score_servidor(cuit: str, bcra_data: dict, en_mora=None, ciudad: str = '') -> dict:
    """
    Wrapper de calcular_rating_predictivo v9.0.
    Carga historial y cheques desde caché local (graceful degradation).
    Mantiene compatibilidad con todos los callers existentes.
    """
    cuit_limpio = str(cuit).replace('-', '').replace(' ', '').strip()

    def _cache_load(fname):
        p = os.path.join(DATA_DIR, fname)
        try:
            if os.path.exists(p):
                with open(p, 'r') as f:
                    return json.load(f).get('payload')
        except: pass
        return None

    hist_data = _cache_load(f'historial_{cuit_limpio}.json')
    cheq_data = _cache_load(f'cheques_{cuit_limpio}.json')

    if not hist_data:
        urls_h = ([w + "/deudas/" + cuit_limpio + "/historial" for w in BCRA_WORKERS]
                  + ["https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/Historicas/" + cuit_limpio])
        for u in urls_h[:3]:
            try:
                r = requests.get(u, timeout=25, verify=False)
                if r.status_code == 200 and len(r.text.strip()) > 10:
                    hist_data = r.json()
                    try:
                        with open(os.path.join(DATA_DIR, f'historial_{cuit_limpio}.json'), 'w') as f:
                            json.dump({'payload': hist_data, 'ts': time.time()}, f)
                    except: pass
                    break
                time.sleep(2)
            except Exception as eh:
                print(f"[score wrapper] hist {cuit_limpio}: {eh}", flush=True)
                time.sleep(2)

    if not cheq_data:
        urls_c = ([w + "/deudas/" + cuit_limpio + "/cheques" for w in BCRA_WORKERS]
                  + ["https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/ChequesRechazados/" + cuit_limpio])
        for u in urls_c[:3]:
            try:
                r = requests.get(u, timeout=25, verify=False)
                if r.status_code == 200 and len(r.text.strip()) > 10:
                    cheq_data = r.json()
                    try:
                        with open(os.path.join(DATA_DIR, f'cheques_{cuit_limpio}.json'), 'w') as f:
                            json.dump({'payload': cheq_data, 'ts': time.time()}, f)
                    except: pass
                    break
                elif r.status_code == 404:
                    cheq_data = {"results": {"causales": []}, "sin_deudas": True}
                    break
                time.sleep(2)
            except Exception as ec:
                print(f"[score wrapper] cheq {cuit_limpio}: {ec}", flush=True)
                time.sleep(2)

    return calcular_rating_predictivo(
        cuit=cuit_limpio, bcra_data=bcra_data,
        hist_data=hist_data, cheq_data=cheq_data,
        en_mora=en_mora, ciudad=ciudad,
    )


# Alias para mantener compatibilidad con código legado
_calcular_score = calcular_score_servidor



def ejecutar_verificacion(cartera_data):
    global verificacion_estado
    _score_session_cache.clear()   # reset session cache para esta verificación
    verificacion_estado["corriendo"] = True
    verificacion_estado["progreso"] = 0
    verificacion_estado["total"] = len(cartera_data)
    verificacion_estado["mensaje"] = "Iniciando verificacion..."

    palabras_riesgo = [
        'rechaz', 'no paga', 'cuidado', 'mora', 'deuda', 'incobrable',
        'estafa', 'desapareci', 'fuga', 'impago', 'quiebra', 'concurso',
        'sin fondos', 'rebotado', 'mal pagador', 'no responde', 'no contesta',
        'bloqueado', 'vencid', 'no cancel', 'no liquido', 'no abono',
        'atencion', 'ojo', 'problema', 'judicial', 'cobrar', 'nos debe', 'debia'
    ]

    wsp_index = {}
    try:
        with open(WSP_FILE, 'r', encoding='utf-8') as f:
            wsp_index = json.load(f)
    except Exception:
        pass

    nuevas_alertas = []
    cartera_actualizada = []

    # ── Pre-poblar alertas_cartera.json con stubs para todos los clientes ─────
    # Esto garantiza que la App Comercial vea todos los clientes desde el inicio,
    # con scores llenándose a medida que el robot avanza.
    def _nc_v(x):
        return str(x or '').replace('-', '').replace(' ', '').strip()

    try:
        stubs = [{
            "cuit": c.get("cuit"), "nombre": c.get("nombre", ""),
            "ultimaSit": c.get("ultimaSit", 1), "ultimaVerif": None,
            "scoreCompleto": None, "scoreRango": None, "scoreColor": None, "scoreEmoji": None,
            "pendiente": True,
        } for c in cartera_data if c.get("cuit")]
        with open(ALERTAS_FILE, 'w', encoding='utf-8') as _f:
            json.dump({
                "alertas": [],
                "ultima_verif": f"En progreso — {time.strftime('%d/%m/%Y %H:%M')}",
                "cartera": stubs,
            }, _f, ensure_ascii=False, indent=2)
        print(f"[verif] Pre-poblado: {len(stubs)} stubs guardados en {ALERTAS_FILE}", flush=True)
    except Exception as _ep:
        print(f"[verif] Error pre-poblar: {_ep}", flush=True)

    try:
        cache_file = os.path.join(DATA_DIR, 'bcra_cache.json')
        if os.path.exists(cache_file):
            os.remove(cache_file)
            print("[verif] Caché BCRA limpiado para verificación fresca", flush=True)
    except: pass
    # Limpiar caché de historial y cheques (pueden tener datos vacíos de consultas fallidas)
    try:
        import glob
        for f in glob.glob(os.path.join(DATA_DIR, 'historial_*.json')):
            os.remove(f)
        for f in glob.glob(os.path.join(DATA_DIR, 'cheques_*.json')):
            os.remove(f)
        print("[verif] Caché historial y cheques limpiado", flush=True)
    except: pass

    def _guardar_alertas(nuevas_alertas, cartera_actualizada, parcial=False):
        sufijo = ' (parcial)' if parcial else ''

        def _entry(c):
            return {
                "cuit": c.get('cuit'), "nombre": c.get('nombre', ''),
                "ciudad": c.get('ciudad', ''),
                "ultimaSit": c.get('ultimaSit'), "ultimaVerif": c.get('ultimaVerif'),
                "scoreCompleto": c.get('scoreCompleto'), "scoreRango": c.get('scoreRango'),
                "scoreColor": c.get('scoreColor'), "scoreEmoji": c.get('scoreEmoji'),
                "alerta_temprana": c.get('alerta_temprana', False),
                "bloquear_oportunidad": c.get('bloquear_oportunidad', False),
                "alerta_logistica": c.get('alerta_logistica', ''),
                "pendiente": c.get('pendiente', False),
            }

        if parcial and os.path.exists(ALERTAS_FILE):
            # Merge: preservar stubs no procesados aún, actualizar los ya procesados
            try:
                with open(ALERTAS_FILE, 'r', encoding='utf-8') as _fr:
                    existente = json.load(_fr)
                base = {_nc_v(c.get('cuit', '')): _entry(c) for c in existente.get('cartera', [])}
                for c in cartera_actualizada:
                    nc = _nc_v(c.get('cuit', ''))
                    if nc:
                        base[nc] = _entry(c)
                cartera_final = list(base.values())
            except Exception:
                cartera_final = [_entry(c) for c in cartera_actualizada]
        else:
            cartera_final = [_entry(c) for c in cartera_actualizada]

        datos = {
            "alertas": nuevas_alertas,
            "ultima_verif": time.strftime('%d/%m/%Y %H:%M') + sufijo,
            "cartera": cartera_final,
        }
        with open(ALERTAS_FILE, 'w', encoding='utf-8') as _f:
            json.dump(datos, _f, ensure_ascii=False, indent=None if parcial else 2)

    total = len(cartera_data)
    try:
        for i, cliente in enumerate(cartera_data):
            cuit         = str(cliente.get('cuit', '') or '').strip()
            nombre       = str(cliente.get('nombre', '') or '').strip()
            sit_anterior = cliente.get('ultimaSit', 1) or 1
            tag          = f"[verif {i+1}/{total} {cuit}]"

            verificacion_estado["progreso"]       = i + 1
            verificacion_estado["cliente_actual"] = nombre
            verificacion_estado["mensaje"]        = f"Verificando {i+1}/{total}: {nombre}"

            cliente_actualizado = dict(cliente)
            bcra_ok = False

            # ── BCRA: 2 intentos con log detallado ───────────────────────────
            for intento in range(2):
                try:
                    lambda_result = consultar_bcra_lambda(cuit)
                    if lambda_result:
                        bcra_data, hist_lambda, cheq_lambda = lambda_result
                        try:
                            with open(os.path.join(DATA_DIR, f'historial_{cuit}.json'), 'w') as _f:
                                json.dump({'payload': hist_lambda, 'ts': time.time()}, _f)
                            with open(os.path.join(DATA_DIR, f'cheques_{cuit}.json'), 'w') as _f:
                                json.dump({'payload': cheq_lambda, 'ts': time.time()}, _f)
                        except Exception as _e:
                            print(f"{tag} Advertencia caché Lambda: {_e}", flush=True)
                    else:
                        bcra_data, _ = consultar_bcra_cached(cuit)

                    # Score
                    score_data = None
                    _ciudad = str(cliente.get('ciudad', '') or '')
                    try:
                        if lambda_result:
                            score_data = calcular_rating_predictivo(
                                cuit=cuit, bcra_data=bcra_data or {},
                                hist_data=hist_lambda, cheq_data=cheq_lambda,
                                en_mora=None, ciudad=_ciudad,
                            )
                        else:
                            score_data = calcular_score_servidor(
                                cuit, bcra_data or {}, en_mora=None, ciudad=_ciudad
                            )
                        cliente_actualizado['scoreCompleto']        = score_data['score']
                        cliente_actualizado['scoreRango']           = score_data['rango']
                        cliente_actualizado['scoreColor']           = score_data['color']
                        cliente_actualizado['scoreEmoji']           = score_data['emoji']
                        cliente_actualizado['alerta_temprana']      = score_data.get('alerta_temprana', False)
                        cliente_actualizado['bloquear_oportunidad'] = score_data.get('bloquear_oportunidad', False)
                        cliente_actualizado['alerta_logistica']     = score_data.get('alerta_logistica', '')
                        print(f"{tag} score={score_data['score']}", flush=True)
                    except Exception as e_sc:
                        print(f"{tag} ERROR score: {type(e_sc).__name__}: {e_sc}", flush=True)

                    # Situación BCRA
                    if bcra_data and bcra_data.get('results') is not None:
                        periodos  = (bcra_data.get('results') or {}).get('periodos') or []
                        entidades = periodos[0].get('entidades', []) if periodos else []
                        max_sit   = max((e.get('situacion', 1) or 1) for e in entidades) if entidades else 1
                        cliente_actualizado['ultimaSit']   = max_sit
                        cliente_actualizado['ultimaVerif'] = time.strftime('%d/%m/%Y')
                        if max_sit > sit_anterior or max_sit >= 3:
                            alerta = {
                                "nombre": nombre, "cuit": cuit,
                                "sitAnterior": sit_anterior, "sitActual": max_sit,
                                "fecha": time.strftime('%d/%m/%Y'), "tipo": "bcra"
                            }
                            if score_data:
                                alerta.update({
                                    "scoreCompleto": score_data["score"], "scoreRango": score_data["rango"],
                                    "scoreColor": score_data["color"], "scoreEmoji": score_data["emoji"]
                                })
                            nuevas_alertas.append(alerta)
                    else:
                        cliente_actualizado['ultimaVerif'] = time.strftime('%d/%m/%Y')

                    bcra_ok = True
                    break  # éxito

                except Exception as e_bcra:
                    tb_short = ' | '.join(traceback.format_exc().splitlines()[-4:])
                    print(f"{tag} ERROR intento {intento+1}/2 — {type(e_bcra).__name__}: {e_bcra}", flush=True)
                    print(f"{tag} Traceback: {tb_short}", flush=True)
                    if intento == 0:
                        print(f"{tag} Reintentando en 3s...", flush=True)
                        time.sleep(3)
                    else:
                        print(f"{tag} FALLIDO definitivo — continúa con siguiente cliente", flush=True)
                        cliente_actualizado['verificacion_fallida'] = True
                        cliente_actualizado['ultimaVerif']          = time.strftime('%d/%m/%Y')

            if not bcra_ok:
                print(f"{tag} Sin datos BCRA — conserva estado anterior (sit={sit_anterior})", flush=True)

            # ── WhatsApp bodegas ──────────────────────────────────────────────
            try:
                from datetime import datetime, timedelta
                threads_cli = wsp_index.get(cuit, [])
                hace_6m     = datetime.now() - timedelta(days=180)
                threads_rec = []
                for t in threads_cli:
                    fs = t.get('fecha') or (t.get('mensajes', [{}])[0].get('fecha') if t.get('mensajes') else None)
                    if fs:
                        try:
                            if datetime.fromisoformat(str(fs)[:10]) >= hace_6m:
                                threads_rec.append(t)
                        except Exception:
                            pass
                if threads_rec:
                    todos_msgs, tiene_sospecha = [], False
                    for t in threads_rec:
                        for m in t.get('mensajes', []):
                            txt = m.get('texto', '')
                            todos_msgs.append(m.get('autor', '') + ': ' + txt)
                            if any(p in txt.lower() for p in palabras_riesgo):
                                tiene_sospecha = True
                    if tiene_sospecha and not any(a['cuit'] == cuit and a['tipo'] == 'bodegas' for a in nuevas_alertas):
                        es_neg, motivo = analizar_bodegas_server(cuit, nombre, todos_msgs[:10])
                        if es_neg:
                            nuevas_alertas.append({"nombre": nombre, "cuit": cuit,
                                "fecha": time.strftime('%d/%m/%Y'), "tipo": "bodegas", "mensajes": [motivo]})
            except Exception:
                pass

            cartera_actualizada.append(cliente_actualizado)
            gc.collect()

            # Guardado parcial cada 10 clientes
            if (i + 1) % 10 == 0:
                try:
                    _guardar_alertas(nuevas_alertas, cartera_actualizada, parcial=True)
                    print(f"[verif] Parcial guardado — {i+1}/{total}", flush=True)
                except Exception as e_sv:
                    print(f"[verif] Error guardado parcial: {e_sv}", flush=True)

            # Delay aleatorio anti-bloqueo (excepto después del último)
            if i < total - 1:
                delay = random.uniform(2.0, 5.0)
                print(f"{tag} Pausa {delay:.1f}s...", flush=True)
                time.sleep(delay)

        # ── Guardado final ────────────────────────────────────────────────────
        _guardar_alertas(nuevas_alertas, cartera_actualizada, parcial=False)
        ok_count  = sum(1 for c in cartera_actualizada if c.get('scoreCompleto'))
        err_count = sum(1 for c in cartera_actualizada if c.get('verificacion_fallida'))
        print(f"[verif] FIN: {ok_count}/{total} con score, {err_count} fallidos, {len(nuevas_alertas)} alerta(s)", flush=True)
        verificacion_estado["mensaje"] = (
            f"Completado: {ok_count}/{total} verificados, {err_count} fallidos, {len(nuevas_alertas)} alerta(s)."
        )
        verificacion_estado["progreso"] = total

    except BaseException as e_fatal:
        print(f"[verif] ERROR FATAL — {type(e_fatal).__name__}: {e_fatal}", flush=True)
        print(f"[verif] Traceback:\n{traceback.format_exc()}", flush=True)
        verificacion_estado["mensaje"] = f"Error crítico: {type(e_fatal).__name__}: {e_fatal}"
        # Guardar lo que se procesó hasta el momento
        if cartera_actualizada:
            try:
                _guardar_alertas(nuevas_alertas, cartera_actualizada, parcial=True)
                print(f"[verif] Guardado de emergencia: {len(cartera_actualizada)} clientes", flush=True)
            except Exception:
                pass
    finally:
        verificacion_estado["corriendo"] = False
        print("[verif] Flag liberado.", flush=True)

# ─── ENDPOINTS ───────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory('static', 'index.html')

@app.route("/ping")
def ping():
    return jsonify({"ok": True, "ts": time.time()})

@app.route("/comercial")
def comercial():
    return send_from_directory('static', 'comercial.html')

@app.route("/todos-los-clientes")
def get_todos_los_clientes():
    """Devuelve la cartera completa con CUITs para que el admin pueda verificar sin localStorage."""
    resp = jsonify(_cartera_comercial)
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route("/vendedores")
def get_vendedores():
    vs = sorted({(c.get('vendedor') or '').strip() for c in _cartera_comercial if (c.get('vendedor') or '').strip()})
    resp = jsonify(vs)
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route("/cliente/guardar", methods=["POST"])
def guardar_cliente():
    global _cartera_comercial
    try:
        data = request.get_json(force=True)
        cuit = str(data.get('cuit', '')).replace('-', '').replace(' ', '').strip()
        nombre = str(data.get('nombre', '')).strip()
        if not cuit or not nombre:
            return jsonify({"ok": False, "error": "CUIT y nombre son obligatorios"}), 400

        cuit_orig = str(data.get('_cuitOriginal', cuit)).replace('-', '').replace(' ', '').strip()

        cliente = {
            'nombre': nombre,
            'cuit': cuit,
            'ciudad': str(data.get('ciudad', '')).strip(),
            'vendedor': str(data.get('vendedor', '')).strip(),
            'email': str(data.get('email', '')).strip(),
            'limiteCredito': float(data.get('limiteCredito', 0) or 0),
        }

        idx = next((i for i, c in enumerate(_cartera_comercial)
                    if str(c.get('cuit', '')).replace('-', '').replace(' ', '').strip() == cuit_orig), None)

        if idx is not None:
            # Preserve fields not managed here (e.g. plazoDias)
            merged = dict(_cartera_comercial[idx])
            merged.update(cliente)
            _cartera_comercial[idx] = merged
            accion = "actualizado"
        else:
            _cartera_comercial.append(cliente)
            accion = "agregado"

        with open(_CC_FILE, 'w', encoding='utf-8') as f:
            json.dump(_cartera_comercial, f, ensure_ascii=False, indent=2)

        return jsonify({"ok": True, "accion": accion, "total": len(_cartera_comercial)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/cartera-por-vendedor/<vendedor>")
def get_cartera_por_vendedor(vendedor):
    """Fuente: cartera_comercial.json (todos los clientes del vendedor).
    Saldo viene del cruce con _saldos_gestion. Nunca limita a clientes con saldo."""
    from urllib.parse import unquote
    v = unquote(vendedor).strip().lower()

    # Base: todos los clientes del vendedor en cartera_comercial
    if v in ('todos', 'all', ''):
        base = _cartera_comercial
    else:
        base = [c for c in _cartera_comercial if (c.get('vendedor') or '').strip().lower() == v]

    # Mapa de saldos desde gestión (o auditoría como fallback)
    fuente_s = _saldos_gestion if _saldos_gestion else _saldos_facturas
    saldo_map = {}
    for f in fuente_s:
        cli = (f.get('cliente') or '').strip()
        if not cli:
            continue
        key = _norm_nombre(cli)
        saldo_map[key] = saldo_map.get(key, 0) + (f.get('saldo') or 0)

    def _nc(x):
        return str(x or '').replace('-', '').replace(' ', '').strip()

    # ── Merge obligatorio desde TODOS los archivos de scores del servidor ────────
    # Fuentes: alertas_bcra.json (queries individuales) + alertas_cartera.json (verificación)
    # Prioridad: alertas_cartera sobreescribe alertas_bcra para el mismo CUIT
    scores = {}            # cuit_norm → entry
    scores_by_nombre = {}  # norm_nombre → entry  (plan B)
    alertas_cuits = set()
    _archivos_usados = []

    def _load_score_file(ruta):
        if not os.path.exists(ruta):
            return
        try:
            with open(ruta, 'r', encoding='utf-8') as _f:
                _ad = json.load(_f)
            loaded = 0
            # Estructura estándar: {cartera: [...], alertas: [...]}
            for _c in _ad.get('cartera', []):
                _nc_val = _nc(_c.get('cuit', ''))
                if _nc_val:
                    scores[_nc_val] = _c  # sobreescribe — última fuente cargada gana
                    loaded += 1
                _n = (_c.get('nombre') or '').strip()
                if _n:
                    scores_by_nombre[_norm_nombre(_n)] = _c
            # Estructura plana: {cuit: score_data} (posible formato de alertas_bcra.json)
            if not _ad.get('cartera') and not _ad.get('alertas'):
                for _k, _v in _ad.items():
                    if isinstance(_v, dict) and _v.get('scoreCompleto'):
                        _nc_val = _nc(_k)
                        if _nc_val:
                            scores[_nc_val] = _v
                            loaded += 1
            for _a in _ad.get('alertas', []):
                _nc_val = _nc(_a.get('cuit', ''))
                if _nc_val:
                    alertas_cuits.add(_nc_val)
                _n = (_a.get('nombre') or '').strip()
                if _n and _a.get('scoreCompleto'):
                    scores_by_nombre.setdefault(_norm_nombre(_n), _a)
            _archivos_usados.append(f"{os.path.basename(ruta)}({loaded}sc)")
        except Exception as _e:
            print(f"[cartera-vendedor] Error leyendo {ruta}: {_e}", flush=True)

    # Cargar en orden ascendente de prioridad (cartera sobreescribe bcra)
    for _ruta in list(dict.fromkeys([
        os.path.join(os.getcwd(), 'alertas_bcra.json'), ALERTAS_BCRA_FILE,
        os.path.join(os.getcwd(), 'alertas_cartera.json'), ALERTAS_FILE,
    ])):
        _load_score_file(_ruta)

    print(
        f"[cartera-vendedor] vendedor={v!r} | base={len(base)} | "
        f"archivos={_archivos_usados or ['NINGUNO']} | "
        f"scores={len(scores)} | scores_nombre={len(scores_by_nombre)} | alertas={len(alertas_cuits)}",
        flush=True
    )
    # ──────────────────────────────────────────────────────────────────────────

    result = []
    for cc in base:
        nombre = (cc.get('nombre') or '').strip()
        cuit = (cc.get('cuit') or '').strip()
        cuit_n = _nc(cuit)

        # Plan A: cruce por CUIT normalizado
        sc = scores.get(cuit_n, {})

        # Plan B: cruce por nombre normalizado (si plan A falla)
        if not sc.get('scoreCompleto'):
            nombre_norm_b = _norm_nombre(nombre)
            sc = scores_by_nombre.get(nombre_norm_b, {})

        # Plan C: primeras 2 palabras del nombre
        if not sc.get('scoreCompleto'):
            prim2_b = ' '.join(_norm_nombre(nombre).split()[:2])
            for _k, _sv in scores_by_nombre.items():
                if ' '.join(_k.split()[:2]) == prim2_b:
                    sc = _sv
                    break

        # Buscar saldo: exacto primero, luego por primeras 2 palabras
        nombre_norm = _norm_nombre(nombre)
        total_saldo = saldo_map.get(nombre_norm, 0)
        if total_saldo == 0:
            prim2 = ' '.join(nombre_norm.split()[:2])
            for k, sv in saldo_map.items():
                if ' '.join(k.split()[:2]) == prim2:
                    total_saldo = sv
                    break

        limite_credito = float(cc.get('limiteCredito') or 0)
        cupo_disponible = max(0.0, limite_credito - total_saldo) if limite_credito > 0 else None
        score_val            = sc.get('scoreCompleto') or None
        alerta_temprana      = sc.get('alerta_temprana', False)
        bloquear_oportunidad = sc.get('bloquear_oportunidad', False)
        alerta_log           = sc.get('alerta_logistica', '') or _alerta_logistica(cc.get('ciudad', ''))

        result.append({
            'nombre': nombre,
            'cuit': cuit,
            'ciudad': cc.get('ciudad', ''),
            'vendedor': cc.get('vendedor', ''),
            'email': cc.get('email', ''),
            'total_saldo': total_saldo,
            'limite_credito': limite_credito,
            'cupo_disponible': cupo_disponible,
            'score': score_val,
            'scoreRango': sc.get('scoreRango') or None,
            'scoreColor': sc.get('scoreColor') or None,
            'scoreEmoji': sc.get('scoreEmoji') or None,
            'ultimaSit': sc.get('ultimaSit') or 1,
            'alerta': cuit_n in alertas_cuits or alerta_temprana,
            'alerta_temprana': alerta_temprana,
            'bloquear_oportunidad': bloquear_oportunidad,
            'alerta_logistica': alerta_log,
            'oportunidad': bool(
                score_val and score_val >= 750
                and total_saldo == 0
                and not bloquear_oportunidad
            ),
        })

    # Primero con saldo (desc), luego sin saldo alfabético
    result.sort(key=lambda x: (0 if x['total_saldo'] > 0 else 1, -(x['total_saldo'] or 0), x['nombre']))
    resp = jsonify(result)
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route("/cartera-comercial/<vendedor>")
def get_cartera_comercial(vendedor):
    from urllib.parse import unquote
    v = unquote(vendedor).strip().lower()
    result = [c for c in _cartera_comercial if c.get('vendedor', '').lower() == v]
    # Enriquecer con score y situación desde alertas
    try:
        if os.path.exists(ALERTAS_FILE):
            with open(ALERTAS_FILE, 'r', encoding='utf-8') as f:
                alertas_data = json.load(f)
            cartera_scores = {c['cuit']: c for c in alertas_data.get('cartera', [])}
            for cliente in result:
                if cliente['cuit'] in cartera_scores:
                    sc = cartera_scores[cliente['cuit']]
                    cliente['score'] = sc.get('scoreCompleto')
                    cliente['ultimaSit'] = sc.get('ultimaSit', 1)
    except: pass
    return jsonify(result)

@app.route("/scores-cartera", methods=["GET"])
def get_scores_cartera():
    """Devuelve scores de toda la cartera indexados por CUIT normalizado.
    Merge de alertas_bcra.json (queries individuales) + alertas_cartera.json (verificación).
    alertas_cartera tiene prioridad para el mismo CUIT."""
    def _nc2(x):
        return str(x or '').replace('-', '').replace(' ', '').strip()

    scores_out = {}
    archivos_log = []

    for _ruta in list(dict.fromkeys([
        os.path.join(os.getcwd(), 'alertas_bcra.json'), ALERTAS_BCRA_FILE,
        os.path.join(os.getcwd(), 'alertas_cartera.json'), ALERTAS_FILE,
    ])):
        if not os.path.exists(_ruta):
            continue
        try:
            with open(_ruta, 'r', encoding='utf-8') as f:
                _ad = json.load(f)
            antes = len(scores_out)
            # Estructura estándar: {cartera: [...]}
            for c in _ad.get('cartera', []):
                if not c.get('scoreCompleto'):
                    continue
                nc = _nc2(c.get('cuit', ''))
                if nc:
                    scores_out[nc] = {
                        "scoreCompleto":        c.get('scoreCompleto'),
                        "scoreRango":           c.get('scoreRango'),
                        "scoreColor":           c.get('scoreColor'),
                        "scoreEmoji":           c.get('scoreEmoji'),
                        "ultimaSit":            c.get('ultimaSit', 1),
                        "nombre":               c.get('nombre', ''),
                        "alerta_temprana":      c.get('alerta_temprana', False),
                        "bloquear_oportunidad": c.get('bloquear_oportunidad', False),
                        "alerta_logistica":     c.get('alerta_logistica', ''),
                    }
            # Estructura plana: {cuit: score_data}
            if not _ad.get('cartera') and not _ad.get('alertas'):
                for k, v in _ad.items():
                    if isinstance(v, dict) and v.get('scoreCompleto'):
                        nc = _nc2(k)
                        if nc:
                            scores_out[nc] = {
                                "scoreCompleto":        v.get('scoreCompleto'),
                                "scoreRango":           v.get('scoreRango'),
                                "scoreColor":           v.get('scoreColor'),
                                "scoreEmoji":           v.get('scoreEmoji'),
                                "ultimaSit":            v.get('ultimaSit', 1),
                                "nombre":               v.get('nombre', ''),
                                "alerta_temprana":      v.get('alerta_temprana', False),
                                "bloquear_oportunidad": v.get('bloquear_oportunidad', False),
                                "alerta_logistica":     v.get('alerta_logistica', ''),
                            }
            nuevos = len(scores_out) - antes
            archivos_log.append(f"{os.path.basename(_ruta)}(+{nuevos})")
        except Exception as e:
            print(f"[scores-cartera] Error {_ruta}: {e}", flush=True)

    print(f"[scores-cartera] {len(scores_out)} scores totales — {archivos_log}", flush=True)
    if scores_out:
        return jsonify({"ok": True, "scores": scores_out, "total": len(scores_out)})
    return jsonify({"ok": False, "scores": {}, "total": 0})


@app.route("/debug-scores")
def debug_scores():
    """Diagnóstico: muestra qué archivos de scores existen, cuántos tienen score real, y muestra de CUITs."""
    def _nc3(x):
        return str(x or '').replace('-', '').replace(' ', '').strip()

    info = {"data_dir": DATA_DIR, "archivos": {}}
    rutas = {
        "alertas_cartera.json": ALERTAS_FILE,
        "alertas_bcra.json":    ALERTAS_BCRA_FILE,
        "alertas_cartera_cwd":  os.path.join(os.getcwd(), 'alertas_cartera.json'),
        "alertas_bcra_cwd":     os.path.join(os.getcwd(), 'alertas_bcra.json'),
    }
    for nombre, ruta in rutas.items():
        existe = os.path.exists(ruta)
        entry = {"ruta": ruta, "existe": existe}
        if existe:
            try:
                size = os.path.getsize(ruta)
                with open(ruta, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                cartera = d.get('cartera', [])
                con_score = [c for c in cartera if c.get('scoreCompleto')]
                alertas  = d.get('alertas', [])
                entry.update({
                    "size_kb": round(size / 1024, 1),
                    "cartera_total": len(cartera),
                    "cartera_con_score": len(con_score),
                    "alertas_total": len(alertas),
                    "ultima_verif": d.get('ultima_verif', ''),
                    "muestra_cuits_cartera": [_nc3(c.get('cuit','')) for c in cartera[:5]],
                    "muestra_cuits_alertas": [_nc3(a.get('cuit','')) for a in alertas[:5]],
                })
            except Exception as e:
                entry["error"] = str(e)
        info["archivos"][nombre] = entry

    # Muestra de CUITs de cartera_comercial para comparar
    info["cartera_comercial_muestra"] = [
        _nc3(c.get('cuit','')) for c in _cartera_comercial[:5]
    ]
    info["cartera_comercial_total"] = len(_cartera_comercial)
    resp = jsonify(info)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route("/whatsapp_index.json")
def wsp_index_route():
    return send_from_directory(os.getcwd(), 'whatsapp_index.json')

@app.route("/moras.json")
def moras():
    moras_path = os.path.join(DATA_DIR, 'moras_piattelli.json')
    if os.path.exists(moras_path):
        return send_from_directory(DATA_DIR, 'moras_piattelli.json')
    return send_from_directory(os.getcwd(), 'moras_piattelli.json')

@app.route("/upload-moras", methods=["POST"])
def upload_moras():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "Sin archivo"}), 400
        file = request.files['file']
        import io
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file.read()))
        ws = wb.active
        headers = [str(c.value or '').strip().lower() for c in ws[1]]
        col_cuit = next((i for i, h in enumerate(headers) if 'cuit' in h), 0)
        col_fecha = next((i for i, h in enumerate(headers) if 'fecha' in h), 1)
        col_saldo = next((i for i, h in enumerate(headers) if 'saldo' in h or 'deuda' in h or 'adeud' in h), 2)
        moras_dict = {}
        for row in ws.iter_rows(min_row=2, values_only=True):
            cuit = str(row[col_cuit] or '').strip().replace('-', '').replace(' ', '')
            if len(cuit) >= 10:
                fecha = str(row[col_fecha] or '').strip()
                saldo = row[col_saldo]
                try:
                    saldo_str = str(saldo).replace('$','').replace(' ','')
                    if ',' in saldo_str and '.' in saldo_str:
                        saldo_str = saldo_str.replace('.','').replace(',','.')
                    elif ',' in saldo_str:
                        saldo_str = saldo_str.replace(',','.')
                    saldo_num = float(saldo_str)
                except:
                    saldo_num = 0
                if cuit in moras_dict:
                    moras_dict[cuit]['saldoAdeudado'] += saldo_num
                    if fecha < moras_dict[cuit]['fechaMora']:
                        moras_dict[cuit]['fechaMora'] = fecha
                else:
                    moras_dict[cuit] = {"fechaMora": fecha, "saldoAdeudado": saldo_num, "enMora": True}
        moras_path = os.path.join(DATA_DIR, 'moras_piattelli.json')
        with open(moras_path, 'w', encoding='utf-8') as f:
            json.dump(moras_dict, f, ensure_ascii=False, indent=2)
        print(f"[moras] Subidas {len(moras_dict)} moras", flush=True)
        return jsonify({"ok": True, "total": len(moras_dict)})
    except Exception as e:
        import traceback
        print(f"[moras] Error: {traceback.format_exc()}", flush=True)
        return jsonify({"error": str(e)}), 500

@app.route("/cartera_inicial.json")
def cartera_inicial():
    # cartera_comercial.json es la fuente única de verdad (514 clientes con vendedor)
    return send_from_directory(os.getcwd(), 'cartera_comercial.json')

@app.route("/datos-bodega", methods=["GET"])
def get_datos_bodega():
    try:
        if os.path.exists(DATOS_FILE):
            with open(DATOS_FILE, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        return jsonify({})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/datos-bodega", methods=["POST"])
def save_datos_bodega():
    try:
        data = request.get_json(force=True)
        with open(DATOS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/alertas/limpiar", methods=["POST", "DELETE"])
def limpiar_alertas():
    """Elimina físicamente alertas_cartera.json. Más robusto que sobrescribir."""
    try:
        if os.path.exists(ALERTAS_FILE):
            os.remove(ALERTAS_FILE)
            print(f"[alertas] Archivo eliminado: {ALERTAS_FILE}", flush=True)
            return jsonify({"ok": True, "mensaje": "Archivo eliminado con éxito. Las alertas fueron limpiadas."})
        return jsonify({"ok": True, "mensaje": "No había archivo de alertas. La cartera está limpia."})
    except PermissionError as e:
        print(f"[alertas] Sin permisos para eliminar: {e}", flush=True)
        return jsonify({"ok": False, "error": f"Sin permisos para eliminar el archivo: {e}"}), 500
    except Exception as e:
        print(f"[alertas] Error al eliminar: {e}", flush=True)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/alertas", methods=["GET"])
def get_alertas():
    try:
        if os.path.exists(ALERTAS_FILE):
            with open(ALERTAS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = {"alertas": [], "ultima_verif": "", "cartera": []}
    except Exception as e:
        data = {"alertas": [], "ultima_verif": "", "cartera": [], "error": str(e)}
    resp = jsonify(data)
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route("/alertas", methods=["POST"])
def save_alertas():
    try:
        data = request.get_json(force=True)
        # Si viene cartera con scores, mergear con la existente
        if data.get('cartera') and os.path.exists(ALERTAS_FILE):
            try:
                with open(ALERTAS_FILE, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                # Actualizar scores en la cartera existente
                score_map = {c['cuit']: c for c in data['cartera'] if c.get('scoreCompleto')}
                for c in existing.get('cartera', []):
                    if c.get('cuit') in score_map:
                        sc = score_map[c['cuit']]
                        c['scoreCompleto'] = sc.get('scoreCompleto')
                        c['scoreRango'] = sc.get('scoreRango')
                        c['scoreColor'] = sc.get('scoreColor')
                        c['scoreEmoji'] = sc.get('scoreEmoji')
                # Actualizar scores en alertas existentes
                for a in existing.get('alertas', []):
                    if a.get('cuit') in score_map:
                        sc = score_map[a['cuit']]
                        a['scoreCompleto'] = sc.get('scoreCompleto')
                        a['scoreRango'] = sc.get('scoreRango')
                        a['scoreColor'] = sc.get('scoreColor')
                        a['scoreEmoji'] = sc.get('scoreEmoji')
                # Reemplazar alertas si vienen nuevas
                if data.get('alertas') is not None:
                    existing['alertas'] = data['alertas']
                with open(ALERTAS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
                return jsonify({"ok": True})
            except: pass
        with open(ALERTAS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/verificar-cartera", methods=["POST"])
def verificar_cartera():
    if verificacion_estado["corriendo"]:
        return jsonify({"error": "Ya hay una verificacion en curso"}), 400
    try:
        # Siempre usar cartera_comercial.json como fuente canónica — ignorar lista del cliente
        cartera_data = [
            {
                "cuit":      str(c.get("cuit") or "").strip(),
                "nombre":    str(c.get("nombre") or "").strip(),
                "ciudad":    str(c.get("ciudad") or "").strip(),
                "ultimaSit": c.get("ultimaSit", 1),
                "ultimaVerif": c.get("ultimaVerif"),
            }
            for c in _cartera_comercial
            if str(c.get("cuit") or "").strip()
        ]
        if not cartera_data:
            return jsonify({"error": "cartera_comercial.json está vacía o sin CUITs"}), 400
        t = threading.Thread(target=ejecutar_verificacion, args=(cartera_data,), daemon=True)
        t.start()
        return jsonify({"ok": True, "mensaje": f"Verificación iniciada: {len(cartera_data)} clientes desde cartera_comercial.json", "total": len(cartera_data)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/verificar-progreso", methods=["GET"])
def verificar_progreso():
    return jsonify(verificacion_estado)

@app.route("/verificar-reset", methods=["POST", "GET"])
def verificar_reset():
    estaba_corriendo = verificacion_estado["corriendo"]
    verificacion_estado["corriendo"] = False
    verificacion_estado["mensaje"] = "Reset manual. Listo para nueva verificacion."
    print(f"[verif] Reset manual. Estaba corriendo: {estaba_corriendo}", flush=True)
    return jsonify({"ok": True, "estaba_corriendo": estaba_corriendo})

@app.route("/analizar-bodegas", methods=["POST"])
def analizar_bodegas():
    if not GEMINI_KEY:
        return jsonify({"es_negativo": False, "motivo": ""})
    try:
        body = request.get_json(force=True)
        cuit = body.get('cuit', '')
        nombre = body.get('nombre', '')
        mensajes = body.get('mensajes', [])
        es_neg, motivo = analizar_bodegas_server(cuit, nombre, mensajes)
        return jsonify({"es_negativo": es_neg, "motivo": motivo})
    except Exception as e:
        return jsonify({"es_negativo": False, "motivo": str(e)})

@app.route("/afip/<cuit>")
def get_afip(cuit):
    cuit_fmt = cuit[:2] + '-' + cuit[2:10] + '-' + cuit[10:] if len(cuit) == 11 else cuit
    try:
        data, error = consultar_bcra_cached(cuit)
        if data.get('results') and data['results'].get('denominacion'):
            return jsonify({"nombre": data['results']['denominacion']})
    except Exception: pass
    try:
        r = requests.get("https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/Historicas/" + cuit, timeout=25, verify=False)
        if r.status_code == 200:
            nombre2 = r.json().get('results', {}).get('denominacion', '')
            if nombre2: return jsonify({"nombre": nombre2})
    except Exception: pass
    try:
        r = requests.get("https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/" + cuit, timeout=25, verify=False)
        if r.status_code == 200:
            nombre3 = r.json().get('results', {}).get('denominacion', '')
            if nombre3: return jsonify({"nombre": nombre3})
    except Exception: pass
    return jsonify({"nombre": cuit_fmt})

@app.route("/deudas/<cuit>")
def get_deudas(cuit):
    try:
        data, error = consultar_bcra_cached(cuit)
        return jsonify(data), 200
    except Exception as e:
        import traceback
        print(f"[deudas] Excepcion {cuit}: {traceback.format_exc()}", flush=True)
        return jsonify({"results": None, "sin_deudas": None, "error_bcra": str(e)}), 200

def _cheques_cache_path(cuit):
    return os.path.join(DATA_DIR, f'cheques_{cuit}.json')

def _cheques_cache_get(cuit):
    try:
        path = _cheques_cache_path(cuit)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if time.time() - data.get('ts', 0) < 86400:
                print(f"[cheques] {cuit} desde caché disco", flush=True)
                return data.get('payload')
    except: pass
    return None

def _cheques_cache_set(cuit, payload):
    try:
        path = _cheques_cache_path(cuit)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'payload': payload, 'ts': time.time()}, f, ensure_ascii=False)
    except: pass

@app.route("/deudas/<cuit>/cheques")
def get_cheques(cuit):
    urls = [w + "/deudas/" + cuit + "/cheques" for w in BCRA_WORKERS] +            ["https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/ChequesRechazados/" + cuit]
    todos_vacios = True
    for url_idx, url in enumerate(urls):
        via = "Worker1" if url_idx == 0 else "Worker2" if url_idx == 1 else "directo"
        for intento in range(2):
            try:
                r = requests.get(url, timeout=25, verify=False)
                if r.status_code == 200:
                    text = r.text.strip()
                    if not text or len(text) < 10:
                        print(f"[cheques] Vacío via {via} para {cuit}", flush=True)
                        break
                    todos_vacios = False
                    data = r.json()
                    results = data.get('results', data) if isinstance(data, dict) else data
                    payload = {"results": results, "sin_deudas": None, "error_bcra": None}
                    _cheques_cache_set(cuit, payload)
                    print(f"[cheques] OK via {via} para {cuit}", flush=True)
                    return jsonify(payload), 200
                if r.status_code in [520, 521, 522, 523, 524]:
                    break
            except Exception as e:
                print(f"[cheques] Error via {via} intento {intento+1} para {cuit}: {e}", flush=True)
                if intento < 1:
                    time.sleep(0.5)
                    continue
                break
    cached = _cheques_cache_get(cuit)
    if cached:
        print(f"[cheques] {cuit} desde caché (BCRA no disponible)", flush=True)
        return jsonify(cached), 200
    if todos_vacios:
        print(f"[cheques] Sin cheques para {cuit}", flush=True)
        return jsonify({"results": {"causales": []}, "sin_deudas": True, "error_bcra": None}), 200
    return jsonify({"results": None, "sin_deudas": None, "error_bcra": "sin_respuesta"}), 200

@app.route("/deudas/<cuit>/historial")
def get_historial(cuit):
    urls = [w + "/deudas/" + cuit + "/historial" for w in BCRA_WORKERS] +            ["https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/Historicas/" + cuit]
    for url_idx, url in enumerate(urls):
        via = "Worker1" if url_idx == 0 else "Worker2" if url_idx == 1 else "directo"
        for intento in range(2):
            try:
                r = requests.get(url, timeout=25, verify=False)
                if r.status_code == 200:
                    text = r.text.strip()
                    if not text or len(text) < 10:
                        break
                    data = r.json()
                    try:
                        hist_path = os.path.join(DATA_DIR, f'historial_{cuit}.json')
                        with open(hist_path, 'w', encoding='utf-8') as f:
                            json.dump({'payload': data, 'ts': time.time()}, f, ensure_ascii=False)
                    except: pass
                    print(f"[historial] OK via {via} para {cuit}", flush=True)
                    return jsonify(data), 200
                if r.status_code in [520, 521, 522, 523, 524]:
                    break
            except Exception as e:
                print(f"[historial] Error via {via} intento {intento+1} para {cuit}: {e}", flush=True)
                if intento < 1:
                    time.sleep(0.5)
                    continue
                break
    try:
        hist_path = os.path.join(DATA_DIR, f'historial_{cuit}.json')
        if os.path.exists(hist_path):
            with open(hist_path, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if time.time() - cached.get('ts', 0) < 86400:
                print(f"[historial] {cuit} desde caché disco", flush=True)
                return jsonify(cached['payload']), 200
    except: pass
    return jsonify({"results": None, "sin_deudas": None, "error_bcra": "sin_respuesta"}), 200

@app.route("/analizar", methods=["POST"])
def analizar():
    if not GEMINI_KEY and not OPENAI_KEY:
        return jsonify({"error": "API key no configurada"}), 500
    try:
        body = request.get_json()
        prompt = body.get('prompt', '')
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        texto, error = gemini_request(payload, timeout=90)
        if error:
            return jsonify({"error": error}), 500
        return jsonify({"texto": texto})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/procesar-veraz", methods=["POST"])
@app.route("/procesar-informe", methods=["POST"])
def procesar_veraz():
    if not GEMINI_KEY:
        return jsonify({"error": "API key no configurada"}), 500
    try:
        body = request.get_json(force=True)
        if not body:
            return jsonify({"error": "Request body vacio o no es JSON"}), 400
        pdf_base64 = body.get('pdf', '')
        print(f"[procesar-informe] PDF recibido: {len(pdf_base64)} chars, ~{len(pdf_base64)*3//4//1024} KB", flush=True)
        if len(pdf_base64) * 3 // 4 > 20 * 1024 * 1024:
            return jsonify({"error": "PDF demasiado grande (max 20MB)"}), 400
        prompt = (
            "Este puede ser un informe de Veraz/Equifax o de Nosis. Detecta el formato automaticamente y extrae los mismos campos. "
            "Responde SOLO con un objeto JSON valido, sin markdown, sin texto adicional. "
            "Estructura exacta: "
            '{"nombre":"","cuit":"","score":0,"situacion_bcra":"","cheques_rechazados":0,'
            '"monto_cheques":"","saldo_vencido":"","deuda_sistema_financiero":"",'
            '"maximo_atraso":"","entidades_problema":[],"resumen":"",'
            '"socios_directores":[{"nombre":"","cuit_dni":"","cargo":"","score":0,"situacion":""}]} '
            "El array socios_directores debe incluir todos los socios, directores o representantes "
            "legales con su informacion crediticia. Si no hay, dejar array vacio []."
        )
        if not OPENAI_KEY:
            return jsonify({"error": "No hay API key de OpenAI configurada"}), 500
        try:
            import base64 as b64mod
            import fitz
            pdf_bytes = b64mod.b64decode(pdf_base64)
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            imagenes_b64 = []
            for page in list(doc)[:3]:
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat)
                img_bytes = pix.tobytes("png")
                imagenes_b64.append(b64mod.b64encode(img_bytes).decode())
            doc.close()
            print(f"[procesar-informe] PDF convertido a {len(imagenes_b64)} paginas", flush=True)
        except Exception as ex:
            print(f"[procesar-informe] Error convirtiendo PDF: {ex}", flush=True)
            return jsonify({"error": "No se pudo convertir el PDF: " + str(ex)}), 500
        content_oai = [{"type": "text", "text": prompt}]
        for img_b64 in imagenes_b64[:4]:
            content_oai.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + img_b64}})
        headers_oai = {"Content-Type": "application/json", "Authorization": "Bearer " + OPENAI_KEY}
        body_oai = {"model": "gpt-4o", "max_tokens": 1500, "messages": [{"role": "user", "content": content_oai}]}
        try:
            r_oai = requests.post("https://api.openai.com/v1/chat/completions", headers=headers_oai, json=body_oai, timeout=250)
            d_oai = r_oai.json()
            print(f"[procesar-informe] OpenAI status {r_oai.status_code}", flush=True)
            if r_oai.status_code == 200:
                texto = d_oai["choices"][0]["message"]["content"]
            else:
                msg = d_oai.get("error", {}).get("message", "Error OpenAI")
                return jsonify({"error": "Error OpenAI: " + msg}), 503
        except Exception as ex:
            return jsonify({"error": str(ex)}), 503
        if not texto:
            return jsonify({"error": "No se pudo procesar el PDF."}), 503
        texto_limpio = texto.strip().replace("```json", "").replace("```", "").strip()
        import re as re_mod
        match = re_mod.search(r'\{[\s\S]+\}', texto_limpio)
        if match:
            texto_limpio = match.group(0)
        return jsonify(json.loads(texto_limpio))
    except json.JSONDecodeError as e:
        return jsonify({"error": "Error al parsear respuesta: " + str(e)}), 500
    except Exception as e:
        import traceback
        print(f"[procesar-informe] Exception: {traceback.format_exc()}", flush=True)
        return jsonify({"error": str(e)}), 500

@app.route("/test-gemini")
def test_gemini():
    if not GEMINI_KEY:
        return jsonify({"error": "No hay API key"}), 500
    payload = {"contents": [{"parts": [{"text": "Responde solo con la palabra OK"}]}]}
    texto, error = gemini_request(payload)
    if error:
        return jsonify({"error": error}), 500
    return jsonify({"ok": True, "respuesta": texto})

@app.route("/cache-stats")
def cache_stats():
    ahora = time.time()
    activos = sum(1 for v in bcra_cache.values() if ahora - v.get('timestamp', 0) < CACHE_TTL)
    return jsonify({"total": len(bcra_cache), "activos": activos, "ttl_horas": CACHE_TTL/3600})

def _fecha_valida(fecha_str, desde):
    try:
        if not fecha_str: return False
        if '/' in str(fecha_str):
            partes = str(fecha_str).split('/')
            from datetime import datetime
            if len(partes[2]) == 2:
                f = datetime(2000+int(partes[2]), int(partes[1]), int(partes[0]))
            else:
                f = datetime(int(partes[2]), int(partes[1]), int(partes[0]))
        else:
            from datetime import datetime
            f = datetime.fromisoformat(str(fecha_str)[:10])
        return f >= desde
    except:
        return True

@app.route("/health")
def health():
    return jsonify({"status": "ok", "gemini": bool(GEMINI_KEY), "comercial": len(_cartera_comercial)})

@app.route("/cache/limpiar/<cuit>", methods=["POST", "GET"])
def limpiar_cache_cuit(cuit):
    cuit_limpio = cuit.replace("-", "")
    eliminados = [key for key in list(bcra_cache.keys()) if key == cuit_limpio]
    for key in eliminados:
        del bcra_cache[key]
    print(f"[cache] Limpiado CUIT {cuit_limpio}: {eliminados}", flush=True)
    return jsonify({"ok": True, "cuit": cuit_limpio, "eliminados": len(eliminados)})

@app.route("/cache/limpiar-todo", methods=["POST", "GET"])
def limpiar_cache_todo():
    total = len(bcra_cache)
    bcra_cache.clear()
    print(f"[cache] Cache completo limpiado: {total} entradas", flush=True)
    return jsonify({"ok": True, "eliminados": total})

@app.route("/dso-ventas/limpiar", methods=["POST"])
def limpiar_dso_ventas():
    try:
        dso_file = os.path.join(DATA_DIR, 'dso_ventas_historico.json')
        if os.path.exists(dso_file):
            os.remove(dso_file)
        return jsonify({"ok": True, "mensaje": "Historial limpiado"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/dso-saldos", methods=["GET"])
def get_dso_saldos():
    try:
        modo = request.args.get('modo', 'actual')
        f_path = os.path.join(DATA_DIR, 'dso_saldos_historico.json' if modo == 'historico' else 'dso_saldos_actual.json')
        if os.path.exists(f_path):
            with open(f_path, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        return jsonify({"saldos": [], "ultima_actualizacion": ""})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/dso-saldos", methods=["POST"])
def save_dso_saldos():
    try:
        body = request.get_json(force=True)
        nuevos = body.get('saldos', [])
        if not nuevos:
            return jsonify({"error": "Sin saldos"}), 400
        from datetime import datetime
        hoy = datetime.now()
        f_actual = os.path.join(DATA_DIR, 'dso_saldos_actual.json')
        with open(f_actual, 'w', encoding='utf-8') as f:
            json.dump({"saldos": nuevos, "ultima_actualizacion": hoy.strftime('%d/%m/%Y %H:%M')}, f, ensure_ascii=False)
        total = sum(s.get('saldo', 0) for s in nuevos)
        print(f"[dso-saldos] Wipe & write: {len(nuevos)} saldos ${total:,.0f}", flush=True)
        return jsonify({"ok": True, "agregados": len(nuevos), "total": len(nuevos)})
    except Exception as e:
        import traceback
        print(f"[dso-saldos] Error: {traceback.format_exc()}", flush=True)
        return jsonify({"error": str(e)}), 500

@app.route("/dso-cheques", methods=["GET"])
def get_dso_cheques():
    try:
        modo = request.args.get('modo', 'actual')
        f_path = os.path.join(DATA_DIR, 'dso_cheques_historico.json' if modo == 'historico' else 'dso_cheques_actual.json')
        if os.path.exists(f_path):
            with open(f_path, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        return jsonify({"cheques": [], "ultima_actualizacion": ""})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/dso-cheques", methods=["POST"])
def save_dso_cheques():
    try:
        body = request.get_json(force=True)
        nuevos = body.get('cheques', [])
        if not nuevos:
            return jsonify({"error": "Sin cheques"}), 400
        from datetime import datetime
        hoy = datetime.now()
        f_actual = os.path.join(DATA_DIR, 'dso_cheques_actual.json')
        with open(f_actual, 'w', encoding='utf-8') as f:
            json.dump({"cheques": nuevos, "ultima_actualizacion": hoy.strftime('%d/%m/%Y %H:%M')}, f, ensure_ascii=False)
        total = sum(abs(c.get('total', 0)) for c in nuevos)
        print(f"[dso-cheques] Wipe & write: {len(nuevos)} cheques ${total:,.0f}", flush=True)
        return jsonify({"ok": True, "agregados": len(nuevos), "total": len(nuevos)})
    except Exception as e:
        import traceback
        print(f"[dso-cheques] Error: {traceback.format_exc()}", flush=True)
        return jsonify({"error": str(e)}), 500

@app.route("/dso-ventas", methods=["GET"])
def get_dso_ventas():
    try:
        dso_file = os.path.join(DATA_DIR, 'dso_ventas_historico.json')
        if os.path.exists(dso_file):
            with open(dso_file, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        return jsonify({"ventas": [], "ultima_actualizacion": ""})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/dso-ventas", methods=["POST"])
def save_dso_ventas():
    """Smart merge: acumula 120 días usando nro_factura+cliente como clave única."""
    try:
        body = request.get_json(force=True)
        nuevas_ventas = body.get('ventas', [])
        if not nuevas_ventas:
            return jsonify({"error": "Sin ventas"}), 400
        from datetime import datetime, timedelta
        hoy = datetime.now()
        hace_4_meses = hoy - timedelta(days=120)
        dso_file = os.path.join(DATA_DIR, 'dso_ventas_historico.json')
        historico = []
        if os.path.exists(dso_file):
            with open(dso_file, 'r', encoding='utf-8') as f:
                historico = json.load(f).get('ventas', [])
        # Filtrar historico: solo últimos 120 días
        filtrado = []
        for v in historico:
            try:
                fs = (v.get('fecha') or '')[:10]
                fd = datetime.fromisoformat(fs) if '-' in fs else datetime(int(fs[6:]), int(fs[3:5]), int(fs[:2]))
                if fd >= hace_4_meses:
                    filtrado.append(v)
            except: pass
        # Smart merge usando nro_factura+cliente como clave primaria
        def _vkey(v):
            nro = (v.get('nro_factura') or '').strip()
            cli = (v.get('cliente') or '').strip()
            fecha = (v.get('fecha') or '')[:10]
            return (nro + '||' + cli) if nro else (cli + '||' + fecha)
        existentes = {_vkey(v) for v in filtrado}
        agregadas = 0
        for v in nuevas_ventas:
            k = _vkey(v)
            if k not in existentes:
                filtrado.append(v)
                existentes.add(k)
                agregadas += 1
        resultado = {"ventas": filtrado, "ultima_actualizacion": hoy.strftime('%d/%m/%Y %H:%M'), "total_registros": len(filtrado)}
        with open(dso_file, 'w', encoding='utf-8') as f:
            json.dump(resultado, f, ensure_ascii=False, indent=2)
        print(f"[dso-ventas] Smart merge: +{agregadas} nuevas, total: {len(filtrado)}", flush=True)
        return jsonify({"ok": True, "agregadas": agregadas, "total": len(filtrado)})
    except Exception as e:
        import traceback
        print(f"[dso-ventas] Error: {traceback.format_exc()}", flush=True)
        return jsonify({"error": str(e)}), 500

@app.route("/test-modelos")
def test_modelos():
    if not GEMINI_KEY:
        return jsonify({"error": "Sin API key"}), 500
    resultados = {}
    combos = [
        ("gemini-1.5-flash-001", "v1beta"), ("gemini-1.5-flash-002", "v1beta"),
        ("gemini-1.5-pro-001", "v1beta"), ("gemini-2.0-flash-001", "v1beta"), ("gemini-2.5-flash", "v1beta"),
    ]
    for modelo, version in combos:
        key = f"{modelo}/{version}"
        url = f"https://generativelanguage.googleapis.com/{version}/models/{modelo}:generateContent?key={GEMINI_KEY}"
        try:
            r = requests.post(url, headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": "di OK"}]}]}, timeout=25)
            data = r.json()
            if "candidates" in data: resultados[key] = "OK"
            elif "error" in data: resultados[key] = data["error"].get("message", "error")[:100]
            else: resultados[key] = "respuesta inesperada"
        except Exception as e:
            resultados[key] = str(e)[:100]
    return jsonify(resultados)


# ── Saldos / Facturas por cliente ──
_saldos_facturas = []
try:
    _sf_path = os.path.join(DATA_DIR, 'saldos_facturas.json')
    with open(_sf_path, encoding='utf-8') as f:
        _saldos_facturas = json.load(f)
    print(f"[saldos] {len(_saldos_facturas)} facturas cargadas", flush=True)
except Exception as e:
    print(f"[saldos] Error cargando facturas: {e}", flush=True)

# ── Saldos de Gestión (vista semanal para vendedores — separado del DSO) ──
_saldos_gestion = []
try:
    _sg_path = os.path.join(DATA_DIR, 'saldos_gestion_vendedores.json')
    if os.path.exists(_sg_path):
        with open(_sg_path, encoding='utf-8') as f:
            _saldos_gestion = json.load(f)
        print(f"[gestion] {len(_saldos_gestion)} facturas de gestión cargadas", flush=True)
    else:
        _saldos_gestion = list(_saldos_facturas)
        print("[gestion] Usando saldos_facturas como fallback inicial para gestión", flush=True)
except Exception as e:
    _saldos_gestion = list(_saldos_facturas)
    print(f"[gestion] Error cargando gestión: {e}", flush=True)

def _norm_nombre(s):
    import unicodedata, re
    s = str(s or '').strip().upper()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

_SUFIJOS_RE = None
def _norm_ultra(s):
    import unicodedata, re
    global _SUFIJOS_RE
    if _SUFIJOS_RE is None:
        _SUFIJOS_RE = re.compile(
            r'\b(S\.?A\.?|S\.?R\.?L\.?|S\.?H\.?|S\.?A\.?S\.?|S\.?C\.?A\.?|SRLH?)\b',
            re.IGNORECASE
        )
    s = str(s or '').strip().upper()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = _SUFIJOS_RE.sub(' ', s)
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

@app.route("/solvencia/<cuit>")
def get_solvencia_endpoint(cuit):
    """Devuelve datos de solvencia cacheados. Si no hay datos, retorna estado 'no disponible'."""
    from urllib.parse import unquote
    cuit_limpio = str(unquote(cuit)).replace('-', '').replace(' ', '').strip()
    data = get_solvency_data(cuit_limpio)
    if data:
        return jsonify({"ok": True, "data": data})
    return jsonify({"ok": False, "mensaje": "Datos de solvencia temporalmente no disponibles"})

@app.route("/saldos-cliente/<cliente>")
def get_saldos_cliente(cliente):
    from urllib.parse import unquote
    nombre_original = unquote(cliente)
    cn = _norm_nombre(nombre_original)
    # Lee de _saldos_gestion (gestión semanal) — _saldos_facturas es solo para DSO/auditoría
    fuente = _saldos_gestion if _saldos_gestion else _saldos_facturas

    # 1. Match exacto
    result = [f for f in fuente if _norm_nombre(f.get('cliente', '')) == cn]

    # 2. Match por primeras 2 palabras con normUltra (strips S.A., S.R.L., etc.)
    if not result:
        cu = _norm_ultra(nombre_original)
        prim2 = ' '.join(cu.split()[:2])
        if len(prim2) > 3:
            result = [f for f in fuente
                      if ' '.join(_norm_ultra(f.get('cliente', '')).split()[:2]) == prim2]
            if result:
                print(f"[match-2p] '{nombre_original}' → prim2='{prim2}' → {len(result)} facturas", flush=True)

    # 3. Match parcial (≥2 palabras en común, longitud >2)
    if not result:
        palabras = [w for w in cn.split() if len(w) > 2]
        if palabras:
            result = [f for f in fuente
                if sum(1 for p in palabras if p in _norm_nombre(f.get('cliente', '')))
                   >= min(2, len(palabras))]
            if result:
                print(f"[match-parcial] '{nombre_original}' → {len(result)} facturas", flush=True)

    # Audit log: sin match → registrar el string exacto de Odoo para debugging
    if not result:
        clientes_en_sf = list({_norm_nombre(f.get('cliente', '')) for f in fuente})[:5]
        print(f"[match-FAIL] No se encontró match para: '{nombre_original}' (normalizado: '{cn}'). "
              f"Primeros 5 clientes en saldos_facturas: {clientes_en_sf}", flush=True)

    total_saldo = sum(f.get('saldo', 0) for f in result)
    return jsonify({"facturas": result, "total_saldo": total_saldo, "cantidad": len(result)})

@app.route("/saldos-cuit/<cuit>")
def get_saldos_cuit(cuit):
    """Busca facturas por CUIT (prioridad absoluta). Si no hay CUIT en registros, cae a nombre."""
    from urllib.parse import unquote
    cuit_limpio = str(unquote(cuit)).replace('-', '').replace(' ', '').strip()
    # Prioridad 1: buscar por CUIT si los registros lo tienen (fuente: gestión semanal)
    fuente_g = _saldos_gestion if _saldos_gestion else _saldos_facturas
    result = [f for f in fuente_g if str(f.get('cuit', '')).replace('-', '').replace(' ', '').strip() == cuit_limpio]
    if result:
        total_saldo = sum(f.get('saldo', 0) for f in result)
        return jsonify({"facturas": result, "total_saldo": total_saldo, "cantidad": len(result), "metodo": "cuit"})
    # Prioridad 2: buscar por nombre en cartera comercial
    nombre_en_cartera = next(
        (str(c.get('nombre', '')).strip() for c in _cartera_comercial
         if str(c.get('cuit', '')).replace('-', '').replace(' ', '').strip() == cuit_limpio),
        None
    )
    if nombre_en_cartera:
        cn = _norm_nombre(nombre_en_cartera)
        result = [f for f in fuente_g if _norm_nombre(f.get('cliente', '')) == cn]
        total_saldo = sum(f.get('saldo', 0) for f in result)
        return jsonify({"facturas": result, "total_saldo": total_saldo, "cantidad": len(result), "metodo": "nombre"})
    return jsonify({"facturas": [], "total_saldo": 0, "cantidad": 0, "metodo": "nulo"})

@app.route("/upload-saldos-gestion", methods=["POST"])
def upload_saldos_gestion():
    """Saldos semanales de gestión — actualiza la vista comercial sin tocar el DSO de cierre de mes."""
    global _saldos_gestion
    try:
        if 'file' not in request.files:
            return jsonify({"error": "Sin archivo"}), 400
        file = request.files['file']
        import io, openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file.read()))
        ws = wb.active
        primera = [str(c or '').strip() for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        tiene_header = any(p.upper() in ('VENDEDOR', 'CLIENTE', 'SALDO', 'TOTAL', 'FACTURA', 'NUMERO') for p in primera)
        min_row = 2 if tiene_header else 1
        def fmt_fecha(d):
            if not d: return ''
            if hasattr(d, 'strftime'): return d.strftime('%d/%m/%Y')
            s = str(d).strip()
            if len(s) == 10 and s[4] == '-':
                return s[8:] + '/' + s[5:7] + '/' + s[:4]
            return s[:10]
        saldos = []
        for row in ws.iter_rows(min_row=min_row, values_only=True):
            if not row: continue
            vals = list(row) + [None] * 9
            vendedor, cliente, nro_fac, fecha_fac, fecha_pago, total, saldo = vals[:7]
            if not cliente: continue
            try: saldo_f = float(saldo or 0)
            except: saldo_f = 0
            if saldo_f <= 0: continue
            try: total_f = float(total or 0)
            except: total_f = 0
            saldos.append({
                'vendedor': str(vendedor or '').strip(),
                'cliente': str(cliente).strip(),
                'nroFactura': str(nro_fac or '').strip(),
                'fechaFactura': fmt_fecha(fecha_fac),
                'fechaPago': fmt_fecha(fecha_pago),
                'totalFactura': total_f,
                'saldo': saldo_f
            })
        sg_path = os.path.join(DATA_DIR, 'saldos_gestion_vendedores.json')
        with open(sg_path, 'w', encoding='utf-8') as f:
            json.dump(saldos, f, ensure_ascii=False, indent=2)
        ts_path = os.path.join(DATA_DIR, 'saldos_timestamp.json')
        with open(ts_path, 'w') as f:
            json.dump({'ts': time.time(), 'fecha': time.strftime('%d/%m/%Y %H:%M'), 'tipo': 'gestion'}, f)
        _saldos_gestion = saldos
        print(f"[gestion] {len(saldos)} facturas de gestión importadas (wipe & write)", flush=True)
        return jsonify({"ok": True, "total": len(saldos)})
    except Exception as e:
        import traceback
        print(f"[gestion] Error: {traceback.format_exc()}", flush=True)
        return jsonify({"error": str(e)}), 500

@app.route("/saldos-timestamp")
def get_saldos_timestamp():
    """Devuelve timestamp de la última carga de saldos para detección de actualizaciones."""
    ts_path = os.path.join(DATA_DIR, 'saldos_timestamp.json')
    try:
        with open(ts_path, 'r') as f:
            data = json.load(f)
        resp = jsonify(data)
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    except:
        return jsonify({"ts": 0, "fecha": None})

@app.route("/dso-global-saldos")
def get_dso_global_saldos():
    """DSO global y por vendedor desde saldos_facturas.json — fuente única de verdad."""
    from datetime import datetime
    if not _saldos_facturas:
        return jsonify({"dso": None, "saldo_total": 0, "clientes_count": 0, "facturas_count": 0})
    hoy = datetime.now()
    saldo_total = sum(f.get('saldo', 0) for f in _saldos_facturas)
    suma_pond = 0.0
    vencidas = 0
    for f in _saldos_facturas:
        try:
            d, m, y = f['fechaFactura'].split('/')
            fe = datetime(int(y), int(m), int(d))
            suma_pond += f.get('saldo', 0) * max(0, (hoy - fe).days)
        except:
            continue
        try:
            dp, mp, yp = f['fechaPago'].split('/')
            if datetime(int(yp), int(mp), int(dp)) < hoy:
                vencidas += 1
        except:
            pass
    dso = round(suma_pond / saldo_total) if saldo_total > 0 else None
    clientes_unicos = len({f.get('cliente', '') for f in _saldos_facturas if f.get('cliente')})
    return jsonify({
        "dso": dso,
        "saldo_total": saldo_total,
        "clientes_count": clientes_unicos,
        "facturas_count": len(_saldos_facturas),
        "facturas_vencidas": vencidas,
        "ultima_actualizacion": time.strftime('%d/%m/%Y')
    })

@app.route("/upload-saldos-facturas", methods=["POST"])
def upload_saldos_facturas():
    """Recibe Excel Odoo: [Vendedor, Cliente, Nro Factura, Fecha Factura, Fecha Pago, Total, Saldo]"""
    global _saldos_facturas
    try:
        if 'file' not in request.files:
            return jsonify({"error": "Sin archivo"}), 400
        file = request.files['file']
        import io, openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file.read()))
        ws = wb.active

        # Detectar si primera fila es encabezado textual
        primera = [str(c or '').strip() for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        tiene_header = any(p.upper() in ('VENDEDOR', 'CLIENTE', 'SALDO', 'TOTAL', 'FACTURA', 'NUMERO') for p in primera)
        min_row = 2 if tiene_header else 1

        def fmt_fecha(d):
            if not d: return ''
            if hasattr(d, 'strftime'): return d.strftime('%d/%m/%Y')
            s = str(d).strip()
            if len(s) == 10 and s[4] == '-':  # YYYY-MM-DD → DD/MM/YYYY
                return s[8:] + '/' + s[5:7] + '/' + s[:4]
            return s[:10]

        saldos = []
        for row in ws.iter_rows(min_row=min_row, values_only=True):
            if not row: continue
            vals = list(row) + [None] * 9
            vendedor, cliente, nro_fac, fecha_fac, fecha_pago, total, saldo = vals[:7]
            if not cliente: continue
            try: saldo_f = float(saldo or 0)
            except: saldo_f = 0
            if saldo_f <= 0: continue
            try: total_f = float(total or 0)
            except: total_f = 0
            saldos.append({
                'vendedor': str(vendedor or '').strip(),
                'cliente': str(cliente).strip(),
                'nroFactura': str(nro_fac or '').strip(),
                'fechaFactura': fmt_fecha(fecha_fac),
                'fechaPago': fmt_fecha(fecha_pago),
                'totalFactura': total_f,
                'saldo': saldo_f
            })

        sf_path = os.path.join(DATA_DIR, 'saldos_facturas.json')
        with open(sf_path, 'w', encoding='utf-8') as f:
            json.dump(saldos, f, ensure_ascii=False, indent=2)
        ts_path = os.path.join(DATA_DIR, 'saldos_timestamp.json')
        with open(ts_path, 'w') as f:
            json.dump({'ts': time.time(), 'fecha': time.strftime('%d/%m/%Y %H:%M')}, f)
        _saldos_facturas = saldos
        print(f"[saldos] {len(saldos)} facturas importadas (Odoo positional)", flush=True)
        return jsonify({"ok": True, "total": len(saldos)})
    except Exception as e:
        import traceback
        print(f"[saldos] Error: {traceback.format_exc()}", flush=True)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)

# Para Gunicorn (Render): 1 worker + 4 threads = eficiente en 512MB RAM
# Comando: gunicorn main:app --workers 1 --threads 4 --timeout 120 --keep-alive 5
