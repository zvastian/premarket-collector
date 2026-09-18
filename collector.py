"""
COLECTOR premarket (versión GitHub Actions) — captura cruda de premarket gainers
(stockanalysis.com), sin decidir umbrales. Guarda el % change de cada ticker visible en el
Top 10, en cada muestreo, entre 06:40 y 09:30 hora de NY.

Port de `collector.py` del repo local "Equity Research" (que corría como tarea de Windows
`PremarketCollector` y dependía de tener la PC prendida). Misma fuente, misma frecuencia, mismas
columnas y misma regla de baseline - lo único que cambia es el destino: en vez de una sqlite
local, un CSV por día en `data/YYYY-MM-DD.csv` que el workflow commitea al repo. El repo local
lo trae de vuelta a `premarket_data.db` con `scripts/sync_premarket.py`.

LIMITACIÓN CONOCIDA (heredada): la fuente es el widget Top-10 de
stockanalysis.com/markets/premarket/ - en mañanas muy activas los tickers que no entran al Top 10
no aparecen. Los conteos que salen de estos datos son un piso, no el total.

Horario: el workflow dispara dos crons en UTC (uno cae antes de las 06:40 ET en horario de
verano, el otro en horario de invierno). Este script es el que decide según la hora de NY:
  - fin de semana o feriado NYSE -> sale sin hacer nada
  - ya existe el CSV de hoy y no estamos en una corrida que lo esté escribiendo -> sale (la
    segunda corrida del día es solo de respaldo por si la primera se cayó o GitHub no la lanzó)
  - antes de START_AT -> espera hasta START_AT
  - después de WINDOW_END -> sale

Uso:
    python collector.py              # corrida normal
    python collector.py --once       # un solo fetch, imprime y no escribe nada (prueba de IP)
"""

import argparse
import csv
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = "https://stockanalysis.com/markets/premarket/"
POLL_INTERVAL_SECONDS = 120  # 2 minutos, igual que el colector original
NY_TZ = ZoneInfo("America/New_York")

START_AT = "06:40"          # misma hora de arranque que la tarea de Windows (04:40 CDMX)
BASELINE_CUTOFF = "07:00"   # toda foto anterior a esta hora se marca is_baseline=1
WINDOW_END = "09:30"        # el colector termina aquí
PUSH_EVERY_SECONDS = 30 * 60  # commit+push intermedio, para no perder la mañana si el job muere

# Días completos sin mercado (NYSE). Medios días (cierre 13:00) no importan: el premarket es normal.
NYSE_HOLIDAYS = {
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}

REPO_DIR = Path(__file__).resolve().parent
DATA_DIR = REPO_DIR / "data"
FIELDS = ["trade_date", "fetched_at", "ticker", "pct_change", "premkt_price",
          "pre_volume", "market_cap", "is_baseline"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; premarket-research-script/1.0)"
}


def log(msg):
    print(f"[{datetime.now(NY_TZ).isoformat(timespec='seconds')}] {msg}", flush=True)


def parse_number(text):
    if text is None:
        return None
    text = text.strip().replace(",", "").replace("%", "").replace("+", "")
    if text in ("", "-", "N/A"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_premarket_gainers():
    resp = requests.get(URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    tables = soup.find_all("table")
    if not tables:
        return []

    results = []
    for row in tables[0].find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        ticker = cells[1].get_text(strip=True)
        pct_change = parse_number(cells[3].get_text(strip=True))
        if ticker and pct_change is not None:
            results.append({
                "ticker": ticker,
                "pct_change": pct_change,
                "premkt_price": parse_number(cells[4].get_text(strip=True)),
                "pre_volume": parse_number(cells[5].get_text(strip=True)),
                "market_cap": cells[6].get_text(strip=True) if len(cells) > 6 else None,
            })
    return results


def append_rows(csv_path, trade_date, fetched_at, gainers, is_baseline):
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for g in gainers:
            w.writerow({"trade_date": trade_date, "fetched_at": fetched_at,
                        "is_baseline": 1 if is_baseline else 0, **g})


def git_push(csv_path, message):
    """Commit + push del CSV del día. Si falla, se loguea y se sigue - el próximo push lo reintenta."""
    cmds = [
        ["git", "add", str(csv_path)],
        ["git", "commit", "-m", message],
        ["git", "pull", "--rebase", "--quiet"],
        ["git", "push", "--quiet"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=REPO_DIR, capture_output=True, text=True)
        if r.returncode != 0 and cmd[1] != "commit":  # commit sin cambios devuelve 1, no es error
            log(f"AVISO git: {' '.join(cmd)} -> {r.stderr.strip() or r.stdout.strip()}")
            return False
    return True


def count_snapshots(csv_path):
    if not csv_path.exists():
        return 0, False
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return len({r["fetched_at"] for r in rows}), any(r["is_baseline"] == "1" for r in rows)


def run_once():
    gainers = fetch_premarket_gainers()
    log(f"Fetch de prueba: {len(gainers)} filas")
    for g in gainers:
        print(g)
    return 0 if gainers else 1


def run(min_snapshots):
    now = datetime.now(NY_TZ)
    trade_date = now.strftime("%Y-%m-%d")

    if now.weekday() >= 5 or trade_date in NYSE_HOLIDAYS:
        log(f"{trade_date} no es día hábil NYSE - nada que hacer.")
        return 0
    if now.strftime("%H:%M") > WINDOW_END:
        log(f"Ya pasó {WINDOW_END} ET - nada que hacer.")
        return 0

    DATA_DIR.mkdir(exist_ok=True)
    csv_path = DATA_DIR / f"{trade_date}.csv"

    # La segunda corrida programada del día (respaldo) sale si la primera ya capturó la mañana.
    # Si la primera se cayó a mitad, el CSV existe pero está incompleto: se retoma igual que el
    # colector original retomaba tras un corte (y sin repetir la baseline si ya quedó grabada).
    existing, baseline_captured = count_snapshots(csv_path)
    if existing and now.strftime("%H:%M") < WINDOW_END and existing >= min_snapshots:
        log(f"{csv_path.name} ya tiene {existing} snapshots - corrida de respaldo innecesaria.")
        return 0
    if existing:
        log(f"{csv_path.name} ya tiene {existing} snapshots (incompleto) - se retoma la captura.")

    while datetime.now(NY_TZ).strftime("%H:%M") < START_AT:
        time.sleep(30)

    log(f"Colector iniciado. Baseline antes de {BASELINE_CUTOFF} ET, captura hasta {WINDOW_END} ET.")
    last_push = time.monotonic()

    while True:
        now = datetime.now(NY_TZ)
        now_str = now.strftime("%H:%M")
        if now_str > WINDOW_END:
            log(f"Fin de ventana ({WINDOW_END} ET).")
            break

        try:
            gainers = fetch_premarket_gainers()
        except Exception as e:
            log(f"ERROR al hacer fetch: {e}")
            gainers = None

        if gainers:
            is_baseline = now_str < BASELINE_CUTOFF
            append_rows(csv_path, trade_date, now.isoformat(), gainers, is_baseline)
            baseline_captured = baseline_captured or is_baseline
            top = gainers[0]
            log(f"Snapshot{' BASELINE' if is_baseline else ''} guardado. "
                f"Top: {top['ticker']} ({top['pct_change']}%). Tickers visibles: {len(gainers)}")
            if not is_baseline and not baseline_captured:
                log(f"AVISO: no hay baseline antes de las {BASELINE_CUTOFF} hoy.")
        elif gainers is not None:
            log("Tabla vacía en esta ronda (posible bloqueo temporal) - se reintenta.")

        if time.monotonic() - last_push >= PUSH_EVERY_SECONDS and csv_path.exists():
            git_push(csv_path, f"data: {trade_date} parcial {now_str} ET")
            last_push = time.monotonic()

        time.sleep(POLL_INTERVAL_SECONDS)

    snapshots, _ = count_snapshots(csv_path)
    if csv_path.exists() and not git_push(csv_path, f"data: {trade_date} completo ({snapshots} snapshots)"):
        log("FALLA: no se pudo subir el CSV del día al repo - los datos se pierden con el runner.")
        return 1

    # Chequeo de salud: la ventana 06:40-09:30 cada 2 min da ~85 snapshots. Si quedaron muy
    # pocos, el job termina con error para que GitHub mande el email de "workflow failed" -
    # un día ciego no puede pasar en silencio como pasó el 09-14 y 09-16 con la tarea de Windows.
    if snapshots < min_snapshots:
        log(f"FALLA: solo {snapshots} snapshots hoy (mínimo esperado {min_snapshots}).")
        return 1
    log(f"Captura del día {trade_date} completa: {snapshots} snapshots.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="un fetch de prueba, sin escribir")
    ap.add_argument("--min-snapshots", type=int, default=60)
    args = ap.parse_args()
    sys.exit(run_once() if args.once else run(args.min_snapshots))
