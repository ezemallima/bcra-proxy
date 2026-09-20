"""Política de refresco de bcra_nomdeu.db (padrón offline del BCRA, ~11 GB).

Por qué existe: la app borraba la base local apenas tenía 32 días (o si el período no
coincidía con BCRA_NOMDEU_PERIODO) y recién después intentaba bajar la nueva. Si R2 no
tenía nada más nuevo, o no había espacio, o un reinicio cortaba la descarga, la app
quedaba SIN base: el motor de scoring pasa al BCRA en vivo (sin el historial de 12
meses) y los scores cambian sin aviso. Así estuvo desde el 2/9/2026.

Invariantes:
  * Una base local válida solo se reemplaza si R2 tiene una MÁS NUEVA (LastModified
    posterior a la descarga local). La edad sola nunca justifica borrarla.
  * Si no se puede verificar R2 o no hay espacio ni borrando la actual, se conserva.
  * Solo se borra antes de descargar cuando la nueva no cabe junto a la actual, y
    únicamente después de comprobar que cabe una vez borrada.
"""

ABRIR = 'abrir'
DESCARGAR = 'descargar'
REEMPLAZAR = 'reemplazar'
BORRAR_Y_DESCARGAR = 'borrar_y_descargar'

DIAS_MAXIMOS = 32
MARGEN_BYTES = 512 * 1024 * 1024


def decidir(existe, valida, edad_dias, periodo_local, periodo_esperado, mtime_local,
            tam_local, libre, obtener_remoto, remoto_verificable=True):
    """Devuelve (accion, motivo).

    obtener_remoto: callable que devuelve {'mtime': epoch, 'tam': bytes} o None; solo se
    llama cuando hay motivo para refrescar, para no consultar R2 en cada arranque.
    remoto_verificable=False (origen sin forma de consultar la fecha) conserva el
    comportamiento anterior: una base vieja se borra y se vuelve a bajar.
    """
    if not existe:
        return DESCARGAR, 'no existe'
    if not valida:
        return BORRAR_Y_DESCARGAR, 'archivo inválido o sin historial_detalle'

    motivo = None
    if periodo_esperado and str(periodo_local) != str(periodo_esperado):
        motivo = f'período local ({periodo_local}) distinto de BCRA_NOMDEU_PERIODO ({periodo_esperado})'
    elif edad_dias >= DIAS_MAXIMOS:
        motivo = f'tiene {edad_dias:.0f} días'
    if motivo is None:
        return ABRIR, 'vigente'

    if not remoto_verificable:
        return BORRAR_Y_DESCARGAR, f'{motivo}; origen sin verificación de fecha'

    remoto = obtener_remoto()
    if not remoto:
        return ABRIR, f'{motivo}; no se pudo consultar R2, se conserva la actual'
    if remoto['mtime'] <= mtime_local:
        return ABRIR, f'{motivo}; R2 no tiene una base más nueva, se conserva la actual'

    necesita = remoto['tam'] + MARGEN_BYTES
    if libre >= necesita:
        return REEMPLAZAR, f'{motivo}; R2 tiene una base más nueva'
    if libre + tam_local >= necesita:
        return BORRAR_Y_DESCARGAR, f'{motivo}; R2 tiene una base más nueva y no cabe junto a la actual'
    return ABRIR, (f'{motivo}; R2 tiene una más nueva pero faltan '
                   f'{(necesita - libre - tam_local) / 1e9:.1f} GB de disco, se conserva la actual')
