from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import urllib3
import os
import json

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__, static_folder='static')
CORS(app)

ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

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
        url = f"https://soa.afip.gob.ar/sr-padron/v2/persona/{cuit}"
        r = requests.get(url, headers={"accept": "application/json"}, timeout=8, verify=False)
        data = r.json()
        persona = data.get('data', {})
        if not persona:
            return jsonify({"error": "No encontrado"}), 404
        actividades = persona.get('actividades', [])
        act_principal = actividades[0].get('descripcion', '-') if actividades else '-'
        impuestos = persona.get('impuestos', [])
        condicion_iva = '-'
        for imp in impuestos:
            if imp.get('idImpuesto') == 30:
                condicion_iva = imp.get('descripcionImpuesto', '-')
                break
        monotributo = persona.get('categoriasMonotributo', [])
        cat_mono = monotributo[0].get('descripcionCategoria', '') if monotributo else ''
        domicilio = persona.get('domicilioFiscal', {})
        dom_str = f"{domicilio.get('direccion','')}, {domicilio.get('localidad','')}, {domicilio.get('descripcionProvincia','')}".strip(', ')
        return jsonify({
            "nombre": persona.get('razonSocial') or f"{persona.get('apellido','')} {persona.get('nombre','')}".strip(),
            "estadoClave": persona.get('estadoClave', '-'),
            "tipoClave": persona.get('tipoClave', '-'),
            "condicionIva": condicion_iva,
            "categoriaMono": cat_mono,
            "actividad": act_principal,
            "domicilioFiscal": dom_str or '-'
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/analizar", methods=["POST"])
def analizar():
    if not ANTHROPIC_KEY:
        return jsonify({"error": "API key no configurada"}), 500
    try:
        body = request.get_json()
        prompt = body.get('prompt', '')
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        data = r.json()
        texto = data.get('content', [{}])[0].get('text', 'Sin respuesta')
        return jsonify({"texto": texto})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/procesar-veraz", methods=["POST"])
def procesar_veraz():
    if not ANTHROPIC_KEY:
        return jsonify({"error": "API key no configurada"}), 500
    try:
        body = request.get_json()
        pdf_base64 = body.get('pdf', '')
        prompt = 'Extraé los datos de este informe Veraz/Equifax y respondé SOLO en JSON sin markdown: {"nombre":"","cuit":"","score":0,"situacion_bcra":"","cheques_rechazados":0,"monto_cheques":"","saldo_vencido":"","deuda_sistema_financiero":"","maximo_atraso":"","entidades_problema":[],"resumen":""}'
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1000, "messages": [{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_base64}},
                {"type": "text", "text": prompt}
            ]}]},
            timeout=30
        )
        data = r.json()
        texto = data.get('content', [{}])[0].get('text', '{}')
        resultado = json.loads(texto.replace('```json','').replace('```','').strip())
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
