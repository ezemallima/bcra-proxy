from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import urllib3
import os
import json

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__, static_folder='static')
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB max

GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

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
        r = requests.post(
            f"{GEMINI_URL}?key={GEMINI_KEY}",
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30
        )
        data = r.json()
        if 'error' in data:
            return jsonify({"error": data['error'].get('message', 'Error de Gemini')}), 500
        texto = data['candidates'][0]['content']['parts'][0]['text']
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
        
        prompt = """Analizá este informe Veraz/Equifax y extraé los datos. 
Respondé ÚNICAMENTE con un objeto JSON válido, sin texto adicional, sin markdown, sin explicaciones:
{"nombre":"","cuit":"","score":0,"situacion_bcra":"","cheques_rechazados":0,"monto_cheques":"","saldo_vencido":"","deuda_sistema_financiero":"","maximo_atraso":"","entidades_problema":[],"resumen":""}"""

        payload = {
            "contents": [{
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "application/pdf",
                            "data": pdf_base64
                        }
                    },
                    {"text": prompt}
                ]
            }],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 1024
            }
        }
        
        r = requests.post(
            f"{GEMINI_URL}?key={GEMINI_KEY}",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=60
        )
        
        data = r.json()
        
        if 'error' in data:
            return jsonify({"error": data['error'].get('message', 'Error de Gemini')}), 500
            
        texto = data['candidates'][0]['content']['parts'][0]['text']
        texto_limpio = texto.strip()
        if '```' in texto_limpio:
            texto_limpio = texto_limpio.split('```')[1]
            if texto_limpio.startswith('json'):
                texto_limpio = texto_limpio[4:]
        
        resultado = json.loads(texto_limpio.strip())
        return jsonify(resultado)
        
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Error parseando respuesta: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok", "gemini": bool(GEMINI_KEY)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
