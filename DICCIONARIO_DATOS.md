# Diccionario de datos — Bot IqOption

> Última actualización: 2026-07-24 · BD: `~/.local/share/iqbot/bot.db` (SQLite, WAL) · 10 tablas

---

## 1. Origen y uso de los datos (panorama)

**Qué es esto.** La BD conserva el histórico retirado de **FibPullback** y,
desde 2026-07-24, el forward test de **Confirmed Band Reversion** sobre velas
de 1 minuto y expiración de 5 minutos en cuenta DEMO/PRACTICE. El
`config_epoch` evita mezclar las dos estrategias.

**Ciclo de un dato (de dónde sale cada fila):**
1. El controlador refresca caché de mercado (apertura de activos, payouts, balance) desde la API de IQ.
2. Por cada activo se piden velas → `ConfirmedBandReversionStrategy` calcula
   Bollinger, RSI, ATR, ADX y rechazo de mecha; emite CALL/PUT/NONE.
   Independientemente del resultado, escribe el embudo de condiciones en
   `strategy_evaluations`.
3. Si la señal es accionable, el **motor de riesgo** (`RiskEngine`) aplica los filtros de "no operar".
   - **Rechazada** → **paper-trade** (`kind='paper'`, `valid_trade=0`): NO se envía a IQ, pero se **sigue virtualmente** con el precio para registrar qué habría pasado. → esto es lo que permite el **análisis contrafactual** de los filtros.
   - **Aprobada** → pasa a la **capa de gestión de riesgo** (`RiskManager`, paso 3b).
3b. **Gestión de riesgo** (solo si pasó los filtros normales y habría sido real):
   - **Sin pausa** → operación **real en demo** (`kind='real'`, `valid_trade=1`).
   - **En pausa** → **paper-trade** (`kind='paper'`, `valid_trade=0`) con `reject_reason` de riesgo (`risk_cooldown_2_consecutive_losses` / `risk_window_6_13_max_3_losses`) y `would_be_real_without_risk=1`. Permite analizar si la pausa evitó pérdidas o también bloqueó ganadores. → detalle en `trade_risk_management` (§9).
4. Cada señal acción genera **1 fila en `trades`** + filas en las **tablas satélite** (cuelgan por `trade_id`).
5. Al vencer, se reconstruye el resultado (win/loss/draw) y las métricas de recorrido del precio → `trade_outcomes`.

**Clave de relación:** `trades.trade_id` (TEXT, hash corto). Todas las satélite referencian `trade_id`. *(Las FOREIGN KEY se declaran como documentación; SQLite no las fuerza aquí — la integridad la mantiene el código.)*

**Las 10 tablas:**
| Tabla | Filas/escala | Para qué sirve |
|---|---|---|
| `trades` | 1 por señal accionada | Operación base (decisión + indicadores resumidos + resultado) |
| `trade_market_snapshots` | 1 por trade·timeframe | Foto técnica completa en el momento de la señal |
| `trade_filter_evaluations` | ~5-8 por trade | 1 fila por filtro evaluado (normales + reglas de riesgo) |
| `strategy_evaluations` | 1 por activo·vela·epoch | Condiciones y motivo de no señal, incluso sin trade |
| `trade_candle_snapshots` | ~50 por trade | OHLCV crudo alrededor de la señal (reconstrucción visual) |
| `trade_price_path` | varias por trade | Recorrido del precio durante la operación |
| `trade_api_context` | 1 por trade | Estado de la API/caché al disparar (latencias, frescura, payout) |
| `trade_risk_management` | 1 por trade que llega a la capa de riesgo | Foto de la gestión de riesgo (¿bloqueada?, contadores, pausa) |
| `trade_outcomes` | 1 por trade cerrado | Métricas finales: exit, MFE/MAE, tiempos, payout real |
| `app_settings` | clave/valor | Configuración (activos, on/off, config JSON) |

---

## 2. Tabla `trades` (operación base)

Una fila por señal que se actuó (real o paper). Mantiene también un **resumen** de indicadores para consulta rápida sin JOIN.

| Columna | Tipo | Significado |
|---|---|---|
| `id` | INT PK | Autoincremental interno |
| `kind` | TEXT | `real` (operada en demo) o `paper` (rechazada, seguida virtualmente) |
| `trade_id` | TEXT | Identificador único (hash). Clave de unión con las satélite |
| `date`, `time` | TEXT | Fecha y hora de la señal |
| `asset` | TEXT | Activo (p. ej. `EURJPY-OTC`) |
| `is_otc` | INT | 1 si es activo OTC |
| `direction` | TEXT | `call` o `put` |
| `amount` | REAL | Monto de la operación |
| `payout` | REAL | Payout usado (fracción 0–1; p. ej. 0.87 = 87%) |
| `expiration` | INT | Minutos de expiración (5) |
| `signal_time` | TEXT | Momento de la señal |
| `entry_time` | TEXT | Momento de entrada real |
| `entry_delay_seconds` | REAL | Retraso señal→entrada (segundos) |
| `order_id` | TEXT | ID de orden devuelto por IQ (real) |
| `result` | TEXT | `win` / `loss` / `draw` / `pending` / `error` |
| `profit` | REAL | Ganancia/pérdida neta |
| `balance_after` | TEXT | Balance tras la operación |
| `valid_trade` | INT | 1 = real (pasó filtros); 0 = paper (rechazada) |
| `reject_reason` | TEXT | Motivo de rechazo (ver §4) |
| `bb_width` | REAL | Ancho de Bollinger en la señal |
| `bb_width_median` | REAL | Mediana del ancho BB (referencia de squeeze) |
| `ema_distance` | REAL | \|EMA50 − EMA200\| (fuerza de tendencia) |
| `candle_range` | REAL | Rango (high−low) de la vela de la señal |
| `avg_range_20` | REAL | Rango medio de 20 velas (referencia de vela gigante) |
| `rsi` | REAL | RSI(14) en la señal |
| `notes` | TEXT | Texto descriptivo del contexto |
| `entry_price` | REAL | Precio de entrada |
| `market_type` | TEXT | `turbo` / `binary` / `binary_assumed` / `digital` (instrumento real usado) |
| `created_at`, `updated_at` | TEXT | Marcas de tiempo de inserción/actualización |

---

## 3. Tabla `trade_market_snapshots` (foto técnica)

Origen: `build_live_snapshot()` a partir del DataFrame de indicadores en la señal (`source='live'`). Filas antiguas migradas desde `trades` por backfill (`source='backfill'`).
**Particularidad:** hoy solo se guarda a **`timeframe_seconds=60`**; los slots 300/900 (5M/15M) están previstos pero aún no se llenan.

| Columna | Significado |
|---|---|
| `timeframe_seconds` | 60 / 300 / 900 (hoy solo 60) |
| `candle_timestamp` | Timestamp de la vela |
| `open/high/low/close/volume` | OHLCV de la vela de la señal |
| `ema50`, `ema200` | Medias exponenciales rápida/lenta |
| `ema_distance`, `ema_distance_pct` | Distancia EMA50−EMA200 (abs y %) → fuerza de tendencia |
| `ema_slope_50_1/_3`, `ema_slope_200_1/_3` | Pendiente de las EMA a 1 y 3 velas (dirección/aceleración) |
| `rsi`, `rsi_prev`, `rsi_delta` | RSI actual, anterior y su variación |
| `bb_top/mid/bottom` | Bandas de Bollinger |
| `bb_width`, `bb_width_pct` | Ancho de banda (abs y %) |
| `bb_width_median_100` | Mediana del ancho en 100 velas |
| `bb_squeeze_ratio` | `bb_width / mediana` (<1 = bandas comprimidas) |
| `bb_width_slope_3` | Tendencia del ancho (expandiendo/contrayendo) |
| `close_position_in_bb` | Posición del cierre dentro de la banda (0=bottom, 1=top) |
| `swing_high/low/range` | Máximo/mínimo/rango del swing (25 velas) |
| `fib_position` | Posición del precio en el rango de Fibonacci (0–1) |
| `distance_to_fib_382/500/618` | Distancia a los niveles de Fibonacci |
| `candle_range`, `avg_range_20`, `candle_range_ratio` | Rango de la vela, medio de 20, y su cociente (>1 = vela grande) |
| `atr_14`, `adx_14`, `range_atr_ratio` | Volatilidad Wilder, fuerza de tendencia y tamaño de vela normalizado |
| `body_size`, `body_ratio` | Tamaño del cuerpo y cuerpo/rango |
| `upper_wick_ratio`, `lower_wick_ratio` | Proporción de las mechas superior/inferior |
| `trend_label` | `bullish` / `bearish` / `lateral` (etiqueta derivada) |
| `source` | `live` (en vivo) o `backfill` (migrado de `trades`) |

---

## 3b. Tabla `strategy_evaluations` (embudo de señal)

Se escribe una fila por activo y vela cerrada aunque la decisión sea `NONE`.
La clave única `(asset, timeframe_seconds, candle_timestamp, config_epoch)`
evita duplicar una vela después de una reconexión.

| Grupo | Columnas | Qué permite diagnosticar |
|---|---|---|
| Identidad | `strategy_revision`, `config_epoch`, `candidate_direction`, `signal_direction` | No mezclar reglas o parámetros distintos |
| Decisión | `actionable`, `rejection_reason` | Motivo exacto de señal/no señal |
| Condiciones | `band_reentry`, `candle_confirm`, `rsi_extreme`, `rsi_turn`, `wick_confirm`, `adx_ok`, `range_atr_ok` | Qué umbral está eliminando candidatos |
| Valores | `rsi`, `prev_rsi`, `adx`, `atr`, `range_atr_ratio`, mechas, bandas y cierre | Recalibrar con los valores reales, no por intuición |

---

## 4. Tabla `trade_filter_evaluations` (por qué se operó/rechazó)

Origen: `RiskEngine.evaluate_all()` — **una fila por filtro evaluado**, sin alterar la decisión (solo la describe).

| Columna | Significado |
|---|---|
| `filter_name` | Normales: `payout`, `already_open`, `bb_squeeze`, `giant_candle`, `lateral_market`, `late_entry`. Riesgo (solo si pasó los normales): `risk_cooldown_2_consecutive_losses`, `risk_window_6_13_max_3_losses` |
| `filter_type` | `HARD` (siempre bloquea), `SOFT` (configurable), `INFO` |
| `triggered` | 1 = la condición de "no operar" se cumplió para ese filtro |
| `blocked` | 1 = ese filtro fue el que **efectivamente** rechazó la señal (coincide con `reject_reason`) |
| `value` | Valor medido (p. ej. el ancho BB) |
| `threshold` | Umbral con el que se comparó |
| `reason` | Texto explicativo |

**Motivos de rechazo (`reject_reason` en `trades`):**
| Motivo | Tipo | Qué significa |
|---|---|---|
| `payout_unavailable` | HARD | Payout = 0/None (no se pudo leer) |
| `low_payout` | HARD/SOFT | Payout por debajo del mínimo configurado |
| `already_open` | HARD | Ya hay operación abierta en ese activo |
| `bb_squeeze` | SOFT | Bandas comprimidas (`bb_width < mediana × squeeze_factor`) |
| `giant_candle` | SOFT | Vela gigante (`range > avg_range_20 × giant_candle_factor`) |
| `lateral_market` | SOFT | Mercado plano (`ema_distance < avg_range_20 × lateral_factor`) |
| `late_entry` | HARD | Entrada tardía (delay > máximo) |
| `no_real_instrument` | — | El activo no resultó operable en ningún tipo (binary/digital). En pares **reales (no-OTC)** la causa habitual NO es un bug: IQ no tenía habilitada la opción **turbo** (la que usamos, binarias ≤5 min) para ese par → la compra devuelve `invalid instrument` / `asset is not available` y cae a paper. Ver §11. |
| `digital_unavailable` | — | Solo quedaba digital y no estaba disponible |
| `risk_cooldown_2_consecutive_losses` | HARD (riesgo) | Pasó los filtros normales pero hay pausa por 2 pérdidas reales consecutivas (15 min desde el cierre de la 2ª). `would_be_real_without_risk=1` |
| `risk_window_6_13_max_3_losses` | HARD (riesgo) | Pasó los filtros normales pero hay pausa por 3 pérdidas reales dentro de 06:00–13:00 (hasta las 13:00). `would_be_real_without_risk=1` |

> ⚠️ **Prioridad de motivos:** primero los filtros normales, luego la gestión de riesgo. Una señal que falla por un filtro normal (p. ej. `lateral_market`) conserva ESE motivo aunque ocurra dentro de 06:00–13:00; nunca se reescribe como pausa de riesgo. La capa de riesgo solo se evalúa si la señal habría sido **real**. Entre las dos reglas de riesgo, si ambas aplican dentro de la ventana, gana la de ventana (pausa hasta 13:00).
>
> **Distinguir los 3 tipos de fila:** real ejecutada = `kind='real'`; paper por filtro normal = `kind='paper' AND reject_reason NOT LIKE 'risk_%'`; paper por gestión de riesgo = `kind='paper' AND reject_reason LIKE 'risk_%'` (o `trade_risk_management.risk_blocked=1`).

---

## 5. Tabla `trade_candle_snapshots` (velas crudas)

Origen: OHLCV alrededor de la señal, para reconstruir el gráfico.
| Columna | Significado |
|---|---|
| `timeframe_seconds` | 60 / 300 / 900 |
| `candle_index` | `−N … 0` (0 = vela que disparó la señal; negativos = anteriores) |
| `candle_timestamp` | Timestamp de la vela |
| `open/high/low/close/volume` | OHLCV |

---

## 6. Tabla `trade_price_path` (recorrido del precio)

Origen: `build_price_path()` a partir de las velas que cubren la operación.
| Columna | Significado |
|---|---|
| `timestamp` | Momento del punto |
| `seconds_from_entry` | Segundos desde la entrada (eje temporal del recorrido) |
| `price` | Precio en ese punto |
| `candle_open/high/low/close` | OHLCV de la vela del punto |

---

## 7. Tabla `trade_api_context` (estado de la API en la señal)

Origen: `bot_runner._build_log_context()` + `controller.market_meta()` + latencia de compra del ejecutor.
| Columna | Significado |
|---|---|
| `asset`, `normalized_asset` | Activo (original y normalizado) |
| `market_type` | Tipo de instrumento usado |
| `asset_exists`, `asset_open` | 1/0: existía en el catálogo / estaba abierto |
| `payout`, `payout_source` | Payout y de dónde salió (`turbo`/`binary`/`unavailable`) |
| `open_time_cache_age_seconds` | **Antigüedad** de la caché de apertura usada (segundos) |
| `profit_cache_age_seconds` | **Antigüedad** de la caché de payout usada (segundos) |
| `api_latency_candles_ms` | Latencia al pedir velas |
| `api_latency_profit_ms` | Latencia de `get_all_profit` |
| `api_latency_open_time_ms` | Latencia de `get_open_times` |
| `api_latency_buy_ms` | Latencia de la compra en IQ |
| `remaining_to_expiration` | Segundos restantes hasta la expiración |
| `connection_ok` | 1/0: conexión OK |

> Las columnas `*_cache_age_seconds`, `api_latency_profit_ms` y `api_latency_open_time_ms` **solo se rellenan desde el 2026-06-26 ~22:25** (cuando se instrumentó la frescura). Filas anteriores las tienen en NULL.

---

## 8. Tabla `trade_outcomes` (resultado y métricas finales)

Origen: `reconstruct_outcome()` con las velas posteriores al cierre.
| Columna | Significado |
|---|---|
| `result`, `profit` | Resultado final y ganancia neta |
| `entry_price`, `exit_price` | Precio de entrada y de cierre |
| `raw_price_delta` | Diferencia de precio bruta (exit − entry) |
| `directional_price_delta` | Diferencia **a favor** de la dirección apostada (signo corregido) |
| `win_margin` | Margen con el que ganó/perdió |
| `max_price_during_trade`, `min_price_during_trade` | Máximo/mínimo del precio durante la operación |
| `max_favorable_excursion` (MFE) | Cuánto llegó a favor en el mejor momento |
| `max_adverse_excursion` (MAE) | Cuánto llegó en contra en el peor momento |
| `seconds_to_first_profit` | Segundos hasta entrar en ganancia por primera vez |
| `seconds_to_max_favorable` / `_max_adverse` | Tiempos a los extremos favorable/adverso |
| `close_time` | Momento del cierre |
| `result_source` | Cómo se obtuvo (`reconstructed`, `virtual`, `check_win_*`) |
| `realized_payout` | Payout **real** derivado del profit en un WIN (`profit/amount`) |

> ⚠️ **MFE/MAE/deltas están en unidades de precio crudas** y mezclan activos de escalas muy distintas (oro vs forex): **no comparar en absoluto sin normalizar** por activo o por % (`analytics()` ya calcula una versión en %).

---

## 9. Tabla `trade_risk_management` (capa de gestión de riesgo)

Origen: `RiskManager` vía `TradeService.process_signal` — **una fila por señal que llegó a la capa de riesgo** (es decir, que ya pasó todos los filtros normales). NO existe para señales rechazadas por un filtro normal. Tabla **aditiva**: cero impacto en `trades` y en los datos ya recolectados (las filas/épocas previas a esta capa simplemente no tienen fila aquí).

Los contadores se **recomputan desde la BD** en cada señal a partir de operaciones **reales cerradas** (`kind='real'`, `result IN ('win','loss')`); los paper-trades, `draw` y `error` no los alimentan.

| Columna | Significado |
|---|---|
| `would_be_real_without_risk` | 1 = la señal habría sido **real** pero la gestión de riesgo la mandó a paper |
| `risk_blocked` | 1 = bloqueada por gestión de riesgo (en pausa) |
| `risk_block_reason` | `risk_cooldown_2_consecutive_losses` / `risk_window_6_13_max_3_losses` / NULL |
| `risk_pause_until` | Hasta cuándo dura la pausa (`YYYY-MM-DD HH:MM:SS`) o NULL |
| `risk_consecutive_losses_at_signal` | Pérdidas reales consecutivas en el momento de la señal |
| `risk_losses_6_13_at_signal` | Pérdidas reales de HOY dentro de 06:00–13:00 en el momento de la señal |
| `in_window` | 1 = `signal_time` cae dentro de 06:00:00–13:00:00 |
| `created_at` | Marca de inserción |

**Reglas (configurables en caliente, pestaña Configuración → "Gestión de riesgo"):**
- **A — Cooldown:** 2 pérdidas reales **consecutivas** ⇒ pausa de **15 min** desde el `close_time` de la 2ª. Mientras `consecutive≥2` y `now < close+15min`, las señales que serían reales se registran como paper con `risk_cooldown_2_consecutive_losses`.
- **B — Ventana 06:00–13:00:** `risk_window_max_losses` pérdidas reales dentro de la ventana (por `signal_time`) ⇒ pausa **hasta las 13:00** del mismo día (`risk_window_6_13_max_3_losses`). Se reinicia al cambiar de día.
- Claves de config: `risk_mgmt_enabled`, `risk_cooldown_losses` (2), `risk_cooldown_minutes` (15), `risk_window_start_hour` (6), `risk_window_end_hour` (13, exclusivo), `risk_window_max_losses` (umbral configurable; default histórico 3).
- Nota: el nombre técnico `risk_window_6_13_max_3_losses` quedó fijo por compatibilidad aunque el umbral real pueda cambiar por configuración.

Las dos reglas también dejan rastro en `trade_filter_evaluations` (`filter_type='HARD'`): `triggered=1` si su pausa está activa, `blocked=1` solo en la regla que efectivamente mandó la señal a paper.

---

## 10. Tabla `app_settings`

Clave/valor. Claves actuales:
- `assets` → JSON con la lista de activos. Snapshot validado 2026-06-29 07:22: `["EURUSD","AUDCAD","EURGBP","AUDJPY","GBPJPY","GBPUSD"]`.
- `bot_enabled` → `1`/`0`.
- `config` → JSON con la configuración de estrategia y filtros (montos, factores, periodos de indicadores).

---

## 11. Particularidades que afectan al análisis (¡importante!)

Cambios hechos en caliente que **segmentan los datos** — hay que tenerlos en cuenta al comparar épocas:

### Ajustes del filtro de velas gigantes (`giant_candle_factor`)
- **2026-06-26 14:57 (2:57 pm):** `2.2 → 1.3` — **cambio grande**: endurece mucho el filtro (rechaza más velas como "gigantes").
- **2026-06-26 20:16 (8:16 pm):** `1.3 → 1.5` — **ajuste fino** del anterior, suaviza un poco.
- **Implicación:** la mayoría del histórico se recogió con `2.2`. Cualquier análisis del filtro `giant_candle` (y del umbral `candle_range_ratio`) **debe segmentarse por estas tres épocas**, no mezclarse.

### Desactivación del filtro `bb_squeeze`
- **2026-06-27 05:15:46:** `reject_bb_squeeze: True → False` — el filtro de squeeze de Bollinger queda **desactivado**.
- **Efecto:** desde ese momento, las señales que cumplen la condición de squeeze **se TOMAN** (operación real) en vez de mandarse a paper. Quedan trazadas en `trade_filter_evaluations` con `filter_name='bb_squeeze'`, `triggered=1`, `blocked=0` y `reason='bb_width bajo umbral; permitido por usuario'`.
- **Implicación:** a partir de aquí hay **outcomes reales** del escenario "operar con squeeze" (antes solo existía la estimación contrafactual vía paper). Para analizar, **segmentar por época** (antes/después de `2026-06-27 05:15`); no mezclar con el periodo en que el filtro bloqueaba.
- Consulta de las "tomadas pese al squeeze": `trade_filter_evaluations` con `filter_name='bb_squeeze' AND triggered=1 AND blocked=0 AND kind='real'`.

### Otros cambios de configuración registrados (de los logs)
- **06-25 18:28:** `base_amount 7 → 100` (cambia la escala de `profit`/`net` antes/después).
- **06-25 19:03 → 22:42:** ventana con `min_payout = 0.0` (el filtro de payout estuvo **apagado** ~3.5 h → entraron trades que normalmente se filtrarían).

### Activación de la capa de gestión de riesgo
- **2026-06-27 (reinicio):** entra en vigor `RiskManager` (reglas A y B). Desde aquí, señales que ANTES habrían sido reales pueden quedar como paper con `reject_reason` `risk_*` y `would_be_real_without_risk=1`.
- **Implicación:** segmentar el análisis de operaciones reales por antes/después de este reinicio. Los `risk_*` en paper son el conjunto contrafactual para medir si la pausa evitó pérdidas o también bloqueó ganadores (`kind='paper' AND reject_reason LIKE 'risk_%'`, luego mirar su `result`).
- Apagable en caliente con `risk_mgmt_enabled=0` (las señales vuelven a operarse en real sin pausas).
- **2026-06-29 07:18:** durante la validación de pares no-OTC se subió `risk_window_max_losses` de 3 a 20 para no cortar tan pronto el experimento de 06:00–13:00. A las 07:13 ya se habían bloqueado señales con el umbral anterior de 3.
- **2026-06-29 07:22:** la configuración quedó guardada con `risk_window_max_losses=4` (valor vigente al validar esta nota). Para análisis, tratar `07:18–07:22` como una ventana transitoria con umbral 20 y desde `07:22` como umbral 4.

### Pares reales (no-OTC): habilitación de opción TURBO
- **Causa de los errores `invalid instrument` / `asset is not available` / paper `no_real_instrument` en pares reales:** NO es un bug del bot ni del ID. La estrategia opera **turbo** (binarias de ≤5 min) y, hasta ahora, IQ **no tenía habilitada la opción turbo** para los pares reales (no-OTC) en esta cuenta. A nivel de librería, esos pares no aparecían en `result.turbo.actives` de `api_option_init_all` (o con `commission=100`), así que `get_all_profit()` devolvía payout **0**; el bot caía al `assumed_payout=0.85` (`binary_assumed`) e IQ rechazaba la compra → paper.
- **2026-06-28 ~20:00 (8 y algo PM): IQ habilitó turbo para `EURUSD`** (y SOLO EURUSD por ahora). A partir de aquí EURUSD sí debería traer payout real de turbo y operar en real.
- **Regla de lectura:** si tras esa hora vuelve a salir `invalid instrument` / `no_real_instrument` **para EURUSD**, eso SÍ es un error a investigar (ya no es "turbo no habilitado"). Para los otros pares reales (AUDUSD, GBPUSD, NZDUSD, EURJPY-real, etc.) sigue siendo lo esperado mientras IQ no les habilite turbo.
- **Para el análisis:** los datos no-OTC previos a esta habilitación son casi todos **paper/virtual** (no fills reales) → no fiables. El dato real limpio de pares reales empieza con EURUSD desde ~2026-06-28 20:00.
- **2026-06-28 19:00:** se activó una prueba con pares no-OTC. En la BD, el tramo 19:00–19:54 dejó señales en `GBPUSD`, `NZDUSD` y `EURJPY`, casi todas paper; varias terminaron en `no_real_instrument` o filtros normales, así que no sirven como evidencia de ejecución real.
- **2026-06-29 01:07:** primera operación real registrada de `EURUSD` dentro de este experimento no-OTC. Hasta la revisión de 07:22, EURUSD llevaba 8 reales decididas: 4 win / 4 loss, neto -60 por payout 0.85. Conclusión provisional: muestra pequeña; EURUSD es el único no-OTC con ejecución real, pero todavía no prueba ventaja.
- **Convención de análisis para no-OTC:** para estimar qué habría pasado sin la pausa de ventana, contar `kind='paper' AND reject_reason='risk_window_6_13_max_3_losses'` como **real hipotético**. No mezclarlo con otros paper (`giant_candle`, `lateral_market`, `no_real_instrument`, `already_open`, etc.), porque esos no pasaron por la misma lógica de "habría sido real pero la ventana lo pausó".

### Cambios operativos (infraestructura)
- **06-26 ~22:09:** la BD se **movió de `~/Documents` (iCloud) a `~/.local/share/iqbot/`** por `disk I/O error` que causaba iCloud al sincronizar los `-wal`/`-shm`. Entre ~21:25 y 22:09 del 26-jun **se perdieron las señales de ese tramo** (inserciones fallidas, sin corrupción del resto).
- **06-26 ~22:25:** el refresco de mercado pasó a **paralelo** y empezaron a llenarse las columnas de **frescura/latencia** de `trade_api_context`.

### Notas técnicas transversales
- La estrategia opera **velas de 1M** (no 5M, pese a algún comentario): `TIMEFRAME_SECONDS=60`, expiración 5M.
- `trade_market_snapshots` solo se llena a **60s** (faltan 300/900).
- `trade_price_path` está **poco poblado** (pocos puntos por trade).
- Cuenta **siempre PRACTICE** (demo); `profit` es dinero virtual.

---

## Cambios 2026-07-10 — Selección automática de mercado real

El proyecto pivota: operar OTC 5min demostró ser coin flip, así que el bot deja de depender de una whitelist manual de activos y pasa a **descubrir solo los activos de mercado REAL (no-OTC) abiertos**, rotando la lista sin intervención humana.

**Config nueva (`config/settings.py` + `config/runtime_config.py`, editable en caliente desde el panel):**
- `auto_asset_selection` (bool, def `True`): si está activo, la whitelist manual (`app_settings.assets`) se ignora como entrada del usuario — el controlador la sobrescribe en cada rotación con la selección automática. Apagarlo restaura el comportamiento manual de siempre.
- `asset_refresh_minutes` (int, def `15`): cada cuántos minutos se recalcula la selección (además de siempre al conectar).
- `otc_fallback` (bool, def `False`): si NO hay ningún activo real abierto (fin de semana/festivo) y esto está encendido, permite operar OTC temporalmente. Por defecto apagado → el bot queda en **idle explícito** (activos = lista vacía, no se escanea nada, log claro cada `asset_refresh_minutes`).

**Lógica (nuevo módulo `domain/asset_selection.py`, función pura sin I/O, testeada en `test_asset_selection.py`):**
- `select_real_assets(open_times, profits, min_payout, max_assets, include_otc=False, universe=None)`: toma activos turbo/binary **abiertos**, excluye OTC (sufijos `-OTC`/`OTC`/`-OP`) salvo `include_otc=True`, cruza con el mejor payout disponible (turbo o binary), descarta los que no llegan a `min_payout`, ordena por payout desc (alfabético en empate) y recorta a `max_assets`.
- `diff_selection(previous, new)`: qué activos entraron/salieron entre dos rotaciones, para el log de auditoría.

**Orquestación (`application/controller.py`, método `_refresh_asset_selection`):**
- Se llama desde el hilo de fondo del `BotController`: una vez al conectar (`force=True`) y luego cada `asset_refresh_minutes`.
- **Reutiliza la caché de mercado ya existente** (`self._open_cache` / `self._profit_cache`, poblada por `IQOptionClient.get_market_snapshot()`, la misma que ya usa el resto del bot) en vez de llamar a `stable_api.get_all_open_time()` — ver "Decisión de diseño" abajo.
- Si no hay snapshot de apertura todavía, conserva la última selección conocida y reintenta barato (sin red) en el siguiente ciclo, con log de espera limitado a 1/min.
- Si el cálculo lanza una excepción, conserva la última selección conocida y loguea (nunca deja al bot sin lista por un fallo transitorio).
- Si no hay NINGÚN activo real abierto: intenta `otc_fallback` si está encendido; si no hay nada ni con fallback, la selección queda vacía (`db.set_assets([])`) → el escaneo del ciclo principal no tiene nada que recorrer → **idle real, sin llamadas de red adicionales por activo** — y se loguea un mensaje de idle explícito, throttlado a 1 cada `asset_refresh_minutes`.
- La lista resultante se escribe en `app_settings.assets` (vía `db.set_assets`, el mismo mecanismo que ya usaba la whitelist manual — así el resto del pipeline de escaneo no cambió) y además se persiste una foto de auditoría en la clave nueva `app_settings.auto_asset_selection_state` (JSON con `ts`, `updated_at`, `reason` [`real`|`otc_fallback`|`idle`], `otc_fallback_active`, `assets` y `detail` con payout/tipo/es_otc por activo).
- Cada rotación con cambios reales loguea explícitamente qué activo entró y cuál salió (`Rotación de activos (...): entran=[...] salen=[...] -> vigentes=[...]`).
- `classify()` se ajustó para permitir activos OTC cuando `otc_fallback` está activo (antes solo miraba `allow_otc` manual).

**`app_settings` — clave nueva:**
- `auto_asset_selection_state` → JSON de auditoría de la última rotación automática (ver estructura arriba). No reemplaza a `assets` (que sigue siendo la lista "vigente" que lee el bucle de escaneo); es la traza de auditoría de *por qué* quedó esa lista.

**Decisión de diseño — por qué NO se usa `stable_api.get_all_open_time()`:** la consigna original pedía usarlo, pero el propio código ya documentaba (`IQOptionClient.get_open_times()`, antes de este cambio) que ese método de la librería lanza 3 hilos (incluida una suscripción a opciones digitales por websocket) y puede colgarse más de 30s — inaceptable en la Raspberry Pi. El bot ya tenía un snapshot rápido y cacheado (`get_market_snapshot()`, ~2s, vía `get_api_option_init_all_v2`) que se refresca cada ciclo con caché de 10s. La selección automática reutiliza esa misma caché en vez de abrir una segunda vía de datos: cumple el espíritu del requisito ("no golpear la API pesada por ciclo", aquí ni siquiera por refresh) sin reintroducir un cuelgue ya conocido y evitado a propósito.

**Limitaciones conocidas:**
- El snapshot turbo/binary (`get_api_option_init_all_v2`) no trae metadatos de clase de activo (no distingue forex/metal/índice/cripto). Por eso `select_real_assets(..., universe=...)` no se usa con restricción todavía: si en el futuro aparece una fuente con esa metadata, se puede pasar `universe` para acotar el descubrimiento a forex+metales+índices líquidos sin tocar la lógica de ranking.
- El payout considerado es solo turbo/binary (la vía principal de la estrategia). No se cruza con el payout digital (`get_digital_payout`): esa llamada es por-activo y lenta (~8-11s cada una), inviable para evaluar todo el universo en cada rotación en la Pi. `resolve_instrument()` sigue intentando digital como fallback por activo ya seleccionado, igual que antes.
- Si `get_market_snapshot()` nunca logra poblar `_open_cache` (fallo persistente de conexión), la selección automática queda congelada en la última lista conocida indefinidamente — mismo comportamiento de "degradado" que ya tenía el resto del bot ante ese escenario.

---

## Cambios 2026-07-10 — Multi-timeframe y fix exit_price

### A. Recolección M5/M15 (solo en señal accionable)

Hasta ahora `trade_market_snapshots`/`trade_candle_snapshots` solo se llenaban a 60s (1M). Se añade M5 (300s) y M15 (900s) como fuente de verdad del trend multi-timeframe (para ML futuro), **sin** aumentar las llamadas a la API por ciclo de escaneo: el fetch extra solo ocurre cuando la señal evaluada es accionable (CALL/PUT), justo antes de ejecutar/registrar el trade.

- **`application/signal_service.py`:** `SignalService` ahora arma internamente dos `CandleRepository` auxiliares (tf=300 y tf=900) sobre el MISMO `client` que ya usaba para M1 (no abre conexiones nuevas). Nuevo parámetro de constructor `collect_multi_tf: bool = True` (compatible hacia atrás: la llamada existente `SignalService(repo, strategy, settings.candle_count)` sigue funcionando con el default). `get_signal()`, tras calcular la señal M1 de siempre, si `collect_multi_tf` está activo y `signal.is_actionable`, llama a `_fetch_extra_timeframe()` por cada timeframe (60 velas M5, 40 velas M15): fetch + `strategy.add_indicators()` **sin** `.dropna()` (con solo 60/40 velas no alcanza para calentar ema200 de ~200 periodos; forzar dropna dejaría el frame vacío y se perderían hasta las velas OHLCV crudas) + `build_live_snapshot()` (ya es NaN-safe por campo vía `_f()`, así que los indicadores sin historia suficiente quedan `None`/NULL limpiamente) + `_candle_rows()` con ventana = nº de velas pedidas. Cualquier fallo en un timeframe extra (excepción de red, sin velas) se loguea como warning y devuelve `(None, [], latencia)`: **nunca** bloquea el trade principal. `SignalResult` gana `multi_tf_snapshots` (list), `multi_tf_candle_rows` (dict `{tf: rows}`) y `multi_tf_latency_ms` (dict `{tf: ms}`).
- **`domain/models.py`:** `TradeLogContext` gana `extra_market_snapshots`, `extra_candle_rows`, `extra_api_latency_ms` (mismo shape que los campos de `SignalResult`). `SignalResult` gana los tres campos descritos arriba.
- **`infrastructure/trade_executor.py`:** `_log_entry_context()` persiste `extra_market_snapshots` (uno por timeframe, vía `db.write_market_snapshot`), `extra_candle_rows` (vía `db.write_candle_snapshots(trade_id, tf, rows)`) y mezcla `extra_api_latency_ms` dentro del `api_context` antes de escribirlo (`{300: "api_latency_candles_m5_ms", 900: "api_latency_candles_m15_ms"}`). Todo best-effort (`_safe`), igual que el resto del contexto satélite. El `entry_delay_seconds` de `trades` **no se toca**: sigue midiendo solo el retraso de entrada M1, sin mezclarse con las latencias M5/M15.
- **`infrastructure/schema.py` / `infrastructure/database.py`:** `trade_api_context` gana columnas `api_latency_candles_m5_ms`, `api_latency_candles_m15_ms` (en el `CREATE TABLE` para instalaciones nuevas + `_add_columns_if_missing` idempotente para BDs viejas). `trade_market_snapshots`/`trade_candle_snapshots` no cambian de esquema: ya tenían `timeframe_seconds` genérico: ahora reciben filas con 300/900 además de 60.
- **Config nueva (`config/settings.py` + `config/runtime_config.py`, grupo "Operación"):** `collect_multi_tf` (bool, def `True`). Apagarlo vuelve al comportamiento anterior (solo M1).
- **Pendiente de wiring (fuera del alcance de este cambio — NO se tocó `application/controller.py` ni `application/bot_runner.py`, en revisión paralela por otro cambio):**
  1. `application/bot_runner.py::_build_log_context()` debe pasar los campos nuevos de `result` (el `SignalResult`) al construir el `TradeLogContext`:
     ```python
     return TradeLogContext(
         timeframe_seconds=self.signal_service.repo.timeframe_seconds,
         market_snapshot=result.market_snapshot,
         candle_rows=result.candle_rows,
         api_context=api_context,
         extra_market_snapshots=result.multi_tf_snapshots,
         extra_candle_rows=result.multi_tf_candle_rows,
         extra_api_latency_ms=result.multi_tf_latency_ms,
     )
     ```
     Sin este cambio, el fetch M5/M15 ocurre igual (gateado dentro de `SignalService`, no depende de `bot_runner.py`) pero sus datos nunca llegan a `TradeLogContext` y por lo tanto no se persisten.
  2. `application/controller.py` (línea donde se instancia `SignalService(repo, strategy, settings.candle_count)`) puede opcionalmente pasar `collect_multi_tf=settings.collect_multi_tf` para que el toggle del panel/`.env` tenga efecto; sin ese cambio, el flag existe pero `SignalService` siempre usa su default (`True`), que coincide con el valor por defecto pedido.

### B. Fix exit_price corrupto (condición de carrera en get_candles)

**Síntoma:** en `trade_outcomes` de trades `kind='real'`, 252/1519 (~16.6%) tenían `exit_price` con la escala de precio de OTRO activo (ej. `USDCHF` con `entry_price=0.82` y `exit_price=4327`, escala de `XAUUSD`). `result`/`profit` (vienen de `get_optioninfo`, otra vía de la API) estaban intactos.

**Causa raíz confirmada:** `infrastructure/iqoption_client.py::_request_candles_raw()` usa `raw.candles.candles_data` (atributo de la librería `iqoptionapi`) como buzón de respuesta de `get_candles()`. Ese atributo es **único y compartido por todos los activos e hilos** de una misma conexión: no hay id de correlación por request. El bot llama a `get_candles()` desde el hilo principal (escaneo de activos) Y desde un hilo en background por cada trade real abierto (`TradeExecutor._await_real_result` → `_log_outcome` → `client.get_candles(...)` para reconstruir `exit_price`/MFE/MAE/`trade_price_path`). Sin lock, dos fetches concurrentes de activos distintos se pisaban: `raw.candles.candles_data = None` de un hilo podía ser sobreescrito por la respuesta que llegó para el fetch del OTRO hilo, y ambos lo leían indistintamente → un trade terminaba con las velas (y por tanto el precio) de otro activo.

Evidencia en la BD copia (`/sessions/.../mnt/outputs/bot.sqlite`, análisis ad-hoc, no persistido en el repo): de los 252 outcomes corruptos, 168 tenían al menos otro trade real de OTRO activo abierto en la misma ventana [entrada, vencimiento]; de esos, 79 tenían un `exit_price` que coincidía (<1%) con el `entry_price`/`exit_price` de ese otro trade concurrente — confirmando el mecanismo de "vela del activo equivocado". El resto de casos corruptos (sin match 1:1 a otro TRADE) es consistente con la misma causa pero contra el escaneo normal de otros activos en el watchlist (no registrado con timestamp exacto en la BD, así que no se puede cruzar 1:1, pero la escala de precio ajena y el patrón idéntico —solo exit_price corrupto, entry_price siempre sano— apunta al mismo origen).

**Fix de raíz (`infrastructure/iqoption_client.py`):** nuevo `self._candles_lock = threading.Lock()` en `__init__`; todo el ciclo pedir→enviar→esperar→leer de `_request_candles_raw()` (antes sin ningún lock) queda serializado dentro de ese lock. Así se garantiza que un hilo siempre lee la respuesta de SU propia solicitud, nunca la de un fetch concurrente de otro activo. Esto ralentiza levemente fetches que coincidan en el tiempo (se encolan en vez de correr en paralelo), pero es la única forma segura dado que la librería vendored no expone ningún id de correlación por request.

**Reparación de datos ya escritos — `tools/repair_exit_prices.py`:** script one-off idempotente (`python3 tools/repair_exit_prices.py <ruta_bd> [--dry-run]`). Detecta `exit_price/entry_price` fuera de `[0.5, 2.0]` (con `entry_price>0`), e intenta recuperar el valor real desde:
  1. `trade_price_path` del propio trade (último punto) — en la práctica **nunca** sirve, porque se construye en el mismo `_log_outcome()` con las MISMAS velas que `exit_price`: si el fetch estaba contaminado, ambos lo están por igual.
  2. `trade_candle_snapshots` (tf=60) del propio trade, **solo** si hay una vela con `candle_timestamp` a ≤90s del `close_time` real — deliberadamente NO se usa "la última vela disponible sin más", porque esa tabla solo guarda velas HASTA la señal de ENTRADA (nunca llega al vencimiento): tomar su última fila daría un `exit_price` ficticio ≈ `entry_price`, no el cierre real.
  3. Si ninguna fuente sirve (caso típico en esta BD): `exit_price = NULL` y `outcome_price_suspect = 1` (columna nueva en `trade_outcomes`, migración idempotente en `schema.py`/`database.py`). `result`/`profit` y el resto de columnas derivadas (`raw_price_delta`, `win_margin`, MFE/MAE, etc.) **no se tocan** en este script.

**Resultado contra la copia de la BD real** (`bot.sqlite`, copia de trabajo en `/sessions/.../mnt/outputs/`): de 4716 outcomes totales, 277 corruptos detectados (252 `kind='real'` — coincide exacto con el conteo reportado — + 25 `kind='paper'`, afectados por el mismo bug ya que `_log_outcome()` se ejecuta para ambos tipos). De esos 277: **0 reparados** (ninguna fuente propia del trade tenía un dato utilizable, según el punto anterior), **277 anulados** (`exit_price=NULL`, `outcome_price_suspect=1`), **4439 intactos**. Segunda corrida sobre la misma BD ya reparada: 0/0/4716 (idempotente, confirmado).

---

## Cambios 2026-07-24 — pivote a Confirmed Band Reversion

La base `bot_20260724_104711.sqlite` contiene 3,506 operaciones no-OTC
resueltas: 47.67% WR, payout promedio aproximado 0.833, breakeven 54.57% y
EV de −12.54% del stake por operación. El resultado es negativo en 11 de 12
días y no cambia de signo al separar real/paper, dirección, activos, alineación
multi-timeframe ni horizontes cercanos.

La búsqueda de sustitutas dejó dos controles obligatorios:

1. Las 64,790 velas M1 únicas reconstruidas desde
   `trade_candle_snapshots` **no son una muestra continua independiente**.
   Cada ventana se guardó porque después apareció una señal FibPullback. Una
   reversión Bollinger aparentemente positiva mostró +74% EV a 1–5 minutos de
   la próxima señal histórica, pero −23% a −27% al alejarse 16–60 minutos:
   fuga de futuro confirmada.
2. La variante `lateral_market < 0.25`, evaluable sobre señales realmente
   observadas, tampoco es estable: +40.96% EV en desarrollo, −20.53% en
   validación (20–22 jul) y +36.79% en holdout. Es una racha de régimen, no
   una regla repetible.

Decisión de código:

- `BotController` construye `ConfirmedBandReversionStrategy`; la clase
  `FibPullbackStrategy` queda solo para reproducibilidad.
- La estrategia nueva no usa Fibonacci. Exige reingreso Bollinger, vela y
  mecha de rechazo, giro desde RSI extremo, ADX moderado y rango acotado por
  ATR. Sus umbrales se aplican en caliente desde el panel.
- El bot vuelve a poder encenderse y restaurar su estado anterior, siempre con
  los seguros de cuenta PRACTICE.
- `strategy_evaluations` registra también las velas `NONE`, condición por
  condición. Esto permite medir si un campo está demasiado estricto antes de
  cambiarlo.
- Los snapshots de trades incorporan `atr_14`, `adx_14` y
  `range_atr_ratio`.
- `config_epoch` incorpora
  `STRATEGY_REVISION=confirmed_band_reversion_v1` y todos sus parámetros.
- Al migrar una configuración FibPullback guardada se preservan opciones del
  usuario, pero se fija expiración 5M y payout mínimo no inferior a 0.82.

## Cambios 2026-07-10 — Filtros log-only y config_epoch

Cierre del análisis estadístico de ~4,700 trades: **ningún filtro de estrategia** (`bb_squeeze`, `giant_candle`, `lateral_market`, `late_entry`, `low_payout`) sobrevive corrección de Bonferroni — los trades bloqueados ganan igual que los permitidos. La gestión de riesgo (cooldown por pérdidas consecutivas) mostró señal débil pero en dirección útil (bloqueados 47.3% WR vs permitidos 52.1% WR, p=0.032 sin corregir) → se conserva sin cambios. Dos consecuencias de diseño:

### A. Filtros de estrategia a LOG-ONLY por defecto

`reject_bb_squeeze`, `reject_giant_candle`, `reject_lateral_market` cambian su **default** de `True` a **`False`** en `config/runtime_config.py::defaults_from_settings()` (no hay defaults espejo en `config/settings.py`: esas claves solo viven en `runtime_config`). `reject_low_payout` y `reject_late_entry` **se mantienen en `True`** (bloqueantes): el primero es una decisión económica (rentabilidad), el segundo un problema de ejecución (el precio ya se movió) — ninguno de los dos es lo que midió el análisis de valor predictivo.

"Log-only" significa: `domain/risk.py::RiskEngine.evaluate()` sigue evaluando la condición del filtro con el mismo umbral de siempre, pero si `reject_<filtro>=False` la señal queda **aprobada** en vez de rechazada (rama `self._permit(...)`, ya existente — no fue necesario tocar la lógica de decisión). En paralelo, `RiskEngine.evaluate_all()` (invocado desde `application/trade_service.py::process_signal()` vía `log_ctx.filter_rows`) sigue escribiendo **una fila por filtro** en `trade_filter_evaluations` con `triggered=1` cuando la condición se cumple (aunque no bloquee) y `blocked=1` solo si ese filtro fue efectivamente el que rechazó la señal — con el filtro apagado, `triggered` puede ser 1 con `blocked=0`, que es exactamente la fila que se necesita para seguir midiendo el filtro sin que bloquee nada. El diseño contrafactual (paper-trade con seguimiento virtual para toda señal rechazada) **sigue intacto** para lo que sí bloquea: `low_payout`, `late_entry`, `already_open`, `payout_unavailable` y la capa de gestión de riesgo (`risk_cooldown_*`, `risk_window_*`). No se tocó `domain/risk.py` ni `application/trade_service.py`: el mecanismo log-only ya existía (se usaba para permisos manuales por trade), solo cambió el **default** de fábrica.

### B. `config_epoch` — huella de configuración por trade

Cada cambio de configuración (indicadores, umbrales, rechazos activos/log-only, gestión de riesgo) fragmenta el dataset: mezclar trades de configuraciones distintas en un mismo análisis de filtro/estrategia contamina el resultado. Se añade `config_epoch` (TEXT) a `trades`, una huella corta que identifica la configuración vigente cuando se generó cada trade (real o paper).

- **`config/epoch.py`** (módulo nuevo): `compute_config_epoch(config: dict) -> str`, función pura. Hash `sha256` truncado a 12 hex de un subconjunto **ordenado y fijo** de claves (`EPOCH_KEYS`, ver docstring del módulo), serializado con `json.dumps(..., sort_keys=True)` — el orden de las claves en el dict de entrada es irrelevante, una clave ausente se trata como `None` (no lanza `KeyError`), y cualquier cambio de valor en una clave incluida cambia el hash.
  - Claves incluidas: `rsi_period`, `bb_period`, `bb_mult`, `atr_period`, `adx_period`, `reversion_rsi_threshold`, `reversion_min_rsi_turn`, `reversion_min_wick_ratio`, `reversion_max_adx`, `reversion_max_range_atr` (estrategia activa); `ema_fast`, `ema_slow`, `fib_lookback` (contexto); `squeeze_factor`, `giant_candle_factor`, `lateral_factor`, `expiration_minutes`, `min_payout`, `allow_otc`, `otc_fallback`, `auto_asset_selection` (umbrales/operación); `reject_low_payout`, `reject_bb_squeeze`, `reject_giant_candle`, `reject_lateral_market`, `reject_late_entry` (rechazos configurables); `risk_mgmt_enabled`, `risk_cooldown_losses`, `risk_cooldown_minutes`, `risk_window_start_hour`, `risk_window_end_hour`, `risk_window_max_losses` (gestión de riesgo); `max_entry_delay_seconds`, `collect_multi_tf` (ejecución/recolección).
  - Claves deliberadamente EXCLUIDAS: `base_amount`, `max_assets`, `asset_refresh_minutes`, `allow_digital`, `operate_without_payout`, `assumed_payout`, `assets` — cambian cómo/cuánto se opera, no qué señal se genera ni si se acepta/rechaza.
- **`infrastructure/schema.py` / `infrastructure/database.py`:** columna `trades.config_epoch TEXT`, migración idempotente vía `_add_columns_if_missing` (igual patrón que `entry_price`/`market_type`) + índice `idx_trades_config_epoch`. Filas anteriores a esta migración quedan con `config_epoch=NULL` (no se puede reconstruir retroactivamente qué config tenían). `Database.insert_trade()` gana el parámetro `config_epoch: str | None = None` (mismo tratamiento que `entry_price`: no vive en `TradeRecord`/`TradeRecord.COLUMNS` porque es metadato de auditoría, no un campo de dominio).
- **`infrastructure/trade_executor.py`:** `TradeExecutor` gana el atributo `self.config_epoch` (constructor, default `""`), estampado en los 5 sitios donde se llama `db.insert_trade(...)` (real, real "compra primero", reinserciones de recuperación de real y de paper, y el registro normal de paper-trades).
- **Dónde se recalcula en caliente (decisión de diseño):** en `application/controller.py`. `build_runner()` calcula `config_epoch = compute_config_epoch(cfg)` y lo pasa al construir/reconstruir el `TradeExecutor` (arranque y recuperación de servicio). `BotController` guarda además su propia copia en `self.config_epoch` (calculada en `__init__` y recalculada en `update_config()`, ANTES de llamar a `_apply_config()`), que expone en `status()`. `_apply_config()` — el método que ya aplicaba en caliente cada cambio de config a los objetos vivos (`risk.*`, `rm.*`, la estrategia) — ahora también hace `self.executor.config_epoch = self.config_epoch`. Se eligió enganchar aquí (en vez de, por ejemplo, recalcular el hash en cada `insert_trade`) porque es el ÚNICO punto por el que pasa cualquier cambio de configuración en caliente (el endpoint `/api/config` del panel llama a `BotController.update_config()`), y porque el hash es barato de calcular (una función pura sobre ~26 claves) frente a llamarlo por cada trade. El costo es aceptar que si algún día se muta `self.config` sin pasar por `update_config()`, el epoch quedaría desincronizado — no ocurre hoy: el único mutador de `self.config` fuera de `__init__` es `update_config()`.
- **Panel (`application/controller.py::status()`):** nueva clave `config_epoch` con el valor vigente.
- **Estrategia (`domain/strategy.py`):** `FibPullbackStrategy` queda como
  referencia histórica. El runtime actual usa
  `ConfirmedBandReversionStrategy`.
