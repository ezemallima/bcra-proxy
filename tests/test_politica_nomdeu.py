"""Pruebas de la política de refresco de bcra_nomdeu.db. Ejecutar: python -m unittest discover tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from politica_nomdeu import (ABRIR, BORRAR_Y_DESCARGAR, DESCARGAR, REEMPLAZAR, MARGEN_BYTES,  # noqa: E402
                             decidir)

GB = 1024 ** 3
LOCAL = 10 * GB
REMOTO_MTIME_NUEVO = 2_000_000
MTIME_LOCAL = 1_000_000


def remoto(mtime=REMOTO_MTIME_NUEVO, tam=LOCAL):
    return lambda: {'mtime': mtime, 'tam': tam}


def caso(**kw):
    base = dict(existe=True, valida=True, edad_dias=5, periodo_local='202607', periodo_esperado='',
                mtime_local=MTIME_LOCAL, tam_local=LOCAL, libre=4 * GB, obtener_remoto=remoto())
    base.update(kw)
    return decidir(**base)


class Politica(unittest.TestCase):
    def test_no_existe_se_descarga(self):
        self.assertEqual(caso(existe=False)[0], DESCARGAR)

    def test_invalida_se_borra_y_descarga(self):
        self.assertEqual(caso(valida=False)[0], BORRAR_Y_DESCARGAR)

    def test_vigente_se_abre_sin_consultar_r2(self):
        def no_debe_llamarse():
            raise AssertionError('no debería consultar R2 en cada arranque')
        self.assertEqual(caso(edad_dias=5, obtener_remoto=no_debe_llamarse), (ABRIR, 'vigente'))
        self.assertEqual(caso(edad_dias=5, periodo_esperado='202607', obtener_remoto=no_debe_llamarse)[0], ABRIR)

    def test_vieja_pero_r2_sin_novedades_se_conserva(self):
        # El caso que dejó a la app sin base desde el 2/9/2026.
        accion, motivo = caso(edad_dias=40, obtener_remoto=remoto(mtime=MTIME_LOCAL))
        self.assertEqual(accion, ABRIR)
        self.assertIn('se conserva', motivo)
        self.assertEqual(caso(edad_dias=40, obtener_remoto=remoto(mtime=MTIME_LOCAL - 5))[0], ABRIR)

    def test_vieja_y_r2_no_disponible_se_conserva(self):
        self.assertEqual(caso(edad_dias=40, obtener_remoto=lambda: None)[0], ABRIR)

    def test_vieja_con_r2_mas_nueva_y_espacio_reemplaza_sin_borrar(self):
        self.assertEqual(caso(edad_dias=40, libre=LOCAL + MARGEN_BYTES + 1)[0], REEMPLAZAR)

    def test_vieja_con_r2_mas_nueva_y_disco_justo_borra_y_descarga(self):
        # 15 GB de disco: no caben las dos bases, pero sí una vez borrada la vieja.
        self.assertEqual(caso(edad_dias=40, libre=4 * GB)[0], BORRAR_Y_DESCARGAR)

    def test_sin_espacio_ni_borrando_se_conserva(self):
        accion, motivo = caso(edad_dias=40, libre=1 * GB, tam_local=5 * GB)
        self.assertEqual(accion, ABRIR)
        self.assertIn('faltan', motivo)

    def test_periodo_distinto_sin_base_nueva_no_entra_en_bucle(self):
        accion, _ = caso(edad_dias=1, periodo_local='202607', periodo_esperado='202608',
                         obtener_remoto=remoto(mtime=MTIME_LOCAL))
        self.assertEqual(accion, ABRIR)

    def test_periodo_distinto_con_base_nueva_refresca(self):
        accion, motivo = caso(edad_dias=1, periodo_local='202607', periodo_esperado='202608',
                              libre=LOCAL + MARGEN_BYTES + 1)
        self.assertEqual(accion, REEMPLAZAR)
        self.assertIn('BCRA_NOMDEU_PERIODO', motivo)

    def test_origen_no_verificable_conserva_el_comportamiento_anterior(self):
        self.assertEqual(caso(edad_dias=40, remoto_verificable=False)[0], BORRAR_Y_DESCARGAR)
        self.assertEqual(caso(edad_dias=5, remoto_verificable=False)[0], ABRIR)


if __name__ == '__main__':
    unittest.main()
