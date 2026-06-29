# Diccionario de datos — Bot IqOption

> Última actualización: 2026-06-29 · BD: `~/.local/share/iqbot/bot.db` (SQLite, WAL) · 9 tablas

---

## 1. Origen y uso de los datos (panorama)

**Qué es esto.** El bot opera la estrategia **FibPullback** sobre **velas de 1 minuto** (`TIMEFRAME_SECONDS=60`) con **expiración de 5 minutos**, en **cuenta DEMO/PRACTICE** de IQ Option. Estamos en **fase de recolección**: el objetivo es acumular contexto de mercado y resultados para analizar, no optimizar la estrategia todavía.

**Ciclo de un dato (de dónde sale cada fila):**
1. El controlador refresca caché de mercado (apertura de activos, payouts, balance) desde la API de IQ.
2. Por cada activo activo: se piden velas → la estrategia (`FibPullbackStrategy`) calcula indicadores (EMA 50/200, RSI 14, Bollinger 20×2, Fibonacci sobre swing de 25 velas) y emite **CALL / PUT / NONE**.
3. Si la señal es accionable, el **motor de riesgo** (`RiskEngine`) aplica los filtros de "no operar".
   - **Rechazada** → **paper-trade** (`kind='paper'`, `valid_trade=0`): NO se envía a IQ, pero se **sigue virtualmente** con el precio para registrar qué habría pasado. → esto es lo que permite el **análisis contrafactual** de los filtros.
   - **Aprobada** → pasa a la **capa de gestión de riesgo** (`RiskManager`, paso 3b).
3b. **Gestión de riesgo** (solo si pasó los filtros normales y habría sido real):
   - **Sin pausa** → operación **real en demo** (`kind='real'`, `valid_trade=1`).
   - **En pausa** → **paper-trade** (`kind='paper'`, `valid_trade=0`) con `reject_reason` de riesgo (`risk_cooldown_2_consecutive_losses` / `risk_window_6_13_max_3_losses`) y `would_be_real_without_risk=1`. Permite analizar si la pausa evitó pérdidas o también bloqueó ganadores. → detalle en `trade_risk_management` (§9).
4. Cada señal acción genera **1 fila en `trades`** + filas en las **tablas satélite** (cuelgan por `trade_id`).
5. Al vencer, se reconstruye el resultado (win/loss/draw) y las métricas de recorrido del precio → `trade_outcomes`.

**Clave de relación:** `trades.trade_id` (TEXT, hash corto). Todas las satélite referencian `trade_id`. *(Las FOREIGN KEY se declaran como documentación; SQLite no las fuerza aquí — la integridad la mantiene el código.)*

**Las 9 tablas:**
| Tabla | Filas/escala | Para qué sirve |
|---|---|---|
| `trades` | 1 por señal accionada | Operación base (decisión + indicadores resumidos + resultado) |
| `trade_market_snapshots` | 1 por trade·timeframe | Foto técnica completa en el momento de la señal |
| `trade_filter_evaluations` | ~5-8 por trade | 1 fila por filtro evaluado (normales + reglas de riesgo) |
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
| `body_size`, `body_ratio` | Tamaño del cuerpo y cuerpo/rango |
| `upper_wick_ratio`, `lower_wick_ratio` | Proporción de las mechas superior/inferior |
| `trend_label` | `bullish` / `bearish` / `lateral` (etiqueta derivada) |
| `source` | `live` (en vivo) o `backfill` (migrado de `trades`) |

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
