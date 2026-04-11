from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import urllib3
import os
import json
import time

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__, static_folder='static')
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024

GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODELS = [
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

def gemini_request(payload, timeout=45):
    for modelo in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={GEMINI_KEY}"
        for intento in range(3):
            try:
                r = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=timeout)
                data = r.json()
                if 'error' in data:
                    code = data['error'].get('code', 0)
                    msg = data['error'].get('message', '')
                    if code in [429, 503] or 'demanda' in msg.lower() or 'quota' in msg.lower():
                        time.sleep(3 * (intento + 1))
                        continue
                    break
                texto = data['candidates'][0]['content']['parts'][0]['text']
                return texto, None
            except Exception as e:
                if intento < 2:
                    time.sleep(2)
                continue
    return None, "No se pudo conectar con el motor de análisis. Intentá de nuevo en unos minutos."

def consultar_bcra(cuit, reintentos=3):
    """Consulta el BCRA con reintentos y manejo de errores mejorado."""
    url = f"https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/{cuit}"
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
                return None, f"Error {r.status_code}"
        except requests.Timeout:
            if i < reintentos - 1:
                time.sleep(2)
                continue
            return None, "timeout"
        except Exception as e:
            return None, str(e)
    return None, "timeout"

@app.route("/")
def index():
    return send_from_directory('static', 'index.html')

@app.route("/whatsapp_index.json")
def wsp_index():
    return send_from_directory(os.getcwd(), 'whatsapp_index.json')

@app.route("/moras_piattelli.json")
def moras():
    return send_from_directory(os.getcwd(), 'moras_piattelli.json')

@app.route("/cartera_inicial.json")
def cartera_inicial():
    return send_from_directory(os.getcwd(), 'cartera_inicial.json')

# Archivo de alertas persistente
ALERTAS_FILE = os.path.join(os.getcwd(), 'alertas_cartera.json')

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

@app.route("/analizar-bodegas", methods=["POST"])
def analizar_bodegas():
    if not GEMINI_KEY:
        return jsonify({"error": "API key no configurada"}), 500
    try:
        body = request.get_json(force=True)
        cuit = body.get('cuit', '')
        nombre = body.get('nombre', '')
        mensajes = body.get('mensajes', [])
        
        if not mensajes:
            return jsonify({"es_negativo": False, "motivo": ""})
        
        mensajes_texto = '
'.join([f"- {m}" for m in mensajes[:10]])
        prompt = f"""Analizá estos mensajes del grupo de bodegas sobre el cliente {nombre} (CUIT: {cuit}).
Determiná si los mensajes indican un riesgo crediticio REAL para este cliente específico.

MENSAJES:
{mensajes_texto}

IMPORTANTE:
- Solo marcá como negativo si hay referencias DIRECTAS a problemas de pago, deudas impagas, cheques rechazados sin resolver, estafas o desaparición del cliente.
- Si el mensaje menciona "cheques rechazados pero los reemplaza" o similar, NO es negativo.
- Si el mensaje habla de OTRO CUIT o razón social diferente, NO es negativo para este cliente.
- Referencias positivas como "buen cliente", "paga en término", "sin problemas" son POSITIVAS.

Respondé SOLO con JSON sin markdown: {{"es_negativo": true/false, "motivo": "explicación breve en una línea"}}"""

        payload = {{"contents": [{{"parts": [{{"text": prompt}}]}}]}}
        texto, error = gemini_request(payload, timeout=30)
        if error:
            return jsonify({{"es_negativo": False, "motivo": ""}})
        
        texto_limpio = texto.strip().replace('```json','').replace('```','').strip()
        resultado = json.loads(texto_limpio)
        return jsonify(resultado)
    except Exception as e:
        return jsonify({{"es_negativo": False, "motivo": str(e)}})

@app.route("/deudas/<cuit>")
def get_deudas(cuit):
    data, error = consultar_bcra(cuit)
    if error == "timeout":
        return jsonify({"error": "timeout", "mensaje": "El BCRA no respondió. El cliente puede no tener deudas registradas o el servicio está temporalmente caído."}), 200
    if error:
        return jsonify({"error": error}), 500
    return jsonify(data), 200

@app.route("/deudas/<cuit>/historial")
def get_historial(cuit):
    try:
        r = requests.get(f"https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/Historicas/{cuit}", timeout=12, verify=False)
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
def procesar_veraz():
    if not GEMINI_KEY:
        return jsonify({"error": "API key no configurada"}), 500
    try:
        body = request.get_json(force=True)
        pdf_base64 = body.get('pdf', '')
        prompt = (
            'Extraé los datos de este informe Veraz/Equifax. '
            'Respondé SOLO con un objeto JSON válido, sin markdown, sin texto adicional. '
            'Estructura exacta: '
            '{"nombre":"","cuit":"","score":0,"situacion_bcra":"","cheques_rechazados":0,'
            '"monto_cheques":"","saldo_vencido":"","deuda_sistema_financiero":"",'
            '"maximo_atraso":"","entidades_problema":[],"resumen":"",'
            '"socios_directores":[{"nombre":"","cuit_dni":"","cargo":"","score":0,"situacion":""}]} '
            'El array socios_directores debe incluir todos los socios, directores o representantes '
            'legales con su informacion crediticia. Si no hay, dejar array vacio [].'
        )
        payload = {"contents": [{"parts": [
            {"inline_data": {"mime_type": "application/pdf", "data": pdf_base64}},
            {"text": prompt}
        ]}]}
        texto, error = gemini_request(payload, timeout=60)
        if error:
            return jsonify({"error": error}), 500
        texto_limpio = texto.strip().replace('```json','').replace('```','').strip()
        resultado = json.loads(texto_limpio)
        return jsonify(resultado)
    except json.JSONDecodeError:
        return jsonify({"error": "Error al procesar el PDF. Intentá de nuevo."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test-gemini")
def test_gemini():
    if not GEMINI_KEY:
        return jsonify({"error": "No hay API key"}), 500
    payload = {"contents": [{"parts": [{"text": "Respondé solo con la palabra OK"}]}]}
    texto, error = gemini_request(payload)
    if error:
        return jsonify({"error": error}), 500
    return jsonify({"ok": True, "respuesta": texto})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "gemini": bool(GEMINI_KEY)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, timeout=120)
