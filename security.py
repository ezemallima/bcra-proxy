"""Control de acceso central de Vende Seguro.

Por qué existe: la app expone datos crediticios y de cartera de clientes reales.
Antes, solo 17 de 123 rutas exigían sesión y los vendedores se "autenticaban" en
el navegador (PIN y lista de CUITs dentro del HTML), o sea, sin autenticación real.

Invariantes:
  * Deny by default: toda ruta que no esté en REGLAS exige rol admin. Una ruta
    nueva que se olvide acá queda cerrada, nunca abierta.
  * La identidad sale SIEMPRE de la sesión firmada del servidor; nada de lo que
    manda el navegador (localStorage, headers, query) otorga permisos.
  * Un vendedor solo lee su cartera; un supervisor, la de su equipo (ver
    _verificar_propiedad). 'todos' queda reservado a admin.
"""
import hmac
import json
import os
import re
import secrets
import threading
import time
import unicodedata

import requests
from flask import Blueprint, jsonify, redirect, request, session

ADMIN = 'admin'
DIRECTOR = 'director'
TURISMO = 'turismo'
SUPERVISOR = 'supervisor'
VENDEDOR = 'vendedor'

SOLO_ADMIN = {ADMIN}
STAFF = {ADMIN, SUPERVISOR, VENDEDOR}
SUPERVISION = {ADMIN, SUPERVISOR}
CONSULTA_BCRA = {ADMIN, SUPERVISOR, VENDEDOR, TURISMO}
STAFF_Y_DIRECTOR = {ADMIN, SUPERVISOR, VENDEDOR, DIRECTOR}
ADMIN_Y_DIRECTOR = {ADMIN, DIRECTOR}

PUBLICA = 'publica'
CRON = 'cron'

# (regex sobre el path completo, métodos o None = todos, roles permitidos).
# La primera que matchea gana. Sin match → SOLO_ADMIN.
REGLAS = [
    # ── Públicas (login, salud, enlaces con token propio) ────────────────────
    (r'/(ping|health|login|logout|director-login|director-logout|'
     r'turismo-login|turismo-logout|supabase-session\.js)', None, PUBLICA),
    (r'/auth/(cuit|supabase)', ['POST'], PUBLICA),
    (r'/auth/(sesion|logout)', None, PUBLICA),
    (r'/f/[^/]+', ['GET'], PUBLICA),
    (r'/f/[^/]+/pdf/.+', ['GET'], PUBLICA),

    # ── Procesos programados: token de cron o sesión admin ──────────────────
    (r'/(warm-padron|update-cheques-db|update-mipyme-db)(/estado)?', None, CRON),

    # ── Páginas ─────────────────────────────────────────────────────────────
    (r'/', ['GET'], SOLO_ADMIN),
    (r'/(comercial|supervisor)', ['GET'], STAFF),
    (r'/director', ['GET'], {DIRECTOR}),
    (r'/turismo', ['GET'], {TURISMO}),

    # ── Director ────────────────────────────────────────────────────────────
    (r'/api/director-data', None, {DIRECTOR}),
    (r'/dso-global-saldos', None, ADMIN_Y_DIRECTOR),
    (r'/api/dso-todos', None, STAFF_Y_DIRECTOR),

    # ── Turismo ─────────────────────────────────────────────────────────────
    (r'/(upload-saldos-turismo|api/turismo-portfolio|api/turismo-facturas/.+)', None, {TURISMO}),

    # ── Consulta de un CUIT (BCRA / score / ARCA) ───────────────────────────
    (r'/(fetch-score|calcular-score|deudas|afip)/[^/]+(/(cheques|historial))?', ['GET'],
     CONSULTA_BCRA),
    (r'/analizar', ['POST'], STAFF),

    # ── Cartera comercial (con verificación de propiedad, ver _verificar_propiedad)
    (r'/(cartera-por-vendedor|cartera-comercial|api/cuenta-corriente-excel)/[^/]+', ['GET'], STAFF),
    (r'/api/(supervisor-cartera|cuenta-corriente-excel-equipo)/[^/]+', ['GET'], SUPERVISION),

    # ── Datos que consume la app comercial ──────────────────────────────────
    (r'/(api-v17-scores|api/alertas|api/alertas-vencimiento|api/clientes-emision-20d|'
     r'saldos-timestamp|api/facturas-estado-resumen)', ['GET'], STAFF),
    (r'/alertas', ['GET', 'POST'], STAFF),
    (r'/recalcular-scores', ['POST'], STAFF),
    (r'/(dso-saldos|dso-ventas)', ['GET', 'POST'], STAFF),
    (r'/api/facturas/zip-meta', ['GET'], STAFF),
    (r'/api/facturas/import-estado', ['GET'], SUPERVISION),
    (r'/api/facturas/(reimportar|configurar-drive)', ['POST'], SUPERVISION),
    (r'/api/facturas/crear-lote', ['POST'], STAFF),
    (r'/api/facturas/[^/]+', ['GET'], STAFF),
    (r'/api/facturas/[^/]+/(marcar-cobrada|desmarcar-cobrada|marcar-whatsapp)', ['POST'], STAFF),
    (r'/api/facturas-pdf/.+', ['GET'], STAFF),
]
_REGLAS = [(re.compile(p + r'\Z'), m, r) for p, m, r in REGLAS]

# Recursos estáticos que las páginas necesitan; el resto de /static (los .html)
# no se sirve directo porque saltearía el control de cada página.
_STATIC_PERMITIDO = re.compile(r'\.(png|svg|ico|jpg|jpeg|webp|gif|css|js|woff2?)\Z', re.I)

_PAGINAS_LOGIN = {'/director': '/director-login', '/turismo': '/turismo-login'}


def _cuit_valido(valor) -> bool:
    """Solo dígitos, con guiones/espacios como separadores. Los handlers arman
    nombres de archivo con el CUIT, así que no puede llegar nada más que números."""
    limpio = str(valor or '').replace('-', '').replace(' ', '')
    return bool(re.fullmatch(r'\d{10,11}', limpio))


def _norm(texto) -> str:
    """Mayúsculas, sin tildes ni puntuación: 'Raúl Maza' == 'RAUL MAZA'."""
    s = unicodedata.normalize('NFD', str(texto or '').strip().upper())
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Z0-9 ]', ' ', s)).strip()


def _solo_digitos(valor) -> str:
    return re.sub(r'\D', '', str(valor or ''))


def _admin_por_clave_directa() -> bool:
    """Operación manual de /admin/* con la clave de administrador (X-Admin-Pass o
    admin_pass en el JSON), sin sesión: curl/Postman durante el mantenimiento mensual."""
    if not request.path.startswith('/admin/'):
        return False
    enviado = request.headers.get('X-Admin-Pass', '')
    if not enviado and request.is_json:
        cuerpo = request.get_json(silent=True) or {}
        enviado = str(cuerpo.get('admin_pass', '') or cuerpo.get('password', ''))
    if not enviado:
        return False
    clave = f'admin-directo:{ip_cliente()}'
    if not limite_login(clave, maximo=10):
        return False
    if _iguales(enviado, os.environ.get('ADMIN_PASS', '')):
        _liberar(clave)
        return True
    return False


def roles_actuales() -> set:
    """Roles habilitados en la sesión (el navegador puede tener más de uno)."""
    roles = set()
    if session.get('logged_in') or _admin_por_clave_directa():
        roles.add(ADMIN)
    if session.get('director_logged_in'):
        roles.add(DIRECTOR)
    if session.get('turismo_logged_in'):
        roles.add(TURISMO)
    usuario = session.get('comercial')
    if isinstance(usuario, dict) and usuario.get('rol') in (SUPERVISOR, VENDEDOR):
        roles.add(usuario['rol'])
    return roles


def _regla_para(path: str, metodo: str):
    for patron, metodos, roles in _REGLAS:
        if patron.match(path) and (metodos is None or metodo in metodos):
            return roles
    return SOLO_ADMIN


def _equipo_permitido(usuario: dict, mapa_supervisores: dict) -> set:
    """Nombres normalizados cuya cartera puede leer este usuario comercial."""
    nombres = {_norm(usuario.get('nombre'))}
    if usuario.get('rol') == SUPERVISOR:
        info = mapa_supervisores.get(_solo_digitos(usuario.get('cuit'))) or {}
        nombres |= {_norm(n) for n in info.get('supervisa', [])}
    return nombres


def _verificar_propiedad(path: str, roles: set, mapa_supervisores: dict) -> bool:
    """Un comercial no admin solo accede a su propia cartera (o la de su equipo)."""
    if ADMIN in roles:
        return True
    usuario = session.get('comercial') or {}
    m = re.match(r'/(cartera-por-vendedor|cartera-comercial|api/cuenta-corriente-excel)/([^/]+)\Z', path)
    if m:
        from urllib.parse import unquote
        pedido = _norm(unquote(m.group(2)))
        return pedido in _equipo_permitido(usuario, mapa_supervisores)
    m = re.match(r'/api/(supervisor-cartera|cuenta-corriente-excel-equipo)/([^/]+)\Z', path)
    if m:
        return (usuario.get('rol') == SUPERVISOR
                and _solo_digitos(m.group(2)) == _solo_digitos(usuario.get('cuit')))
    return True


def _token_cron_valido() -> bool:
    esperado = os.environ.get('CRON_TOKEN', '')
    if not esperado:
        return False
    recibido = request.headers.get('X-Cron-Token') or request.args.get('token') or ''
    return hmac.compare_digest(recibido.encode(), esperado.encode())


def _es_pagina(path: str) -> bool:
    return request.method == 'GET' and (
        path == '/' or path in ('/comercial', '/supervisor', '/director', '/turismo'))


def _denegar(path: str, autenticado: bool):
    if _es_pagina(path):
        return redirect(_PAGINAS_LOGIN.get(path, '/login'))
    codigo = 403 if autenticado else 401
    resp = jsonify({'ok': False, 'error': 'Acceso no autorizado' if autenticado
                    else 'Sesión requerida', 'code': 'FORBIDDEN' if autenticado
                    else 'AUTH_REQUIRED'})
    resp.status_code = codigo
    if not autenticado:
        resp.headers['X-Auth-Required'] = '1'
    return resp


def ip_cliente() -> str:
    """IP del cliente detrás de Cloudflare + Render.

    CF-Connecting-IP solo es confiable cuando el tráfico pasa por Cloudflare; quien
    golpee el origen directo puede falsearlo. Por eso el límite de intentos también
    se aplica por identificador (ver limite_login) y no depende solo de la IP.
    """
    cf = request.headers.get('CF-Connecting-IP', '').strip()
    if cf:
        return cf
    xff = [p.strip() for p in request.headers.get('X-Forwarded-For', '').split(',') if p.strip()]
    return xff[-1] if xff else (request.remote_addr or 'desconocida')


def init_app(app, mapa_supervisores):
    """Instala cookies seguras, cabeceras y el control de acceso global.

    mapa_supervisores: callable que devuelve {cuit: {'nombre', 'supervisa': [...]}}.
    """
    en_produccion = bool(os.environ.get('RENDER')) or os.environ.get('FORCE_SECURE_COOKIES') == '1'
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=en_produccion,
        PERMANENT_SESSION_LIFETIME=12 * 3600,
    )

    @app.before_request
    def _control_de_acceso():
        if request.method == 'OPTIONS':
            return None
        path = request.path
        metodo = 'GET' if request.method == 'HEAD' else request.method
        if path.startswith('/static/'):
            if _STATIC_PERMITIDO.search(path):
                return None
            return _denegar(path, autenticado=False)

        for clave, valor in (request.view_args or {}).items():
            if clave == 'cuit' and not _cuit_valido(valor):
                return jsonify({'ok': False, 'error': 'CUIT inválido'}), 400

        permitidos = _regla_para(path, metodo)
        if permitidos == PUBLICA:
            return None
        roles = roles_actuales()
        if permitidos == CRON:
            if _token_cron_valido() or ADMIN in roles:
                return None
            if not os.environ.get('CRON_TOKEN'):
                print('[SECURITY] Ruta de cron sin CRON_TOKEN configurado: bloqueada', flush=True)
            return _denegar(path, autenticado=bool(roles))

        efectivos = roles & permitidos
        if not efectivos:
            return _denegar(path, autenticado=bool(roles))
        if not _verificar_propiedad(path, efectivos, mapa_supervisores()):
            print(f'[SECURITY] Acceso fuera de cartera propia — ruta: {path}', flush=True)
            return _denegar(path, autenticado=True)
        return None

    @app.after_request
    def _cabeceras(resp):
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        resp.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        resp.headers['Content-Security-Policy'] = (
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'")
        if en_produccion:
            resp.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        if not request.path.startswith('/static/') and not request.path.startswith('/f/'):
            resp.headers.setdefault('Cache-Control', 'no-store')
        return resp


# ── Login del lado del servidor ─────────────────────────────────────────────

def _cargar_usuarios_comercial() -> dict:
    """{cuit: nombre} desde COMERCIAL_USERS (JSON). Vacío si no está configurado."""
    crudo = os.environ.get('COMERCIAL_USERS', '').strip()
    if not crudo:
        return {}
    try:
        datos = json.loads(crudo)
        return {_solo_digitos(k): str(v).strip() for k, v in datos.items() if _solo_digitos(k)}
    except (ValueError, AttributeError) as e:
        print(f'[SECURITY] COMERCIAL_USERS inválido: {e}', flush=True)
        return {}


def _iguales(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


_intentos = {}
_intentos_lock = threading.Lock()
_MAX_INTENTOS = 5
_VENTANA = 900


def limite_login(clave: str, maximo: int = _MAX_INTENTOS) -> bool:
    """True si todavía puede intentar. Cuenta cada intento en una ventana de 15 min."""
    ahora = time.time()
    with _intentos_lock:
        hist = [t for t in _intentos.get(clave, []) if ahora - t < _VENTANA]
        if len(hist) >= maximo:
            _intentos[clave] = hist
            return False
        hist.append(ahora)
        _intentos[clave] = hist
    return True


def _liberar(clave: str):
    with _intentos_lock:
        _intentos.pop(clave, None)


def _abrir_sesion(**datos):
    session.clear()
    session.permanent = True
    for k, v in datos.items():
        session[k] = v


def crear_blueprint(mapa_supervisores):
    bp = Blueprint('auth_seguro', __name__)

    @bp.route('/auth/cuit', methods=['POST'])
    def auth_cuit():
        """Login por CUIT: administrador o vendedor/supervisor, siempre verificado acá."""
        datos = request.get_json(silent=True) or {}
        cuit = _solo_digitos(datos.get('cuit'))
        clave = str(datos.get('password', '')).strip()
        ip = ip_cliente()
        # Por IP (5) y por cuenta (10): quien falsee la IP contra el origen directo
        # sigue topado por cuenta; el tope por cuenta es mayor para que un tercero
        # no pueda dejar afuera al administrador con unos pocos intentos.
        if not (limite_login(f'ip:{ip}') and limite_login(f'cuit:{cuit}', maximo=10)):
            print(f'[SECURITY] Login por CUIT bloqueado por límite — IP: {ip}', flush=True)
            return jsonify({'ok': False, 'error': 'Demasiados intentos. Esperá 15 minutos.'}), 429

        admin_cuit = os.environ.get('ADMIN_CUIT', '')
        admin_pass = os.environ.get('ADMIN_PASS', '')
        if cuit and _iguales(cuit, _solo_digitos(admin_cuit)) and _iguales(clave, admin_pass):
            _abrir_sesion(logged_in=True)
            _liberar(f'cuit:{cuit}')
            _liberar(f'ip:{ip}')
            return jsonify({'ok': True, 'destino': '/'})

        usuarios = _cargar_usuarios_comercial()
        nombre = usuarios.get(cuit)
        if nombre and _iguales(clave, os.environ.get('COMERCIAL_PIN', '')):
            rol = SUPERVISOR if cuit in (mapa_supervisores() or {}) else VENDEDOR
            _abrir_sesion(comercial={'nombre': nombre, 'cuit': cuit, 'rol': rol, 'via': 'pin'})
            _liberar(f'cuit:{cuit}')
            _liberar(f'ip:{ip}')
            return jsonify({'ok': True, 'destino': '/comercial',
                            'usuario': {'nombre': nombre, 'cuit': cuit, 'rol': rol}})

        print(f'[SECURITY] Login por CUIT fallido — IP: {ip}', flush=True)
        return jsonify({'ok': False, 'error': 'Credenciales incorrectas.'}), 401

    @bp.route('/auth/supabase', methods=['POST'])
    def auth_supabase():
        """Convierte un token de Supabase en sesión del servidor, verificándolo con Supabase."""
        datos = request.get_json(silent=True) or {}
        token = str(datos.get('access_token', '')).strip()
        ip = ip_cliente()
        if not limite_login(f'ip:{ip}'):
            return jsonify({'ok': False, 'error': 'Demasiados intentos. Esperá 15 minutos.'}), 429
        if len(token) < 20:
            return jsonify({'ok': False, 'error': 'Token inválido.'}), 401

        base = os.environ.get('SUPABASE_URL', 'https://rurxkrhbmoiomdqbkavy.supabase.co').rstrip('/')
        anon = os.environ.get('SUPABASE_ANON_KEY', 'sb_publishable_KE_-r2F760BVWRun_aqxhg_OloyoVW5')
        cabeceras = {'apikey': anon, 'Authorization': f'Bearer {token}'}
        try:
            r_user = requests.get(f'{base}/auth/v1/user', headers=cabeceras, timeout=8)
            if r_user.status_code != 200:
                return jsonify({'ok': False, 'error': 'Sesión inválida.'}), 401
            uid = str(r_user.json().get('id', ''))
            if not re.fullmatch(r'[0-9a-fA-F-]{36}', uid):
                return jsonify({'ok': False, 'error': 'Sesión inválida.'}), 401
            r_perfil = requests.get(
                f'{base}/rest/v1/usuarios',
                params={'id': f'eq.{uid}', 'select': 'nombre,rol,email,empresa_id,activo'},
                headers=cabeceras, timeout=8)
            filas = r_perfil.json() if r_perfil.status_code == 200 else []
        except (requests.RequestException, ValueError) as e:
            print(f'[SECURITY] Verificación Supabase falló: {type(e).__name__}', flush=True)
            return jsonify({'ok': False, 'error': 'No se pudo verificar la sesión.'}), 503

        perfil = filas[0] if isinstance(filas, list) and filas else None
        if not perfil or not perfil.get('activo') or not str(perfil.get('nombre', '')).strip():
            return jsonify({'ok': False, 'error': 'Usuario no habilitado.'}), 403

        _abrir_sesion(comercial={'nombre': str(perfil['nombre']).strip(), 'cuit': '',
                                 'rol': VENDEDOR, 'via': 'supabase',
                                 'email': str(perfil.get('email', ''))})
        return jsonify({'ok': True, 'destino': '/comercial'})

    @bp.route('/auth/sesion', methods=['GET'])
    def auth_sesion():
        """Estado de la sesión, para que las pantallas detecten que vencieron."""
        roles = sorted(roles_actuales())
        return jsonify({'autenticado': bool(roles), 'roles': roles})

    @bp.route('/auth/logout', methods=['POST'])
    def auth_logout():
        session.clear()
        return jsonify({'ok': True})

    return bp


def clave_secreta(directorio_datos: str) -> str:
    """SECRET_KEY estable: variable de entorno, o archivo persistente generado una vez.

    Sin esto la firma de las sesiones dependería de un valor público del repositorio.
    """
    entorno = os.environ.get('SECRET_KEY', '').strip()
    if len(entorno) >= 32:
        return entorno
    ruta = os.path.join(directorio_datos, '.flask_secret')
    try:
        with open(ruta, 'r', encoding='utf-8') as f:
            valor = f.read().strip()
        if len(valor) >= 32:
            return valor
    except OSError:
        pass
    valor = secrets.token_hex(32)
    try:
        os.makedirs(directorio_datos, exist_ok=True)
        with open(ruta, 'w', encoding='utf-8') as f:
            f.write(valor)
        os.chmod(ruta, 0o600)
    except OSError as e:
        print(f'[SECURITY] No se pudo persistir la clave de sesión: {e}', flush=True)
    return valor
