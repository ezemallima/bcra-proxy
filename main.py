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
    """Intenta con múltiples modelos y reintentos automáticos."""
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

@app.route("/")
def index():
    return send_from_directory('static', 'index.html')

@app.route("/whatsapp_index.json")
def wsp_index():
    return send_from_directory(os.getcwd(), 'whatsapp_index.json')

@app.route("/moras_piattelli.json")
def moras():
    return send_from_directory(os.getcwd(), 'moras_piattelli.json')

@app.route("/deudas/<cuit>")
def get_deudas(cuit):
    try:
        r = requests.get(f"https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/{cuit}", timeout=10, verify=False)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/deudas/<cuit>/historial")
def get_historial(cuit):
    try:
        r = requests.get(f"https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/{cuit}/Historial", timeout=10, verify=False)
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
        texto, error = gemini_request(payload)
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
        prompt = 'Extraé los datos de este informe Veraz/Equifax y respondé SOLO en JSON sin markdown: {"nombre":"","cuit":"","score":0,"situacion_bcra":"","cheques_rechazados":0,"monto_cheques":"","saldo_vencido":"","deuda_sistema_financiero":"","maximo_atraso":"","entidades_problema":[],"resumen":""}'
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
    app.run(host="0.0.0.0", port=8080)
