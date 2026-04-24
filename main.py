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

def consultar_bcra_cached(cuit):
    ahora = time.time()
    if cuit in bcra_cache:
        entrada = bcra_cache[cuit]
        if ahora - entrada['timestamp'] < CACHE_TTL:
            return entrada['data'], None  # hit de caché
    # Miss — consultar BCRA real
    data, error = consultar_bcra(cuit)
    if data and not error:
        bcra_cache[cuit] = {'data': data, 'timestamp': ahora}
    return data, error

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

def consultar_bcra(cuit, reintentos=3):
    url = "https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/" + cuit
    for i in range(reintentos):
        try:
            r = requests.get(url, timeout=12, verify=False)
            if r.status_code == 200:
                return r.json(), None
            elif r.status_code == 404:
                return {"results": {"denominacion": "", "periodos": []}, "sin_deudas": True}, None
            elif r.status_code in [500, 503]:
                if i < reintentos - 1:
                    time.sleep(2)
                    continue
                return None, "timeout"
            else:
                return None, "Error " + str(r.status_code)
        except requests.Timeout:
            if i < reintentos - 1:
                time.sleep(2)
                continue
            return None, "timeout"
        except Exception as e:
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
            if bcra_data and not error:
                entidades = []
                try:
                    entidades = bcra_data['results']['periodos'][0]['entidades']
                except Exception:
                    pass
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
        if data and not error:
            nombre = data.get('results', {}).get('denominacion', '')
            if nombre:
                return jsonify({"nombre": nombre})
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

    # Intento 4: API TangoFactura (ARCA/AFIP) — trae nombre, domicilio, estado
    try:
        r = requests.get(
            "https://afip.tangofactura.com/Rest/GetContribuyenteFull?cuit=" + cuit,
            timeout=10, verify=False,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        print(f"[afip] TangoFactura status: {r.status_code}, bytes: {len(r.content)}", flush=True)
        if r.status_code == 200:
            data4 = r.json()
            print(f"[afip] TangoFactura response: {str(data4)[:300]}", flush=True)
            # TangoFactura puede devolver el objeto directo o dentro de 'contribuyente'
            contrib = data4.get('contribuyente') or data4.get('Contribuyente') or data4 or {}
            nombre4 = contrib.get('nombreApellidoRazonSocial', '')
            estado4 = contrib.get('estadoClave', '')
            domicilio4 = ''
            dom = contrib.get('domicilioFiscal') or {}
            partes = [
                dom.get('direccion',''),
                dom.get('localidad',''),
                dom.get('descripcionProvincia','')
            ]
            domicilio4 = ', '.join([p for p in partes if p])
            if nombre4:
                return jsonify({
                    "nombre": nombre4,
                    "estado_afip": estado4,
                    "domicilio": domicilio4
                })
    except Exception:
        pass

    # Fallback: CUIT formateado
    return jsonify({"nombre": cuit_fmt})

@app.route("/deudas/<cuit>")
def get_deudas(cuit):
    data, error = consultar_bcra_cached(cuit)
    if error == "timeout":
        return jsonify({"error": "timeout", "mensaje": "El BCRA no respondio."}), 200
    if error:
        return jsonify({"error": error}), 500
    return jsonify(data), 200

@app.route("/deudas/<cuit>/cheques")
def get_cheques(cuit):
    try:
        r = requests.get("https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/ChequesRechazados/" + cuit, timeout=12, verify=False)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/deudas/<cuit>/historial")
def get_historial(cuit):
    try:
        r = requests.get("https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/Historicas/" + cuit, timeout=12, verify=False)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

@app.route("/health")
def health():
    return jsonify({"status": "ok", "gemini": bool(GEMINI_KEY)})

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
