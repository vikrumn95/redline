"""Main entry point.

  python -m redline.main run [--dry-run]   # run full rebalance cycle
  python -m redline.main status            # print status from SQLite + live balances
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid

from redline.brokers.coinbase_spot import CoinbaseSpotBroker
from redline.config import Config, load_config
from redline.core.portfolio import plan_liquidation_orders, plan_orders
from redline.core.reconcile import compute_equity
from redline.core.risk import evaluate_circuit_breakers
from redline.core.store import Store
from redline.core.types import EquitySnapshot, RunRecord, RunStatus
from redline.data.candles import close_prices
from redline.data.providers.coinbase import CoinbaseDataProvider
from redline.execution.errors import ExecutionHaltError
from redline.execution.policy import DryRunPolicy, LiveExecutionPolicy
from redline.logging import RunLogger
from redline.monitoring.alerts import AlertManager
from redline.sizing.risk_parity import compute_target_notionals, inverse_vol_weights
from redline.sizing.volatility import annualized_vol
from redline.strategies.crypto.btc_eth_trend import sma200_signal
from redline.timeutil import utcnow_iso

PRODUCTS = ["BTC-USD", "ETH-USD"]


def _skip_reason(
    signals: dict[str, bool],
    target_notionals: dict[str, float],
    balances: dict,
    prices: dict[str, float],
    executed: list,
    min_trade_usd: float,
) -> str | None:
    """Return a skip reason if no orders were placed, else None.

    Possible values:
      risk_off_both       — all assets are risk-off (target = 0 for all)
      min_trade_threshold — deltas exist but all are below the $50 minimum
      at_target           — already positioned at target; nothing to trade
    """
    if executed:
        return None

    risk_on = [p for p, s in signals.items() if s]
    if not risk_on:
        return "risk_off_both"

    # Check whether any non-trivial delta exists before the min-trade filter
    from redline.core.types import Balance

    has_delta = False
    for product_id, target_notional in target_notionals.items():
        base = product_id.split("-")[0]
        price = prices.get(base, 0.0)
        if price <= 0:
            continue
        current_qty = balances.get(base, Balance(base, 0.0)).available
        current_notional = current_qty * price
        if abs(target_notional - current_notional) >= min_trade_usd:
            has_delta = True
            break

    return "min_trade_threshold" if has_delta else "at_target"


def _overrides_active(args: argparse.Namespace) -> bool:
    """Return True if any test override flag deviates from its default."""
    return (
        getattr(args, "force_btc_signal", "COMPUTED") != "COMPUTED"
        or getattr(args, "force_eth_signal", "COMPUTED") != "COMPUTED"
        or getattr(args, "force_gross_usd", None) is not None
    )


def _apply_signal_overrides(
    signals: dict[str, bool],
    force_btc: str,
    force_eth: str,
) -> dict[str, bool]:
    """Apply per-asset signal overrides (ON/OFF) on top of computed signals."""
    result = dict(signals)
    if force_btc == "ON":
        result["BTC-USD"] = True
    elif force_btc == "OFF":
        result["BTC-USD"] = False
    if force_eth == "ON":
        result["ETH-USD"] = True
    elif force_eth == "OFF":
        result["ETH-USD"] = False
    return result


def _compute_forced_targets(
    force_gross_usd: float,
    signals: dict[str, bool],
    equity: float,
    max_gross_notional: float,
    max_per_asset_notional: float,
) -> dict[str, float]:
    """Allocate a fixed gross USD across risk-on assets, enforcing caps.

    Both risk-on → 50/50 split. One risk-on → 100%. None → all zeros.
    """
    risk_on = [p for p in PRODUCTS if signals.get(p)]
    targets = {p: 0.0 for p in PRODUCTS}
    gross = min(force_gross_usd, max_gross_notional * equity)
    if len(risk_on) == 2:
        per_asset = min(gross / 2.0, max_per_asset_notional * equity)
        for p in risk_on:
            targets[p] = per_asset
    elif len(risk_on) == 1:
        targets[risk_on[0]] = min(gross, max_per_asset_notional * equity)
    return targets


def cmd_run(args: argparse.Namespace, cfg: Config) -> None:
    run_id = uuid.uuid4().hex[:8]
    logger = RunLogger(run_id, cfg.log_level)
    dry_run = args.dry_run or cfg.dry_run

    logger.info("run_start", dry_run=dry_run, env=cfg.env, mock=cfg.mock)

    # ------------------------------------------------------------------ #
    # 1. Open SQLite                                                       #
    # ------------------------------------------------------------------ #
    store = Store(cfg.db_path)
    alerts = AlertManager(cfg.slack_webhook_url, run_id, logger, cfg.env)

    halt_flag, halt_reason, halted_at = store.get_halt_state_full()
    if halt_flag:
        last_run = store.load_last_run()
        logger.warning(
            "HALT_GATING_ACTIVE",
            halt_reason=halt_reason,
            halted_at=halted_at,
            last_run_id=last_run.run_id if last_run else None,
        )
        alerts.send_alert(
            "HALT_GATING_ACTIVE",
            {
                "halt_reason": halt_reason,
                "halted_at": halted_at,
                "last_run_id": last_run.run_id if last_run else None,
            },
            dry_run,
        )
        equity_snaps = store.load_equity_history(days=1)
        last_equity = equity_snaps[0].equity_usd if equity_snaps else 0.0
        peak_equity_for_skip = store.load_peak_equity() or last_equity
        drawdown_for_skip = (
            (peak_equity_for_skip - last_equity) / peak_equity_for_skip
            if peak_equity_for_skip > 0
            else 0.0
        )
        store.save_run(
            RunRecord(
                run_id=run_id,
                ts_utc=utcnow_iso(),
                status=RunStatus.SKIPPED,
                dry_run=dry_run,
                equity_usd=last_equity,
                peak_equity_usd=peak_equity_for_skip,
                drawdown=drawdown_for_skip,
                halt_flag=True,
                halt_reason=halt_reason,
                skip_reason="HALTED",
            )
        )
        sys.exit(0)

    # ------------------------------------------------------------------ #
    # 1b. Override gating: reject test flags in non-dev / disallowed envs  #
    # ------------------------------------------------------------------ #
    force_btc = getattr(args, "force_btc_signal", "COMPUTED")
    force_eth = getattr(args, "force_eth_signal", "COMPUTED")
    force_gross = getattr(args, "force_gross_usd", None)

    if _overrides_active(args):
        if not (cfg.allow_test_overrides and cfg.env == "dev"):
            print(
                "Error: test override flags require REDLINE_ALLOW_TEST_OVERRIDES=true "
                "and REDLINE_ENV=dev",
                file=sys.stderr,
            )
            sys.exit(1)
        logger.warning(
            "TEST_OVERRIDE_ACTIVE",
            force_btc_signal=force_btc,
            force_eth_signal=force_eth,
            force_gross_usd=force_gross,
        )
        alerts.send_alert(
            "TEST_OVERRIDE_ACTIVE",
            {
                "force_btc_signal": force_btc,
                "force_eth_signal": force_eth,
                "force_gross_usd": force_gross,
            },
            dry_run,
        )

    # ------------------------------------------------------------------ #
    # 2a. Fetch candles (needed for current prices)                        #
    # ------------------------------------------------------------------ #
    data_provider = CoinbaseDataProvider(cfg.coinbase_api_key, cfg.coinbase_api_secret, cfg.mock)
    candles: dict = {}
    for product in PRODUCTS:
        try:
            candles[product] = data_provider.fetch_candles(product, min_count=250)
            logger.info("candles_fetched", product=product, count=len(candles[product]))
        except Exception as exc:
            logger.error("candle_fetch_failed", product=product, error=str(exc))
            alerts.send_alert("HALT_TRIGGERED", {"reason": f"candle_fetch_failed:{product}"}, dry_run)
            sys.exit(1)

    # Use last candle close as current price
    prices: dict[str, float] = {}
    for product in PRODUCTS:
        base = product.split("-")[0]
        if candles.get(product):
            prices[base] = sorted(candles[product], key=lambda c: c.ts_utc)[-1].close

    # ------------------------------------------------------------------ #
    # 2b. Reconcile balances and compute equity                            #
    # ------------------------------------------------------------------ #
    broker = CoinbaseSpotBroker(cfg.coinbase_api_key, cfg.coinbase_api_secret, cfg.mock)
    balances = broker.fetch_balances()
    equity = compute_equity(balances, prices)
    logger.info(
        "equity_computed",
        equity_usd=round(equity, 2),
        prices=prices,
        balances={k: v.available for k, v in balances.items()},
    )

    # ------------------------------------------------------------------ #
    # 3. Evaluate circuit breakers                                         #
    # ------------------------------------------------------------------ #
    equity_history = store.load_equity_history(days=2)
    peak_equity = store.load_peak_equity() or equity

    triggered, cb_reason = evaluate_circuit_breakers(
        equity, peak_equity, equity_history, cfg.max_daily_loss, cfg.max_drawdown
    )
    if triggered:
        logger.warning("circuit_breaker_triggered", reason=cb_reason)

        constraints = {p: data_provider.fetch_product(p) for p in PRODUCTS}
        liq_orders = plan_liquidation_orders(balances, prices, constraints, cfg.min_trade_usd)
        policy = DryRunPolicy(logger)
        executed = policy.execute(liq_orders)

        drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
        store.set_halt(
            cb_reason or "circuit_breaker",
            run_id=run_id,
            details_json=json.dumps({"equity_usd": equity, "peak_equity_usd": peak_equity, "drawdown": f"{drawdown:.4f}"}),
        )
        store.save_equity_snapshot(EquitySnapshot(run_id=run_id, ts_utc=utcnow_iso(), equity_usd=equity))
        store.save_run(
            RunRecord(
                run_id=run_id,
                ts_utc=utcnow_iso(),
                status=RunStatus.HALTED,
                dry_run=dry_run,
                equity_usd=equity,
                peak_equity_usd=peak_equity,
                drawdown=drawdown,
                halt_flag=True,
                halt_reason=cb_reason,
                skip_reason="halted",
                positions={k: v.available for k, v in balances.items()},
                planned_orders=[o.to_dict() for o in executed],
            )
        )
        alerts.send_alert(
            "RISK_CIRCUIT_BREAKER",
            {
                "reason": cb_reason,
                "skip_reason": "halted",
                "equity_usd": f"{equity:.2f}",
                "peak_equity_usd": f"{peak_equity:.2f}",
                "drawdown": f"{drawdown:.2%}",
                "planned_liquidation_orders": len(executed),
            },
            dry_run,
        )
        sys.exit(0)

    # ------------------------------------------------------------------ #
    # 4. Compute SMA200 signals and annualized vols                        #
    # ------------------------------------------------------------------ #
    signals: dict[str, bool] = {}
    vols: dict[str, float] = {}
    for product in PRODUCTS:
        closes = close_prices(candles[product])
        try:
            signals[product] = sma200_signal(closes)
            vols[product] = annualized_vol(closes)
        except ValueError as exc:
            logger.error("signal_compute_failed", product=product, error=str(exc))
            alerts.send_alert("HALT_TRIGGERED", {"reason": f"insufficient_data:{product}"}, dry_run)
            sys.exit(1)
        logger.info(
            "signal_computed",
            product=product,
            risk_on=signals[product],
            vol_ann=round(vols[product], 4),
            last_close=round(closes[-1], 2),
        )

    # Apply per-asset signal overrides (ON/OFF) if active
    if _overrides_active(args):
        signals = _apply_signal_overrides(signals, force_btc, force_eth)
        logger.info(
            "signals_after_overrides",
            BTC_USD=signals.get("BTC-USD"),
            ETH_USD=signals.get("ETH-USD"),
        )

    # ------------------------------------------------------------------ #
    # 5. Compute target notionals (inverse-vol weights + vol scaling)      #
    # ------------------------------------------------------------------ #
    risk_on_assets = {p: vols[p] for p in PRODUCTS if signals[p]}
    target_notionals: dict[str, float] = {p: 0.0 for p in PRODUCTS}

    if risk_on_assets:
        weights = inverse_vol_weights(risk_on_assets)
        scaled = compute_target_notionals(
            equity,
            weights,
            risk_on_assets,
            cfg.target_vol,
            cfg.max_gross_notional,
            cfg.max_per_asset_notional,
        )
        target_notionals.update(scaled)

    # Apply forced gross USD override (replaces vol-target sizing)
    if force_gross is not None:
        target_notionals = _compute_forced_targets(
            force_gross, signals, equity, cfg.max_gross_notional, cfg.max_per_asset_notional
        )

    logger.info(
        "targets_computed",
        risk_on_assets=list(risk_on_assets.keys()),
        target_notionals={k: round(v, 2) for k, v in target_notionals.items()},
    )

    # ------------------------------------------------------------------ #
    # 6. Plan orders                                                        #
    # ------------------------------------------------------------------ #
    constraints = {p: data_provider.fetch_product(p) for p in PRODUCTS}
    planned = plan_orders(balances, target_notionals, prices, constraints, equity, cfg.min_trade_usd)

    # ------------------------------------------------------------------ #
    # 7. Execute                                                           #
    # ------------------------------------------------------------------ #
    submitted_orders_dicts: list[dict] = []

    if dry_run:
        policy = DryRunPolicy(logger)
        executed = policy.execute(planned)
    else:
        live_policy = LiveExecutionPolicy(
            store=store,
            broker=broker,
            run_id=run_id,
            logger=logger,
            timeout_s=60.0,
        )
        try:
            executed = live_policy.execute(planned)
            submitted_orders_dicts = [o.to_dict() for o in executed]
        except ExecutionHaltError as exc:
            logger.error("execution_halt", reason=exc.reason)
            drawdown_halt = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            store.set_halt(
                exc.reason,
                run_id=run_id,
                details_json=json.dumps({"error": exc.reason, "equity_usd": equity, "drawdown": f"{drawdown_halt:.4f}"}),
            )
            store.save_equity_snapshot(
                EquitySnapshot(run_id=run_id, ts_utc=utcnow_iso(), equity_usd=equity)
            )
            store.save_run(
                RunRecord(
                    run_id=run_id,
                    ts_utc=utcnow_iso(),
                    status=RunStatus.HALTED,
                    dry_run=dry_run,
                    equity_usd=equity,
                    peak_equity_usd=peak_equity,
                    drawdown=drawdown_halt,
                    halt_flag=True,
                    halt_reason=exc.reason,
                    skip_reason="halted",
                    signals=signals,
                    vols=vols,
                    targets=target_notionals,
                    planned_orders=[o.to_dict() for o in planned],
                    submitted_orders=[],
                    positions={k: v.available for k, v in balances.items()},
                )
            )
            alerts.send_alert(
                "HALT_TRIGGERED",
                {
                    "reason": exc.reason,
                    "equity_usd": f"{equity:.2f}",
                    "peak_equity_usd": f"{peak_equity:.2f}",
                    "drawdown": f"{drawdown_halt:.2%}",
                },
                dry_run,
            )
            sys.exit(0)

    # ------------------------------------------------------------------ #
    # 8. Persist                                                           #
    # ------------------------------------------------------------------ #
    reason = _skip_reason(signals, target_notionals, balances, prices, executed, cfg.min_trade_usd)
    new_peak = max(peak_equity, equity)
    drawdown = (new_peak - equity) / new_peak if new_peak > 0 else 0.0
    store.save_equity_snapshot(EquitySnapshot(run_id=run_id, ts_utc=utcnow_iso(), equity_usd=equity))
    store.save_run(
        RunRecord(
            run_id=run_id,
            ts_utc=utcnow_iso(),
            status=RunStatus.OK,
            dry_run=dry_run,
            equity_usd=equity,
            peak_equity_usd=new_peak,
            drawdown=drawdown,
            halt_flag=False,
            skip_reason=reason,
            signals=signals,
            vols=vols,
            targets=target_notionals,
            planned_orders=[o.to_dict() for o in planned],
            submitted_orders=submitted_orders_dicts,
            positions={k: v.available for k, v in balances.items()},
            force_btc_signal=force_btc,
            force_eth_signal=force_eth,
            force_gross_usd=force_gross,
        )
    )

    # ------------------------------------------------------------------ #
    # 9. Alert                                                             #
    # ------------------------------------------------------------------ #
    rebalance_data: dict = {
        "equity_usd": f"{equity:.2f}",
        "peak_equity_usd": f"{new_peak:.2f}",
        "drawdown": f"{drawdown:.2%}",
        "BTC_risk_on": signals.get("BTC-USD"),
        "ETH_risk_on": signals.get("ETH-USD"),
        "BTC_target_notional": f"{target_notionals.get('BTC-USD', 0):.2f}",
        "ETH_target_notional": f"{target_notionals.get('ETH-USD', 0):.2f}",
        "orders": len(executed) if executed else "none",
        "skip_reason": reason,
    }
    if _overrides_active(args):
        rebalance_data["TEST_OVERRIDE"] = (
            f"btc={force_btc} eth={force_eth} gross_usd={force_gross}"
        )
    alerts.send_alert(
        "REBALANCE_COMPLETED",
        rebalance_data,
        dry_run,
    )

    logger.info(
        "run_complete",
        run_id=run_id,
        status="ok",
        equity_usd=round(equity, 2),
        orders_planned=len(executed),
        skip_reason=reason,
    )


def cmd_status(cfg: Config) -> None:
    store = Store(cfg.db_path)
    halt_flag, halt_reason, halted_at = store.get_halt_state_full()
    last = store.load_last_run()

    broker = CoinbaseSpotBroker(cfg.coinbase_api_key, cfg.coinbase_api_secret, cfg.mock)
    try:
        balances = broker.fetch_balances()
    except Exception as exc:
        balances = {}
        print(f"[warn] Could not fetch live balances: {exc}")

    print("=== Redline Status ===")
    print(f"Halt flag  : {halt_flag}")
    if halt_reason:
        print(f"Halt reason: {halt_reason}")
    if halted_at:
        print(f"Halted at  : {halted_at}")

    halt_events = store.load_halt_events(limit=20)
    last_triggered = next((e for e in halt_events if e["event_type"] == "HALT_TRIGGERED"), None)
    last_reset = next((e for e in halt_events if e["event_type"] == "HALT_RESET"), None)
    if last_triggered:
        print(f"Last HALT_TRIGGERED: {last_triggered['ts_utc']} — {last_triggered['reason']}")
    if last_reset:
        print(f"Last HALT_RESET    : {last_reset['ts_utc']} — {last_reset['reason']}")

    if last:
        print(f"Last run   : {last.ts_utc}  [{last.status.value}]")
        print(f"Skip reason: {last.skip_reason or 'none (orders placed)'}")
        print(f"Equity     : ${last.equity_usd:,.2f}")
        print(f"Peak equity: ${last.peak_equity_usd:,.2f}")
        print(f"Drawdown   : {last.drawdown:.2%}")
    else:
        print("No previous runs found.")

    print("\n--- Live Balances ---")
    if balances:
        for asset, bal in sorted(balances.items()):
            print(f"  {asset:5s}: {bal.available:.8f} available")
    else:
        print("  (none)")

    if last:
        print("\n--- Last Signals ---")
        for product, risk_on in (last.signals or {}).items():
            print(f"  {product}: {'risk-ON ' if risk_on else 'risk-OFF'}")

        print("\n--- Last Targets ---")
        for product, notional in (last.targets or {}).items():
            print(f"  {product}: ${notional:,.2f}")

        if (
            last.force_btc_signal != "COMPUTED"
            or last.force_eth_signal != "COMPUTED"
            or last.force_gross_usd is not None
        ):
            print("\n--- Last Run Overrides ---")
            print(f"  force_btc_signal: {last.force_btc_signal}")
            print(f"  force_eth_signal: {last.force_eth_signal}")
            print(f"  force_gross_usd : {last.force_gross_usd}")


def cmd_reset_halt(args: argparse.Namespace, cfg: Config) -> None:
    store = Store(cfg.db_path)
    halt_flag, halt_reason, halted_at = store.get_halt_state_full()

    if not halt_flag:
        print("Not halted")
        sys.exit(0)

    reason = (args.reason or "").strip()
    if not reason:
        print("Error: --reason is required and must not be empty", file=sys.stderr)
        sys.exit(1)

    if args.require_reconcile:
        data_provider = CoinbaseDataProvider(cfg.coinbase_api_key, cfg.coinbase_api_secret, cfg.mock)
        broker = CoinbaseSpotBroker(cfg.coinbase_api_key, cfg.coinbase_api_secret, cfg.mock)
        try:
            candles: dict = {}
            for product in PRODUCTS:
                candles[product] = data_provider.fetch_candles(product, min_count=250)
            prices: dict[str, float] = {}
            for product in PRODUCTS:
                base = product.split("-")[0]
                if candles.get(product):
                    prices[base] = sorted(candles[product], key=lambda c: c.ts_utc)[-1].close
            balances = broker.fetch_balances()
            compute_equity(balances, prices)
        except Exception as exc:
            print(f"Reconcile failed: {exc}", file=sys.stderr)
            sys.exit(2)

    last_run = store.load_last_run()
    details = json.dumps({
        "previous_halt_reason": halt_reason,
        "previous_halted_at": halted_at,
    })
    store.log_halt_event(
        "HALT_RESET",
        reason,
        run_id=last_run.run_id if last_run else None,
        details_json=details,
    )
    store.clear_halt()
    print(f"Halt cleared. Reason: {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m redline.main", description="Redline BTC/ETH trading bot"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run a rebalance cycle")
    run_p.add_argument("--dry-run", action="store_true", default=False)
    run_p.add_argument("--mock", action="store_true", default=False, help="Use synthetic data (no Coinbase credentials needed)")
    _signal_choices = ["COMPUTED", "ON", "OFF"]
    run_p.add_argument(
        "--force-btc-signal",
        choices=_signal_choices,
        default="COMPUTED",
        help="Override BTC-USD SMA200 signal for this run (requires REDLINE_ALLOW_TEST_OVERRIDES=true + REDLINE_ENV=dev)",
    )
    run_p.add_argument(
        "--force-eth-signal",
        choices=_signal_choices,
        default="COMPUTED",
        help="Override ETH-USD SMA200 signal for this run (requires REDLINE_ALLOW_TEST_OVERRIDES=true + REDLINE_ENV=dev)",
    )
    run_p.add_argument(
        "--force-gross-usd",
        type=float,
        default=None,
        help="Override desired gross notional USD for this run; 50/50 split when both assets risk-on (requires REDLINE_ALLOW_TEST_OVERRIDES=true + REDLINE_ENV=dev)",
    )

    status_p = sub.add_parser("status", help="Print status from SQLite + live balances")
    status_p.add_argument("--mock", action="store_true", default=False, help="Use synthetic data for live balances")

    reset_p = sub.add_parser("reset-halt", help="Clear a halt and write an audit record")
    reset_p.add_argument("--reason", required=True, help="Human-readable reason for clearing the halt")
    reset_p.add_argument(
        "--require-reconcile",
        action="store_true",
        default=False,
        help="Perform a read-only reconciliation check before clearing (exits 2 if it fails)",
    )
    reset_p.add_argument("--mock", action="store_true", default=False, help="Use synthetic data")

    args = parser.parse_args()
    cfg = load_config()
    if args.mock:
        cfg.mock = True

    if args.command == "run":
        cmd_run(args, cfg)
    elif args.command == "status":
        cmd_status(cfg)
    elif args.command == "reset-halt":
        cmd_reset_halt(args, cfg)


if __name__ == "__main__":
    main()
