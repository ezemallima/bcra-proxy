from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import urllib3
import os
import json
import time
import threading
import base64
import io
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import pandas as pd
    PANDAS_OK = True
except ImportError:
    PANDAS_OK = False

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__, static_folder='static')
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024

GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]
ALERTAS_FILE = os.path.join(os.getcwd(), 'alertas_cartera.json')
DATOS_FILE = os.path.join(os.getcwd(), 'datos_bodega.json')
WSP_FILE = os.path.join(os.getcwd(), 'whatsapp_index.json')

# Caché BCRA en memoria — evita consultas repetidas
import time as time_module
bcra_cache = {}
BCRA_CACHE_TTL = 86400  # 24 horas

def bcra_get_cache(cuit):
    cached = bcra_cache.get(cuit)
    if cached and (time_module.time() - cached['ts']) < BCRA_CACHE_TTL:
        return cached['data']
    return None

def bcra_set_cache(cuit, data):
    bcra_cache[cuit] = {'data': data, 'ts': time_module.time()}

# Estado de verificación en memoria
verificacion_estado = {
    "corriendo": False,
    "progreso": 0,
    "total": 0,
    "cliente_actual": "",
    "mensaje": ""
}

def gemini_request(payload, timeout=45):
    for modelo in GEMINI_MODELS:
        url = "https://generativelanguage.googleapis.com/v1beta/models/" + modelo + ":generateContent?key=" + GEMINI_KEY
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
            except Exception:
                if intento < 2:
                    time.sleep(2)
                continue
    return None, "No se pudo conectar."

def consultar_bcra(cuit, reintentos=3):
    # Verificar caché primero
    cached = bcra_get_cache(cuit)
    if cached:
        return cached, None
    url = "https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/" + cuit
    for i in range(reintentos):
        try:
            r = requests.get(url, timeout=12, verify=False)
            if r.status_code == 200:
                data = r.json()
                bcra_set_cache(cuit, data)
                return data, None
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
    if not GEMINI_KEY or not mensajes:
        return False, ""
    try:
        mensajes_texto = "\n".join(["- " + m for m in mensajes[:10]])
        prompt = (
            "Analiza estos mensajes del grupo de bodegas sobre " + nombre + " (CUIT: " + cuit + ").\n"
            "Determina si hay riesgo crediticio REAL para este cliente especifico.\n\n"
            "MENSAJES:\n" + mensajes_texto + "\n\n"
            "REGLAS:\n"
            "- Solo negativo si hay deudas impagas NO resueltas, estafas o desaparicion.\n"
            "- Cheques rechazados pero reemplazados = NO negativo.\n"
            "- Mensaje sobre OTRO CUIT diferente = NO negativo para este cliente.\n"
            "- Buen cliente, paga en termino = POSITIVO.\n\n"
            'Responde SOLO con este JSON: {"es_negativo": false, "motivo": "texto"}'
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        texto, error = gemini_request(payload, timeout=30)
        if error or not texto:
            return False, ""
        texto_limpio = texto.strip().replace("```json", "").replace("```", "").strip()
        resultado = json.loads(texto_limpio)
        return resultado.get("es_negativo", False), resultado.get("motivo", "")
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

    # Verificar BCRA en paralelo — 8 consultas simultáneas
    def verificar_cliente_bcra(cliente):
        cuit = cliente.get('cuit', '')
        nombre = cliente.get('nombre', '')
        sit_anterior = cliente.get('ultimaSit', 1) or 1
        cliente_actualizado = dict(cliente)
        alerta = None
        try:
            bcra_data, error = consultar_bcra(cuit)
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
                    alerta = {
                        "nombre": nombre, "cuit": cuit,
                        "sitAnterior": sit_anterior, "sitActual": max_sit,
                        "fecha": time.strftime('%d/%m/%Y'), "tipo": "bcra"
                    }
        except Exception:
            pass
        return cliente_actualizado, alerta

    verificacion_estado["mensaje"] = "Consultando BCRA en paralelo..."
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(verificar_cliente_bcra, c): i for i, c in enumerate(cartera_data)}
        completados = 0
        for future in as_completed(futures):
            completados += 1
            verificacion_estado["progreso"] = completados
            verificacion_estado["mensaje"] = f"BCRA: {completados}/{len(cartera_data)} consultados"
            try:
                cliente_act, alerta = future.result()
                cartera_actualizada.append(cliente_act)
                if alerta:
                    nuevas_alertas.append(alerta)
            except Exception:
                pass

    # Analizar grupo bodegas (secuencial, usa IA)
    verificacion_estado["mensaje"] = "Analizando grupo de bodegas..."
    for i, cliente in enumerate(cartera_actualizada):
        cuit = cliente.get('cuit', '')
        nombre = cliente.get('nombre', '')
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
                                "nombre": nombre, "cuit": cuit,
                                "fecha": time.strftime('%d/%m/%Y'),
                                "tipo": "bodegas", "mensajes": [motivo]
                            })
        except Exception:
            pass

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
    try:
        data, error = consultar_bcra(cuit)
        if data and not error:
            nombre = data.get('results', {}).get('denominacion', '')
            if nombre:
                return jsonify({"nombre": nombre})
    except Exception:
        pass
    return jsonify({"nombre": "", "error": "No encontrado"})

@app.route("/deudas/<cuit>")
def get_deudas(cuit):
    data, error = consultar_bcra(cuit)
    if error == "timeout":
        return jsonify({"error": "timeout", "mensaje": "El BCRA no respondio."}), 200
    if error:
        return jsonify({"error": error}), 500
    return jsonify(data), 200

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
        pdf_base64 = body.get('pdf', '')
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
        payload = {"contents": [{"parts": [
            {"inline_data": {"mime_type": "application/pdf", "data": pdf_base64}},
            {"text": prompt}
        ]}]}
        texto, error = gemini_request(payload, timeout=60)
        if error:
            return jsonify({"error": error}), 500
        texto_limpio = texto.strip().replace("```json", "").replace("```", "").strip()
        resultado = json.loads(texto_limpio)
        return jsonify(resultado)
    except json.JSONDecodeError:
        return jsonify({"error": "Error al procesar el PDF. Intenta de nuevo."}), 500
    except Exception as e:
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

@app.route("/calcular-dso", methods=["POST"])
def calcular_dso():
    if not PANDAS_OK:
        return jsonify({"error": "pandas no disponible"}), 500
    try:
        body = request.get_json(force=True)
        archivos = body.get('archivos', {})
        umbral_bajo = int(body.get('umbral_bajo', 45))
        umbral_alto = int(body.get('umbral_alto', 65))

        def leer_excel(b64):
            data = base64.b64decode(b64)
            return pd.read_excel(io.BytesIO(data))

        df_saldos = None
        df_ventas = None
        df_cheques = None

        if archivos.get('saldos'):
            df_saldos = leer_excel(archivos['saldos'])
        if archivos.get('ventas'):
            df_ventas = leer_excel(archivos['ventas'])
        if archivos.get('cheques'):
            df_cheques = leer_excel(archivos['cheques'])

        if df_saldos is None:
            return jsonify({"error": "Se requiere al menos el reporte de saldos"}), 400

        # Detectar columnas clave en saldos
        cols_saldo = [c for c in df_saldos.columns if 'saldo' in c.lower() or 'pendiente' in c.lower()]
        cols_contacto = [c for c in df_saldos.columns if 'contacto' in c.lower() or 'cliente' in c.lower() or 'razon' in c.lower()]
        cols_fecha_vto = [c for c in df_saldos.columns if 'vencimiento' in c.lower() or 'vto' in c.lower() or 'siguiente' in c.lower() or 'pago' in c.lower()]
        cols_fecha_fac = [c for c in df_saldos.columns if 'factura' in c.lower() or 'fecha' in c.lower()]

        if not cols_saldo or not cols_contacto:
            return jsonify({"error": "No se encontraron columnas de saldo o contacto en el archivo"}), 400

        col_saldo = cols_saldo[0]
        col_contacto = cols_contacto[0]
        col_vto = cols_fecha_vto[0] if cols_fecha_vto else None

        # Fecha de corte = fecha maxima de factura (no vencimientos futuros)
        fecha_corte = None
        cols_factura = [c for c in df_saldos.columns if 'factura' in c.lower() or 'emision' in c.lower() or 'emisi' in c.lower()]
        if cols_factura:
            try:
                fechas = pd.to_datetime(df_saldos[cols_factura[0]], errors='coerce').dropna()
                if len(fechas) > 0:
                    fecha_corte = fechas.max()
            except Exception:
                pass
        # Fallback: buscar en todas las columnas pero tomar la mas comun/logica
        if fecha_corte is None:
            for col in df_saldos.columns:
                try:
                    fechas = pd.to_datetime(df_saldos[col], errors='coerce').dropna()
                    # Ignorar columnas con fechas futuras (vencimientos)
                    fechas_pasadas = fechas[fechas <= pd.Timestamp.now()]
                    if len(fechas_pasadas) > 0:
                        fc = fechas_pasadas.max()
                        if fecha_corte is None or fc > fecha_corte:
                            fecha_corte = fc
                except Exception:
                    pass
        if fecha_corte is None:
            fecha_corte = pd.Timestamp.now()

        # Calcular saldo total pendiente
        df_saldos[col_saldo] = pd.to_numeric(df_saldos[col_saldo], errors='coerce').fillna(0)
        saldo_total = df_saldos[df_saldos[col_saldo] > 0][col_saldo].sum()

        # Cheques en cartera pendientes de cobro
        cheques_pendientes = 0.0
        if df_cheques is not None:
            cols_imp = [c for c in df_cheques.columns if 'importe' in c.lower() or 'monto' in c.lower() or 'total' in c.lower()]
            cols_fpago = [c for c in df_cheques.columns if 'pago' in c.lower() or 'vto' in c.lower() or 'vencimiento' in c.lower()]
            if cols_imp:
                col_imp = cols_imp[0]
                df_cheques[col_imp] = pd.to_numeric(df_cheques[col_imp], errors='coerce').fillna(0)
                if cols_fpago:
                    col_fpago = cols_fpago[0]
                    df_cheques[col_fpago] = pd.to_datetime(df_cheques[col_fpago], errors='coerce')
                    cheques_pendientes = df_cheques[df_cheques[col_fpago] >= fecha_corte][col_imp].sum()
                else:
                    cheques_pendientes = df_cheques[df_cheques[col_imp] > 0][col_imp].sum()

        # Ventas del período
        ventas_diarias = None
        dias_periodo = None
        ventas_netas = None
        advertencia = ""
        if df_ventas is not None:
            cols_total = [c for c in df_ventas.columns if 'total' in c.lower() or 'importe' in c.lower() or 'monto' in c.lower()]
            cols_fecha_v = [c for c in df_ventas.columns if 'fecha' in c.lower()]
            if cols_total and cols_fecha_v:
                col_total = cols_total[0]
                col_fecha_v = cols_fecha_v[0]
                df_ventas[col_total] = pd.to_numeric(df_ventas[col_total], errors='coerce').fillna(0)
                df_ventas[col_fecha_v] = pd.to_datetime(df_ventas[col_fecha_v], errors='coerce')
                df_ventas_ok = df_ventas.dropna(subset=[col_fecha_v])
                if len(df_ventas_ok) > 0:
                    fecha_inicio = df_ventas_ok[col_fecha_v].min()
                    fecha_fin = df_ventas_ok[col_fecha_v].max()
                    dias_periodo = max((fecha_fin - fecha_inicio).days, 1)
                    ventas_netas = df_ventas_ok[df_ventas_ok[col_total] > 0][col_total].sum()
                    ventas_diarias = ventas_netas / dias_periodo

        # DSO global
        saldo_neto = saldo_total - cheques_pendientes
        if ventas_diarias and ventas_diarias > 0:
            dso_global = round(saldo_neto / ventas_diarias, 1)
            calculo_completo = df_cheques is not None and df_ventas is not None
        else:
            # Fallback: promedio ponderado de dias vencidos por saldo
            advertencia = "Sin reporte de ventas — DSO calculado por antigüedad de saldos"
            calculo_completo = False
            if col_vto:
                df_saldos[col_vto] = pd.to_datetime(df_saldos[col_vto], errors='coerce')
                df_pos = df_saldos[df_saldos[col_saldo] > 0].copy()
                df_pos['dias'] = (fecha_corte - df_pos[col_vto]).dt.days.clip(lower=0)
                pond = (df_pos['dias'] * df_pos[col_saldo]).sum()
                dso_global = round(pond / df_pos[col_saldo].sum(), 1) if df_pos[col_saldo].sum() > 0 else 0
            else:
                dso_global = 0

        # DSO por cliente
        df_pos = df_saldos[df_saldos[col_saldo] > 0].copy()
        if col_vto and col_vto in df_saldos.columns:
            df_saldos[col_vto] = pd.to_datetime(df_saldos[col_vto], errors='coerce')
            df_pos['dias_vencido'] = (fecha_corte - df_saldos.loc[df_pos.index, col_vto]).dt.days.clip(lower=0)
        else:
            df_pos['dias_vencido'] = 0

        por_cliente = df_pos.groupby(col_contacto).agg(
            saldo=(col_saldo, 'sum'),
            dias_max=('dias_vencido', 'max')
        ).reset_index()
        por_cliente = por_cliente[por_cliente['saldo'] > 0].sort_values('dias_max', ascending=False)

        clientes = []
        for _, row in por_cliente.iterrows():
            dso_cli = int(row['dias_max'])
            if dso_cli < umbral_bajo:
                estado = 'Normal'
            elif dso_cli < umbral_alto:
                estado = 'Atención'
            else:
                estado = 'Crítico'
            clientes.append({
                'nombre': str(row[col_contacto]),
                'saldo': round(float(row['saldo']), 2),
                'dso': dso_cli,
                'estado': estado
            })

        return jsonify({
            'dso_global': dso_global,
            'saldo_total': round(float(saldo_total), 2),
            'cheques_pendientes': round(float(cheques_pendientes), 2),
            'total_clientes': len(clientes),
            'calculo_completo': calculo_completo,
            'fecha_corte': fecha_corte.strftime('%d/%m/%Y'),
            'advertencia': advertencia,
            'clientes': clientes
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ping")
def ping():
    return jsonify({"pong": True})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "gemini": bool(GEMINI_KEY)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, timeout=120)
