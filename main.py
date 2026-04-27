from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import urllib3
import os
import json
import time
import threading

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__, static_folder='static')
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024

GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')
OPENAI_KEY = os.environ.get('OPENAI_API_KEY', '')
GEMINI_MODEL = "gemini-2.5-flash"
# Usar disco persistente de Render si existe, sino carpeta local
DATA_DIR = '/data' if os.path.exists('/data') else os.getcwd()
ALERTAS_FILE = os.path.join(DATA_DIR, 'alertas_cartera.json')
DATOS_FILE = os.path.join(DATA_DIR, 'datos_bodega.json')
print(f"[init] Almacenamiento en: {DATA_DIR}", flush=True)
WSP_FILE = os.path.join(os.getcwd(), 'whatsapp_index.json')

# Caché de consultas BCRA — evita re-consultar el mismo CUIT en 24hs
bcra_cache = {}  # {cuit: {data: ..., timestamp: ...}}
CACHE_TTL = 60 * 60 * 24  # 24 horas en segundos

CACHE_TTL_ERROR = 300  # 5 min para errores
BCRA_VACIO = {"results": None, "sin_deudas": None, "error_bcra": None}

def consultar_bcra_cached(cuit):
    """Siempre devuelve (dict, error_str|None). Nunca devuelve data=None."""
    ahora = time.time()
    if cuit in bcra_cache:
        entrada = bcra_cache[cuit]
        ttl = CACHE_TTL_ERROR if entrada.get('es_error') else CACHE_TTL
        if ahora - entrada['timestamp'] < ttl:
            origen = "cache-error" if entrada.get('es_error') else "cache"
            print(f"[bcra] {cuit} desde {origen}", flush=True)
            return entrada['data'], entrada.get('error')
    # Miss — consultar BCRA real
    print(f"[bcra] {cuit} consultando BCRA...", flush=True)
    data, error = consultar_bcra(cuit)
    if error or not data:
        # Siempre guardar objeto consistente, nunca None
        data_cache = {"results": None, "sin_deudas": None, "error_bcra": str(error or "sin_respuesta")}
        bcra_cache[cuit] = {'data': data_cache, 'error': error, 'es_error': True, 'timestamp': ahora}
        print(f"[bcra] {cuit} error: {error} (cacheado 5min)", flush=True)
        return data_cache, error
    bcra_cache[cuit] = {'data': data, 'error': None, 'es_error': False, 'timestamp': ahora}
    print(f"[bcra] {cuit} OK desde BCRA", flush=True)
    return data, None

# Estado de verificación en memoria
verificacion_estado = {
    "corriendo": False,
    "progreso": 0,
    "total": 0,
    "cliente_actual": "",
    "mensaje": ""
}

def gemini_request(payload, timeout=120):
    """Intenta Gemini primero, si falla usa OpenAI como fallback."""
    # --- INTENTO 1: Gemini ---
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

    # --- INTENTO 2: OpenAI como fallback ---
    if OPENAI_KEY:
        try:
            # Extraer el texto del prompt desde el payload de Gemini
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

    return None, "No hay APIs de IA disponibles. Configurá GEMINI_API_KEY o OPENAI_API_KEY."

BCRA_WORKER = "https://orange-recipe-3bb1.ezetombacapo.workers.dev"

def consultar_bcra(cuit, reintentos=3):
    # Usar Cloudflare Worker como proxy para evitar bloqueos del BCRA
    url = BCRA_WORKER + "/deudas/" + cuit
    for i in range(reintentos):
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get('error'):
                    print(f"[bcra] Worker error para {cuit}: {data['error']}", flush=True)
                    if i < reintentos - 1:
                        time.sleep(3)
                        continue
                    return None, data['error']
                # El BCRA devuelve {"status":200,"results":{...}} — normalizar
                if 'status' in data and 'results' in data:
                    data = data  # ya tiene results
                # Normalizar sin_deudas
                results = data.get('results') or {}
                periodos = results.get('periodos') or []
                if not periodos:
                    data['sin_deudas'] = True
                else:
                    data['sin_deudas'] = False
                print(f"[bcra] {cuit} sin_deudas={data['sin_deudas']} periodos={len(periodos)}", flush=True)
                return data, None
            elif r.status_code == 404:
                return {"results": {"denominacion": "", "periodos": []}, "sin_deudas": True}, None
            else:
                if i < reintentos - 1:
                    time.sleep(3)
                    continue
                return None, f"HTTP_{r.status_code}"
        except requests.exceptions.ConnectionError as e:
            print(f"[bcra] ConnectionError Worker intento {i+1} para {cuit}", flush=True)
            if i < reintentos - 1:
                time.sleep(3)
                continue
            return None, "connection_error"
        except Exception as e:
            print(f"[bcra] Error inesperado {cuit}: {e}", flush=True)
            return None, str(e)
    return None, "timeout"

def analizar_bodegas_server(cuit, nombre, mensajes):
    if not mensajes:
        return False, ""
    try:
        mensajes_texto = "\n".join(["- " + m for m in mensajes[:20]])
        prompt = (
            "Sos un Analista de Riesgo Crediticio experto en el sector vitinicola argentino.\n"
            "Analiza estos mensajes del grupo de bodegas sobre " + nombre + " (CUIT: " + cuit + ").\n\n"
            "DICCIONARIO DE TERMINOS (OBLIGATORIO USAR):\n"
            "- LC: Limite de Credito\n"
            "- MM: Millones de pesos\n"
            "- s/ CP: Segun condiciones de pago pactadas\n"
            "- fct: Facturas\n"
            "- opera con...: Relacion comercial activa\n"
            "- contado anticipado: Paga antes de recibir mercaderia (mejor escenario)\n"
            "- pagar con +X dias: Cliente se financia con la bodega (riesgo de flujo)\n"
            "- cheque reemplazado / repuesto: Problema resuelto, NO es negativo\n\n"
            "REGLAS:\n"
            "- Priorizá el chat sobre el reporte financiero. El chat es la realidad operativa.\n"
            "- Solo negativo si hay deudas impagas NO resueltas, estafas o desaparicion.\n"
            "- Si distintas bodegas dicen cosas contradictorias, marcalo como comportamiento_inconsistente=true.\n"
            "- Cheques rechazados pero reemplazados = NO negativo.\n"
            "- Mensaje sobre OTRO CUIT diferente = NO negativo para este cliente.\n"
            "- NUNCA digas que no hay antecedentes si el chat tiene mensajes. Usa la informacion disponible.\n\n"
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

    # Cargar índice de WhatsApp
    wsp_index = {}
    try:
        with open(WSP_FILE, 'r', encoding='utf-8') as f:
            wsp_index = json.load(f)
    except Exception:
        pass

    nuevas_alertas = []
    cartera_actualizada = []

    for i, cliente in enumerate(cartera_data):
        cuit = cliente.get('cuit', '')
        nombre = cliente.get('nombre', '')
        sit_anterior = cliente.get('ultimaSit', 1) or 1

        verificacion_estado["progreso"] = i + 1
        verificacion_estado["cliente_actual"] = nombre
        verificacion_estado["mensaje"] = "Verificando " + str(i+1) + "/" + str(len(cartera_data)) + ": " + nombre

        cliente_actualizado = dict(cliente)

        try:
            # Consultar BCRA
            bcra_data, error = consultar_bcra_cached(cuit)
            if bcra_data.get('results') is not None:
                entidades = []
                periodos = (bcra_data.get('results') or {}).get('periodos') or []
                if periodos:
                    entidades = periodos[0].get('entidades', [])
                max_sit = 1
                if entidades:
                    max_sit = max((e.get('situacion', 1) or 1) for e in entidades)

                cliente_actualizado['ultimaSit'] = max_sit
                cliente_actualizado['ultimaVerif'] = time.strftime('%d/%m/%Y')

                if max_sit > sit_anterior or max_sit >= 3:
                    nuevas_alertas.append({
                        "nombre": nombre,
                        "cuit": cuit,
                        "sitAnterior": sit_anterior,
                        "sitActual": max_sit,
                        "fecha": time.strftime('%d/%m/%Y'),
                        "tipo": "bcra"
                    })
        except Exception:
            pass

        # Analizar grupo bodegas
        try:
            threads = wsp_index.get(cuit, [])
            if threads:
                todos_mensajes = []
                tiene_sospecha = False
                for t in threads:
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
                                "nombre": nombre,
                                "cuit": cuit,
                                "fecha": time.strftime('%d/%m/%Y'),
                                "tipo": "bodegas",
                                "mensajes": [motivo]
                            })
        except Exception:
            pass

        cartera_actualizada.append(cliente_actualizado)

        if i < len(cartera_data) - 1:
            time.sleep(5)

    # Guardar resultados
    ahora = time.strftime('%d/%m/%Y %H:%M')
    try:
        datos_guardar = {
            "alertas": nuevas_alertas,
            "ultima_verif": ahora,
            "cartera": [{"cuit": c.get('cuit'), "ultimaSit": c.get('ultimaSit'), "ultimaVerif": c.get('ultimaVerif')} for c in cartera_actualizada]
        }
        with open(ALERTAS_FILE, 'w', encoding='utf-8') as f:
            json.dump(datos_guardar, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    verificacion_estado["corriendo"] = False
    verificacion_estado["mensaje"] = "Verificacion completada. " + str(len(nuevas_alertas)) + " alerta(s) detectada(s)."
    verificacion_estado["progreso"] = len(cartera_data)

# ─── ENDPOINTS ───────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory('static', 'index.html')

@app.route("/whatsapp_index.json")
def wsp_index_route():
    return send_from_directory(os.getcwd(), 'whatsapp_index.json')

@app.route("/moras.json")
def moras():
    return send_from_directory(os.getcwd(), 'moras_piattelli.json')

@app.route("/cartera_inicial.json")
def cartera_inicial():
    return send_from_directory(os.getcwd(), 'cartera_inicial.json')

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

    # Intento 1: deudas activas (tiene denominacion si hay deuda)
    try:
        data, error = consultar_bcra_cached(cuit)
        if data.get('results') and data['results'].get('denominacion'):
            return jsonify({"nombre": data['results']['denominacion']})
    except Exception:
        pass

    # Intento 2: historial 24 meses (tiene denominacion aunque no haya deuda activa)
    try:
        r = requests.get(
            "https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/Historicas/" + cuit,
            timeout=15, verify=False
        )
        if r.status_code == 200:
            data2 = r.json()
            nombre2 = data2.get('results', {}).get('denominacion', '')
            if nombre2:
                return jsonify({"nombre": nombre2})
    except Exception:
        pass

    # Intento 3: directo deudas sin cache
    try:
        r = requests.get(
            "https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/" + cuit,
            timeout=15, verify=False
        )
        if r.status_code == 200:
            data3 = r.json()
            nombre3 = data3.get('results', {}).get('denominacion', '')
            if nombre3:
                return jsonify({"nombre": nombre3})
    except Exception:
        pass

    # Fallback: CUIT formateado
    return jsonify({"nombre": cuit_fmt})

@app.route("/deudas/<cuit>")
def get_deudas(cuit):
    try:
        data, error = consultar_bcra_cached(cuit)
        # data siempre es un dict consistente, nunca None
        return jsonify(data), 200
    except Exception as e:
        import traceback
        print(f"[deudas] Excepcion {cuit}: {traceback.format_exc()}", flush=True)
        return jsonify({"results": None, "sin_deudas": None, "error_bcra": str(e)}), 200

@app.route("/deudas/<cuit>/cheques")
def get_cheques(cuit):
    url = BCRA_WORKER + "/deudas/" + cuit + "/cheques"
    for intento in range(3):
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                results = data.get('results', data) if isinstance(data, dict) else data
                return jsonify({"results": results, "sin_deudas": None, "error_bcra": None}), 200
            print(f"[cheques] HTTP {r.status_code} para {cuit}", flush=True)
            return jsonify({"results": None, "sin_deudas": None, "error_bcra": f"HTTP {r.status_code}"}), 200
        except requests.exceptions.ConnectionError as e:
            print(f"[cheques] ConnectionError intento {intento+1} para {cuit}", flush=True)
            if intento < 2:
                time.sleep(3)
                continue
            return jsonify({"results": None, "sin_deudas": None, "error_bcra": "connection_error"}), 200
        except Exception as e:
            print(f"[cheques] Error {cuit}: {e}", flush=True)
            return jsonify({"results": None, "sin_deudas": None, "error_bcra": str(e)}), 200
    return jsonify({"results": None, "sin_deudas": None, "error_bcra": "timeout"}), 200

@app.route("/deudas/<cuit>/historial")
def get_historial(cuit):
    url = BCRA_WORKER + "/deudas/" + cuit + "/historial"
    for intento in range(3):
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                return jsonify(r.json()), 200
            print(f"[historial] HTTP {r.status_code} para {cuit}", flush=True)
            return jsonify({"results": None, "sin_deudas": None, "error_bcra": f"HTTP {r.status_code}"}), 200
        except requests.exceptions.ConnectionError as e:
            print(f"[historial] ConnectionError intento {intento+1} para {cuit}", flush=True)
            if intento < 2:
                time.sleep(3)
                continue
            return jsonify({"results": None, "sin_deudas": None, "error_bcra": "connection_error"}), 200
        except Exception as e:
            print(f"[historial] Error {cuit}: {e}", flush=True)
            return jsonify({"results": None, "sin_deudas": None, "error_bcra": str(e)}), 200
    return jsonify({"results": None, "sin_deudas": None, "error_bcra": "timeout"}), 200

@app.route("/analizar", methods=["POST"])
def analizar():
    if not GEMINI_KEY:
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
        # Convertir PDF a imagenes y enviar a OpenAI gpt-4o
        if not OPENAI_KEY:
            return jsonify({"error": "No hay API key de OpenAI configurada"}), 500
        try:
            import base64 as b64mod
            import fitz  # PyMuPDF
            pdf_bytes = b64mod.b64decode(pdf_base64)
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            imagenes_b64 = []
            for page in doc:
                mat = fitz.Matrix(2, 2)  # zoom 2x para mejor calidad
                pix = page.get_pixmap(matrix=mat)
                img_bytes = pix.tobytes("png")
                imagenes_b64.append(b64mod.b64encode(img_bytes).decode())
            doc.close()
            print(f"[procesar-informe] PDF convertido a {len(imagenes_b64)} paginas", flush=True)
        except Exception as ex:
            print(f"[procesar-informe] Error convirtiendo PDF: {ex}", flush=True)
            return jsonify({"error": "No se pudo convertir el PDF: " + str(ex)}), 500

        content_oai = [{"type": "text", "text": prompt}]
        for img_b64 in imagenes_b64[:4]:  # max 4 paginas
            content_oai.append({
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + img_b64}
            })

        headers_oai = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + OPENAI_KEY
        }
        body_oai = {
            "model": "gpt-4o",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": content_oai}]
        }
        try:
            r_oai = requests.post("https://api.openai.com/v1/chat/completions",
                headers=headers_oai, json=body_oai, timeout=120)
            d_oai = r_oai.json()
            print(f"[procesar-informe] OpenAI status {r_oai.status_code}", flush=True)
            if r_oai.status_code == 200:
                texto = d_oai["choices"][0]["message"]["content"]
            else:
                msg = d_oai.get("error", {}).get("message", "Error OpenAI")
                print(f"[procesar-informe] OpenAI error: {msg}", flush=True)
                return jsonify({"error": "Error OpenAI: " + msg}), 503
        except Exception as ex:
            print(f"[procesar-informe] Excepcion OpenAI: {ex}", flush=True)
            return jsonify({"error": str(ex)}), 503
        if not texto:
            return jsonify({"error": "No se pudo procesar el PDF."}), 503
        texto_limpio = texto.strip().replace("```json", "").replace("```", "").strip()
        import re as re_mod
        match = re_mod.search(r'\{[\s\S]+\}', texto_limpio)
        if match:
            texto_limpio = match.group(0)
        resultado = json.loads(texto_limpio)
        return jsonify(resultado)
    except json.JSONDecodeError as e:
        print(f"[procesar-informe] JSON decode error: {e}", flush=True)
        return jsonify({"error": "Error al parsear respuesta de Gemini: " + str(e)}), 500
    except Exception as e:
        import traceback
        print(f"[procesar-informe] Exception: {traceback.format_exc()}", flush=True)
        return jsonify({"error": str(e), "detalle": traceback.format_exc()}), 500

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
    import time
    ahora = time.time()
    activos = sum(1 for v in bcra_cache.values() if ahora - v['timestamp'] < CACHE_TTL)
    return jsonify({"total": len(bcra_cache), "activos": activos, "ttl_horas": CACHE_TTL/3600})

def _fecha_valida(fecha_str, desde):
    """Retorna True si la fecha es >= desde"""
    try:
        if not fecha_str: return False
        if '/' in str(fecha_str):
            partes = str(fecha_str).split('/')
            if len(partes[2]) == 2:
                from datetime import datetime
                f = datetime(2000+int(partes[2]), int(partes[1]), int(partes[0]))
            else:
                from datetime import datetime
                f = datetime(int(partes[2]), int(partes[1]), int(partes[0]))
        else:
            from datetime import datetime
            f = datetime.fromisoformat(str(fecha_str)[:10])
        return f >= desde
    except:
        return True  # si no parsea, mantener

@app.route("/health")
def health():
    return jsonify({"status": "ok", "gemini": bool(GEMINI_KEY)})

@app.route("/dso-ventas/limpiar", methods=["POST"])
def limpiar_dso_ventas():
    """Elimina el historial de ventas para re-carga limpia"""
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
        if modo == 'historico':
            f_path = os.path.join(DATA_DIR, 'dso_saldos_historico.json')
        else:
            f_path = os.path.join(DATA_DIR, 'dso_saldos_actual.json')
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

        # 1. ACTUAL — reemplaza siempre (para DSO global)
        f_actual = os.path.join(DATA_DIR, 'dso_saldos_actual.json')
        with open(f_actual, 'w', encoding='utf-8') as f:
            json.dump({"saldos": nuevos, "ultima_actualizacion": hoy.strftime('%d/%m/%Y %H:%M')}, f, ensure_ascii=False)

        # 2. HISTORICO — acumula 4 meses (para score individual)
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
        if modo == 'historico':
            f_path = os.path.join(DATA_DIR, 'dso_cheques_historico.json')
        else:
            f_path = os.path.join(DATA_DIR, 'dso_cheques_actual.json')
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

        # 1. ACTUAL — reemplaza siempre (para DSO global)
        f_actual = os.path.join(DATA_DIR, 'dso_cheques_actual.json')
        with open(f_actual, 'w', encoding='utf-8') as f:
            json.dump({"cheques": nuevos, "ultima_actualizacion": hoy.strftime('%d/%m/%Y %H:%M')}, f, ensure_ascii=False)

        # 2. HISTORICO — acumula 4 meses (para score individual)
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
    """Devuelve el historial acumulado de ventas DSO"""
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
    """Acumula ventas nuevas manteniendo max 4 meses de historial"""
    try:
        body = request.get_json(force=True)
        nuevas_ventas = body.get('ventas', [])
        if not nuevas_ventas:
            return jsonify({"error": "Sin ventas"}), 400

        dso_file = os.path.join(DATA_DIR, 'dso_ventas_historico.json')

        # Cargar historial existente
        historico = []
        if os.path.exists(dso_file):
            with open(dso_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                historico = data.get('ventas', [])

        # Determinar corte de 4 meses atrás
        from datetime import datetime, timedelta
        hoy = datetime.now()
        hace_4_meses = hoy - timedelta(days=120)

        # Filtrar historial: solo mantener los últimos 4 meses
        historico_filtrado = []
        for v in historico:
            try:
                # Fecha puede venir como DD/MM/YYYY o YYYY-MM-DD
                fecha_str = v.get('fecha', '')
                if '/' in fecha_str:
                    partes = fecha_str.split('/')
                    if len(partes) == 3:
                        fecha = datetime(int(partes[2]), int(partes[1]), int(partes[0]))
                    else:
                        continue
                else:
                    fecha = datetime.fromisoformat(fecha_str[:10])
                if fecha >= hace_4_meses:
                    historico_filtrado.append(v)
            except Exception:
                pass

        def normalizar_fecha(f):
            """Convierte cualquier formato a YYYY-MM-DD"""
            if not f: return f
            s = str(f).strip()
            if len(s) >= 10 and s[4] == '-': return s[:10]  # ya es YYYY-MM-DD
            if '/' in s:
                p = s.split('/')
                if len(p) == 3:
                    try:
                        a, b, c = int(p[0]), int(p[1]), int(p[2])
                        anio = 2000 + c if c < 100 else c
                        # Formato M/DD/YY (americano): si b > 12, entonces a=mes, b=dia
                        if b > 12: mes, dia = a, b
                        # Formato DD/MM/YY: si a > 12, entonces a=dia, b=mes
                        elif a > 12: dia, mes = a, b
                        # Ambiguo: en Argentina es DD/MM/YY
                        else: dia, mes = a, b
                        # Validar rango
                        if 1 <= mes <= 12 and 1 <= dia <= 31:
                            return f"{anio}-{mes:02d}-{dia:02d}"
                    except:
                        pass
            return s

        # Agregar nuevas ventas evitando duplicados exactos
        existentes = set((v.get('cliente',''), v.get('fecha',''), str(v.get('total',''))) for v in historico_filtrado)
        agregadas = 0
        for v in nuevas_ventas:
            v['fecha'] = normalizar_fecha(v.get('fecha',''))
            key = (v.get('cliente',''), v.get('fecha',''), str(v.get('total','')))
            if key not in existentes:
                historico_filtrado.append(v)
                existentes.add(key)
                agregadas += 1

        # Guardar
        resultado = {
            "ventas": historico_filtrado,
            "ultima_actualizacion": hoy.strftime('%d/%m/%Y %H:%M'),
            "total_registros": len(historico_filtrado)
        }
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
        ("gemini-1.5-flash-001", "v1beta"),
        ("gemini-1.5-flash-002", "v1beta"),
        ("gemini-1.5-pro-001", "v1beta"),
        ("gemini-2.0-flash-001", "v1beta"),
        ("gemini-2.5-flash", "v1beta"),
    ]
    for modelo, version in combos:
        key = f"{modelo}/{version}"
        url = f"https://generativelanguage.googleapis.com/{version}/models/{modelo}:generateContent?key={GEMINI_KEY}"
        try:
            r = requests.post(url, headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": "di OK"}]}]}, timeout=10)
            data = r.json()
            if "candidates" in data:
                resultados[key] = "OK"
            elif "error" in data:
                resultados[key] = data["error"].get("message", "error")[:100]
            else:
                resultados[key] = "respuesta inesperada"
        except Exception as e:
            resultados[key] = str(e)[:100]
    return jsonify(resultados)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
