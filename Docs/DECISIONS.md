# Architecture decisions

> Nota de entrega: `docs/DECISIONS.md` y `docs/FORECASTING.md` no existían en el árbol de
> trabajo cuando se implementó el forecast oficial (solo estaban
> `Docs/Forecast_Staffing_Planner_Source_v19.txt` y `Docs/Forecast_Discrepancy_Assessment.docx`).
> Este archivo se creó en esa entrega con las decisiones vigentes y los ADR solicitados.
> En Windows `docs/` y `Docs/` son el mismo directorio.

## ADR-005 — Pesos del ranking (`SCORE_WEIGHTS`)

**Estado:** abierto (riesgo declarado).

`SCORE_WEIGHTS` en `forecast_engine.py` usa MAPE 30 %, WAPE 40 % y 7,5 % para cada uno de
variance, correlation, stdev e IQR. El documento fuente solo confirma MAPE 30 % y WAPE 40 %;
el reparto del 30 % restante **no está verificado contra el Excel**.

Consecuencia: el método elegido por "statistical best" —y por tanto el valor de los períodos
en `Auto`— puede no coincidir con el Excel si los pesos reales son distintos. La verificación
pendiente es contrastar los cuatro pesos contra `Docs/Forecast_Discrepancy_Assessment.docx` o
el Excel original. No se modificó el motor en esta entrega porque las fórmulas están validadas
y el archivo está fuera de alcance.

## ADR-012 — Rolling official forecast

**Estado:** implementado (antes fuera de alcance).

La vista `calculations` pasa de auditoría pasiva a constructor del forecast oficial. Para cada
período del horizonte el usuario elige **un** método (o `Auto` = statistical best) y,
opcionalmente, un volumen manual. El backend resuelve:

```
method(i) = periods[i].method  si existe y ∈ METHODS
            else default       si ∈ METHODS
            else best          (statistical best del back-test)
value(i)  = round(manual)                       si periods[i].manual es numérico
            else round(forecast(values, method(i))[i])
```

Los valores por canal se calculan con `channelComparison[ch][method(i)][i]`; cuando hay manual,
se escalan proporcionalmente para que su suma sea el total manual. `resolved[i]` guarda
`{method, manual, value, overridden}` y `officialSaved` indica si hay default no-auto o al menos
un override.

El oficial se propaga a `future`, `staff`, `ledger`, `channelNeeds` y `coverage`; overview y
staffing planner consumen esos campos. El chart de la vista y el de overview muestran **tres
series**: **Actual**, **Official** (backtest one-step de `officialMethod` en el histórico +
forecast oficial futuro) y **Best** (backtest one-step y forecast futuro del método
estadísticamente mejor, con línea punteada). No muestran los siete métodos; el gráfico
multi-método se conserva únicamente en `Forecast methods`.

Se descartó el diseño intermedio de tabla con una columna por método y pick por celda: no era
un selector de método por período y no tenía persistencia. No existe en el código actual.

## ADR-015 — Persistencia server-side del forecast oficial

**Estado:** aceptado (reemplaza el almacenamiento en `localStorage` del borrador anterior).

La fuente de verdad es **`data/official.json`**, escrita por `save_official()` y leída por
`load_official()`. `GET /api/official`, `POST /api/official` y `POST /api/official/reset`
expuestos por `server.py`; `/api/state` incluye el objeto `official`. Si `/api/compute` no
recibe `official`, usa el persistido. Esto hace que el forecast sea idéntico en cualquier
navegador que apunte al server y sobreviva reinicios.

Consecuencia declarada: las claves de `periods` son el **índice** del período (0, 1, 2…), no una
fecha. Al cambiar de grano (monthly → weekly → daily) los overrides se aplican a los períodos
0, 1, 2 del nuevo grano (semanas 1–3, días 1–3). No es un bug: un período no tiene identidad
estable entre granos. Mitigación: `Reset` (que persiste inmediatamente el estado vacío) y el
aviso en el README. Un modelo con clave por fecha queda fuera de alcance.

`data/official.json` es estado de ejecución, no un dataset. La copia de trabajo refleja el
último Save/load; `Reset` deja el default vacío (`{"default": "auto", "periods": {}}`). Puede
agregarse a `.gitignore` sin afectar el arranque, porque el servidor lo recrea en el siguiente
guardado. No se usa `localStorage`.

## ADR-016 — Histórico pre-agregado en `_compute_core` y pasado a `plan_for`

**Estado:** implementado.

Antes, `plan_for` recorría la lista de filas filtradas (hasta ~15 000) por cada canal y por
cada período del horizonte. Ahora `_compute_core` agrega una sola vez los totales históricos
por canal (`Voice`, `Messaging`, `Email` y sus `*Off`) en un único dict y lo pasa a
`staffing.plan_for`. La firma de `plan_for` acepta ese dict, la lista de filas original o una
lista con un dict agregado, así que el cálculo es idéntico (el reparto regular/off usa las
mismas sumas). Verificado con datos de control: `future`, `staff` y los volúmenes del `ledger`
no cambian para el escenario default.

## ADR-019 — Backtest one-step incremental y caché de backtest

**Estado:** implementado.

El backtest oficial y el del método estadísticamente mejor se calculan **una sola vez por
request**, no llamando a `forecast()` una vez por período. La definición es la exigida:

```
backtest[t] = forecast(values[:t], method, 1, period, params)[0]   para t = 1..n-1
backtest[0] = null
```

`backtestOfficial` usa `officialMethod` (el default efectivo resuelto; ver ADR-021) y
`backtestBest` usa `bestMethod`. Ambos arrays tienen exactamente la longitud de `history` y
están alineados con él. Si ambos métodos coinciden, el servidor calcula un solo array y lo
reutiliza para las dos claves de la respuesta.

La implementación `backtest_series()` en `server.py` recorre la serie una vez y mantiene el
estado suficiente para cada método: acumuladores de media/exponencial/pesos, ventanas de
naive/media móvil/cma y buckets estacionales. El caso **trend** también es exacto: mantiene
acumuladores lineales (`n`, `Σx`, `Σy`, `Σx²`, `Σxy`) y los buckets de CMA/estacional, sin
aproximar la pendiente ni reajustar todo el histórico en cada paso. Por eso **no se usó una
aproximación de tendencia y no se creó ADR-022**.

`backtestMs` mide el tiempo de ese cálculo (incluida la lectura/escritura de su caché) y vale
`0.0` cuando el request completo es un hit de `_CACHE`. Medición entregada con 15.401 filas
parseadas y la serie diaria de 1.024 puntos: cold de `/api/compute` **7,30 s** de pared y
`backtestMs = 10,3`; tres llamadas warm **13–15 ms** y `backtestMs = 0.0`. El tiempo de pared
cold incluye el scoring preexistente de todos los métodos; el backtest incremental queda muy
por debajo del límite de 10 s. La medición aislada se hizo sin el auto-open del navegador para
evitar que una segunda request compitiera con la primera; el handler y los datos son los
mismos.

El resultado se guarda en memoria (`_BACKTEST_CACHE`) y en disco:

```
data/backtest/{method}_{grain}_{form_hash10}_{business}_{channel}.json
```

La entrada de disco contiene un SHA-256 de `values + params + period` y la serie. La clave de
memoria incluye `STATE["version"]`; `load_rows()` limpia ambas cachés. Una entrada de disco
obsoleta se detecta por el hash y se recalcula. El directorio es solo caché y se puede borrar.
El dataset real tiene 15.401 filas, pero la serie diaria solo 1.024 puntos; `check_backtest.py`
valida `t = 1, 2, 100, 1023` contra la serie real y `t = 5.000 / 15.000` contra una serie
sintética determinista de 15.001 puntos. La comparación contra `forecast()` es exacta para
`double` (diferencia 0) y la de `trend` queda en el orden de `1e-12`.

## ADR-020 — Snapshots, historial y CSV append-only

**Estado:** implementado.

Cada **Save** escribe, en este orden:

1. `data/official.json` — estado limpio, solo `{"default": ..., "periods": {...}}`, sin
   `context`, `savedAt` ni `snapshot_id`.
2. `data/official.json.history` — sidecar JSON con los últimos **20** snapshots (FIFO). El
   archivo conserva el orden de append; `GET /api/official/history` lo devuelve ordenado por
   `savedAt` descendente (más nuevo primero).
3. `data/forecast_history.csv` — una fila por período futuro del Save.

El snapshot tiene esta forma:

```json
{
  "savedAt": "2026-10-28T18:37:02.494827-06:00",
  "default": "double",
  "periods": {},
  "snapshot_id": "dcae914e"
}
```

`savedAt` es ISO-8601 con microsegundos y zona horaria. `snapshot_id` son los primeros 8
caracteres de un SHA-1 del snapshot (`savedAt + default + periods`), con comprobación de
colisión contra los IDs existentes. El sidecar mantiene `official.json` limpio y permite que el
estado actual y sus versiones se lean por separado.

El CSV es UTF-8, sin BOM, con este encabezado exacto y una fila por período futuro:

```
saved_at,snapshot_id,default,period_index,period_label,method,manual,official_value,best_method,best_value,actual_value
```

`actual_value` queda vacío porque son períodos futuros; `manual` queda vacío cuando no se fijó
un volumen manual. El `context` que envía el frontend (grano, canal, form, business, params y
assumptions) se usa para recalcular los valores del CSV y se elimina antes de persistir el
estado. Si el CSV falla, el Save no se rompe: la respuesta incluye `csvError` y el snapshot ya
quedó guardado.

Cuando el archivo superaría **5.000 filas de datos**, `_rotate_history_csv()` lo mueve a
`data/forecast_history_YYYY-MM.csv` (con sufijo numérico si ya existe) antes del siguiente
append; el archivo nuevo vuelve a escribir el encabezado. La descarga
`GET /api/official/history/csv` devuelve el archivo actual como `text/csv` con
`Content-Disposition: attachment; filename="forecast_history.csv"`.

`POST /api/official/load/{snapshot_id}` escribe el snapshot elegido en `data/official.json`
**solamente**: no agrega un snapshot al historial y no agrega filas al CSV. `Reset` guarda
`{"default":"auto","periods":{}}` y tampoco escribe historial ni CSV. La prueba controlada E4
produjo 2 snapshots (`07c17ec3` auto con un override y `dcae914e` double) y 24 filas de CSV
(dos Saves mensuales de 12 períodos). Después de la aceptación, el navegador abierto agregó dos
Saves más (`39e01437` y `4c51a985`); el workspace conserva esos 4 snapshots y 48 filas.

## ADR-021 — Edición solo futura; backtest histórico inmutable

**Estado:** implementado.

La tabla editable de `Official forecast` muestra únicamente los períodos futuros. Las filas
históricas del panel **Backtest vs. actual** son de solo lectura: muestran la predicción
one-step, el valor real y el error, y no aceptan método ni volumen manual.

El backtest histórico usa siempre el **default efectivo** (`officialMethod`). No aplica los
overrides por período ni los volúmenes manuales que el usuario elige para el futuro. Es
deliberado: un override es una decisión tomada mirando el futuro; aplicarla hacia atrás
convertiría el backtest en una evaluación contaminada y rompería la comparación con el método
estadístico. El futuro sí usa los overrides y manuales; la serie `Best` sigue siendo puramente
estadística.

El selector **12 / 24 / 52 / All** solo recorta las filas mostradas y recalcula los MAPE de la
tabla; el chart mantiene el histórico completo. El mapa de colores y la etiqueta `Histórico`
dejan claro que ese tramo no es editable.

## Nota de alcance — ADR-022 no aplica

No se creó ADR-022 porque el backtest de tendencia no usa una aproximación: usa acumuladores
lineales exactos y se verificó contra `forecast()` en las series sintéticas largas. Si en el
futuro se reemplazara por una aproximación, correspondería registrar ese cambio como ADR-022.
