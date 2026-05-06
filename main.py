from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import urllib3
import os
import json
import time
import threading
import gc
try:
    import boto3
    BOTO3_OK = True
except ImportError:
    BOTO3_OK = False
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
ALERTAS_FILE = os.path.join(DATA_DIR, 'alertas_cartera.json')
DATOS_FILE = os.path.join(DATA_DIR, 'datos_bodega.json')
print(f"[init] Almacenamiento en: {DATA_DIR}", flush=True)
WSP_FILE = os.path.join(os.getcwd(), 'whatsapp_index.json')

bcra_cache = {}
CACHE_TTL = 60 * 60 * 2
CACHE_TTL_ERROR = 300
BCRA_VACIO = {"results": None, "sin_deudas": None, "error_bcra": None}

# ── Cartera comercial ──
_cartera_comercial = []
try:
    _cc_path = os.path.join(os.getcwd(), 'cartera_comercial.json')
    with open(_cc_path, encoding='utf-8') as f:
        _cartera_comercial = json.load(f)
    print(f"[comercial] {len(_cartera_comercial)} clientes cargados", flush=True)
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
            aws_secret_access_key=aws_secret
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


def calcular_score_servidor(cuit, bcra_data, en_mora=None):
    """Score completo con historial 24m, cheques y mora.
    Mejoras Gemini: memoria de historial, hard caps sit4/5, puntos ganados con datos reales.
    """
    puntos = 0
    cuit_limpio = str(cuit).replace('-', '').replace(' ', '').strip()
    sin_deudas_real = bcra_data.get('sin_deudas', False)
    periodos = (bcra_data.get('results') or {}).get('periodos') or []
    max_sit = 1
    nro_entidades = 0
    monto_total_m = 0

    if periodos:
        entidades = periodos[0].get('entidades', [])
        nro_entidades = len(entidades)
        if entidades:
            max_sit = max((e.get('situacion', 1) or 1) for e in entidades)
            monto_total_m = sum(e.get('monto', 0) or 0 for e in entidades) / 1000
    elif sin_deudas_real:
        max_sit = 1

    # ── N_PERIODOS_H: usar historial guardado (sin límite de TTL) ──────────
    hist_path = os.path.join(DATA_DIR, f'historial_{cuit_limpio}.json')
    hist_cached = None
    try:
        if os.path.exists(hist_path):
            with open(hist_path, 'r') as f:
                hc = json.load(f)
            hist_cached = hc.get('payload')  # sin límite de TTL — memoria del historial
    except: pass

    periodos_para_nph = (hist_cached.get('results') or {}).get('periodos') or [] if hist_cached else periodos

    n_periodos_h = 0
    n_periodos_recientes = 0
    for idx_p, p in enumerate(periodos_para_nph[:24]):
        tiene_deuda = any((e.get('monto') or 0) > 0 for e in p.get('entidades', []))
        if tiene_deuda:
            n_periodos_h += 1
            if idx_p < 6:
                n_periodos_recientes += 1
    n_periodos_h = min(n_periodos_h, n_periodos_recientes * 4)

    # ── 1. SITUACIÓN BCRA ponderada + validación de historial crediticio ───
    # monto_total_m está en miles de pesos → monto real = monto_total_m * 1000
    monto_real = monto_total_m * 1000  # pesos reales

    if max_sit == 1:
        # Validar profundidad crediticia — sit1 con deuda mínima no es igual a sit1 con $5M
        if monto_real == 0:
            # Sin deuda en sistema financiero — sin historial bancario
            pts_sit = 200
        elif monto_real < 500000:
            # Perfil pequeño — baja exposición crediticia
            pts_sit = 250
        elif monto_real < 2500000:
            # Perfil moderado — historial en crecimiento
            pts_sit = 300
        else:
            # Perfil consolidado — maneja volúmenes similares al crédito que pide
            if   n_periodos_h >= 12: pts_sit = 400
            elif n_periodos_h >= 6:  pts_sit = 350
            elif n_periodos_h >= 2:  pts_sit = 300
            else:                    pts_sit = 250
    elif max_sit == 2: pts_sit = 200
    elif max_sit == 3: pts_sit = 50
    else:              pts_sit = 0
    puntos += pts_sit

    # ── 2. HISTORIAL 24M — intentar fresco, sino usar caché guardado ───────
    pts_hist = 0  # 0 hasta tener datos reales
    penalidad_arrastre = False
    hist_fresco = None
    try:
        if hist_cached:
            with open(hist_path, 'r') as _tf:
                if time.time() - json.load(_tf).get('ts', 0) < 86400:
                    hist_fresco = hist_cached
    except: pass

    if not hist_fresco:
        urls_hist = [w + "/deudas/" + cuit_limpio + "/historial" for w in BCRA_WORKERS] +                     ["https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/Historicas/" + cuit_limpio]
        for intento_global in range(6):
            url_h = urls_hist[intento_global % len(urls_hist)]
            try:
                r_h = requests.get(url_h, timeout=25, verify=False)
                if r_h.status_code == 200 and len(r_h.text.strip()) > 10:
                    hist_fresco = r_h.json()
                    try:
                        with open(hist_path, 'w') as f:
                            json.dump({'payload': hist_fresco, 'ts': time.time()}, f)
                    except: pass
                    print(f"[score] {cuit_limpio} historial OK intento {intento_global+1}", flush=True)
                    break
                time.sleep(3)
            except Exception as e:
                print(f"[score] {cuit_limpio} historial error intento {intento_global+1}: {e}", flush=True)
                time.sleep(3)
        # Si sigue sin datos frescos, usar caché guardado aunque sea viejo
        if not hist_fresco and hist_cached:
            hist_fresco = hist_cached
            print(f"[score] {cuit_limpio} historial usando caché guardado (memoria)", flush=True)

    if hist_fresco:
        periodos_h = (hist_fresco.get('results') or {}).get('periodos') or []
        # Regla 6 meses: cualquier sit≥3 en los últimos 6 meses → pts_hist=0 automático
        sit_grave_6m = any(
            max(((e.get('situacion') or 1) for e in p.get('entidades', [])), default=1) >= 3
            for p in periodos_h[:6]
        )
        if sit_grave_6m:
            pts_hist = 0
            print(f"[score] {cuit_limpio} sit≥3 en últimos 6m → pts_hist=0", flush=True)
        else:
            meses_malos = sum(1 for p in periodos_h[:24]
                if max(((e.get('situacion') or 1) for e in p.get('entidades', [])), default=1) > 1)
            # Arrastre 2-5 meses → penalización -25% global al final
            penalidad_arrastre = 2 <= meses_malos <= 5
            if meses_malos > 5:
                pts_hist = 0
            elif meses_malos > 0:
                pts_hist = 75 if meses_malos <= 2 else 0
            else:
                if   n_periodos_h >= 12: pts_hist = 150
                elif n_periodos_h >= 3:  pts_hist = 90
                elif n_periodos_h >= 1:  pts_hist = 40
                else:                    pts_hist = 60
    elif sin_deudas_real:
        pts_hist = 60
    else:
        pts_hist = 0  # sin datos = 0, no neutral
        print(f"[score] {cuit_limpio} historial sin datos — pts_hist=0", flush=True)
    puntos += pts_hist

    # ── 3. CHEQUES — intentar fresco, sino usar caché guardado ─────────────
    pts_cheq = 0  # 0 hasta tener datos reales
    cheq_path = os.path.join(DATA_DIR, f'cheques_{cuit_limpio}.json')
    cheq_cached = None
    try:
        if os.path.exists(cheq_path):
            with open(cheq_path, 'r') as f:
                cc = json.load(f)
            cheq_cached = cc.get('payload')  # sin límite de TTL
    except: pass

    cheq_fresco = None
    try:
        if cheq_cached:
            with open(cheq_path, 'r') as _tf:
                if time.time() - json.load(_tf).get('ts', 0) < 86400:
                    cheq_fresco = cheq_cached
    except: pass

    if not cheq_fresco:
        urls_cheq = [w + "/deudas/" + cuit_limpio + "/cheques" for w in BCRA_WORKERS] +                     ["https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/ChequesRechazados/" + cuit_limpio]
        for intento_global in range(6):
            url_c = urls_cheq[intento_global % len(urls_cheq)]
            try:
                r_c = requests.get(url_c, timeout=25, verify=False)
                if r_c.status_code == 200 and len(r_c.text.strip()) > 10:
                    cheq_fresco = r_c.json()
                    try:
                        with open(cheq_path, 'w') as f:
                            json.dump({'payload': cheq_fresco, 'ts': time.time()}, f)
                    except: pass
                    print(f"[score] {cuit_limpio} cheques OK intento {intento_global+1}", flush=True)
                    break
                elif r_c.status_code == 404:
                    cheq_fresco = {"results": {"causales": []}, "sin_deudas": True}
                    break
                time.sleep(3)
            except Exception as e:
                print(f"[score] {cuit_limpio} cheques error intento {intento_global+1}: {e}", flush=True)
                time.sleep(3)
        if not cheq_fresco and cheq_cached:
            cheq_fresco = cheq_cached
            print(f"[score] {cuit_limpio} cheques usando caché guardado (memoria)", flush=True)

    if cheq_fresco:
        if cheq_fresco.get('sin_deudas'):
            # Sin cheques — puntaje según antigüedad en sistema financiero
            if   n_periodos_h >= 12: pts_cheq = 150
            elif n_periodos_h >= 6:  pts_cheq = 100
            elif n_periodos_h >= 2:  pts_cheq = 70
            elif n_periodos_h >= 1:  pts_cheq = 50
            else:                    pts_cheq = 30  # sin historial
        else:
            res_c = (cheq_fresco.get('results') or {}) if isinstance(cheq_fresco, dict) else {}
            causales = res_c.get('causales') or [] if isinstance(res_c, dict) else []
            detalles = []
            for causal in causales:
                for ent in causal.get('entidades', []):
                    detalles.extend(ent.get('detalle', []))
            total_ch = len(detalles)
            activos_ch = sum(1 for d in detalles
                if not d.get('fechaPago') or d.get('estadoMulta') == 'IMPAGA')
            if activos_ch > 5 or total_ch >= 113:
                print(f"[score] {cuit_limpio} cheques críticos: {activos_ch} activos, {total_ch} total → Rechazar", flush=True)
                return {"score": 1, "rango": "Rechazar", "color": "#7f1d1d", "emoji": "⛔"}
            if total_ch == 0:
                pts_cheq = 150 if n_periodos_h > 6 else (70 if n_periodos_h >= 1 else 40)
            elif activos_ch == 0: pts_cheq = 75
            else:                 pts_cheq = 0
    else:
        pts_cheq = 0  # sin datos = 0, no neutral
        print(f"[score] {cuit_limpio} cheques sin datos — pts_cheq=0", flush=True)
    # Regla de riesgo cruzado: sit BCRA cap al puntaje de cheques
    if max_sit >= 3:
        pts_cheq = 0   # Sit 3+: cheques anulados incluso sin rechazos activos
    elif max_sit >= 2 and pts_cheq > 75:
        pts_cheq = 75  # Sit 2: techo 75/150
    puntos += pts_cheq

    # ── 4. MORA PIATTELLI — normalización de CUIT ──────────────────────────
    if en_mora is None:
        try:
            moras_path = os.path.join(DATA_DIR, 'moras_piattelli.json')
            if not os.path.exists(moras_path):
                moras_path = os.path.join(os.getcwd(), 'moras_piattelli.json')
            with open(moras_path, 'r', encoding='utf-8') as mf:
                moras_d = json.load(mf)
            # Normalizar ambos lados antes de comparar
            moras_norm = {str(k).replace('-','').replace(' ','').strip() for k in moras_d.keys()}
            en_mora = cuit_limpio in moras_norm
        except:
            en_mora = False

    # Clientes nuevos (no en cartera activa) no acumulan puntos de relación comercial
    en_cartera = any(
        str(c.get('cuit', '')).replace('-', '').replace(' ', '').strip() == cuit_limpio
        for c in _cartera_comercial
    )
    if en_cartera:
        puntos += 0 if en_mora else 100   # Historial Piattelli
        puntos += 0 if en_mora else 50    # DSO individual
        puntos += 30                      # Red bodegas
    else:
        print(f"[score] {cuit_limpio} no en cartera → Piattelli/DSO/Bodegas=0", flush=True)

    # ── 5. CONCENTRACIÓN DEUDA ─────────────────────────────────────────────
    if nro_entidades == 0 or sin_deudas_real: pts_conc = 39
    elif nro_entidades == 1 and monto_total_m < 50:   pts_conc = 35
    elif nro_entidades <= 2 and monto_total_m < 100:  pts_conc = 28
    elif nro_entidades <= 3 and monto_total_m < 500:  pts_conc = 20
    elif nro_entidades <= 5 and monto_total_m < 2000: pts_conc = 10
    else: pts_conc = 0
    puntos += pts_conc

    # ── 6. SOLVENCIA — ratio de apalancamiento BCRA/AFIP ──────────────────
    solvency = get_solvency_data(cuit_limpio)
    if solvency:
        ingresos = solvency.get('ingresos_anuales') or 0
        if ingresos > 0:
            deuda_bcra_pesos = monto_total_m * 1000  # miles → pesos
            ratio = deuda_bcra_pesos / ingresos
            if ratio > 0.5:
                puntos -= 200
                print(f"[score] {cuit_limpio} ratio apalancamiento {ratio:.2f} → -200 pts", flush=True)

    # ── PENALIDAD ARRASTRE — 2-5 meses irregulares → -25% global ──────────
    if penalidad_arrastre:
        puntos = round(puntos * 0.75)
        print(f"[score] {cuit_limpio} arrastre irregular → -25% global", flush=True)

    # ── TECHOS DUROS — se aplican al final ─────────────────────────────────
    if en_mora:         puntos = min(puntos, 300)
    if max_sit >= 5:    puntos = min(puntos, 1)    # sit5/6 irrecuperable
    elif max_sit >= 4:  puntos = min(puntos, 250)  # sit4 alto riesgo
    elif max_sit == 3:  puntos = min(puntos, 400)  # sit3 cap

    score = max(1, min(999, round(puntos)))
    if score >= 700: rango, color, emoji = 'Excelente', '#16a34a', '🟢'
    elif score >= 400: rango, color, emoji = 'Bueno',    '#ca8a04', '🟡'
    elif score >= 200: rango, color, emoji = 'Revisar',  '#ea580c', '🟠'
    elif score >= 100: rango, color, emoji = 'Alto riesgo', '#dc2626', '🔴'
    else:              rango, color, emoji = 'Rechazar', '#7f1d1d', '⛔'

    return {"score": score, "rango": rango, "color": color, "emoji": emoji}


def ejecutar_verificacion(cartera_data):
    global verificacion_estado
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

    try:
        for i, cliente in enumerate(cartera_data):
            cuit = cliente.get('cuit', '')
            nombre = cliente.get('nombre', '')
            sit_anterior = cliente.get('ultimaSit', 1) or 1

            verificacion_estado["progreso"] = i + 1
            verificacion_estado["cliente_actual"] = nombre
            verificacion_estado["mensaje"] = "Verificando " + str(i+1) + "/" + str(len(cartera_data)) + ": " + nombre

            cliente_actualizado = dict(cliente)

            try:
                # Intentar Lambda primero (trae deudas + historial + cheques en una sola llamada)
                lambda_result = consultar_bcra_lambda(cuit)
                if lambda_result:
                    bcra_data, hist_lambda, cheq_lambda = lambda_result
                    # Guardar historial y cheques en caché para que calcular_score_servidor los use
                    try:
                        hist_path = os.path.join(DATA_DIR, f'historial_{cuit}.json')
                        with open(hist_path, 'w') as f:
                            json.dump({'payload': hist_lambda, 'ts': time.time()}, f)
                        cheq_path = os.path.join(DATA_DIR, f'cheques_{cuit}.json')
                        with open(cheq_path, 'w') as f:
                            json.dump({'payload': cheq_lambda, 'ts': time.time()}, f)
                    except: pass
                    error = None
                else:
                    # Fallback a Workers de Cloudflare
                    bcra_data, error = consultar_bcra_cached(cuit)
                score_data = None
                try:
                    score_data = calcular_score_servidor(cuit, bcra_data or {}, en_mora=None)
                    cliente_actualizado['scoreCompleto'] = score_data['score']
                    cliente_actualizado['scoreRango'] = score_data['rango']
                    cliente_actualizado['scoreColor'] = score_data['color']
                    cliente_actualizado['scoreEmoji'] = score_data['emoji']
                    print(f"[verif] {cuit} score={score_data['score']}", flush=True)
                except Exception as e_score:
                    print(f"[verif] Error score {cuit}: {e_score}", flush=True)

                if bcra_data and bcra_data.get('results') is not None:
                    periodos = (bcra_data.get('results') or {}).get('periodos') or []
                    entidades = periodos[0].get('entidades', []) if periodos else []
                    max_sit = max((e.get('situacion', 1) or 1) for e in entidades) if entidades else 1
                    cliente_actualizado['ultimaSit'] = max_sit
                    cliente_actualizado['ultimaVerif'] = time.strftime('%d/%m/%Y')

                    if max_sit > sit_anterior or max_sit >= 3:
                        alerta = {
                            "nombre": nombre, "cuit": cuit,
                            "sitAnterior": sit_anterior, "sitActual": max_sit,
                            "fecha": time.strftime('%d/%m/%Y'), "tipo": "bcra"
                        }
                        if score_data:
                            alerta.update({"scoreCompleto": score_data["score"], "scoreRango": score_data["rango"],
                                           "scoreColor": score_data["color"], "scoreEmoji": score_data["emoji"]})
                        nuevas_alertas.append(alerta)
                else:
                    cliente_actualizado['ultimaVerif'] = time.strftime('%d/%m/%Y')
            except Exception as e_verif:
                print(f"[verif] Error {cuit}: {e_verif}", flush=True)

            try:
                threads = wsp_index.get(cuit, [])
                from datetime import datetime, timedelta
                hace_6_meses = datetime.now() - timedelta(days=180)
                threads_recientes = []
                for t in threads:
                    fecha_str = t.get('fecha') or (t.get('mensajes', [{}])[0].get('fecha') if t.get('mensajes') else None)
                    if fecha_str:
                        try:
                            if datetime.fromisoformat(str(fecha_str)[:10]) >= hace_6_meses:
                                threads_recientes.append(t)
                        except: pass
                if threads_recientes:
                    todos_mensajes = []
                    tiene_sospecha = False
                    for t in threads_recientes:
                        for m in t.get('mensajes', []):
                            texto_msg = m.get('texto', '')
                            todos_mensajes.append(m.get('autor', '') + ': ' + texto_msg)
                            if any(p in texto_msg.lower() for p in palabras_riesgo):
                                tiene_sospecha = True
                    if tiene_sospecha:
                        ya_existe = any(a['cuit'] == cuit and a['tipo'] == 'bodegas' for a in nuevas_alertas)
                        if not ya_existe:
                            es_negativo, motivo = analizar_bodegas_server(cuit, nombre, todos_mensajes[:10])
                            if es_negativo:
                                nuevas_alertas.append({
                                    "nombre": nombre, "cuit": cuit,
                                    "fecha": time.strftime('%d/%m/%Y'),
                                    "tipo": "bodegas", "mensajes": [motivo]
                                })
            except Exception:
                pass

            cartera_actualizada.append(cliente_actualizado)
            gc.collect()  # liberar memoria después de cada cliente

            # Guardado parcial cada 50 clientes
            if (i + 1) % 50 == 0:
                try:
                    parcial = {
                        "alertas": nuevas_alertas,
                        "ultima_verif": time.strftime('%d/%m/%Y %H:%M') + ' (parcial)',
                        "cartera": [{
                            "cuit": c.get('cuit'), "ultimaSit": c.get('ultimaSit'),
                            "ultimaVerif": c.get('ultimaVerif'), "scoreCompleto": c.get('scoreCompleto'),
                            "scoreRango": c.get('scoreRango'), "scoreColor": c.get('scoreColor'),
                            "scoreEmoji": c.get('scoreEmoji')
                        } for c in cartera_actualizada]
                    }
                    with open(ALERTAS_FILE, 'w', encoding='utf-8') as f:
                        json.dump(parcial, f, ensure_ascii=False)
                    print(f"[verif] Guardado parcial en cliente {i+1}", flush=True)
                except: pass

            if i < len(cartera_data) - 1:
                time.sleep(0.5)

        ahora = time.strftime('%d/%m/%Y %H:%M')
        try:
            datos_guardar = {
                "alertas": nuevas_alertas,
                "ultima_verif": ahora,
                "cartera": [{
                    "cuit": c.get('cuit'), "ultimaSit": c.get('ultimaSit'),
                    "ultimaVerif": c.get('ultimaVerif'), "scoreCompleto": c.get('scoreCompleto'),
                    "scoreRango": c.get('scoreRango'), "scoreColor": c.get('scoreColor'),
                    "scoreEmoji": c.get('scoreEmoji')
                } for c in cartera_actualizada]
            }
            with open(ALERTAS_FILE, 'w', encoding='utf-8') as f:
                json.dump(datos_guardar, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[verif] Error guardando alertas: {e}", flush=True)

        verificacion_estado["mensaje"] = "Verificacion completada. " + str(len(nuevas_alertas)) + " alerta(s) detectada(s)."
        verificacion_estado["progreso"] = len(cartera_data)

    except Exception as e:
        print(f"[verif] Error general: {e}", flush=True)
        verificacion_estado["mensaje"] = f"Error: {str(e)}"
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
    """Devuelve scores de toda la cartera. Si no existen los calcula en background."""
    try:
        if os.path.exists(ALERTAS_FILE):
            with open(ALERTAS_FILE, 'r', encoding='utf-8') as f:
                alertas_data = json.load(f)
            cartera = alertas_data.get('cartera', [])
            con_score = [c for c in cartera if c.get('scoreCompleto')]
            total = len(_cartera_comercial)
            if con_score:  # devolver cualquier score disponible
                return jsonify({
                    "ok": True,
                    "scores": {c['cuit']: {
                        "scoreCompleto": c.get('scoreCompleto'),
                        "scoreRango": c.get('scoreRango'),
                        "scoreColor": c.get('scoreColor'),
                        "scoreEmoji": c.get('scoreEmoji'),
                        "ultimaSit": c.get('ultimaSit', 1)
                    } for c in con_score if c.get('cuit')},
                    "total": len(con_score)
                })
        # No hay scores suficientes todavía
        return jsonify({"ok": False, "mensaje": "Sin scores disponibles. Corré la verificación desde la app principal.", "total": 0, "scores": {}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "scores": {}})

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

@app.route("/alertas", methods=["GET"])
def get_alertas():
    try:
        if os.path.exists(ALERTAS_FILE):
            with open(ALERTAS_FILE, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        return jsonify({"alertas": [], "ultima_verif": "", "cartera": []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        body = request.get_json(force=True)
        cartera_data = body.get('cartera', [])
        if not cartera_data:
            return jsonify({"error": "Cartera vacia"}), 400
        t = threading.Thread(target=ejecutar_verificacion, args=(cartera_data,), daemon=True)
        t.start()
        return jsonify({"ok": True, "mensaje": "Verificacion iniciada en el servidor"})
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
        from datetime import datetime, timedelta
        hoy = datetime.now()
        f_actual = os.path.join(DATA_DIR, 'dso_saldos_actual.json')
        with open(f_actual, 'w', encoding='utf-8') as f:
            json.dump({"saldos": nuevos, "ultima_actualizacion": hoy.strftime('%d/%m/%Y %H:%M')}, f, ensure_ascii=False)
        f_hist = os.path.join(DATA_DIR, 'dso_saldos_historico.json')
        historico = []
        if os.path.exists(f_hist):
            with open(f_hist, 'r', encoding='utf-8') as f:
                historico = json.load(f).get('saldos', [])
        hace_4_meses = hoy - timedelta(days=120)
        filtrado = [s for s in historico if _fecha_valida(s.get('fecha_factura',''), hace_4_meses)]
        existentes = set((s.get('cliente',''), s.get('fecha_factura',''), str(s.get('saldo',''))) for s in filtrado)
        for s in nuevos:
            key = (s.get('cliente',''), s.get('fecha_factura',''), str(s.get('saldo','')))
            if key not in existentes:
                filtrado.append(s)
                existentes.add(key)
        with open(f_hist, 'w', encoding='utf-8') as f:
            json.dump({"saldos": filtrado, "ultima_actualizacion": hoy.strftime('%d/%m/%Y %H:%M'), "total_registros": len(filtrado)}, f, ensure_ascii=False)
        total = sum(s.get('saldo', 0) for s in nuevos)
        print(f"[dso-saldos] Actual: {len(nuevos)} registros ${total:,.0f} | Historico: {len(filtrado)}", flush=True)
        return jsonify({"ok": True, "agregados": len(nuevos), "total": len(filtrado)})
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
        from datetime import datetime, timedelta
        hoy = datetime.now()
        f_actual = os.path.join(DATA_DIR, 'dso_cheques_actual.json')
        with open(f_actual, 'w', encoding='utf-8') as f:
            json.dump({"cheques": nuevos, "ultima_actualizacion": hoy.strftime('%d/%m/%Y %H:%M')}, f, ensure_ascii=False)
        f_hist = os.path.join(DATA_DIR, 'dso_cheques_historico.json')
        historico = []
        if os.path.exists(f_hist):
            with open(f_hist, 'r', encoding='utf-8') as f:
                historico = json.load(f).get('cheques', [])
        hace_4_meses = hoy - timedelta(days=120)
        filtrado = [c for c in historico if _fecha_valida(c.get('fecha_pago',''), hace_4_meses)]
        existentes = set((c.get('cliente',''), c.get('fecha_pago',''), str(c.get('total',''))) for c in filtrado)
        for c in nuevos:
            key = (c.get('cliente',''), c.get('fecha_pago',''), str(c.get('total','')))
            if key not in existentes:
                filtrado.append(c)
                existentes.add(key)
        with open(f_hist, 'w', encoding='utf-8') as f:
            json.dump({"cheques": filtrado, "ultima_actualizacion": hoy.strftime('%d/%m/%Y %H:%M'), "total_registros": len(filtrado)}, f, ensure_ascii=False)
        total = sum(abs(c.get('total', 0)) for c in nuevos)
        print(f"[dso-cheques] Actual: {len(nuevos)} registros ${total:,.0f} | Historico: {len(filtrado)}", flush=True)
        return jsonify({"ok": True, "agregados": len(nuevos), "total": len(filtrado)})
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
    try:
        body = request.get_json(force=True)
        nuevas_ventas = body.get('ventas', [])
        if not nuevas_ventas:
            return jsonify({"error": "Sin ventas"}), 400
        dso_file = os.path.join(DATA_DIR, 'dso_ventas_historico.json')
        historico = []
        if os.path.exists(dso_file):
            with open(dso_file, 'r', encoding='utf-8') as f:
                historico = json.load(f).get('ventas', [])
        from datetime import datetime, timedelta
        hoy = datetime.now()
        hace_4_meses = hoy - timedelta(days=120)
        historico_filtrado = []
        for v in historico:
            try:
                fecha_str = v.get('fecha', '')
                if '/' in fecha_str:
                    partes = fecha_str.split('/')
                    if len(partes) == 3:
                        fecha = datetime(int(partes[2]), int(partes[1]), int(partes[0]))
                    else: continue
                else:
                    fecha = datetime.fromisoformat(fecha_str[:10])
                if fecha >= hace_4_meses:
                    historico_filtrado.append(v)
            except Exception: pass

        def normalizar_fecha(f):
            if not f: return f
            s = str(f).strip()
            if len(s) >= 10 and s[4] == '-': return s[:10]
            if '/' in s:
                p = s.split('/')
                if len(p) == 3:
                    try:
                        a, b, c = int(p[0]), int(p[1]), int(p[2])
                        anio = 2000 + c if c < 100 else c
                        if b > 12: mes, dia = a, b
                        elif a > 12: dia, mes = a, b
                        else: dia, mes = a, b
                        if 1 <= mes <= 12 and 1 <= dia <= 31:
                            return f"{anio}-{mes:02d}-{dia:02d}"
                    except: pass
            return s

        existentes = set((v.get('cliente',''), v.get('fecha',''), str(v.get('total',''))) for v in historico_filtrado)
        agregadas = 0
        for v in nuevas_ventas:
            v['fecha'] = normalizar_fecha(v.get('fecha',''))
            key = (v.get('cliente',''), v.get('fecha',''), str(v.get('total','')))
            if key not in existentes:
                historico_filtrado.append(v)
                existentes.add(key)
                agregadas += 1

        resultado = {"ventas": historico_filtrado, "ultima_actualizacion": hoy.strftime('%d/%m/%Y %H:%M'), "total_registros": len(historico_filtrado)}
        with open(dso_file, 'w', encoding='utf-8') as f:
            json.dump(resultado, f, ensure_ascii=False, indent=2)
        print(f"[dso-ventas] Agregadas {agregadas} ventas nuevas, total: {len(historico_filtrado)}", flush=True)
        return jsonify({"ok": True, "agregadas": agregadas, "total": len(historico_filtrado)})
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
    _sf_path = os.path.join(os.getcwd(), 'saldos_facturas.json')
    with open(_sf_path, encoding='utf-8') as f:
        _saldos_facturas = json.load(f)
    print(f"[saldos] {len(_saldos_facturas)} facturas cargadas", flush=True)
except Exception as e:
    print(f"[saldos] Error cargando facturas: {e}", flush=True)

def _norm_nombre(s):
    import unicodedata, re
    s = str(s or '').strip().upper()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
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

    # 1. Match exacto
    result = [f for f in _saldos_facturas if _norm_nombre(f.get('cliente', '')) == cn]

    # 2. Match por primeras 2 palabras (caso Odoo: "ARRAYGADA LAURA" → "ARRAYGADA LAURA CAROLINA")
    if not result:
        prim2 = ' '.join(cn.split()[:2])
        if len(prim2) > 3:
            result = [f for f in _saldos_facturas
                      if _norm_nombre(f.get('cliente', '')).startswith(prim2)]
            if result:
                print(f"[match-2p] '{nombre_original}' → prim2='{prim2}' → {len(result)} facturas", flush=True)

    # 3. Match parcial (≥2 palabras en común, longitud >2)
    if not result:
        palabras = [w for w in cn.split() if len(w) > 2]
        if palabras:
            result = [f for f in _saldos_facturas
                if sum(1 for p in palabras if p in _norm_nombre(f.get('cliente', '')))
                   >= min(2, len(palabras))]
            if result:
                print(f"[match-parcial] '{nombre_original}' → {len(result)} facturas", flush=True)

    # Audit log: sin match → registrar el string exacto de Odoo para debugging
    if not result:
        clientes_en_sf = list({_norm_nombre(f.get('cliente', '')) for f in _saldos_facturas})[:5]
        print(f"[match-FAIL] No se encontró match para: '{nombre_original}' (normalizado: '{cn}'). "
              f"Primeros 5 clientes en saldos_facturas: {clientes_en_sf}", flush=True)

    total_saldo = sum(f.get('saldo', 0) for f in result)
    return jsonify({"facturas": result, "total_saldo": total_saldo, "cantidad": len(result)})

@app.route("/saldos-cuit/<cuit>")
def get_saldos_cuit(cuit):
    """Busca facturas por CUIT (prioridad absoluta). Si no hay CUIT en registros, cae a nombre."""
    from urllib.parse import unquote
    cuit_limpio = str(unquote(cuit)).replace('-', '').replace(' ', '').strip()
    # Prioridad 1: buscar por CUIT si los registros lo tienen
    result = [f for f in _saldos_facturas if str(f.get('cuit', '')).replace('-', '').replace(' ', '').strip() == cuit_limpio]
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
        result = [f for f in _saldos_facturas if _norm_nombre(f.get('cliente', '')) == cn]
        total_saldo = sum(f.get('saldo', 0) for f in result)
        return jsonify({"facturas": result, "total_saldo": total_saldo, "cantidad": len(result), "metodo": "nombre"})
    return jsonify({"facturas": [], "total_saldo": 0, "cantidad": 0, "metodo": "nulo"})

@app.route("/saldos-timestamp")
def get_saldos_timestamp():
    """Devuelve timestamp de la última carga de saldos para detección de actualizaciones."""
    ts_path = os.path.join(os.getcwd(), 'saldos_timestamp.json')
    try:
        with open(ts_path, 'r') as f:
            data = json.load(f)
        return jsonify(data)
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

        sf_path = os.path.join(os.getcwd(), 'saldos_facturas.json')
        with open(sf_path, 'w', encoding='utf-8') as f:
            json.dump(saldos, f, ensure_ascii=False, indent=2)
        ts_path = os.path.join(os.getcwd(), 'saldos_timestamp.json')
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
