"""Genera datos de demostración (2 años de ticks sintéticos) para probar la API sin esperar al recolector.

    python -m scripts.seed_demo            # escribe en RATES_DB_PATH
    python -m scripts.seed_demo --days 30
"""
from __future__ import annotations

import argparse
import math
import random
import time

from app.db import Tick, get_db


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rnd = random.Random(args.seed)

    now = int(time.time())
    start = now - args.days * 86400
    ticks: list[Tick] = []
    p2p = 36.0  # nivel inicial arbitrario
    bcv = 35.0
    day = 0
    for ts in range(start, now, 1800):  # un tick cada 30 min para binance/yadio
        t_days = (ts - start) / 86400
        drift = math.exp(0.0045 * t_days)  # depreciación exponencial suave
        noise = 1 + rnd.gauss(0, 0.004)
        price = p2p * drift * noise
        ticks.append(Tick("binance_p2p", ts, price, {"buy": price * 1.004, "sell": price * 0.996}))
        ticks.append(Tick("yadio", ts + 60, price * (1 + rnd.gauss(0, 0.001))))
        if int(t_days) != day:
            day = int(t_days)
            # BCV: un valor por día hábil
            from datetime import datetime, timezone, timedelta
            d = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=-4)))
            if d.weekday() < 5:
                ticks.append(Tick("bcv", int(d.replace(hour=0, minute=0, second=0).timestamp()), bcv * math.exp(0.0043 * t_days)))
                ticks.append(Tick("paralelo", int(d.replace(hour=18, minute=0, second=0).timestamp()), price * 1.01))
    n = get_db().insert_ticks(ticks)
    print(f"Insertados {n} ticks de demostración ({args.days} días)")


if __name__ == "__main__":
    main()
