"""Pruebas del control de acceso (security.py). Ejecutar: python -m unittest discover tests -v

Usan una app Flask mínima: verifican las reglas sin cargar main.py ni sus bases de datos.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402

import security  # noqa: E402

SUPERVISORES = {
    '27224289966': {'nombre': 'Valeria Gutierrez Castex',
                    'supervisa': ['Raul Maza', 'Marcelo Fernandez']},
    '20207619923': {'nombre': 'Alejandro Valan', 'supervisa': ['Anabel Borrageiros']},
}
USUARIOS = {'20208913604': 'Adrian Arango', '27224289966': 'Valeria Gutierrez Castex',
            '20254118959': 'Raul Maza'}
CUIT_OK = '20123456789'


def crear_app():
    app = Flask(__name__)
    app.secret_key = 'clave-de-prueba-de-32-caracteres-como-minimo'
    security.init_app(app, lambda: SUPERVISORES)
    app.register_blueprint(security.crear_blueprint(lambda: SUPERVISORES))

    @app.route('/fetch-score/<cuit>')
    def _score(cuit):
        return 'ok'

    @app.route('/deudas/<cuit>')
    def _deudas(cuit):
        return 'ok'

    @app.route('/', defaults={'p': ''}, methods=['GET', 'POST', 'DELETE'])
    @app.route('/<path:p>', methods=['GET', 'POST', 'DELETE'])
    def _cualquiera(p):
        return 'ok'

    return app


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            'ADMIN_CUIT': '30710295022', 'ADMIN_PASS': 'clave-admin-de-prueba',
            'COMERCIAL_PIN': 'pin-de-prueba', 'COMERCIAL_USERS': json.dumps(USUARIOS),
            'CRON_TOKEN': 'token-cron-de-prueba'}, clear=False)
        self.env.start()
        security._intentos.clear()
        self.app = crear_app()
        self.c = self.app.test_client()

    def tearDown(self):
        self.env.stop()

    def sesion(self, **datos):
        with self.c.session_transaction() as s:
            s.update(datos)

    def como(self, rol, nombre='Adrian Arango', cuit='20208913604'):
        if rol == 'admin':
            self.sesion(logged_in=True)
        elif rol == 'director':
            self.sesion(director_logged_in=True)
        elif rol == 'turismo':
            self.sesion(turismo_logged_in=True)
        else:
            self.sesion(comercial={'nombre': nombre, 'cuit': cuit, 'rol': rol})


class SinSesion(Base):
    def test_api_privada_devuelve_401(self):
        for ruta in ('/alertas', '/score-cache-all', '/cartera_inicial.json', '/api/alertas',
                     '/todos-los-clientes', '/vendedores', '/moras.json', '/upload-cartera',
                     f'/fetch-score/{CUIT_OK}', '/cartera-por-vendedor/todos'):
            r = self.c.get(ruta)
            self.assertEqual(r.status_code, 401, ruta)
            self.assertEqual(r.headers.get('X-Auth-Required'), '1', ruta)

    def test_paginas_redirigen_al_login_que_corresponde(self):
        esperado = {'/': '/login', '/comercial': '/login', '/supervisor': '/login',
                    '/director': '/director-login', '/turismo': '/turismo-login'}
        for ruta, destino in esperado.items():
            r = self.c.get(ruta)
            self.assertEqual(r.status_code, 302, ruta)
            self.assertTrue(r.headers['Location'].endswith(destino), ruta)

    def test_head_de_paginas_redirige_igual_que_get(self):
        # Render chequea "HEAD /" al arrancar; antes recibía una redirección, no un 401.
        r = self.c.head('/')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers['Location'].endswith('/login'))
        self.assertEqual(self.c.head('/director').status_code, 302)
        self.assertEqual(self.c.head('/alertas').status_code, 401)

    def test_rutas_publicas(self):
        for ruta in ('/ping', '/health', '/login', '/logout', '/director-login',
                     '/turismo-login', '/supabase-session.js', '/f/abc123', '/f/abc123/pdf/FA-A 1'):
            self.assertEqual(self.c.get(ruta).status_code, 200, ruta)

    def test_estaticos_solo_recursos(self):
        # La app de prueba no tiene archivos: 404 = "pasó el control", 401 = "bloqueado".
        self.assertNotEqual(self.c.get('/static/logo.png').status_code, 401)
        self.assertNotEqual(self.c.get('/static/supabase.min.js').status_code, 401)
        for ruta in ('/static/index.html', '/static/comercial.html', '/static/login.html',
                     '/static/datos.json'):
            self.assertEqual(self.c.get(ruta).status_code, 401, ruta)

    def test_ruta_nueva_no_listada_queda_cerrada(self):
        self.assertEqual(self.c.get('/ruta-nueva-que-nadie-registro').status_code, 401)
        self.como('vendedor')
        self.assertEqual(self.c.get('/ruta-nueva-que-nadie-registro').status_code, 403)
        self.como('admin')
        self.assertEqual(self.c.get('/ruta-nueva-que-nadie-registro').status_code, 200)

    def test_cabeceras_de_seguridad(self):
        r = self.c.get('/ping')
        self.assertEqual(r.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(r.headers['X-Frame-Options'], 'DENY')
        self.assertIn("frame-ancestors 'none'", r.headers['Content-Security-Policy'])
        self.assertEqual(r.headers['Cache-Control'], 'no-store')

    def test_cookie_de_sesion_httponly_y_samesite(self):
        r = self.c.post('/auth/cuit', json={'cuit': '30710295022', 'password': 'clave-admin-de-prueba'})
        cookie = r.headers.get('Set-Cookie', '')
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)


class ValidacionCuit(Base):
    def test_cuit_con_basura_se_rechaza(self):
        self.como('admin')
        # Un solo segmento de URL con basura (incluye doble codificación de "/").
        for malo in ('2012345678%00', 'abc', '123', '20123456789;ls', '20123456789%252F..%252Fx',
                     '20123456789.json'):
            self.assertEqual(self.c.get(f'/fetch-score/{malo}').status_code, 400, malo)
        self.assertEqual(self.c.get('/fetch-score/20123456789').status_code, 200)
        self.assertEqual(self.c.get('/fetch-score/20-12345678-9').status_code, 200)


class Vendedor(Base):
    def setUp(self):
        super().setUp()
        self.como('vendedor')

    def test_ve_solo_su_cartera(self):
        self.assertEqual(self.c.get('/cartera-por-vendedor/Adrian%20Arango').status_code, 200)
        self.assertEqual(self.c.get('/cartera-por-vendedor/adrian arango').status_code, 200)
        self.assertEqual(self.c.get('/api/cuenta-corriente-excel/Adrian%20Arango').status_code, 200)
        for ajeno in ('Raul%20Maza', 'todos', 'all'):
            self.assertEqual(self.c.get(f'/cartera-por-vendedor/{ajeno}').status_code, 403, ajeno)
        self.assertEqual(self.c.get('/cartera-comercial/Raul%20Maza').status_code, 403)

    def test_no_accede_a_administracion(self):
        for ruta in ('/admin/padron-info', '/score-cache-all', '/cartera_inicial.json',
                     '/todos-los-clientes', '/vendedores', '/moras.json', '/version',
                     '/api/supervisor-cartera/27224289966', '/api/alertas-mora'):
            self.assertEqual(self.c.get(ruta).status_code, 403, ruta)
        for ruta in ('/upload-cartera', '/upload-saldos-facturas', '/cache/limpiar-todo',
                     '/api/cartera/eliminar-cliente', '/alertas/limpiar', '/limpiar-solvency',
                     '/api/facturas/reimportar', '/api/facturas/configurar-drive'):
            self.assertEqual(self.c.post(ruta).status_code, 403, ruta)

    def test_no_accede_a_otros_roles(self):
        for ruta in ('/api/director-data', '/api/turismo-portfolio', '/dso-global-saldos'):
            self.assertEqual(self.c.get(ruta).status_code, 403, ruta)

    def test_conserva_lo_que_usa_el_envio_de_estado_de_cuenta(self):
        # El mensaje de WhatsApp con PDFs lee zip-meta para todos los vendedores.
        for ruta in ('/api/facturas/zip-meta', f'/api/facturas/{CUIT_OK}', '/api/facturas-pdf/x.pdf',
                     '/saldos-timestamp', '/dso-saldos', '/dso-ventas', '/alertas', '/api/alertas',
                     '/api-v17-scores', f'/afip/{CUIT_OK}', f'/deudas/{CUIT_OK}'):
            self.assertEqual(self.c.get(ruta).status_code, 200, ruta)
        for ruta in ('/api/facturas/crear-lote', f'/api/facturas/{CUIT_OK}/marcar-cobrada',
                     f'/api/facturas/{CUIT_OK}/marcar-whatsapp', '/analizar', '/recalcular-scores',
                     '/dso-saldos'):
            self.assertEqual(self.c.post(ruta).status_code, 200, ruta)
        self.assertEqual(self.c.get('/api/facturas/import-estado').status_code, 403)

    def test_normalizacion_de_tildes(self):
        self.como('vendedor', nombre='Raúl Maza', cuit='20254118959')
        self.assertEqual(self.c.get('/cartera-por-vendedor/Raul%20Maza').status_code, 200)
        self.assertEqual(self.c.get('/cartera-por-vendedor/RA%C3%9AL%20MAZA').status_code, 200)


class Supervisor(Base):
    def setUp(self):
        super().setUp()
        self.como('supervisor', nombre='Valeria Gutierrez Castex', cuit='27224289966')

    def test_ve_su_equipo_y_no_otro(self):
        for propio in ('Valeria%20Gutierrez%20Castex', 'Raul%20Maza', 'Marcelo%20Fernandez'):
            self.assertEqual(self.c.get(f'/cartera-por-vendedor/{propio}').status_code, 200, propio)
        self.assertEqual(self.c.get('/cartera-por-vendedor/Anabel%20Borrageiros').status_code, 403)
        self.assertEqual(self.c.get('/cartera-por-vendedor/todos').status_code, 403)

    def test_endpoints_de_supervisor_solo_con_su_cuit(self):
        self.assertEqual(self.c.get('/api/supervisor-cartera/27224289966').status_code, 200)
        self.assertEqual(self.c.get('/api/cuenta-corriente-excel-equipo/27224289966').status_code, 200)
        self.assertEqual(self.c.get('/api/supervisor-cartera/20207619923').status_code, 403)

    def test_puede_reimportar_facturas_pero_no_subir_cartera(self):
        self.assertEqual(self.c.post('/api/facturas/reimportar').status_code, 200)
        self.assertEqual(self.c.get('/api/facturas/zip-meta').status_code, 200)
        self.assertEqual(self.c.post('/upload-cartera').status_code, 403)


class Admin(Base):
    def test_admin_accede_a_todo(self):
        self.como('admin')
        for ruta in ('/cartera-por-vendedor/todos', '/admin/padron-info', '/score-cache-all',
                     '/api/supervisor-cartera/27224289966', '/comercial'):
            self.assertEqual(self.c.get(ruta).status_code, 200, ruta)
        self.assertEqual(self.c.post('/upload-cartera').status_code, 200)


class AdminPorClaveDirecta(Base):
    def test_clave_por_cabecera_o_json_habilita_solo_admin(self):
        cab = {'X-Admin-Pass': 'clave-admin-de-prueba'}
        self.assertEqual(self.c.get('/admin/padron-info', headers=cab).status_code, 200)
        self.assertEqual(self.c.post('/admin/importar-padron',
                                     json={'admin_pass': 'clave-admin-de-prueba'}).status_code, 200)
        self.assertEqual(self.c.get('/score-cache-all', headers=cab).status_code, 401)

    def test_clave_incorrecta_o_ausente(self):
        self.assertEqual(self.c.get('/admin/padron-info', headers={'X-Admin-Pass': 'mala'}).status_code, 401)
        self.assertEqual(self.c.get('/admin/padron-info').status_code, 401)

    def test_admin_pass_vacio_no_abre_nada(self):
        with mock.patch.dict(os.environ, {'ADMIN_PASS': ''}):
            self.assertEqual(self.c.get('/admin/padron-info', headers={'X-Admin-Pass': ''}).status_code, 401)
            self.assertEqual(self.c.get('/admin/padron-info', headers={'X-Admin-Pass': ' '}).status_code, 401)

    def test_fuerza_bruta_se_corta(self):
        for _ in range(10):
            self.c.get('/admin/padron-info', headers={'X-Admin-Pass': 'mala'})
        r = self.c.get('/admin/padron-info', headers={'X-Admin-Pass': 'clave-admin-de-prueba'})
        self.assertEqual(r.status_code, 401)


class DirectorYTurismo(Base):
    def test_director(self):
        self.como('director')
        for ruta in ('/api/director-data', '/dso-global-saldos', '/api/dso-todos', '/director'):
            self.assertEqual(self.c.get(ruta).status_code, 200, ruta)
        for ruta in ('/alertas', '/score-cache-all', '/api/turismo-portfolio'):
            self.assertEqual(self.c.get(ruta).status_code, 403, ruta)

    def test_turismo_no_ve_la_cartera_de_la_empresa(self):
        self.como('turismo')
        for ruta in ('/api/turismo-portfolio', '/api/turismo-facturas/Cliente%20X', '/turismo',
                     f'/fetch-score/{CUIT_OK}', f'/deudas/{CUIT_OK}'):
            self.assertEqual(self.c.get(ruta).status_code, 200, ruta)
        self.assertEqual(self.c.post('/upload-saldos-turismo').status_code, 200)
        for ruta in ('/alertas', '/cartera-por-vendedor/todos', '/score-cache-all',
                     '/api/dso-todos', '/api/director-data'):
            self.assertEqual(self.c.get(ruta).status_code, 403, ruta)


class Cron(Base):
    RUTAS = ('/warm-padron', '/update-cheques-db', '/update-mipyme-db', '/update-cheques-db/estado')

    def test_sin_token_ni_sesion_se_rechaza(self):
        for ruta in self.RUTAS:
            self.assertEqual(self.c.get(ruta).status_code, 401, ruta)

    def test_con_token_correcto(self):
        for ruta in self.RUTAS:
            self.assertEqual(self.c.get(ruta, headers={'X-Cron-Token': 'token-cron-de-prueba'}).status_code, 200)
            self.assertEqual(self.c.get(ruta + '?token=token-cron-de-prueba').status_code, 200)

    def test_token_incorrecto_o_vacio(self):
        self.assertEqual(self.c.get('/warm-padron', headers={'X-Cron-Token': 'otro'}).status_code, 401)
        self.assertEqual(self.c.get('/warm-padron?token=').status_code, 401)

    def test_sin_cron_token_configurado_solo_admin(self):
        with mock.patch.dict(os.environ, {'CRON_TOKEN': ''}):
            self.assertEqual(self.c.get('/warm-padron?token=').status_code, 401)
            self.como('admin')
            self.assertEqual(self.c.get('/warm-padron').status_code, 200)

    def test_vendedor_no_dispara_crons(self):
        self.como('vendedor')
        self.assertEqual(self.c.get('/warm-padron').status_code, 403)


class LoginCuit(Base):
    def login(self, cuit, clave):
        return self.c.post('/auth/cuit', json={'cuit': cuit, 'password': clave})

    def test_admin(self):
        r = self.login('30-71029502-2', 'clave-admin-de-prueba')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['destino'], '/')
        self.assertEqual(self.c.get('/admin/padron-info').status_code, 200)

    def test_vendedor_y_supervisor_reciben_su_rol(self):
        r = self.login('20208913604', 'pin-de-prueba')
        self.assertEqual(r.get_json()['usuario']['rol'], 'vendedor')
        self.assertEqual(self.c.get('/cartera-por-vendedor/Adrian%20Arango').status_code, 200)
        self.assertEqual(self.c.get('/cartera-por-vendedor/Raul%20Maza').status_code, 403)
        r = self.login('27224289966', 'pin-de-prueba')
        self.assertEqual(r.get_json()['usuario']['rol'], 'supervisor')
        self.assertEqual(self.c.get('/cartera-por-vendedor/Raul%20Maza').status_code, 200)

    def test_clave_incorrecta_o_cuit_desconocido(self):
        self.assertEqual(self.login('30710295022', 'mala').status_code, 401)
        self.assertEqual(self.login('20208913604', 'mala').status_code, 401)
        self.assertEqual(self.login('20999999999', 'pin-de-prueba').status_code, 401)
        self.assertEqual(self.c.get('/alertas').status_code, 401)

    def test_sin_variables_configuradas_no_hay_acceso_por_defecto(self):
        with mock.patch.dict(os.environ, {'ADMIN_PASS': '', 'COMERCIAL_PIN': '', 'COMERCIAL_USERS': ''}):
            self.assertEqual(self.login('30710295022', '').status_code, 401)
            self.assertEqual(self.login('30710295022', 'Artel2026').status_code, 401)
            self.assertEqual(self.login('20208913604', '').status_code, 401)
            self.assertEqual(self.login('20208913604', 'un-pin-cualquiera').status_code, 401)

    def test_bloqueo_por_intentos(self):
        for _ in range(5):
            self.assertEqual(self.login('30710295022', 'mala').status_code, 401)
        self.assertEqual(self.login('30710295022', 'clave-admin-de-prueba').status_code, 429)

    def test_login_exitoso_libera_el_contador_de_la_ip(self):
        for _ in range(4):
            self.login('20208913604', 'mala')
        self.assertEqual(self.login('20208913604', 'pin-de-prueba').status_code, 200)
        self.assertEqual(self.login('20254118959', 'pin-de-prueba').status_code, 200)

    def test_bloqueo_por_cuenta_aunque_cambie_la_ip(self):
        for i in range(10):
            r = self.c.post('/auth/cuit', json={'cuit': '30710295022', 'password': 'mala'},
                            headers={'CF-Connecting-IP': f'10.0.0.{i}'})
            self.assertEqual(r.status_code, 401)
        r = self.c.post('/auth/cuit', json={'cuit': '30710295022', 'password': 'clave-admin-de-prueba'},
                        headers={'CF-Connecting-IP': '10.9.9.9'})
        self.assertEqual(r.status_code, 429)

    def test_logout_cierra_la_sesion(self):
        self.login('30710295022', 'clave-admin-de-prueba')
        self.c.post('/auth/logout')
        self.assertEqual(self.c.get('/admin/padron-info').status_code, 401)


class LoginSupabase(Base):
    def _respuesta(self, status, cuerpo):
        r = mock.Mock()
        r.status_code = status
        r.json.return_value = cuerpo
        return r

    def test_token_valido_abre_sesion_de_vendedor_con_su_nombre(self):
        uid = '123e4567-e89b-12d3-a456-426614174000'
        perfil = [{'nombre': 'Adrian Arango', 'rol': 'vendedor', 'email': 'a@x.com', 'activo': True}]
        with mock.patch('security.requests.get',
                        side_effect=[self._respuesta(200, {'id': uid}), self._respuesta(200, perfil)]):
            r = self.c.post('/auth/supabase', json={'access_token': 'x' * 40})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get('/cartera-por-vendedor/Adrian%20Arango').status_code, 200)
        self.assertEqual(self.c.get('/cartera-por-vendedor/Raul%20Maza').status_code, 403)
        self.assertEqual(self.c.get('/admin/padron-info').status_code, 403)

    def test_token_rechazado_por_supabase(self):
        with mock.patch('security.requests.get', return_value=self._respuesta(401, {})):
            r = self.c.post('/auth/supabase', json={'access_token': 'x' * 40})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.c.get('/alertas').status_code, 401)

    def test_usuario_inactivo(self):
        uid = '123e4567-e89b-12d3-a456-426614174000'
        perfil = [{'nombre': 'Ex Empleado', 'rol': 'vendedor', 'activo': False}]
        with mock.patch('security.requests.get',
                        side_effect=[self._respuesta(200, {'id': uid}), self._respuesta(200, perfil)]):
            r = self.c.post('/auth/supabase', json={'access_token': 'x' * 40})
        self.assertEqual(r.status_code, 403)

    def test_token_ausente_o_corto(self):
        self.assertEqual(self.c.post('/auth/supabase', json={}).status_code, 401)
        self.assertEqual(self.c.post('/auth/supabase', json={'access_token': 'abc'}).status_code, 401)

    def test_supabase_caido_no_abre_sesion(self):
        import requests as rq
        with mock.patch('security.requests.get', side_effect=rq.ConnectionError()):
            r = self.c.post('/auth/supabase', json={'access_token': 'x' * 40})
        self.assertEqual(r.status_code, 503)


class ClaveSecreta(unittest.TestCase):
    def test_usa_la_variable_de_entorno(self):
        with mock.patch.dict(os.environ, {'SECRET_KEY': 'a' * 40}):
            self.assertEqual(security.clave_secreta('/ruta/inexistente'), 'a' * 40)

    def test_genera_y_reutiliza_un_archivo_persistente(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {'SECRET_KEY': ''}):
            k1 = security.clave_secreta(d)
            k2 = security.clave_secreta(d)
            self.assertEqual(k1, k2)
            self.assertGreaterEqual(len(k1), 32)
            self.assertNotEqual(k1, 'vs-artel-2026-key')


if __name__ == '__main__':
    unittest.main()
