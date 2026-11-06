"""Capturas del tablero con Playwright, contra una BD temporal con datos de demostración.

python scripts/screenshot.py                 # escribe docs/screenshots/{light,dark,mobile}.png
python scripts/screenshot.py --out /tmp/shots

Levanta la API en un puerto libre con el recolector apagado, siembra `seed_demo --days 400` en una
BD temporal y no toca `data/rates.db`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = {
    "light": {"viewport": {"width": 1280, "height": 900}, "color_scheme": "light"},
    "dark": {"viewport": {"width": 1280, "height": 900}, "color_scheme": "dark"},
    "mobile": {"viewport": {"width": 400, "height": 860}, "color_scheme": "light", "device_scale_factor": 2},
}
CHROME_FALLBACKS = ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as res:
                if res.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"La API no respondió en {timeout:.0f} s: {url}")


def launch_browser(pw):
    try:
        return pw.chromium.launch()
    except Exception:  # navegador de Playwright no instalado: probar uno del sistema
        for path in CHROME_FALLBACKS:
            if Path(path).exists():
                return pw.chromium.launch(executable_path=path)
        raise


def capture(base_url: str, out: Path) -> list[Path]:
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    written = []
    with sync_playwright() as pw:
        browser = launch_browser(pw)
        for name, opts in SHOTS.items():
            page = browser.new_page(**opts)
            errors: list[str] = []
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            page.on("console", lambda m, errors=errors: m.type == "error" and errors.append(m.text))
            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_selector(".tile")
            page.wait_for_selector("#status .live", state="attached")
            page.wait_for_timeout(400)
            path = out / f"{name}.png"
            page.screenshot(path=str(path), full_page=True)
            written.append(path)
            # el 404/405 del sondeo HEAD de XLSX/PDF es esperado hasta la fase D
            real = [e for e in errors if "404" not in e and "405" not in e]
            if real:
                print(f"[{name}] errores en la página: {real}", file=sys.stderr)
            page.close()
        browser.close()
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    ap.add_argument("--days", type=int, default=400)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="ves-shots-"))
    env = {
        **os.environ,
        "RATES_DB_PATH": str(tmp / "rates.db"),
        "RATES_COLLECTOR_ENABLED": "false",
    }
    server = None
    try:
        subprocess.run(
            [sys.executable, "-m", "scripts.seed_demo", "--days", str(args.days), "--force"],
            cwd=ROOT,
            env=env,
            check=True,
        )
        port = free_port()
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
            cwd=ROOT,
            env=env,
        )
        base = f"http://127.0.0.1:{port}"
        wait_ready(f"{base}/health")
        for path in capture(f"{base}/", args.out):
            print(path)
    finally:
        if server:
            server.terminate()
            server.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
