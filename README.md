# premarket-collector

Captura cada 2 minutos el Top-10 de premarket gainers de stockanalysis.com entre 06:40 y 09:30 ET
(días hábiles NYSE) y guarda un CSV por día en `data/YYYY-MM-DD.csv`. Corre en GitHub Actions
(`.github/workflows/collect.yml`), sin depender de ninguna PC prendida.

Columnas: `trade_date, fetched_at, ticker, pct_change, premkt_price, pre_volume, market_cap,
is_baseline` (`is_baseline=1` = foto anterior a las 07:00 ET).

- Prueba manual: Actions → premarket-collect → Run workflow (con "once" marcado hace un solo fetch).
- Si un día queda con menos de 60 snapshots el job falla y GitHub manda email.
- Limitación: solo ve el Top 10 del widget, los conteos son un piso.
