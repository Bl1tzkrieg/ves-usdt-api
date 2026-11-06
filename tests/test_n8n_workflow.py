"""Comprobaciones estructurales del flujo n8n y de la lógica de alertas de su nodo Code.

No hay un n8n disponible en las pruebas: se valida el JSON (nodos, conexiones, rutas, ausencia de
secretos) y, si hay `node` en el PATH, se ejecuta el JavaScript de "Calcular alertas" con un arnés
que simula `$input`, `$env` y los datos estáticos del flujo.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

N8N_DIR = Path(__file__).resolve().parent.parent / "n8n"
WORKFLOW_PATH = N8N_DIR / "ves-rates-workflow.json"
FIXTURE_PATH = N8N_DIR / "fixture-validated-events.json"
PREFIX = "={{ $env.RATES_BASE_URL }}/api/v1/rates"

EXPECTED_TYPE_VERSIONS = {
    "n8n-nodes-base.scheduleTrigger": 1.2,
    "n8n-nodes-base.httpRequest": 4.2,
    "n8n-nodes-base.if": 2.2,
    "n8n-nodes-base.code": 2,
    "n8n-nodes-base.telegram": 1.2,
    "n8n-nodes-base.emailSend": 2.1,
    "n8n-nodes-base.noOp": 1,
    "n8n-nodes-base.set": 3.4,
    "n8n-nodes-base.manualTrigger": 1,
}

SECRET_PATTERNS = [
    re.compile(r"x-api-key", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk-(?:proj-)?|gh[pousr]_)[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # token de bot de Telegram
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"bearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE),
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _nodes_by_name(workflow: dict) -> dict[str, dict]:
    return {n["name"]: n for n in workflow["nodes"]}


def _strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW_PATH)


@pytest.fixture(scope="module")
def fixture_workflow() -> dict:
    return _load(FIXTURE_PATH)


@pytest.mark.parametrize("path", [WORKFLOW_PATH, FIXTURE_PATH], ids=["workflow", "fixture"])
def test_workflow_is_inactive_and_connections_are_consistent(path):
    wf = _load(path)
    assert wf["active"] is False
    names = [n["name"] for n in wf["nodes"]]
    ids = [n["id"] for n in wf["nodes"]]
    assert len(names) == len(set(names))
    assert len(ids) == len(set(ids))
    for source, ports in wf["connections"].items():
        assert source in names, source
        for groups in ports.values():
            for group in groups:
                for edge in group:
                    assert edge["node"] in names, edge
                    assert edge["type"] == "main"
                    assert isinstance(edge["index"], int) and edge["index"] >= 0


@pytest.mark.parametrize("path", [WORKFLOW_PATH, FIXTURE_PATH], ids=["workflow", "fixture"])
def test_workflow_has_no_secrets_or_credential_bindings(path):
    wf = _load(path)
    for node in wf["nodes"]:
        assert "credentials" not in node, node["name"]
        assert "headerParameters" not in node["parameters"], node["name"]
        assert "sendHeaders" not in node["parameters"], node["name"]
    for text in _strings(wf):
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(text), f"posible secreto con el patrón {pattern.pattern!r}"


@pytest.mark.parametrize("path", [WORKFLOW_PATH, FIXTURE_PATH], ids=["workflow", "fixture"])
def test_node_type_versions_exist_in_n8n_1x(path):
    for node in _load(path)["nodes"]:
        assert node["typeVersion"] == EXPECTED_TYPE_VERSIONS[node["type"]], node["name"]


def test_http_nodes_point_at_documented_paths(workflow):
    nodes = _nodes_by_name(workflow)
    http = {n["name"]: n["parameters"] for n in workflow["nodes"] if n["type"].endswith(".httpRequest")}
    assert set(http) == {"POST /collect", "POST /backfill/yadio 7 días", "GET /stats"}

    collect = http["POST /collect"]
    assert (collect["method"], collect["url"]) == ("POST", PREFIX + "/collect")
    backfill = http["POST /backfill/yadio 7 días"]
    assert (backfill["method"], backfill["url"]) == ("POST", PREFIX + "/backfill/yadio")
    assert backfill["queryParameters"]["parameters"] == [{"name": "days", "value": "7"}]
    for protected in (collect, backfill):
        assert protected["authentication"] == "genericCredentialType"
        assert protected["genericAuthType"] == "httpHeaderAuth"

    stats = http["GET /stats"]
    assert (stats["method"], stats["url"]) == ("GET", PREFIX + "/stats")
    assert "authentication" not in stats
    query = {p["name"]: p["value"] for p in stats["queryParameters"]["parameters"]}
    assert query == {"sources": "binance_p2p,yadio,bcv,paralelo", "reference": "bcv"}

    assert nodes["POST /backfill/yadio 7 días"]["onError"] == "continueErrorOutput"
    assert nodes["GET /stats"]["onError"] == "continueErrorOutput"


def test_schedules_and_error_branches(workflow):
    assert workflow["settings"]["timezone"] == "America/Caracas"
    nodes = _nodes_by_name(workflow)
    daily = nodes["Cada día 06:00"]["parameters"]["rule"]["interval"][0]
    assert (daily["field"], daily["triggerAtHour"], daily["triggerAtMinute"]) == ("days", 6, 0)
    assert nodes["Cada hora"]["parameters"]["rule"]["interval"][0]["field"] == "hours"

    conn = workflow["connections"]
    backfill_ok, backfill_error = conn["POST /backfill/yadio 7 días"]["main"]
    assert [e["node"] for e in backfill_error] == ["Mensaje: backfill falló"]
    assert {e["node"] for e in conn["Mensaje: backfill falló"]["main"][0]} == {"Telegram", "Email"}
    alerts_true, alerts_false = conn["¿Hay alertas?"]["main"]
    assert {e["node"] for e in alerts_true} == {"Telegram", "Email"}
    assert [e["node"] for e in alerts_false] == ["Sin alertas"]


def test_fixture_uses_the_same_alert_code(workflow, fixture_workflow):
    code = _nodes_by_name(workflow)["Calcular alertas"]["parameters"]["jsCode"]
    fixture_nodes = _nodes_by_name(fixture_workflow)
    for name in ("Calcular alertas", "Calcular alertas (segundo tick)", "Calcular alertas (malformado)"):
        assert fixture_nodes[name]["parameters"]["jsCode"] == code, name


HARNESS = """
const fs = require('fs');
const { code, items, env, staticData } = JSON.parse(fs.readFileSync(0, 'utf8'));
const $input = { all: () => items, first: () => items[0] };
try {
  const out = new Function('$input', '$env', '$getWorkflowStaticData', code)($input, env, () => staticData);
  console.log(JSON.stringify({ ok: true, out, staticData }));
} catch (e) {
  console.log(JSON.stringify({ ok: false, error: e.message, staticData }));
}
"""


def _fixture_items(fixture_workflow: dict, node_name: str) -> list:
    js = _nodes_by_name(fixture_workflow)[node_name]["parameters"]["jsCode"]
    assert js.startswith("return ") and js.endswith(";")
    return json.loads(js[len("return ") : -1])


def _run_code(code: str, items: list, env: dict, static_data: dict) -> dict:
    payload = json.dumps({"code": code, "items": items, "env": env, "staticData": static_data})
    proc = subprocess.run(
        ["node", "-e", HARNESS], input=payload, capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node no se puede ejecutar el nodo Code")
def test_alert_code_dedups_within_and_across_runs(workflow, fixture_workflow):
    code = _nodes_by_name(workflow)["Calcular alertas"]["parameters"]["jsCode"]
    valid = _fixture_items(fixture_workflow, "Stats válidos (con fuente repetida)")
    env = {"RATES_BASE_URL": "http://localhost:8000/"}

    first = _run_code(code, valid, env, {})
    assert first["ok"]
    summary = first["out"][0]["json"]
    assert summary["threshold"] == 3
    assert [a["source"] for a in summary["alerts"]] == ["binance_p2p", "yadio"]
    assert summary["duplicates"] == ["binance_p2p|2026-11-06T10:00:00-04:00"]
    assert summary["invalid"] == []
    binance = summary["alerts"][0]
    assert (binance["price"], binance["pct"], binance["from_price"]) == (650.5, 4.2, 624.25)
    assert binance["from_date"] == "2026-11-05T10:00:00-04:00"
    assert "650,50" in summary["message"] and "+4,20 %" in summary["message"]
    assert "624,25" in summary["message"] and "2026-11-05T10:00:00-04:00" in summary["message"]
    assert summary["message"].endswith("Ver: http://localhost:8000/")

    second = _run_code(code, valid, env, first["staticData"])
    assert second["ok"]
    assert second["out"][0]["json"]["alerts"] == []
    assert len(second["out"][0]["json"]["duplicates"]) == 3


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node no se puede ejecutar el nodo Code")
def test_alert_code_rejects_malformed_stats_and_reads_threshold(workflow, fixture_workflow):
    code = _nodes_by_name(workflow)["Calcular alertas"]["parameters"]["jsCode"]
    malformed = _fixture_items(fixture_workflow, "Stats malformados")
    result = _run_code(code, malformed, {}, {})
    assert not result["ok"]
    assert "malformada" in result["error"]
    assert result["staticData"] == {}

    valid = _fixture_items(fixture_workflow, "Stats válidos (con fuente repetida)")
    high = _run_code(code, valid, {"RATES_ALERT_PCT": "5"}, {})["out"][0]["json"]
    assert (high["threshold"], high["alerts"]) == (5, [])
    blank = _run_code(code, valid, {"RATES_ALERT_PCT": ""}, {})["out"][0]["json"]
    assert blank["threshold"] == 3


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node no se puede ejecutar el nodo Code")
def test_stats_failure_notice_is_sent_once_per_outage(workflow):
    nodes = _nodes_by_name(workflow)
    notice = nodes["Aviso de fallo de stats (una vez por caída)"]["parameters"]["jsCode"]
    error_item = [{"json": {"error": "connect ECONNREFUSED"}}]
    first = _run_code(notice, error_item, {}, {})
    assert "ECONNREFUSED" in first["out"][0]["json"]["message"]
    assert _run_code(notice, error_item, {}, first["staticData"])["out"] == []

    valid = [{"json": {"source": "bcv", "latest": 1.0, "latest_date": "d", "change": {"24h": None}}}]
    recovered = _run_code(nodes["Calcular alertas"]["parameters"]["jsCode"], valid, {}, first["staticData"])
    assert recovered["staticData"]["statsDown"] is False
    assert _run_code(notice, error_item, {}, recovered["staticData"])["out"] != []
