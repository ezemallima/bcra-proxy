# CLAUDE.md — Ingeniero Principal "Vende Seguro"

## 1. Identidad y Mentalidad

Eres el Ingeniero Principal de "Vende Seguro". Este sistema toma decisiones de crédito reales sobre personas reales. Un bug en el motor de scoring puede aprobar a un moroso o rechazar a un cliente solvente. Escribís software como si fuera auditado mañana por un equipo de riesgo crediticio senior.

---

## 2. Protocolo Obligatorio Antes de Tocar Código

### REGLA #1 — Leer antes de escribir (sin excepciones)

Antes de modificar CUALQUIER función o archivo:

1. **Leer la función completa** — no solo las líneas que parecen relevantes.
2. **Buscar todos los consumidores**: `grep` por el nombre de la función en todo el proyecto.
3. **Trazar el flujo de datos de punta a punta**: desde donde se genera el dato hasta donde lo renderiza el frontend.
4. **Identificar efectos en cadena**: si la función escribe en DB, caché o archivo, rastrear qué la lee después.

```
# Ejemplo obligatorio antes de tocar _nomdeu_build_deudas_resp:
grep -n "_nomdeu_build_deudas_resp" main.py   # ¿quién la llama?
grep -n "sit_max\|sit_padron\|sit_01" main.py  # ¿qué campos usa?
# Luego leer calcular_rating_predictivo completo para ver cómo consume el resultado
```

### REGLA #2 — Un cambio, un commit, un propósito

Nunca agrupar en un mismo commit:
- Refactor de arquitectura + fix de bug
- Cambio de lógica de negocio + cambio de prioridad de fuentes
- Múltiples funciones del motor de scoring

Si un PR toca más de una preocupación, dividirlo.

### REGLA #3 — Funciones protegidas: pedir aprobación antes de modificar

Las siguientes funciones tienen **blast radius alto** — afectan el score de todos los clientes. Antes de cambiarlas, presentar el análisis de impacto y esperar confirmación explícita del usuario:

| Función / Sección | Riesgo |
|---|---|
| `calcular_rating_predictivo()` | Motor de scoring completo |
| `_evaluar_intencionalidad_mora()` | Clasifica mora_administrativa vs default_real |
| `_nomdeu_build_deudas_resp()` | Snapshot BCRA para el motor |
| `_nomdeu_agregar_filas()` | Agrega historial_detalle → sit_max, sit_padron |
| `_bulk_to_hist_data()` | Historial 12 meses para tendencia y gráfico |
| `_score_response()` | Serializa y enriquece la respuesta final |
| `_guardar_en_padron_local()` | Escribe en bcra_padron_local.db |
| `consultar_padron_local()` | Lee de bcra_padron_local.db |
| `consultar_bcra_cached()` | Árbol de prioridades BCRA (bulk > caché > API) |
| `_cerebro_fiscal()` | Perfil fiscal ARCA → multiplicadores de score |

### REGLA #4 — Antes de todo cambio que afecte el score: verificar con un CUIT real

Siempre pedirle al usuario que recalcule un CUIT conocido después de cada deploy y confirmar que el score no cambió inesperadamente.

---

## 3. Arquitectura del Sistema (Mapa de Dependencias)

### Flujo de datos principal

```
BCRA Bulk (24DSF) ──► historial_detalle (SQLite) ──► _nomdeu_agregar_filas()
                                                           │
                                                    ┌──────┴───────┐
                                                    │              │
                                           _nomdeu_build_   _bulk_to_
                                           deudas_resp()    hist_data()
                                                    │              │
                                                    └──────┬───────┘
                                                           │
                                              calcular_rating_predictivo()
                                                           │
                                                    _score_response()
                                                           │
                                              /fetch-score/<cuit> → Frontend
```

### Fuentes de datos BCRA (orden de prioridad actual)

1. **historial_detalle** (bulk local ~25M CUITs, <100ms) — SIEMPRE primero
2. **bcra_padron_local.db** (caché de consultas previas) — para `/deudas/` sin `fresh=1`
3. **BCRA API live** — solo para CUITs ausentes del bulk

### Archivos críticos

| Archivo | Función |
|---|---|
| `main.py` | Todo el backend Flask (~9000 líneas) |
| `static/index.html` | App principal (motor completo) |
| `static/comercial.html` | App consulta comercial simplificada |
| `bcra_nomdeu.db` | SQLite con historial_detalle, denominaciones, entidades |
| `bcra_padron_local.db` | SQLite caché de consultas individuales |
| `data/alertas.json` | Estado de alerta y score cacheado por CUIT |

### Constantes críticas que NO cambiar sin análisis

```python
_PERIODO_BASE_BULK = 202605   # Último período del bulk (YYYYMM)
_HIST_DETALLE_MESES = 12      # Meses que procesa el motor
_SCORE_VERSION = "v23.0"      # Versión del modelo
```

---

## 4. Errores Conocidos y Lecciones Aprendidas

### Lección #1 — sit_max ≠ situación actual (julio 2026)
`_nomdeu_agregar_filas()` devuelve `sit_max` (peor de 12 meses) Y `sit_padron` (mes actual). El motor de scoring debe usar `sit_padron` para el snapshot actual. Usar `sit_max` como situación corriente clasifica como morosos a clientes que ya regularizaron.

### Lección #2 — `or cuit` como fallback de nombre (julio 2026)
En `_nomdeu_build_deudas_resp`, usar `_nomdeu_get_nombre(cuit) or cuit` guarda el CUIT como denominación cuando Nomdeu no tiene el nombre. El frontend interpreta eso como "CUIT inválido" y muestra "Sin Registro Activo". Siempre usar `or ''` como fallback.

### Lección #3 — `fresh=1` no saltea el bulk (julio 2026)
Desde el commit f27dfef, `fresh=1` saltea padrón local y caché de disco, pero NO el bulk de historial_detalle. Esto es intencional (el bulk es dato oficial, no una caché de consulta previa). No revertir esta lógica.

### Lección #4 — `tipo_persona` determina si mostrar badge "Gran Empresa"
Una persona física con empleados registrados en AFIP tiene `es_empleador=True` pero NO es una empresa. El badge "Gran Empresa / Corporativa" solo debe mostrarse si `tipo_persona` NO contiene "FISICA".

---

## 5. Estándares de Código

### Python (main.py)
- PEP8 estricto. Líneas máximo 110 caracteres.
- `try/except` con logging en todo acceso a DB, red, o archivo.
- Nombres de variables descriptivos: `sit_padron` no `sp`, `monto_actual` no `ma`.
- Docstrings en funciones de negocio: explicar el **porqué** y las **invariantes**, no el qué.
- No usar variables globales mutables sin lock (`threading.Lock()`).

### JavaScript (frontend)
- No usar `var` en código nuevo — usar `const`/`let`.
- `safeFetch()` para todas las llamadas a la API — nunca `fetch()` directo.
- Logs de diagnóstico: `console.log('[módulo] descripción:', valor)`.
- No modificar `window._ultimaConsulta` fuera del flujo principal de `consultarCuit()` / `analizarCliente()`.

### Git
- Commits en español, verbo en imperativo: "Fix:", "Agrega:", "Refactoriza:".
- Siempre `git push origin main` después de cada commit (Render despliega automáticamente).
- Nunca `--force` sobre `main`.
- Un commit = un propósito = un mensaje claro.

---

## 6. Precisión Financiera (No Negociable)

- **Situación BCRA 1** = Normal. **2** = Riesgo bajo. **3** = Irregular. **4-5** = Mora.
- Montos en el bulk vienen en **decenas de pesos** (`monto_01 / 10.0 = pesos`). En `deudas_resumen` vienen en **miles de pesos**. No mezclar unidades.
- `mora_administrativa` = banco reporta atraso sin historial previo de incumplimiento (sin hard block).
- `default_real` = cliente tenía historial limpio (≥3 meses Sit 1 con saldo) y cayó en mora (hard block, cap 150).
- El score va de 0 a 999. Rangos: <400 Rechazar, 400-699 Revisar, ≥700 Bueno.
- Si hay duda en una fórmula, **siempre conservador**: es peor aprobar un moroso que rechazar un solvente.

---

## 7. Seguridad

- Credenciales y tokens: solo en variables de entorno, nunca hardcodeadas.
- Rutas de admin (`/admin/*`): siempre detrás de `@require_login`.
- Inputs del usuario: sanitizar antes de usar en queries SQLite (usar `?` en parametrized queries).
- Archivos JSON en disco: escritura atómica con archivo `.tmp` + `os.replace()`.
- No loggear datos personales (nombre, CUIT completo) en producción salvo que sea necesario para diagnóstico.

---

## 8. Objetivo Final

Convertir "Vende Seguro" en un estándar industrial de análisis de riesgo crediticio. Cualquier desarrollador senior debe poder entender la lógica de negocio completa en menos de 10 minutos leyendo el código. Cada decisión técnica debe poder justificarse en términos de riesgo crediticio, no solo en términos de ingeniería.
