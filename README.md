# IQ Binary Bot — Fib Pullback 5M (SOLO DEMO / ESTUDIO)

Bot para **probar** una estrategia de opciones binarias en IQ Option usando
**únicamente cuenta PRACTICE (demo)**. El objetivo de esta versión es **recolectar
estadísticas** para ajustar la estrategia: corre de forma continua, sin límites
diarios ni stop.

> ⚠️ La librería `iqoptionapi` es **comunitaria, no oficial** y su propio repo
> advierte *"ONLY FOR STUDY"*. Este bot está diseñado con seguros fuertes para
> **nunca** operar en cuenta real. No lo uses con dinero real.

---

## ¿Cómo está organizado? (y por qué)

La lógica de negocio **no depende** de IQ Option. Si la librería se rompe o cambia,
solo se toca `infrastructure/iqoption_client.py`.

```
config/         settings.py        -> carga .env (pydantic) + blindaje de cuenta
domain/         models.py          -> piezas del dominio (Signal, Trade, etc.)
                strategy.py        -> estrategia pura: velas -> CALL/PUT/NONE
                risk.py            -> filtros de "NO operar"
infrastructure/ iqoption_client.py -> ÚNICO punto que toca iqoptionapi
                candle_repository.py-> velas crudas -> DataFrame (min/max -> low/high)
                trade_executor.py  -> ejecuta demo (en hilos) y paper-trades
                database.py        -> persistencia SQLite (trades + settings)
application/    signal_service.py  -> velas + estrategia
                trade_service.py   -> riesgo + ejecución
                bot_runner.py      -> escaneo de un ciclo (señal->filtros->op)
                controller.py      -> hilo del bot: encender/apagar, conexión
                webapp.py          -> panel web (FastAPI) + API REST
web/            index.html         -> dashboard (estadísticas, activos, on/off)
main.py         -> arranca el panel en http://127.0.0.1:8000
```

- La **estrategia** no sabe nada de IQ Option: solo recibe velas y devuelve `CALL/PUT/NONE`.
- El **ejecutor** no decide nada: solo ejecuta lo que el **motor de riesgo** aprobó.

---

## Seguros de cuenta DEMO (blindajes)

1. `settings.py`: si `BALANCE_MODE=REAL` y `ALLOW_REAL=false` → **la app no arranca**.
2. `ensure_practice()`: fuerza `change_balance("PRACTICE")` y **verifica** que el modo
   activo sea PRACTICE antes de continuar.
3. `assert_practice()`: se ejecuta **justo antes de cada compra**. Si el modo no es
   PRACTICE, aborta. Nunca se llama `change_balance("REAL")`.

---

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # incluye iqoptionapi desde GitHub
```

Configura credenciales:

```bash
cp .env.example .env
# edita IQ_EMAIL e IQ_PASSWORD
```

## Ejecución

```bash
python main.py
```

Abre el panel en **http://127.0.0.1:8000**. Detén el servidor con `Ctrl+C`.

Desde el panel puedes:
- **Encender / apagar** el bot (botones ▶ / ■). El estado se recuerda entre reinicios.
- **Añadir / quitar activos** en caliente (máximo **4** a la vez).
- Ver **estadísticas** (ganado/perdido/win-rate/profit) filtrando por **Real (demo)**,
  **Paper (filtrado)** o **Todo**, con gráficos por activo y por día.
- Ver el **rendimiento de los filtros**: win-rate de las señales rechazadas
  (paper-trade) por motivo de rechazo, para saber si un filtro te está costando
  operaciones buenas.
- Ver la tabla de **operaciones recientes**.

---

## Estrategia (Fib Pullback 5M)

- Timeframe **1M**, expiración **5M**.
- EMA 50 / 200, RSI 14, Bollinger 20×2, Fibonacci sobre swing de 25 velas.

**CALL**: tendencia alcista (`close>EMA200`, `EMA50>EMA200`), precio en zona Fib de
retroceso, toque de EMA50/BB media, vela alcista dentro de banda, RSI≥51 y subiendo.

**PUT**: espejo del anterior.

### Filtros de "NO operar"
- Payout < `MIN_PAYOUT` (80%).
- Squeeze Bollinger: `bb_width < mediana(100)*SQUEEZE_FACTOR`.
- Vela gigante: `range > avg_range_20 * GIANT_CANDLE_FACTOR`.
- Mercado lateral: `|EMA50-EMA200| < avg_range_20 * LATERAL_FACTOR`.
- Entrada tarde: más de `MAX_ENTRY_DELAY_SECONDS` desde el cierre de vela.
- Operación simultánea en el mismo activo.

---

## Filtros + estadísticas (paper-trade)

Cuando un filtro **rechaza** una señal, **no se opera en demo**, pero se hace
**seguimiento virtual (paper-trade)**: se guarda el precio de entrada y, al
vencimiento (5M), se compara con el precio para determinar win/loss. Así puedes
medir si cada filtro realmente aporta, sin gastar operaciones.

Todo se guarda en **SQLite** (`data/bot.db`), en la tabla `trades` con una columna
`kind` que distingue:
- `real`  → operaciones reales en demo
- `paper` → señales rechazadas por un filtro (seguimiento virtual)

Cada fila tiene las 25 métricas pedidas:

```
trade_id, date, time, asset, is_otc, direction, amount, payout, expiration,
signal_time, entry_time, entry_delay_seconds, order_id, result, profit,
balance_after, valid_trade, reject_reason, bb_width, bb_width_median,
ema_distance, candle_range, avg_range_20, rsi, notes
```

`notes` describe el contexto de mercado (tendencia, RSI, ancho de bandas, etc.)
para entender el porqué de cada resultado.

> ¿Prefieres Postgres? La capa de datos está aislada en `infrastructure/database.py`;
> es el único archivo a cambiar. Pásame las credenciales y lo migro.

---

## Activos y OTC

- `ASSETS=EURUSD,GBPUSD` (lista separada por comas).
- Los activos OTC (terminan en `-OTC`) se operan en la **misma corrida** y se marcan
  con `is_otc=true` en el CSV, para que puedas filtrarlos después sin que hagan ruido.
- `ALLOW_OTC=false` ignora los activos OTC de la lista.

---

## Notas importantes

- **Límites diarios desactivados** en esta fase (`ENFORCE_LIMITS=false`). Las variables
  `MAX_TRADES_PER_DAY`, `DAILY_STOP_LOSS_R`, etc. se dejan para la versión definitiva
  con gestión de riesgo (racha mala, horario, stop).
- **Retraso de datos**: `iqoptionapi` puede tardar unos segundos en exponer la vela
  recién cerrada. Si ves que muchas señales caen en `late_entry` (paper) en vez de
  operarse, sube `MAX_ENTRY_DELAY_SECONDS` en el `.env`.
- Para opciones de 5M se usa el payout **turbo** (con fallback a binary).
