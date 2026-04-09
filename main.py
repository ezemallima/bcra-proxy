from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import requests
import urllib3
import os

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__, static_folder='static')
CORS(app)

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

@app.route("/afip/<cuit>")
def get_afip(cuit):
    try:
        r = requests.get(f"https://soa.afip.gob.ar/sr-padron/v2/persona/{cuit}", timeout=8, verify=False)
        data = r.json()
        persona = data.get('data', {})
        actividades = persona.get('actividades', [])
        act_principal = actividades[0].get('descripcion', '-') if actividades else '-'
        return jsonify({
            "estadoClave": persona.get('estadoClave', '-'),
            "tipoClave": persona.get('tipoClave', '-'),
            "actividad": act_principal,
            "nombre": persona.get('nombre', '') or f"{persona.get('apellido','')} {persona.get('nombre','')}".strip()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
