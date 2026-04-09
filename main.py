from flask import Flask, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

BCRA_BASE = "https://api.bcra.gob.ar/centraldedeudores/v1.0"

@app.route("/deudas/<cuit>")
def get_deudas(cuit):
    try:
        r = requests.get(f"{BCRA_BASE}/Deudas/{cuit}", timeout=10, verify=False)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/deudas/<cuit>/historial")
def get_historial(cuit):
    try:
        r = requests.get(f"{BCRA_BASE}/Deudas/{cuit}/Historial", timeout=10, verify=False)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
