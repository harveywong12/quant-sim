"""Minimal FastAPI gateway for qsim."""

from __future__ import annotations
from qsim.services.mutation_lock import serialized_mutation, MutationBusy, mutation_status


import asyncio
import logging
import os
import secrets
import subprocess
import sys
import threading
import datetime as dt
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from qsim.allocation.policy import DEFAULT_REGIME_WEIGHTS, default_policy
from qsim.services.web.mobile import render_page as _render_mobile
from qsim.services.web import qsim_data as _qdata
from qsim.services import realtime as _realtime
from qsim.services import paper_orders as _paper_orders
from qsim.services.scheduler import Scheduler
from qsim.services import events as _events
from qsim.market import legacy_adapter as _legacy_adapter

logger = logging.getLogger("qsim.api")
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LEGACY_TRIGGER_FALLBACK = "http://127.0.0.1:8502"
_LEGACY_AUTH_TOKEN_FILE = Path(
    os.environ.get("QSIM_LEGACY_AUTH_TOKEN_FILE", str(_PROJECT_ROOT / ".secrets" / "legacy-auth"))
)
_AUTH_TOKEN_FILE = Path(
    os.environ.get("QSIM_AUTH_TOKEN_FILE", str(_PROJECT_ROOT / ".secrets" / "qsim-auth-token"))
)

# The legacy producer publishes end-of-day data after the market closes.  Keep
# the daily jobs ordered so paper and shadow never intentionally run before
# the source-to-refactor sync window.
DAILY_JOB_TIMES: dict[str, tuple[int, int]] = {
    "legacy_stock_fetch": (18, 0),
    "legacy_daily_settlement": (19, 30),
    "sync_refactor_data": (20, 0),
    "paper_daily": (20, 30),
    "meta_shadow": (20, 45),
    "drift_check": (20, 50),
    "shadow_diff": (21, 0),
    "qc_check": (21, 15),
    "ai_review": (21, 30),
    "preflight": (8, 40),
}


def _require_native_auth(request: Request) -> None:
    """Require the qsim-only token for every native write endpoint."""
    try:
        expected = _AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error("qsim auth token is unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="qsim auth is not configured") from exc
    supplied = request.headers.get("X-Auth-Token", "")
    if not expected or not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid qsim auth token")


def _require_success(result: dict) -> dict:
    if result.get("ok") is False:
        raise HTTPException(status_code=503, detail=result)
    return result


def _refresh_native_realtime() -> dict:
    if not _realtime.is_trading_session():
        return {"ok": True, "skipped": "outside-session"}
    return _require_success(_realtime.refresh_realtime())


def _run_realtime_quotes_job() -> None:
    """Refresh marks only during an open A-share session.

    The APScheduler trigger is deliberately limited to weekday market-hour
    slots.  ``is_trading_session`` remains the authority for holidays and the
    09:30/11:30/13:00/15:00 boundaries, so skipped executions never call the
    quote source or mutate any account/ledger tables.
    """
    if not _realtime.is_trading_session():
        return
    result = _realtime.refresh_realtime()
    if result.get("ok") is False:
        raise RuntimeError(f"realtime quote refresh failed: {result}")
    logger.info("realtime quote refresh: %s", result)


def _run_versioned_open_job() -> None:
    """Execute a frozen v3/v4 order once during the 09:30-09:35 window."""
    for name, runner in (("v3", _paper_orders.execute_v3_open), ("v4", _paper_orders.execute_v4_open)):
        result = runner()
        if result.get("ok") is False:
            raise RuntimeError(f"{name} open execution failed: {result}")
        logger.info("%s open execution: %s", name, result)


def _legacy_trigger_urls() -> list[str]:
    """Resolve the legacy trigger without pinning an ephemeral Docker IP."""
    configured = os.environ.get("QSIM_LEGACY_TRIGGER", "").strip()
    if configured:
        return [configured.rstrip("/")]
    candidates: list[str] = []
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", "quant-sim-trigger"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if proc.returncode == 0:
            candidates.extend(f"http://{address}:8502" for address in proc.stdout.split() if address)
    except (OSError, subprocess.SubprocessError):
        pass
    candidates.append(_LEGACY_TRIGGER_FALLBACK)
    return list(dict.fromkeys(item.rstrip("/") for item in candidates))


def _legacy_auth_token() -> str:
    """Read the legacy trigger credential without exposing it in process args."""
    try:
        for line in _LEGACY_AUTH_TOKEN_FILE.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "QUANT_AUTH_TOKEN":
                return value.strip().strip("\"'")
    except OSError as exc:
        raise RuntimeError("legacy trigger credential is unavailable") from exc
    raise RuntimeError("legacy trigger credential is unavailable")


@serialized_mutation
def _call_legacy_trigger(name: str, *, timeout: int = 600) -> dict:
    headers = {"X-Auth-Token": _legacy_auth_token()}
    base_url = _legacy_trigger_urls()[0]
    try:
        resp = requests.post(f"{base_url}/{name}", headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        raise HTTPException(status_code=502, detail=f"legacy trigger failed; verify remote state before retry: {name}") from None
    try:
        result = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail=f"legacy trigger returned invalid JSON: {name}")
    if isinstance(result, dict) and result.get("ok") is False:
        raise HTTPException(status_code=502, detail=result)
    return result


def _proxy_job(name: str, request: Request) -> dict:
    # Native request authentication happened before this call. The legacy
    # service has a separate token and must never depend on a caller token.
    del request
    from qsim.services import native_market
    if native_market.enabled() and name in {"fetch-stocks", "run-daily", "catch-up", "ensure-stock-data"}:
        result = native_market.run()
        if name in {"run-daily", "catch-up"}:
            _require_fresh_market_data(name)
            from qsim.services.paper_daily import run_daily_settlement
            return {"ok": True, "market": result, "settlement": run_daily_settlement()}
        return result
    return _call_legacy_trigger(name)


def _run_legacy_stock_fetch_job() -> None:
    """Refresh canonical legacy OHLCV before qsim's 20:00 import window."""
    from qsim.services import native_market
    if native_market.enabled():
        native_market.run()
        return
    expected = _legacy_adapter.market_data_gate_status().get("expected_latest_trade_date")
    today = dt.datetime.now().strftime("%Y-%m-%d")
    if expected != today:
        logger.info("legacy stock fetch skipped: no current trading session (%s)", expected)
        return
    result = _call_legacy_trigger("fetch-stocks", timeout=1200)
    if not result.get("ok", True):
        raise RuntimeError(f"legacy stock fetch failed: {result}")


def _run_legacy_daily_settlement_job() -> None:
    """Update legacy ETF OHLCV and settle its canonical daily ledger."""
    from qsim.services import native_market
    if native_market.enabled():
        native_market.run()
        return
    expected = _legacy_adapter.market_data_gate_status().get("expected_latest_trade_date")
    if expected != dt.datetime.now().strftime("%Y-%m-%d"):
        logger.info("legacy daily settlement skipped: no current trading session (%s)", expected)
        return
    result = _call_legacy_trigger("run-daily", timeout=1200)
    if not result.get("ok", True):
        raise RuntimeError(f"legacy daily settlement failed: {result}")


def _run_sync_data_job() -> None:
    from qsim.services import native_market
    if native_market.enabled():
        native_market.run()
        _require_fresh_market_data("sync")
        return
    script = _PROJECT_ROOT / "scripts" / "sync-data-from-legacy.sh"
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        logger.error("sync-data-from-legacy failed: %s", proc.stderr[-500:])
        raise RuntimeError("sync-data-from-legacy failed")
    _require_fresh_market_data("sync")


def _require_fresh_market_data(stage: str) -> dict:
    """Reject scheduled work unless the complete daily stock data is fresh."""
    gate = _legacy_adapter.market_data_gate_status()
    if not gate.get("ok"):
        logger.error("%s blocked by market-data freshness gate: %s", stage, gate)
        raise RuntimeError(f"{stage} blocked by market-data freshness gate: {gate}")
    from qsim.services import native_market
    if native_market.enabled():
        from qsim.services.market_quality import check
        quality = check(gate["expected_latest_trade_date"], require_publication=True)
        if not quality["ok"]:
            raise RuntimeError(f"{stage} blocked by data quality: {quality['issues']}")
    return gate


def _run_paper_job() -> None:
    _require_fresh_market_data("paper_daily")
    from qsim.services.paper_daily import run_daily_settlement
    run_daily_settlement()


def _run_shadow_job() -> None:
    _require_fresh_market_data("shadow_diff")
    script = _PROJECT_ROOT / "scripts" / "run_shadow_diff.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        logger.error("shadow diff failed: %s", proc.stderr[-500:])
        raise RuntimeError("shadow diff failed")


def _run_meta_shadow_job() -> None:
    """Generate read-only meta evidence after paper marks settle, before reconciliation."""
    _require_fresh_market_data("meta_shadow")
    script = _PROJECT_ROOT / "qsim" / "services" / "meta_shadow.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        logger.error("meta shadow failed: %s", proc.stderr[-500:])
        raise RuntimeError("meta shadow failed")



def _run_qc_check_job() -> None:
    from qsim.services import qc_check
    result = qc_check.run_daily()
    if not result.get("ok"):
        raise RuntimeError(f"qc check failed: {result.get('verdict')}")


def _run_ai_review_job() -> None:
    from qsim.services import ai_advisor
    result = ai_advisor.run_daily_review()
    if not result.get("ok"):
        raise RuntimeError(f"ai review failed: {result.get('error')}")


def _run_preflight_job() -> None:
    from qsim.services import preflight
    result = preflight.run_daily()
    if not result.get("ok"):
        raise RuntimeError("preflight issues: " + "; ".join(result.get("issues", []))[:200])

def _run_stock_ensure_job() -> None:
    from qsim.services import stock_ensure

    result = stock_ensure.run_scheduled()
    if not result.get("ok"):
        raise RuntimeError(f"stock ensure failed: {result.get('status')}")


def _run_drift_check_job() -> None:
    from qsim.services import drift_monitor

    result = drift_monitor.evaluate()
    if result["events"]:
        logger.warning("drift events: %s", result["events"])
    else:
        logger.info("drift check ok: %s", result["checked_at"])


def _drift_state() -> dict:
    import json as _json

    path = _PROJECT_ROOT / "artifacts" / "drift_state.json"
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def create_app() -> FastAPI:
    scheduler = Scheduler()
    sync_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qsim-sync")
    sync_state_lock = threading.Lock()
    sync_future: Future | None = None
    sse_stop = threading.Event()

    def _watch_data_version() -> None:
        last: str | None = None
        while not sse_stop.wait(5):
            try:
                v = _qdata.data_version()
            except Exception:
                logger.warning("data-version watcher failed", exc_info=True)
                continue
            if v != last:
                if last is not None:
                    _events.publish({"type": "data_version", "version": v})
                last = v

    def _start_sse_watcher() -> None:
        threading.Thread(
            target=_watch_data_version, name="qsim-sse-watch", daemon=True
        ).start()

    def start_background_sync() -> JSONResponse:
        nonlocal sync_future
        if mutation_status().get("running"):
            return JSONResponse(status_code=409, content={"ok": False, "status": "running",
                                                         "detail": "a data or ledger task is running"})
        with sync_state_lock:
            if sync_future and not sync_future.done():
                return JSONResponse(
                    status_code=409,
                    content={"ok": False, "status": "running", "detail": "market sync is already running"},
                )
            sync_future = sync_executor.submit(_legacy_adapter.sync_legacy_market_data)
        return JSONResponse(status_code=202, content={"ok": True, "status": "started"})

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if os.environ.get("QSIM_SCHEDULER_ENABLED", "1") != "0":
            scheduler.start()
        _start_sse_watcher()
        try:
            yield
        finally:
            sse_stop.set()
            scheduler.shutdown()
            sync_executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="qsim gateway", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(MutationBusy)
    async def mutation_busy_handler(_request: Request, exc: MutationBusy):
        return JSONResponse(status_code=409, content={"ok": False, "error": str(exc),
                                                     "task": mutation_status()})

    scheduler.add_daily_job(
        "legacy_stock_fetch", _run_legacy_stock_fetch_job,
        hour=DAILY_JOB_TIMES["legacy_stock_fetch"][0],
        minute=DAILY_JOB_TIMES["legacy_stock_fetch"][1],
        retry_once=True,
    )
    scheduler.add_daily_job(
        "legacy_daily_settlement", _run_legacy_daily_settlement_job,
        hour=DAILY_JOB_TIMES["legacy_daily_settlement"][0],
        minute=DAILY_JOB_TIMES["legacy_daily_settlement"][1],
        retry_once=True,
    )
    scheduler.add_daily_job(
        "sync_refactor_data",
        _run_sync_data_job,
        hour=DAILY_JOB_TIMES["sync_refactor_data"][0],
        minute=DAILY_JOB_TIMES["sync_refactor_data"][1],
        retry_once=True,
    )
    scheduler.add_daily_job(
        "paper_daily",
        _run_paper_job,
        hour=DAILY_JOB_TIMES["paper_daily"][0],
        minute=DAILY_JOB_TIMES["paper_daily"][1],
        retry_once=True,
    )
    scheduler.add_daily_job(
        "meta_shadow",
        _run_meta_shadow_job,
        hour=DAILY_JOB_TIMES["meta_shadow"][0],
        minute=DAILY_JOB_TIMES["meta_shadow"][1],
    )
    scheduler.add_daily_job(
        "shadow_diff",
        _run_shadow_job,
        hour=DAILY_JOB_TIMES["shadow_diff"][0],
        minute=DAILY_JOB_TIMES["shadow_diff"][1],
        retry_once=True,
    )
    # 09:30–11:30 and 13:00–15:00, aligned to five-minute clock slots.
    # The 09:00–09:25 wakeups are guard-only (no Sina request); keeping the
    # trigger compact makes the session boundary policy live in one place.
    scheduler.add_cron_job(
        "versioned_open_execution",
        _run_versioned_open_job,
        day_of_week="mon-fri",
        hour=9,
        minute="30-35",
    )
    scheduler.add_cron_job(
        "realtime_quotes_5m",
        _run_realtime_quotes_job,
        day_of_week="mon-fri",
        hour="9-11,13-14",
        minute="*/5",
    )
    scheduler.add_cron_job(
        "realtime_quotes_close",
        _run_realtime_quotes_job,
        day_of_week="mon-fri",
        hour=15,
        minute=0,
    )

    scheduler.add_daily_job(
        "drift_check",
        _run_drift_check_job,
        hour=DAILY_JOB_TIMES["drift_check"][0],
        minute=DAILY_JOB_TIMES["drift_check"][1],
    )

    scheduler.add_daily_job(
        "qc_check",
        _run_qc_check_job,
        hour=DAILY_JOB_TIMES["qc_check"][0],
        minute=DAILY_JOB_TIMES["qc_check"][1],
    )
    scheduler.add_daily_job(
        "ai_review",
        _run_ai_review_job,
        hour=DAILY_JOB_TIMES["ai_review"][0],
        minute=DAILY_JOB_TIMES["ai_review"][1],
    )
    scheduler.add_daily_job(
        "preflight",
        _run_preflight_job,
        hour=DAILY_JOB_TIMES["preflight"][0],
        minute=DAILY_JOB_TIMES["preflight"][1],
    )
    scheduler.add_cron_job(
        "stock_ensure",
        _run_stock_ensure_job,
        day_of_week="mon-fri",
        hour="15-21",
        minute="40,10",
    )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    def readyz():
        from qsim.services.operations import readiness
        result = readiness()
        return JSONResponse(status_code=200 if result["ok"] else 503, content=result)

    @app.get("/api/operations")
    def operations_status():
        from qsim.services.operations import status
        return status()

    _WEB_DIR = Path(__file__).resolve().parents[2] / "services" / "web"

    @app.get("/", include_in_schema=False)
    def index():
        from fastapi.responses import HTMLResponse

        return HTMLResponse(_render_mobile())

    @app.get("/api/dashboard")
    def dashboard() -> dict:
        return {
            "ok": True,
            "version": _qdata.data_version(),
            "realtime_status": _qdata.paper_realtime_status(),
            "paper_accounts": _qdata.paper_accounts(),
            "paper_order_intents": _paper_orders.list_v3_intents(),
            "paper_latest_nav": _qdata.paper_latest_nav(),
            "market_status": _qdata.market_status(),
            "strategy_overview": _qdata.strategy_overview(),
            "adaptive_replay": _qdata.adaptive_replay(),
            "rebaseline": _qdata.latest_rebaseline(),
            "provisional_paper": _qdata.provisional_paper(),
            "meta_shadow": _qdata.meta_shadow(),
            "experiments": _qdata.experiments(),
            "backtest_curves": _qdata.backtest_curves(),
            "market_gate_curves": _qdata.market_gate_curves(),
            "shadow": _qdata.latest_shadow(),
            "hot_zone": _qdata.hot_zone_snapshot(),
        }

    @app.get("/api/data-version")
    def data_version() -> dict:
        return {"ok": True, "version": _qdata.data_version()}

    @app.get("/api/experiments")
    def experiments(kind: str | None = None, limit: int = 200) -> dict:
        from qsim.research.experiment_store import ExperimentStore

        artifacts = _PROJECT_ROOT / "artifacts"
        store = ExperimentStore(artifacts / "experiments.jsonl")
        try:
            rows = store.query(kind=kind, limit=min(limit, 1000))
        except Exception:
            rows = []
        if not rows and not (artifacts / "experiments.db").is_file():
            rows = store.load()[-limit:]
        return {"ok": True, "count": len(rows), "rows": rows}

    @app.get("/api/events")
    def sse() -> Any:
        import json as _json
        from fastapi.responses import StreamingResponse

        try:
            sub = _events.subscribe()
        except RuntimeError:
            raise HTTPException(status_code=503, detail="sse subscriber limit")

        async def stream():
            sub.loop = asyncio.get_running_loop()
            sub.aq = asyncio.Queue(maxsize=64)
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(sub.aq.get(), timeout=15)
                        yield f"data: {_json.dumps(item, ensure_ascii=False)}\n\n"
                    except TimeoutError:
                        yield ": ping\n\n"
            finally:
                _events.unsubscribe(sub)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/static/{path:path}", include_in_schema=False)
    def static(path: str):
        from fastapi.responses import FileResponse

        base = _WEB_DIR / "static"
        target = (base / path).resolve()
        if not target.is_relative_to(base.resolve()) or not target.is_file():
            return None
        return FileResponse(target)

    @app.get("/api/status")
    def status() -> dict:
        meta = _qdata.meta_shadow()
        return {
            "mode": "meta_shadow_v1" if meta else "meta_shadow_pending",
            "risk": "shadow_only",
            "meta_shadow_state": meta.get("state") if meta else "not_generated",
            "scheduler_running": scheduler.running,
            "scheduler_jobs": scheduler.jobs,
            "scheduler_job_details": scheduler.job_details,
            "scheduler_last_runs": scheduler.last_runs,
            "scheduler_consecutive_failures": scheduler.consecutive_failures,
            "drift_monitor": _drift_state(),
            "realtime_quotes": _qdata.paper_realtime_status(),
            "market_data": _legacy_adapter.latest_sync_status(),
        }

    @app.get("/api/market/status")
    def market_status() -> dict:
        return _legacy_adapter.latest_sync_status()

    @app.get("/api/config")
    def config() -> dict:
        policy = default_policy()
        return {
            "default_regime_weights": DEFAULT_REGIME_WEIGHTS,
            "risk_rule": "tiered_drawdown",
            "target_weights": policy.target_weights(
                {"up": 0.4, "range": 0.3, "down": 0.3}
            ),
        }

    @app.post(
        "/api/sync-refactor-data",
        dependencies=[Depends(_require_native_auth)],
        response_model=None,
    )
    def sync_refactor_data(background: bool = False) -> dict | JSONResponse:
        if background:
            return start_background_sync()
        return _require_success(_legacy_adapter.sync_legacy_market_data())

    @app.post(
        "/api/market/sync",
        dependencies=[Depends(_require_native_auth)],
        response_model=None,
    )
    def sync_market_data(background: bool = False) -> dict | JSONResponse:
        if background:
            return start_background_sync()
        return _require_success(_legacy_adapter.sync_legacy_market_data())

    @app.post("/api/paper/refresh-realtime", dependencies=[Depends(_require_native_auth)])
    def refresh_realtime() -> dict:
        return _refresh_native_realtime()

    @app.get("/api/paper/realtime")
    def paper_realtime() -> dict:
        return {"ok": True, "prices": _realtime.latest_prices(),
                "status": _qdata.paper_realtime_status(),
                "in_session": _realtime.is_trading_session()}

    @app.get("/api/paper/orders")
    def paper_orders() -> dict:
        return {"ok": True, "orders": {
            "multi_factor_v3": _paper_orders.list_v3_intents(),
            "multi_factor_v4": _paper_orders.list_v4_intents(),
        }}

    @app.post("/api/paper/replay", dependencies=[Depends(_require_native_auth)])
    def historical_replay() -> dict:
        from qsim.services.historical_replay import run
        return run()

    @app.post("/api/paper/run", dependencies=[Depends(_require_native_auth)])
    def run_paper() -> dict:
        try:
            _require_fresh_market_data("paper_daily")
            from qsim.services.paper_daily import run_daily_settlement
            result = run_daily_settlement()
        except MutationBusy:
            raise
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "output": str(result["official"]), "signals": result["signals"]}

    @app.post("/api/paper/orders/freeze-v3", dependencies=[Depends(_require_native_auth)])
    def freeze_v3_order() -> dict:
        return _require_success(_paper_orders.freeze_v3_intent())

    @app.post("/api/paper/orders/execute-v3", dependencies=[Depends(_require_native_auth)])
    def execute_v3_order() -> dict:
        return _require_success(_paper_orders.execute_v3_open())

    @app.post("/api/paper/orders/freeze-v4", dependencies=[Depends(_require_native_auth)])
    def freeze_v4_order() -> dict:
        return _require_success(_paper_orders.freeze_v4_intent())

    @app.post("/api/paper/orders/execute-v4", dependencies=[Depends(_require_native_auth)])
    def execute_v4_order() -> dict:
        return _require_success(_paper_orders.execute_v4_open())

    @app.post("/api/shadow/run", dependencies=[Depends(_require_native_auth)])
    def run_shadow() -> dict:
        script = _PROJECT_ROOT / "scripts" / "run_shadow_diff.py"
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if proc.returncode != 0:
            raise HTTPException(status_code=503, detail="shadow diff failed")
        return {"ok": True, "output": proc.stdout[-3000:]}

    job_paths = [
        "run-daily",
        "catch-up",
        "run-intraday",
        "fetch-stocks",
        "ensure-stock-data",
        "run-league",
        "watchdog",
        "maintain",
        "backup",
    ]
    def make_job_handler(job_path: str):
        def handler(request: Request) -> dict:
            return _proxy_job(job_path, request)
        return handler

    for path in job_paths:
        app.add_api_route(
            f"/{path}",
            make_job_handler(path),
            methods=["POST"],
            name=f"legacy_{path.replace('-', '_')}",
        )

    @app.post("/refresh-realtime", dependencies=[Depends(_require_native_auth)])
    def refresh_realtime_job() -> dict:
        """Qinglong 兼容入口，直接执行 qsim 原生实时刷新。"""
        result = _refresh_native_realtime()
        logger.info("native realtime refresh via trigger route: %s", result)
        return result

    @app.post("/api/ai-summary", dependencies=[Depends(_require_native_auth)])
    def ai_summary() -> dict:
        """Trigger the daily AI review (idempotent per date, safe to re-run)."""
        from qsim.services import ai_advisor
        return ai_advisor.run_daily_review()

    @app.post("/api/chat", dependencies=[Depends(_require_native_auth)])
    async def ai_chat(request: Request) -> dict:
        """Lightweight quant assistant chat grounded in today's qsim context."""
        import json as _json
        body = await request.json()
        question = str(body.get("question") or "").strip()
        history = body.get("history") or []
        if not question:
            return {"error": "empty question"}
        try:
            from qsim.services import ai_advisor, qc_check
            client = ai_advisor.LlmClient()
        except RuntimeError as exc:
            return {"error": str(exc)}
        try:
            today = dt.datetime.now().strftime("%Y%m%d")
            checks = qc_check.run_checks(today)
            conn = ai_advisor._conn()
            try:
                context = ai_advisor.build_context(conn, today, checks)
            finally:
                conn.close()
            hist_text = "\n".join(
                f"{m.get('role', 'user')}: {m.get('content', '')}" for m in history[-8:])
            answer = client.chat(
                "你是量化模拟盘监控助手。仅基于给定上下文回答，不要编造数据；"
                "上下文没有的信息直接说明未知。回答简明，可用 markdown。",
                f"{context}\n\n## 近期对话\n{hist_text}\n\nuser: {question}",
                json_mode=False, timeout=120)
            return {"answer": answer}
        except Exception as exc:
            logger.warning("ai chat failed: %s", exc)
            return {"error": str(exc)[:300]}

    @app.post("/api/scheduler/start", dependencies=[Depends(_require_native_auth)])
    def start_scheduler() -> dict:
        if os.environ.get("QSIM_SCHEDULE_OWNER") == "systemd":
            raise HTTPException(status_code=409, detail="Scheduling is managed by systemd")
        scheduler.start()
        return {
            "ok": True,
            "scheduler_running": scheduler.running,
            "jobs": scheduler.jobs,
        }

    @app.post("/api/scheduler/shutdown", dependencies=[Depends(_require_native_auth)])
    def shutdown_scheduler() -> dict:
        scheduler.shutdown()
        return {
            "ok": True,
            "scheduler_running": scheduler.running,
            "jobs": scheduler.jobs,
        }

    return app
