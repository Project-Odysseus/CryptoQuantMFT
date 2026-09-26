"""Application entry point for the CryptoQuantMFT trading engine."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings
from src.backtest import BacktestConfig, EventDrivenSimulator, StrategyPlotter, compare_backtests, evaluate_walk_forward, resolve_strategy, run_backtest
from src.data.exchanges import FiriConnector, KrakenConnector, MockExchangeConnector, KrakenFuturesConnector
from src.execution import ExecutionRouter, KrakenExecutionAdapter, PaperTradingEngine
from src.execution.perps import SandboxPerpExecutionAdapter, assumed_perp_contract
from src.risk.controls import DEFAULT_EXCHANGE_RISK_LIMITS, RiskControlConfig, RiskManager
from src.risk.kill_switch import KillSwitchController
from src.data.historical import fetch_kraken_ohlcv
from src.data.pipeline import MarketDataPipeline
from src.runtime import RuntimeConfig, RuntimeOrchestrator, RuntimeWatchdogError, build_runtime_config_from_args
from src.storage.bar_aggregator import OHLCVBar
from src.storage.market_store import MarketStore
from src.storage.order_book import OrderBookSnapshot
from src.storage.trade_logger import TradeLogger
from src.utils.logger import logger

LIVE_TRADING_CONFIRMATION = "ENABLE_LIVE_TRADING"
PERP_EXCHANGE_NAME = "kraken_futures"
PERP_SANDBOX_STATE_DIR = Path("data")
LIVE_PERP_MAX_LEVERAGE = 3.0
KRAKEN_MANUAL_ORDER_CONFIRMATION = "SUBMIT_KRAKEN_ORDER"


async def run_pipeline(iterations: int = 3, interval_seconds: float = 1.0) -> None:
    """Run the data pipeline for a small number of cycles using the available connectors."""
    store = MarketStore(database_path=settings.database_path)
    pipeline = MarketDataPipeline(store=store, interval_seconds=max(1, int(round(interval_seconds))))

    kraken_symbol = _resolve_trading_symbol(exchange="kraken")
    pipeline.add_connector(KrakenConnector(symbol=kraken_symbol))

    if settings.firi_api_key:
        pipeline.add_connector(FiriConnector(symbol=_resolve_trading_symbol(exchange="firi")))

    for index in range(iterations):
        snapshots = await pipeline.run_once()
        bars = pipeline.flush_bars()
        logger.info(
            "pipeline_cycle=%s snapshots=%s bars=%s",
            index + 1,
            [snapshot.symbol for snapshot in snapshots],
            len(bars),
        )
        if index < iterations - 1:
            await asyncio.sleep(interval_seconds)


def build_runtime_orchestrator(
    *,
    config: RuntimeConfig | None = None,
    mode: str | None = None,
    interval_seconds: float | None = None,
    use_mock_connector: bool | None = None,
    watchdog_timeout_seconds: float | None = None,
    exchange: str | None = None,
    risk_per_trade_pct: float | None = None,
    perp_max_leverage: float = 2.0,
    perp_sandbox_reset: bool = False,
    target_annual_volatility: float | None = None,
) -> tuple[RuntimeOrchestrator, MarketDataPipeline]:
    """Build the runtime orchestrator and its market-data pipeline for a run."""
    runtime_config = config or RuntimeConfig(
        mode=mode or "paper",
        interval_seconds=interval_seconds or 1.0,
        use_mock_connector=use_mock_connector or False,
        watchdog_timeout_seconds=watchdog_timeout_seconds or 30.0,
        exchange=exchange,
    )

    market_data_interval_seconds = runtime_config.bar_interval_seconds or max(1, int(round(runtime_config.interval_seconds)))
    store = MarketStore(database_path=settings.database_path)
    pipeline = MarketDataPipeline(store=store, interval_seconds=market_data_interval_seconds)
    requested_exchange = _resolve_runtime_exchange(runtime_config.exchange)
    effective_exchange = requested_exchange
    is_perp = (runtime_config.exchange or "").lower() == PERP_EXCHANGE_NAME
    trading_symbol = runtime_config.trading_symbol or ("BTC/USD" if is_perp else _resolve_trading_symbol(exchange=requested_exchange))
    # Perpetuals need an exchange-backed cycle: live_dry_run uses the in-process margin sandbox, live the real
    # Kraken Futures adapter. Paper mode has no margin account, so it is refused rather than silently mis-simulated.
    if is_perp and runtime_config.mode not in {"live_dry_run", "live"}:
        raise SystemExit(f"--execution-exchange {PERP_EXCHANGE_NAME} runs with --runtime live_dry_run or live, not {runtime_config.mode}")

    if runtime_config.use_mock_connector:
        pipeline.add_connector(MockExchangeConnector(symbol=trading_symbol))
    elif is_perp:
        # Perps are priced, margined and liquidated on the contract's mark price, so bars come from it too.
        pipeline.add_connector(KrakenFuturesConnector(symbol=trading_symbol))
        effective_exchange = PERP_EXCHANGE_NAME
    else:
        if requested_exchange == "kraken":
            pipeline.add_connector(KrakenConnector(symbol=trading_symbol))
        elif requested_exchange == "firi" and settings.firi_api_key:
            pipeline.add_connector(FiriConnector(symbol=trading_symbol))
        elif requested_exchange == "firi":
            logger.warning("runtime_firi_api_key_missing falling back to kraken")
            effective_exchange = "kraken"
            fallback_symbol = runtime_config.trading_symbol or _resolve_trading_symbol(exchange=effective_exchange)
            pipeline.add_connector(KrakenConnector(symbol=fallback_symbol))

    logger.info(
        "runtime_market_data_config interval_seconds={} requested_exchange={} effective_exchange={} use_mock={}",
        market_data_interval_seconds,
        runtime_config.exchange or "auto",
        effective_exchange,
        runtime_config.use_mock_connector,
    )

    risk_manager = RiskManager(
        RiskControlConfig(
            max_drawdown_pct=0.25,
            max_volatility_pct=0.50,
            risk_per_trade_pct=risk_per_trade_pct if risk_per_trade_pct is not None else 0.10,
            target_annual_volatility=target_annual_volatility,
            max_position_size=1.0,
            volatility_window=10,
            kelly_fraction=0.5,
            kelly_window=20,
            max_slippage_pct=0.20,
            max_spread_pct=0.20,
            max_notional_per_trade=10000.0,
            max_total_notional=25000.0,
            paper_mode=(runtime_config.mode == "paper"),
            # Position-level exits and a rolling daily-loss circuit breaker,
            # distinct from the all-time peak drawdown above: a slow all-time
            # drawdown that never breaches hard_stop_drawdown_pct can still be
            # caught if a single day is bad enough on its own, and a stuck
            # position can't ride a signal that never reverses.
            daily_loss_limit_pct=0.05,
            time_stop_bars=60,
            atr_stop_multiplier=3.0,
            atr_window=14,
            # A plain percentage stop against entry, on top of the ATR stop
            # above: cuts a position (long or short) once it's down this much
            # from entry, regardless of ATR-implied distance.
            position_drawdown_stop_pct=0.05,
            # Perps only: cut a position when price is within 10% of its liquidation price.
            liquidation_buffer_pct=0.10 if is_perp else None,
        )
    )
    trade_logger = TradeLogger(database_path=settings.database_path)
    perp_adapter = _build_perp_adapter(mode=runtime_config.mode, symbol=trading_symbol, max_leverage=perp_max_leverage, reset_sandbox=perp_sandbox_reset, mock_prices=runtime_config.use_mock_connector) if is_perp else None
    execution_router = ExecutionRouter(mode=runtime_config.mode, exchange=effective_exchange, adapter=perp_adapter)

    # --runtime live starts a brand-new adapter with no local balance/position
    # state. Without this, equity/cash would silently read as 0 (or whatever
    # the adapter's constructor default is) and get compared against a
    # hardcoded 1000.0 "peak equity" reference that has nothing to do with
    # the real account - producing a fake ~100% drawdown that blocks every
    # entry. Fail closed rather than guess: if the real balance can't be
    # fetched and reconciled, refuse to start live trading at all.
    initial_cash = 1000.0
    if runtime_config.mode == "live" and execution_router.adapter is not None:
        fetch_balance_snapshot = getattr(execution_router.adapter, "fetch_balance_snapshot", None)
        if not callable(fetch_balance_snapshot):
            raise SystemExit("refusing to start --runtime live: execution adapter cannot fetch a real account balance")
        try:
            balance_snapshot = fetch_balance_snapshot()
        except Exception as exc:
            raise SystemExit(f"refusing to start --runtime live: could not fetch real Kraken balance to initialize equity ({exc})")
        execution_router.adapter.reconcile_account_state(
            balances=balance_snapshot.get("balances"),
            positions=balance_snapshot.get("positions"),
        )
        base_currency = getattr(execution_router.adapter, "_base_currency", "EUR")
        initial_cash = float((balance_snapshot.get("balances") or {}).get(base_currency, 0.0))
        if initial_cash <= 0.0:
            raise SystemExit(f"refusing to start --runtime live: fetched {base_currency} balance is {initial_cash}, nothing to trade with")
        logger.info("runtime_live_balance_initialized base_currency={} initial_cash={}", base_currency, initial_cash)

    engine = PaperTradingEngine(
        initial_cash=initial_cash,
        default_order_size=1.0,
        partial_fill_fraction=1.0,
        max_order_lifetime_bars=3,
        risk_manager=risk_manager,
        trade_logger=trade_logger,
        execution_adapter=execution_router.adapter,
        exchange_name=PERP_EXCHANGE_NAME if is_perp else effective_exchange,
        # The tax ledger models spot FIFO lots only; derivative P&L is not written into it.
        enable_tax_logging=(runtime_config.mode == "live" and not is_perp),
        record_derivative_ledger=(runtime_config.mode == "live" and is_perp),
        strategy_id=runtime_config.strategy_name,
        # PaperTradingEngine.run() (used by "paper") has always allowed
        # shorting unconditionally, regardless of this flag - it only gates
        # the exchange-backed run_exchange_cycle() path (live_dry_run/live).
        # Reflect that here so the startup banner is accurate for paper too.
        # live_dry_run always routes through the sandbox adapter regardless
        # of exchange, so enabling it there can never reach a real order;
        # --runtime live must never get allow_short=True (real Kraken/Firi
        # spot has no margin/short capability).
        allow_short=(runtime_config.mode in {"paper", "live_dry_run"}),
    )
    strategy = resolve_strategy(runtime_config.strategy_name, **runtime_config.strategy_params)
    # Derive the primary trading symbol from the first connector so bars and
    # live-price tracking stay consistent with a single currency.
    primary_symbol: str | None = None
    if pipeline.connectors:
        primary_symbol = getattr(pipeline.connectors[0], "symbol", None)
    orchestrator = RuntimeOrchestrator(
        pipeline=pipeline,
        execution_engine=engine,
        strategy=strategy,
        mode=runtime_config.mode,
        interval_seconds=runtime_config.interval_seconds,
        watchdog_timeout_seconds=runtime_config.watchdog_timeout_seconds,
        trade_logger=trade_logger,
        strategy_name=runtime_config.strategy_name,
        strategy_params=runtime_config.strategy_params,
        runtime_config=runtime_config,
        checkpoint_path=runtime_config.state_path,
        live_plot=runtime_config.live_plot,
        live_plot_path=runtime_config.live_plot_path,
        trading_symbol=primary_symbol,
        bar_interval_seconds=runtime_config.bar_interval_seconds,
    )
    if runtime_config.bar_interval_seconds and runtime_config.warmup_bars > 0 and not runtime_config.use_mock_connector:
        try:
            history = _load_warmup_bars(is_perp=is_perp, symbol=primary_symbol, interval_seconds=runtime_config.bar_interval_seconds, count=runtime_config.warmup_bars)
            seeded = orchestrator.seed_history(history)
            logger.info("runtime_history_seeded bars={} interval_seconds={} first={} last={}", seeded, runtime_config.bar_interval_seconds, history[0].timestamp if history else None, history[-1].timestamp if history else None)
        except Exception as exc:
            logger.warning("runtime_history_seed_failed error={} strategies will wait for enough live bars before signalling", exc)
    return orchestrator, pipeline


def _load_warmup_bars(*, is_perp: bool, symbol: str, interval_seconds: int, count: int) -> list[Any]:
    """The last `count` completed bars for the runtime symbol: futures mark candles for perps, spot OHLC otherwise."""
    if is_perp:
        from src.data.kraken_futures import fetch_candles, venue_symbol_for

        return fetch_candles(venue_symbol_for(symbol), interval_seconds=interval_seconds, count=count, symbol=symbol)
    bars = fetch_kraken_ohlcv(symbol=symbol, interval_seconds=interval_seconds, count=min(count + 1, 720))
    now = datetime.now(timezone.utc)
    completed = [bar for bar in bars if bar.timestamp.timestamp() + interval_seconds <= now.timestamp()]
    return completed[-count:]


def _build_perp_adapter(*, mode: str, symbol: str, max_leverage: float, reset_sandbox: bool = False, mock_prices: bool = False) -> Any:
    """The margin adapter for a perpetual run: sandbox for live_dry_run, the real Kraken Futures API for live.

    Live fails closed: it needs futures API credentials and a contract spec
    fetched from Kraken's public data. A dry run uses the real spec when it
    can fetch it and the offline placeholder otherwise.
    """
    from src.data.kraken_futures import fetch_fee_schedules, fetch_instrument, venue_symbol_for
    from src.execution.kraken_futures_adapter import KrakenFuturesExecutionAdapter
    from src.execution.perps import perp_contract_from_instrument

    def verified_contract():
        instrument = fetch_instrument(venue_symbol_for(symbol))
        return perp_contract_from_instrument(instrument, fetch_fee_schedules()[instrument["feeScheduleUid"]], symbol=symbol)

    if mode == "live":
        if not settings.kraken_futures_api_key or not settings.kraken_futures_secret:
            raise SystemExit("refusing to start --runtime live with kraken_futures: KRAKEN_FUTURES_API_KEY / KRAKEN_FUTURES_SECRET are not set")
        try:
            contract = verified_contract()
        except Exception as exc:
            raise SystemExit(f"refusing to start --runtime live with kraken_futures: could not load the contract spec for {symbol} ({exc})")
        return KrakenFuturesExecutionAdapter(contract=contract, api_key=settings.kraken_futures_api_key, api_secret=settings.kraken_futures_secret, max_leverage=max_leverage)

    try:
        contract = verified_contract()
    except Exception as exc:
        logger.warning("perp_contract_spec_unavailable symbol={} error={} using offline placeholder", symbol, exc)
        contract = assumed_perp_contract(symbol)
    # Mock-connector runs trade on invented prices, so they keep their own account file.
    state_path = PERP_SANDBOX_STATE_DIR / f"perp_sandbox_{contract.venue_symbol}{'_mock' if mock_prices else ''}.json"
    if reset_sandbox and state_path.exists():
        backup = state_path.with_name(f"{state_path.stem}.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.bak.json")
        state_path.replace(backup)
        logger.info("perp_sandbox_reset previous_state_moved_to={}", backup)
    adapter = SandboxPerpExecutionAdapter(contract=contract, max_leverage=max_leverage, state_path=state_path)
    if adapter.restored_from_state:
        logger.info("perp_sandbox_restored path={} wallet={} position={}", state_path, adapter.wallet_balance(), adapter.position_size())
    return adapter


def _resolve_trading_symbol(*, exchange: str | None = None) -> str:
    normalized_exchange = (exchange or "kraken").lower()
    if normalized_exchange == "firi":
        return "BTC/NOK"
    return "BTC/EUR"


def _resolve_runtime_exchange(exchange: str | None) -> str:
    normalized_exchange = (exchange or "kraken").lower()
    if normalized_exchange == "auto":
        return "kraken"
    if normalized_exchange in {"kraken", "firi"}:
        return normalized_exchange
    return "kraken"


def _validate_live_runtime_request(
    *,
    runtime_config: RuntimeConfig,
    use_mock_connector: bool,
    enable_live_trading: bool,
    live_confirmation: str | None,
    kill_switch_controller: KillSwitchController | None = None,
    perp_max_leverage: float = 2.0,
) -> None:
    if runtime_config.mode != "live":
        return
    if not enable_live_trading:
        raise SystemExit("refusing to start --runtime live without --enable-live-trading")
    if live_confirmation != LIVE_TRADING_CONFIRMATION:
        raise SystemExit(f"refusing to start --runtime live without --live-confirmation {LIVE_TRADING_CONFIRMATION}")
    if use_mock_connector:
        raise SystemExit("refusing to start --runtime live with --use-mock-connector")
    if runtime_config.exchange is None:
        raise SystemExit("refusing to start --runtime live without an explicit --execution-exchange")
    if runtime_config.exchange.lower() == PERP_EXCHANGE_NAME:
        if perp_max_leverage > LIVE_PERP_MAX_LEVERAGE:
            raise SystemExit(f"refusing to start --runtime live with {PERP_EXCHANGE_NAME} above {LIVE_PERP_MAX_LEVERAGE:g}x leverage (requested {perp_max_leverage:g}x)")
        if not settings.kraken_futures_api_key or not settings.kraken_futures_secret:
            raise SystemExit(f"refusing to start --runtime live with {PERP_EXCHANGE_NAME}: KRAKEN_FUTURES_API_KEY / KRAKEN_FUTURES_SECRET are not set")

    exchange_name = _resolve_runtime_exchange(runtime_config.exchange)
    if exchange_name not in DEFAULT_EXCHANGE_RISK_LIMITS:
        raise SystemExit(f"refusing to start --runtime live because no exchange risk limits are defined for {exchange_name}")

    exchange_caps = DEFAULT_EXCHANGE_RISK_LIMITS[exchange_name]
    if float(exchange_caps.get("max_position_size", 0.0)) > 0.5:
        raise SystemExit("refusing to start --runtime live because max_position_size exceeds the live safety ceiling")
    if float(exchange_caps.get("max_notional_per_trade", 0.0)) > 500.0:
        raise SystemExit("refusing to start --runtime live because max_notional_per_trade exceeds the live safety ceiling")
    if float(exchange_caps.get("max_total_notional", 0.0)) > 2500.0:
        raise SystemExit("refusing to start --runtime live because max_total_notional exceeds the live safety ceiling")
    if int(exchange_caps.get("max_open_positions", 0)) > 1 or int(exchange_caps.get("max_open_orders", 0)) > 2:
        raise SystemExit("refusing to start --runtime live because exchange order/position caps exceed the live safety ceiling")

    controller = kill_switch_controller or KillSwitchController()
    readiness = controller.ensure_ready()
    if not readiness["ready"]:
        raise SystemExit("refusing to start --runtime live because kill-switch state is not ready")
    if readiness["active"]:
        raise SystemExit("refusing to start --runtime live while the kill switch is active")


def _validate_kraken_manual_submit_request(
    *,
    symbol: str,
    side: str,
    quote_amount: float,
    enable_live_trading: bool,
    live_confirmation: str | None,
    submit_confirmation: str | None,
    kill_switch_controller: KillSwitchController | None = None,
) -> None:
    if not enable_live_trading:
        raise SystemExit("refusing to submit a manual Kraken order without --enable-live-trading")
    if live_confirmation != LIVE_TRADING_CONFIRMATION:
        raise SystemExit(f"refusing to submit a manual Kraken order without --live-confirmation {LIVE_TRADING_CONFIRMATION}")
    if submit_confirmation != KRAKEN_MANUAL_ORDER_CONFIRMATION:
        raise SystemExit(
            f"refusing to submit a manual Kraken order without --kraken-submit-confirmation {KRAKEN_MANUAL_ORDER_CONFIRMATION}"
        )
    if side.lower() != "buy":
        raise SystemExit("refusing to submit a manual Kraken order because only buy is currently supported")
    if quote_amount <= 0.0:
        raise SystemExit("refusing to submit a manual Kraken order because --kraken-submit-quote-amount must be positive")
    if not symbol.upper().endswith("/EUR"):
        raise SystemExit("refusing to submit a manual Kraken order because only EUR-quoted symbols are currently supported")

    kraken_caps = DEFAULT_EXCHANGE_RISK_LIMITS.get("kraken", {})
    max_notional = float(kraken_caps.get("max_notional_per_trade", 500.0))
    if quote_amount > max_notional:
        raise SystemExit(
            f"refusing to submit a manual Kraken order because the requested notional exceeds the Kraken safety ceiling of {max_notional}"
        )

    controller = kill_switch_controller or KillSwitchController()
    readiness = controller.ensure_ready()
    if not readiness["ready"]:
        raise SystemExit("refusing to submit a manual Kraken order because kill-switch state is not ready")
    if readiness["active"]:
        raise SystemExit("refusing to submit a manual Kraken order while the kill switch is active")


def _validate_kraken_manual_close_request(
    *,
    symbol: str,
    enable_live_trading: bool,
    live_confirmation: str | None,
    close_confirmation: str | None,
    kill_switch_controller: KillSwitchController | None = None,
) -> None:
    if not enable_live_trading:
        raise SystemExit("refusing to close a manual Kraken position without --enable-live-trading")
    if live_confirmation != LIVE_TRADING_CONFIRMATION:
        raise SystemExit(f"refusing to close a manual Kraken position without --live-confirmation {LIVE_TRADING_CONFIRMATION}")
    if close_confirmation != KRAKEN_MANUAL_ORDER_CONFIRMATION:
        raise SystemExit(
            f"refusing to close a manual Kraken position without --kraken-close-confirmation {KRAKEN_MANUAL_ORDER_CONFIRMATION}"
        )
    if not symbol.upper().endswith("/EUR"):
        raise SystemExit("refusing to close a manual Kraken position because only EUR-quoted symbols are currently supported")

    controller = kill_switch_controller or KillSwitchController()
    readiness = controller.ensure_ready()
    if not readiness["ready"]:
        raise SystemExit("refusing to close a manual Kraken position because kill-switch state is not ready")
    if readiness["active"]:
        raise SystemExit("refusing to close a manual Kraken position while the kill switch is active")


async def run_runtime_orchestrator(
    *,
    config: RuntimeConfig | None = None,
    mode: str = "paper",
    iterations: int = 3,
    interval_seconds: float = 1.0,
    use_mock_connector: bool = False,
    watchdog_timeout_seconds: float = 30.0,
    watchdog_restarts: int = 0,
    exchange: str | None = None,
    resume_runtime: bool = False,
    risk_per_trade_pct: float | None = None,
    perp_max_leverage: float = 2.0,
    perp_sandbox_reset: bool = False,
    target_annual_volatility: float | None = None,
) -> RuntimeOrchestrator | None:
    """Run the runtime orchestrator over a simple market-data pipeline."""
    runtime_config = config or RuntimeConfig(
        mode=mode,
        iterations=iterations,
        interval_seconds=interval_seconds,
        use_mock_connector=use_mock_connector,
        watchdog_timeout_seconds=watchdog_timeout_seconds,
        watchdog_restarts=watchdog_restarts,
        exchange=exchange,
    )
    restart_attempts = max(0, runtime_config.watchdog_restarts) + 1
    last_orchestrator: RuntimeOrchestrator | None = None

    for attempt in range(restart_attempts):
        orchestrator, _pipeline = build_runtime_orchestrator(
            config=runtime_config,
            mode=runtime_config.mode,
            interval_seconds=runtime_config.interval_seconds,
            use_mock_connector=runtime_config.use_mock_connector,
            watchdog_timeout_seconds=runtime_config.watchdog_timeout_seconds,
            exchange=runtime_config.exchange,
            risk_per_trade_pct=risk_per_trade_pct,
            perp_max_leverage=perp_max_leverage,
            perp_sandbox_reset=perp_sandbox_reset and attempt == 0,
            target_annual_volatility=target_annual_volatility,
        )
        loop = asyncio.get_running_loop()

        def _request_shutdown(signum: int) -> None:
            logger.warning("runtime_shutdown_signal signal={}", signum)
            orchestrator.request_shutdown(reason=f"signal:{signum}")

        def _install_signal_handlers() -> None:
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, lambda current_signum=signum: _request_shutdown(current_signum))
                except (AttributeError, NotImplementedError):
                    signal.signal(signum, lambda current_signum, _frame: _request_shutdown(current_signum))

        def _remove_signal_handlers() -> None:
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.remove_signal_handler(signum)
                except (AttributeError, NotImplementedError):
                    pass

        _install_signal_handlers()
        try:
            await orchestrator.run_loop(
                iterations=runtime_config.iterations,
                interval_seconds=runtime_config.interval_seconds,
                resume_from_checkpoint=resume_runtime,
            )
        except RuntimeWatchdogError as exc:
            orchestrator.request_shutdown(reason="watchdog_timeout")
            logger.warning("runtime_watchdog_triggered attempt={} reason={}", attempt + 1, exc)
            if attempt + 1 >= restart_attempts:
                raise
            logger.info("runtime_restarting attempt={} of={}", attempt + 2, restart_attempts)
            continue
        except (asyncio.CancelledError, KeyboardInterrupt) as exc:
            orchestrator.request_shutdown(reason="interrupted")
            logger.warning("runtime_interrupted error={}", exc)
        finally:
            _remove_signal_handlers()

        last_orchestrator = orchestrator
        break

    if last_orchestrator is None:
        logger.info("runtime_complete mode={} iterations={} completed=0", runtime_config.mode, runtime_config.iterations)
        return None

    logger.info("runtime_health_report {}", last_orchestrator.get_health_report())

    last_cycle = last_orchestrator.last_cycle
    if last_cycle is None or last_cycle.execution_result is None:
        logger.info("runtime_complete mode={} iterations={} completed=0", runtime_config.mode, runtime_config.iterations)
        return last_orchestrator

    portfolio_history = last_cycle.execution_result.portfolio_history
    final_equity = portfolio_history[-1].equity if portfolio_history else 1000.0
    logger.info(
        "runtime_complete mode={} iterations={} trades={} final_equity={}",
        runtime_config.mode,
        runtime_config.iterations,
        len(last_cycle.execution_result.trades),
        final_equity,
    )
    return last_orchestrator


def build_demo_bars(count: int = 60) -> list[OHLCVBar]:
    """Create a synthetic OHLCV series for a simple demo backtest."""
    closes: list[float] = []
    close = 100.0
    for index in range(count):
        close = close + (0.6 if index % 3 else -0.2) + (0.15 if index % 5 == 0 else 0.0)
        closes.append(close)

    bars: list[OHLCVBar] = []
    for index, close in enumerate(closes):
        timestamp = datetime(2024, 1, 1, 0, index, tzinfo=timezone.utc)
        bars.append(
            OHLCVBar(
                exchange="demo",
                symbol="BTC/NOK",
                interval_seconds=60,
                timestamp=timestamp,
                open=close - 0.5,
                high=close + 1.5,
                low=close - 1.5,
                close=close,
                volume=10.0 + index,
            )
        )
    return bars


def build_kraken_bars(symbol: str = "BTC/EUR", count: int = 200) -> list[OHLCVBar]:
    """Fetch recent Kraken OHLCV bars for the configured backtest."""
    return fetch_kraken_ohlcv(symbol=symbol, interval_seconds=60, count=count)


def build_demo_order_book_snapshots() -> list[OrderBookSnapshot]:
    """Create a small synthetic sequence of L2-style order book snapshots."""
    mid_prices = [100.0, 101.0, 99.5, 100.8, 102.0, 101.0]
    snapshots: list[OrderBookSnapshot] = []
    for index, mid_price in enumerate(mid_prices):
        spread = 0.6 + (index * 0.05)
        half_spread = spread / 2.0
        bids = [(mid_price - half_spread, 0.8), (mid_price - half_spread - 0.2, 0.6), (mid_price - half_spread - 0.4, 0.4)]
        asks = [(mid_price + half_spread, 0.8), (mid_price + half_spread + 0.2, 0.6), (mid_price + half_spread + 0.4, 0.4)]
        snapshots.append(
            OrderBookSnapshot(
                bids=bids,
                asks=asks,
                timestamp=datetime(2024, 1, 1, 0, index, tzinfo=timezone.utc),
            )
        )
    return snapshots


def build_demo_l2_signals(snapshots: list[OrderBookSnapshot]) -> list[float]:
    """Create simple momentum-based signals from synthetic order-book snapshots."""
    if not snapshots:
        return []

    signals: list[float] = []
    previous_mid = None
    for snapshot in snapshots:
        mid_price = (snapshot.bids[0][0] + snapshot.asks[0][0]) / 2.0 if snapshot.bids and snapshot.asks else 0.0
        if previous_mid is None:
            signals.append(0.0)
        elif mid_price > previous_mid:
            signals.append(1.0)
        else:
            signals.append(-1.0)
        previous_mid = mid_price
    return signals


def run_demo_backtest(output_dir: str | Path = "plots", bars: list[OHLCVBar] | None = None) -> None:
    """Run a small synthetic backtest and generate equity/trade plots."""
    bars = bars or build_demo_bars()
    config = BacktestConfig(strategy_name="moving_average_crossover", include_costs=False)
    result = run_backtest(bars, config=config)

    plotter = StrategyPlotter(output_dir=output_dir)
    equity_path = plotter.plot_equity_curve(
        result.equity_series,
        timestamps=result.timestamps,
        title="Demo Signal Equity Curve",
    )
    trade_path = plotter.plot_equity_and_trades(
        result.equity_series,
        result.trade_prices,
        timestamps=result.timestamps,
        trade_timestamps=result.trade_timestamps,
        price_series=[bar.close for bar in bars],
        price_timestamps=[bar.timestamp for bar in bars],
        title="Demo Signal Equity and Trades",
    )

    logger.info(
        "demo_backtest_complete total_return={} trades={} final_equity={} sharpe={} sortino={} max_drawdown={} profit_factor={} equity_plot={} trade_plot={}",
        result.total_return,
        result.trades,
        result.final_equity,
        result.metrics.sharpe_ratio,
        result.metrics.sortino_ratio,
        result.metrics.max_drawdown,
        result.metrics.profit_factor,
        equity_path,
        trade_path,
    )


def run_l2_simulation() -> None:
    """Run the lightweight L2 simulator over synthetic order-book snapshots."""
    snapshots = build_demo_order_book_snapshots()
    signals = build_demo_l2_signals(snapshots)
    simulator = EventDrivenSimulator(
        latency_ms=200,
        max_slippage=0.01,
        initial_equity=1000.0,
        position_size=1.0,
        queue_position_penalty=0.0002,
        impact_penalty=0.001,
        adverse_selection_penalty=0.0005,
    )
    trades, equity_curve = simulator.run(snapshots, signals)

    logger.info(
        "l2_simulator_complete trades={} final_equity={} first_equity={}",
        len(trades),
        equity_curve[-1] if equity_curve else 1000.0,
        equity_curve[0] if equity_curve else 1000.0,
    )


def run_paper_trading(
    bars: list[OHLCVBar],
    signals: list[float | int | str | None] | None = None,
    *,
    strategy_name: str = "moving_average_crossover",
    strategy_params: dict | None = None,
) -> None:
    """Run the paper-trading engine over bars and signals."""
    if signals is None:
        strategy = resolve_strategy(strategy_name, **(strategy_params or {}))
        signals = []
        for index in range(len(bars)):
            history = list(bars[: index + 1])
            signals.append(strategy(history, index, bars[index]))

    risk_manager = RiskManager(
        RiskControlConfig(
            max_drawdown_pct=0.25,
            max_volatility_pct=0.10,
            risk_per_trade_pct=0.10,
            max_position_size=1.0,
            volatility_window=10,
            kelly_fraction=0.5,
            kelly_window=20,
        )
    )
    trade_logger = TradeLogger(database_path="data/trades.db")
    engine = PaperTradingEngine(
        initial_cash=1000.0,
        default_order_size=1.0,
        partial_fill_fraction=1.0,
        max_order_lifetime_bars=3,
        risk_manager=risk_manager,
        trade_logger=trade_logger,
    )
    result = engine.run(bars, signals)
    final_equity = result.portfolio_history[-1].equity if result.portfolio_history else 1000.0

    logger.info(
        "paper_trading_complete orders={} trades={} final_equity={} final_cash={} final_position={}",
        len(result.orders),
        len(result.trades),
        final_equity,
        result.portfolio_history[-1].cash if result.portfolio_history else 1000.0,
        result.portfolio_history[-1].position_size if result.portfolio_history else 0.0,
    )


def print_trade_report(limit: int = 10) -> None:
    """Print the most recent trades, equity snapshots, and operational events from the SQLite logger."""
    logger = TradeLogger(database_path=settings.database_path)
    trades = logger.list_trades(limit=limit)
    snapshots = logger.list_equity_snapshots(limit=limit)
    events = logger.list_events(limit=limit)

    print(f"Recent trades (last {len(trades)}):")
    if not trades:
        print("  (none)")
    else:
        for trade in trades:
            print(
                f"  {trade['timestamp']} | {trade['side']} {trade['pair']} @ {trade['price']:.4f} size={trade['size']:.4f} fee={trade['fee']:.4f}"
            )

    print(f"\nRecent equity snapshots (last {len(snapshots)}):")
    if not snapshots:
        print("  (none)")
    else:
        for snapshot in snapshots:
            print(
                f"  {snapshot['timestamp']} | equity={snapshot['equity']:.4f} cash={snapshot['cash']:.4f} position={snapshot['position_size']:.4f}"
            )

    print(f"\nRecent operational events (last {len(events)}):")
    if not events:
        print("  (none)")
    else:
        for event in events:
            print(
                f"  {event['timestamp']} | [{event['level']}] {event['event_type']} | {event['message']}"
            )


def print_health_dashboard(
    *,
    limit: int = 10,
    runtime_config: RuntimeConfig | None = None,
    orchestrator: RuntimeOrchestrator | None = None,
) -> None:
    """Print a compact health dashboard for the current runtime state."""
    logger_store = TradeLogger(database_path=settings.database_path)
    events = logger_store.list_events(limit=limit)

    latest_event = events[0] if events else None
    latest_trade = logger_store.list_trades(limit=1)[0] if logger_store.list_trades(limit=1) else None
    latest_snapshot = logger_store.list_equity_snapshots(limit=1)[0] if logger_store.list_equity_snapshots(limit=1) else None

    report = orchestrator.get_operational_report(limit=limit) if orchestrator is not None else None

    print("Runtime health dashboard")
    print("-" * 28)
    if report is not None:
        runtime_info = report["runtime"]
        print(f"Mode: {runtime_info['mode']}")
        print(f"Strategy: {runtime_info['strategy_name']} ({runtime_info['strategy_params']})")
        print(f"Exchange: {runtime_info['exchange']}")
        print(f"Cycles: {runtime_info['cycles_completed']}")
        print(f"Healthy: {'yes' if report['runtime']['last_error'] is None else 'no'}")
        print(f"Circuit breaker: {'active' if report['safety']['circuit_breaker']['active'] else 'inactive'}")
        print(f"Kill switch: {'active' if report['safety']['kill_switch']['active'] else 'inactive'}")
        print(f"Reconciliation: {report['reconciliation']['status']}")
        print(f"Unsettled orders: {report['reconciliation']['unsettled_order_count']}")
        print(f"Mismatches: {len(report['reconciliation']['mismatches'])}")
        heartbeat = report.get("heartbeat", {})
        print(f"Heartbeat: {'stalled' if heartbeat.get('stalled') else 'healthy'} ({heartbeat.get('seconds_since_last_heartbeat')}s since last)")
        print(f"Active alerts: {', '.join(sorted(report.get('active_alerts', []))) or 'none'}")
        format_reason = report["no_trade_summary"].get("reason")
        print(f"No-trade reason: {format_reason or 'none'}")
    else:
        print(f"Mode: {runtime_config.mode if runtime_config is not None else 'unknown'}")
        print(f"Strategy: {runtime_config.strategy_name if runtime_config is not None else 'unknown'}")
        print(f"Exchange: {runtime_config.exchange if runtime_config is not None else 'unknown'}")
        print(f"Last event: {latest_event['timestamp'] if latest_event else 'none'}")
        print(f"Last event type: {latest_event['event_type'] if latest_event else 'none'}")
        print(f"Last trade: {latest_trade['timestamp'] if latest_trade else 'none'}")
        print(f"Last equity snapshot: {latest_snapshot['timestamp'] if latest_snapshot else 'none'}")
        if latest_snapshot:
            print(f"Current equity: {latest_snapshot['equity']:.4f} | cash: {latest_snapshot['cash']:.4f} | position: {latest_snapshot['position_size']:.4f}")

    if report is not None:
        account_state = report["account_state"]
        balances = account_state.get("balances", {})
        positions = account_state.get("positions", {})
        print("Account state:")
        if balances:
            for currency, amount in balances.items():
                print(f"  - balance {currency}: {amount:.4f}")
        if positions:
            for symbol, size in positions.items():
                print(f"  - position {symbol}: {size:.4f}")
        if not balances and not positions:
            print("  - (empty)")

    print("Recent operational events:")
    if not events:
        print("  (none)")
    else:
        for event in events[:5]:
            print(f"  - [{event['level']}] {event['event_type']}: {event['message']}")


def print_post_run_analysis(limit: int | None = None, since: str | None = None) -> None:
    """Print a detailed post-session analysis from the SQLite logger.

    Shows:
    - Completed round-trip trades (buy→sell) with per-trade PnL and fees
    - Open positions left over at session end
    - Equity curve statistics (start, end, peak, max drawdown)
    - Blocked/cancelled entry breakdown from operational events

    Args:
        limit: Max number of closed trades to show in the table.
        since: ISO date string (YYYY-MM-DD) to filter to trades on or after that date.
    """
    import json as _json
    from collections import Counter

    logger_store = TradeLogger(database_path=settings.database_path)
    all_trades = list(reversed(logger_store.list_trades(limit=None)))
    all_snapshots = list(reversed(logger_store.list_equity_snapshots(limit=None)))
    all_events = list(reversed(logger_store.list_events(limit=None)))

    # Apply date filter
    if since:
        def _after(ts: str) -> bool:
            return str(ts)[:10] >= since
        all_trades    = [t for t in all_trades    if _after(t["timestamp"])]
        all_snapshots = [s for s in all_snapshots if _after(s["timestamp"])]
        all_events    = [e for e in all_events    if _after(e["timestamp"])]

    # --- Pair buys and sells to compute per-trade PnL ---
    buys: list[dict] = []
    closed_trades: list[dict] = []
    for trade in all_trades:
        if trade["side"] == "buy":
            buys.append(trade)
        elif trade["side"] == "sell" and buys:
            entry = buys.pop(0)
            pnl = (trade["price"] - entry["price"]) * trade["size"] - entry["fee"] - trade["fee"]
            closed_trades.append({
                "entry_time": entry["timestamp"],
                "exit_time": trade["timestamp"],
                "entry_price": entry["price"],
                "exit_price": trade["price"],
                "size": trade["size"],
                "entry_fee": entry["fee"],
                "exit_fee": trade["fee"],
                "total_fees": entry["fee"] + trade["fee"],
                "gross_pnl": (trade["price"] - entry["price"]) * trade["size"],
                "net_pnl": pnl,
            })
    open_positions = list(buys)

    # --- Equity curve stats ---
    equities = [s["equity"] for s in all_snapshots]
    start_equity = equities[0] if equities else 0.0
    end_equity = equities[-1] if equities else 0.0
    peak_equity = max(equities) if equities else 0.0
    trough_equity = min(equities) if equities else 0.0
    max_drawdown = peak_equity - trough_equity
    max_drawdown_pct = max_drawdown / peak_equity if peak_equity > 0 else 0.0
    total_pnl = end_equity - start_equity

    # --- Entry decision stats from events ---
    blocked_reasons: Counter = Counter()
    allowed_count = 0
    blocked_count = 0
    cancelled_reasons: Counter = Counter()
    cancelled_count = 0
    for event in all_events:
        etype = event.get("event_type", "")
        meta_raw = event.get("metadata") or "{}"
        try:
            meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
        except (_json.JSONDecodeError, TypeError):
            meta = {}
        if etype == "entry_decision":
            if meta.get("allowed"):
                allowed_count += 1
            else:
                blocked_count += 1
                reason = str(meta.get("reason") or "unknown")
                blocked_reasons[reason] += 1
        elif etype == "order_lifecycle":
            status = str(meta.get("status") or "")
            if status == "CANCELED":
                cancelled_count += 1
                reason = str(meta.get("reason") or "unknown")
                cancelled_reasons[reason] += 1

    # --- Print ---
    sep = "-" * 60
    since_label = f" (since {since})" if since else ""
    print("\n" + sep)
    print(f"POST-RUN ANALYSIS{since_label}")
    print(sep)

    print(f"\nEQUITY SUMMARY")
    print(f"  Start equity : {start_equity:.4f}")
    print(f"  End equity   : {end_equity:.4f}")
    print(f"  Peak equity  : {peak_equity:.4f}")
    pnl_pct_text = f"{total_pnl / start_equity:.2%}" if start_equity > 0 else "n/a"
    print(f"  Net PnL      : {total_pnl:+.4f} ({pnl_pct_text})")
    print(f"  Max drawdown : {max_drawdown:.4f} ({max_drawdown_pct:.2%})")

    print(f"\nTRADE SUMMARY")
    print(f"  Total raw fills   : {len(all_trades)}")
    print(f"  Closed round-trips: {len(closed_trades)}")
    print(f"  Open positions    : {len(open_positions)}")
    if closed_trades:
        wins = [t for t in closed_trades if t["net_pnl"] > 0]
        losses = [t for t in closed_trades if t["net_pnl"] <= 0]
        total_gross = sum(t["gross_pnl"] for t in closed_trades)
        total_fees  = sum(t["total_fees"] for t in closed_trades)
        total_net   = sum(t["net_pnl"] for t in closed_trades)
        avg_net     = total_net / len(closed_trades)
        print(f"  Wins / Losses     : {len(wins)} / {len(losses)}")
        print(f"  Win rate          : {len(wins)/len(closed_trades):.1%}")
        print(f"  Total gross PnL   : {total_gross:+.4f}")
        print(f"  Total fees paid   : {total_fees:.4f}")
        print(f"  Total net PnL     : {total_net:+.4f}")
        print(f"  Avg net per trade : {avg_net:+.4f}")

    n = limit or 20
    if closed_trades:
        print(f"\nCLOSED TRADES (last {min(n, len(closed_trades))})")
        print(f"  {'Entry time':<28} {'Exit time':<28} {'Size':>8} {'Entry':>10} {'Exit':>10} {'Fees':>8} {'Net PnL':>10}")
        for t in closed_trades[-n:]:
            print(f"  {t['entry_time']!s:<28} {t['exit_time']!s:<28} {t['size']:>8.5f} {t['entry_price']:>10.2f} {t['exit_price']:>10.2f} {t['total_fees']:>8.4f} {t['net_pnl']:>+10.4f}")

    if open_positions:
        print(f"\nUNCLOSED BUYS (no matching sell, showing last {min(5, len(open_positions))})")
        for b in open_positions[-5:]:
            print(f"  {b['timestamp']}  size={b['size']:.5f}  price={b['price']:.2f}  fee={b['fee']:.4f}")

    print(f"\nENTRY DECISIONS")
    print(f"  Allowed : {allowed_count}")
    print(f"  Blocked : {blocked_count}")
    if blocked_reasons:
        print(f"  Blocked breakdown:")
        for reason, count in blocked_reasons.most_common():
            print(f"    {reason:30s}: {count}")

    print(f"\nCANCELLED ORDERS")
    print(f"  Count : {cancelled_count}")
    if cancelled_reasons:
        print(f"  Reason breakdown:")
        for reason, count in cancelled_reasons.most_common():
            print(f"    {reason:30s}: {count}")

    print(sep)


def print_tax_report(*, tax_year: int, export_path: str | None = None, export_format: str | None = None) -> None:
    """Print the annual Norwegian tax summary and optionally export the raw ledger."""
    logger_store = TradeLogger(database_path=settings.database_path)
    summary = logger_store.get_tax_year_summary(tax_year)

    print("Norwegian tax summary")
    print("---------------------")
    print(f"Tax year: {summary['tax_year']}")
    print(f"Gross taxable gains (NOK): {summary['total_gross_taxable_gains_nok']:.4f}")
    print(f"Gross deductible losses (NOK): {summary['total_gross_deductible_losses_nok']:.4f}")
    print(f"Net foreign currency gain/loss (NOK): {summary['net_foreign_currency_gain_loss_nok']:.4f}")
    print(f"Trading fees (NOK): {summary['total_trading_fees_nok']:.4f}")
    print(f"Funding fees (NOK): {summary['total_funding_fees_nok']:.4f}")
    print(f"Tax events: {summary['tax_event_count']}")
    derivatives = summary.get("derivatives") or {}
    if derivatives.get("event_count"):
        print(
            "Of which perpetual futures (NOK): gains {gains:.4f}, losses {losses:.4f}, fees {fees:.4f}, funding paid {paid:.4f}, funding received {received:.4f} ({count} events)".format(
                gains=derivatives["realized_gains_nok"],
                losses=derivatives["realized_losses_nok"],
                fees=derivatives["fees_nok"],
                paid=derivatives["funding_paid_nok"],
                received=derivatives["funding_received_nok"],
                count=int(derivatives["event_count"]),
            )
        )
        print("  Perpetual rows are record-keeping, valued at Norges Bank USD/NOK. Funding received is not in the gross gains total above.")
        print("  Confirm how Skatteetaten treats derivative gains, fees and funding before filing.")

    wealth_snapshot = summary.get("wealth_tax_snapshot")
    if wealth_snapshot is None:
        print("Year-end holdings valuation: not recorded")
    else:
        print(
            "Year-end holdings valuation: {value_nok:.4f} NOK ({value_eur:.4f} EUR @ {fx_rate:.4f})".format(
                value_nok=wealth_snapshot["total_value_nok"],
                value_eur=wealth_snapshot["total_value_eur"],
                fx_rate=wealth_snapshot["norges_bank_fx_rate"],
            )
        )

    if export_path:
        export_target = logger_store.export_tax_ledger(path=export_path, tax_year=tax_year, export_format=export_format)
        print(f"Exported ledger: {export_target}")


def record_tax_fiat_conversion(*, amount_eur: float, fx_rate: float | None, reference: str | None) -> None:
    """Record a manual EUR fiat-pool adjustment for tax-basis tracking."""
    logger_store = TradeLogger(database_path=settings.database_path)
    entry_id = logger_store.log_fiat_conversion(
        timestamp=datetime.now(timezone.utc),
        amount_eur=amount_eur,
        source="cli_manual_tax_entry",
        fx_rate=fx_rate,
        reference=reference,
        metadata={"entered_via": "cli"},
    )
    print("Recorded EUR fiat conversion")
    print("----------------------------")
    print(f"Entry id: {entry_id}")
    print(f"Amount EUR: {amount_eur:.8f}")
    if fx_rate is not None:
        print(f"FX rate: {fx_rate:.4f}")
    if reference:
        print(f"Reference: {reference}")


def _run_futures_venue_check(*, symbol: str) -> None:
    """Print Kraken Futures' public contract spec, fees, live prices and funding history for one perpetual."""
    from src.data.kraken_futures import fetch_fee_schedules, fetch_funding_history, fetch_instrument, fetch_tickers, summarize_funding, venue_symbol_for
    from src.execution.perps import perp_contract_from_instrument

    venue_symbol = venue_symbol_for(symbol)
    instrument = fetch_instrument(venue_symbol)
    contract = perp_contract_from_instrument(instrument, fetch_fee_schedules()[instrument["feeScheduleUid"]], symbol=symbol)
    ticker = fetch_tickers().get(venue_symbol, {})
    print(f"Kraken Futures {venue_symbol} ({instrument.get('type')}, {contract.collateral_currency}-quoted, linear)")
    print(f"  size step {contract.size_step:g} {contract.base_asset}, tick {contract.tick_size:g}, max position {instrument.get('maxPositionSize')}")
    print(f"  fees (entry tier): taker {contract.taker_fee_rate:.4%}, maker {contract.maker_fee_rate:.4%}")
    print(f"  margin (first tier): initial {1.0 / contract.max_leverage:.2%} (up to {contract.max_leverage:g}x), maintenance {contract.maintenance_margin_rate:.2%}; {len(contract.margin_tiers)} tiers by position size")
    print(f"  countries banned: {instrument.get('countriesBanned') or 'none listed'}; platforms permitted: {', '.join(instrument.get('platformsPermitted', []))}")
    if ticker:
        print(f"  mark {ticker.get('markPrice')} index {ticker.get('indexPrice')} last {ticker.get('last')} bid/ask {ticker.get('bid')}/{ticker.get('ask')}")
    summary = summarize_funding(fetch_funding_history(venue_symbol))
    print(
        f"  funding over {int(summary['hours'])}h (% of notional per day, positive = longs pay): mean {summary['mean_pct_per_day']:.4f}, median {summary['median_pct_per_day']:.4f}, "
        f"p10 {summary['p10_pct_per_day']:.4f}, p90 {summary['p90_pct_per_day']:.4f}, negative {summary['share_negative']:.0%} of hours, min {summary['min_pct_per_day']:.3f}, max {summary['max_pct_per_day']:.3f}"
    )
    print("  note: whether this product is open to your account and jurisdiction is not in the public data; confirm with Kraken.")


def _load_recorder_config(config_path: str | None) -> "RecorderConfig":
    from src.data.recorder import RecorderConfig

    if not config_path:
        return RecorderConfig()
    if not Path(config_path).exists():
        raise SystemExit(f"Recorder config not found: {config_path}")
    return RecorderConfig.from_json(config_path)


def _run_market_data_recorder(*, config_path: str | None, duration_minutes: float | None) -> None:
    """Record public trades, books, tickers and liquidations until Ctrl-C, SIGTERM or the duration ends.

    Public data only: no credentials are read and nothing is sent to an exchange except subscriptions.
    """
    from src.data.recorder import MarketDataRecorder

    config = _load_recorder_config(config_path)
    recorder = MarketDataRecorder(config)
    print(f"Recording to {config.root}/ ({', '.join(feed.name for feed in recorder.feeds)}). Stop with Ctrl-C.")

    async def _record() -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, recorder.stop, "SIGTERM")
        await recorder.run(duration_seconds=None if duration_minutes is None else duration_minutes * 60.0)

    try:
        asyncio.run(_record())
    except KeyboardInterrupt:
        pass
    print("Recorder stopped; rows written: " + ", ".join(f"{key} {count}" for key, count in sorted(recorder.counts.items())))


def _run_market_data_status(*, config_path: str | None) -> None:
    """Print stored rows and disk use per venue/channel, and the recording gaps of the last 7 days."""
    import pandas as pd

    from src.data.recorder import load_market_data, market_data_status, recording_gaps

    root = _load_recorder_config(config_path).root
    status = market_data_status(root)
    if status.empty:
        print(f"No recorded market data under {root}/")
        return
    print(status.to_string(index=False))
    print(f"Total size: {status['size_mb'].sum():.1f} MB")
    events = load_market_data("recorder", "events", root=root)
    if not events.empty:
        last = events.iloc[-1]
        state = "stopped" if last["event"] == "stopped" else "running, or died without stopping"
        print(f"Last recorder event: {last['event']} at {last['received_at']:%Y-%m-%d %H:%M:%S} UTC ({state})")
    gaps = recording_gaps(root)
    recent = gaps[gaps["gap_to"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)] if not gaps.empty else gaps
    if recent.empty:
        print("No recording gaps in the last 7 days.")
    else:
        print(f"Recording gaps in the last 7 days ({recent['seconds'].sum() / 3600:.2f} feed-hours):")
        print(recent.to_string(index=False))


def _run_futures_verify_credentials(*, symbol: str) -> None:
    """Authenticate against Kraken Futures with read-only calls and print margin, positions and open orders."""
    if not settings.kraken_futures_api_key or not settings.kraken_futures_secret:
        raise SystemExit("KRAKEN_FUTURES_API_KEY / KRAKEN_FUTURES_SECRET are not set in .env")
    adapter = _build_perp_adapter(mode="live", symbol=symbol, max_leverage=1.0)
    account = adapter.sync_account(force=True)
    open_orders = adapter._private("GET", "openorders").get("openOrders", [])
    print(f"Kraken Futures credentials OK for {adapter.contract.venue_symbol}")
    print(f"  margin equity {adapter.margin_equity} {adapter.contract.collateral_currency}, available margin {adapter.available_margin}")
    print(f"  position {account['remote_size']} {adapter.contract.base_asset} (entry {adapter._position_entry_price.get(adapter.position_symbol)}), unrealized funding {adapter.unrealized_funding}")
    print(f"  open orders on the account: {len(open_orders)}")


_ACCOUNT_SUMMARY_ACTION_EVENT_TYPES = {
    "kraken_manual_order_submission",
    "kraken_manual_close_submission",
    "kraken_order_preview",
    "kraken_close_preview",
    "kraken_dry_run_verification",
    "kill_switch_activated",
}


def _run_account_summary(*, trade_logger: TradeLogger, symbol: str, recent_limit: int) -> dict[str, Any]:
    """Print a concise, non-destructive account-summary view for balances, positions, open orders, and recent actions."""
    adapter = KrakenExecutionAdapter()
    has_credentials = bool(adapter.api_key and adapter.api_secret)

    balance_snapshot: dict[str, Any] | None = None
    balance_error: str | None = None
    open_orders: list[dict[str, Any]] | None = None
    open_orders_error: str | None = None
    if has_credentials:
        try:
            balance_snapshot = adapter.fetch_balance_snapshot()
        except Exception as exc:
            balance_error = str(exc)
        try:
            open_orders = adapter.fetch_open_orders()
        except Exception as exc:
            open_orders_error = str(exc)

    pair_metadata: dict[str, Any] | None = None
    pair_metadata_error: str | None = None
    ticker: dict[str, Any] | None = None
    ticker_error: str | None = None
    try:
        pair_metadata = adapter.fetch_asset_pair_metadata(symbol=symbol)
    except Exception as exc:
        pair_metadata_error = str(exc)
    try:
        ticker = adapter.fetch_ticker_snapshot(symbol=symbol)
    except Exception as exc:
        ticker_error = str(exc)

    print("Kraken account summary")
    print("-----------------------")
    print(f"Symbol: {symbol}")
    if not has_credentials:
        print("Credentials: not configured (showing exchange rules and locally persisted history only)")
    else:
        print("Credentials: configured")

    print()
    print("Balances:")
    if balance_error:
        print(f"  unavailable: {balance_error}")
    elif balance_snapshot is not None:
        balances = balance_snapshot.get("balances", {}) or {}
        if not balances:
            print("  (none reported)")
        for currency, amount in balances.items():
            print(f"  {currency}: {float(amount):.8f}")
    else:
        print("  n/a (no credentials configured)")

    print()
    print("Positions:")
    if balance_error:
        print(f"  unavailable: {balance_error}")
    elif balance_snapshot is not None:
        positions = balance_snapshot.get("positions", {}) or {}
        if not positions:
            print("  (flat / no open positions)")
        for asset, size in positions.items():
            print(f"  {asset}: {float(size):.10f}")
    else:
        print("  n/a (no credentials configured)")

    print()
    print("Open orders:")
    if open_orders_error:
        print(f"  unavailable: {open_orders_error}")
    elif open_orders is not None:
        if not open_orders:
            print("  (none)")
        for order in open_orders:
            print(
                f"  {order.get('order_id', 'unknown')} | {order.get('side', 'unknown')} "
                f"{order.get('size', 0.0)} @ {order.get('price', 'n/a')} | status={order.get('status', 'unknown')}"
            )
    else:
        print("  n/a (no credentials configured)")

    print()
    print(f"Exchange minimums for {symbol} (from Kraken, live):")
    if pair_metadata_error:
        print(f"  unavailable: {pair_metadata_error}")
    else:
        ordermin = float(pair_metadata["ordermin"])
        costmin = float(pair_metadata["costmin"])
        base_asset = adapter._base_asset(symbol)
        print(f"  minimum order size: {ordermin:.10f} {base_asset}")
        print(f"  minimum order notional: {costmin:.8f} {adapter._quote_asset(symbol)}")
        if ticker_error:
            print(f"  current price: unavailable ({ticker_error})")
        else:
            ask_price = float(ticker["ask"])
            implied_min_notional = max(costmin, ordermin * ask_price)
            print(f"  current ask price: {ask_price:.2f}")
            print(f"  smallest order Kraken will accept right now: ~{implied_min_notional:.4f} {adapter._quote_asset(symbol)}")

    print()
    print(f"Recent trades (last {recent_limit}):")
    for trade in trade_logger.list_trades(limit=recent_limit):
        strategy_tag = f" [{trade['strategy_id']}]" if trade.get("strategy_id") else ""
        print(f"  {trade['timestamp']} | {trade['side']} {trade['pair']} @ {trade['price']:.4f} size={trade['size']:.8f} fee={trade['fee']:.6f}{strategy_tag}")

    print()
    print(f"Recent live/manual actions (last {recent_limit}):")
    recent_actions = trade_logger.list_events(limit=recent_limit, event_types=sorted(_ACCOUNT_SUMMARY_ACTION_EVENT_TYPES))
    if not recent_actions:
        print("  (none recorded)")
    for event in recent_actions:
        print(f"  {event['timestamp']} | [{event['level']}] {event['event_type']}: {event['message']}")

    return {
        "symbol": symbol,
        "has_credentials": has_credentials,
        "balance_snapshot": balance_snapshot,
        "open_orders": open_orders,
        "pair_metadata": pair_metadata,
        "ticker": ticker,
    }


def _run_kraken_dry_run_verification(
    *,
    trade_logger: TradeLogger,
    symbol: str,
    size: float,
    probe_order_id: str | None,
) -> dict[str, Any]:
    """Run a non-destructive Kraken private-endpoint verification and print the result."""
    adapter = KrakenExecutionAdapter()
    verification = adapter.verify_dry_run(symbol=symbol, size=size, probe_order_id=probe_order_id)
    kill_switch_preview = KillSwitchController(trade_logger=trade_logger).preview_activation(execution_adapter=adapter)
    verification["kill_switch_preview"] = kill_switch_preview

    trade_logger.log_event(
        timestamp=datetime.now(timezone.utc),
        level="INFO" if verification.get("status") == "passed" else "WARNING",
        event_type="kraken_dry_run_verification",
        message=f"Kraken dry-run verification {verification.get('status', 'unknown')}",
        source="main",
        metadata={
            "status": verification.get("status"),
            "symbol": symbol,
            "size": size,
            "probe_order_id": probe_order_id,
            "recovered_order_count": verification.get("recovered_order_count", 0),
            "kill_switch_preview_order_count": kill_switch_preview.get("order_count", 0),
            "check_results": [
                {
                    "name": check.get("name"),
                    "ok": check.get("ok"),
                    "message": check.get("message"),
                }
                for check in verification.get("checks", [])
            ],
        },
    )

    print("Kraken dry-run verification")
    print("---------------------------")
    print(f"Status: {verification.get('status', 'unknown')}")
    print(f"Symbol: {verification.get('symbol', symbol)}")
    print(f"Size: {verification.get('size', size)}")
    print(f"Credentials configured: {verification.get('credentials_configured', False)}")
    if verification.get("balance_snapshot"):
        balances = verification["balance_snapshot"].get("balances", {})
        positions = verification["balance_snapshot"].get("positions", {})
        print(f"Balance currencies: {len(balances)}")
        print(f"Open position symbols: {len(positions)}")
    print(f"Open orders observed: {verification.get('open_order_count', 0)}")
    print(f"Closed orders observed: {verification.get('closed_order_count', 0)}")
    print(f"Recovered orders: {verification.get('recovered_order_count', 0)}")
    print(f"Kill-switch preview cancellations: {kill_switch_preview.get('order_count', 0)}")
    print("Checks:")
    for check in verification.get("checks", []):
        marker = "PASS" if check.get("ok") else "FAIL"
        print(f"  - [{marker}] {check.get('name')}: {check.get('message')}")
    if kill_switch_preview.get("orders_to_cancel"):
        print("Kill-switch preview:")
        for order in kill_switch_preview["orders_to_cancel"][:10]:
            print(
                "  - order_id={order_id} remote_order_id={remote_order_id} status={status} exchange={exchange}".format(
                    order_id=order.get("order_id"),
                    remote_order_id=order.get("remote_order_id") or "n/a",
                    status=order.get("status"),
                    exchange=order.get("exchange") or "unknown",
                )
            )

    if verification.get("status") != "passed":
        raise SystemExit("Kraken dry-run verification failed")
    return verification


def _run_kraken_order_preview(
    *,
    trade_logger: TradeLogger,
    symbol: str,
    side: str,
    quote_amount: float,
) -> dict[str, Any]:
    """Preview a Kraken order by quote notional using validate=true without sending it."""
    adapter = KrakenExecutionAdapter()
    preview = adapter.preview_quote_order(symbol=symbol, quote_amount=quote_amount, side=side)

    trade_logger.log_event(
        timestamp=datetime.now(timezone.utc),
        level="INFO",
        event_type="kraken_order_preview",
        message="Kraken order preview completed without submission",
        source="main",
        metadata={
            "symbol": preview.get("symbol"),
            "side": preview.get("side"),
            "requested_quote_amount": preview.get("requested_quote_amount"),
            "quote_currency": preview.get("quote_currency"),
            "rounded_size": preview.get("rounded_size"),
            "estimated_cost": preview.get("estimated_cost"),
            "can_submit": preview.get("can_submit"),
            "has_validation": preview.get("validation") is not None,
            "validation_error": preview.get("validation_error"),
        },
    )

    metadata = preview["pair_metadata"]
    validation = preview.get("validation")
    print("Kraken order preview")
    print("--------------------")
    print("Mode: validate-only (no order submitted)")
    print(f"Symbol: {preview['symbol']}")
    print(f"Side: {preview['side']}")
    print(f"Pair code: {metadata['pair_code']}")
    print(f"Pair status: {metadata['status']}")
    print(f"Available {preview['quote_currency']}: {preview['available_quote_balance']:.8f}")
    print(f"Requested {preview['quote_currency']} notional: {preview['requested_quote_amount']:.8f}")
    print(f"Reference ask price: {preview['reference_price']:.8f}")
    print(f"Raw base size: {preview['raw_size']:.10f}")
    print(f"Rounded base size: {preview['rounded_size']:.10f}")
    print(f"Estimated order cost: {preview['estimated_cost']:.8f} {preview['quote_currency']}")
    print(f"Minimum base size: {preview['minimum_size']:.10f}")
    print(f"Minimum order cost: {preview['minimum_cost']:.8f} {preview['quote_currency']}")
    print(f"Lot decimals: {metadata['lot_decimals']}")
    print(f"Price decimals: {metadata['pair_decimals']}")
    print(f"Tick size: {metadata['tick_size']:.8f}")
    print("Checks:")
    print(f"  - balance sufficient: {'yes' if preview['sufficient_balance'] else 'no'}")
    print(f"  - minimum size met: {'yes' if preview['meets_minimum_size'] else 'no'}")
    print(f"  - minimum cost met: {'yes' if preview['meets_minimum_cost'] else 'no'}")
    if validation is not None:
        print("Kraken validate=true response: accepted")
        print(f"  - description: {validation.get('description') or 'n/a'}")
    elif preview.get("validation_error"):
        print(f"Kraken validate=true response: rejected ({preview['validation_error']})")
    else:
        print("Kraken validate=true response: skipped (pre-checks failed)")
    return preview


def _run_kraken_close_preview(
    *,
    trade_logger: TradeLogger,
    symbol: str,
) -> dict[str, Any]:
    """Preview closing the current Kraken position without sending it."""
    adapter = KrakenExecutionAdapter()
    preview = adapter.preview_close_position(symbol=symbol)

    trade_logger.log_event(
        timestamp=datetime.now(timezone.utc),
        level="INFO",
        event_type="kraken_close_preview",
        message="Kraken close-position preview completed without submission",
        source="main",
        metadata={
            "symbol": preview.get("symbol"),
            "base_asset": preview.get("base_asset"),
            "rounded_size": preview.get("rounded_size"),
            "estimated_proceeds": preview.get("estimated_proceeds"),
            "can_submit": preview.get("can_submit"),
            "has_validation": preview.get("validation") is not None,
            "validation_error": preview.get("validation_error"),
        },
    )

    metadata = preview["pair_metadata"]
    validation = preview.get("validation")
    print("Kraken close-position preview")
    print("-----------------------------")
    print("Mode: validate-only (no order submitted)")
    print(f"Symbol: {preview['symbol']}")
    print(f"Side: {preview['side']}")
    print(f"Pair code: {metadata['pair_code']}")
    print(f"Pair status: {metadata['status']}")
    print(f"Available {preview['base_asset']} position: {preview['available_position_size']:.10f}")
    print(f"Rounded close size: {preview['rounded_size']:.10f}")
    print(f"Reference bid price: {preview['reference_price']:.8f}")
    print(f"Estimated proceeds: {preview['estimated_proceeds']:.8f} {adapter._quote_asset(symbol)}")
    print(f"Minimum base size: {preview['minimum_size']:.10f}")
    print(f"Minimum order cost: {preview['minimum_cost']:.8f} {adapter._quote_asset(symbol)}")
    print(f"Lot decimals: {metadata['lot_decimals']}")
    print(f"Price decimals: {metadata['pair_decimals']}")
    print(f"Tick size: {metadata['tick_size']:.8f}")
    print("Checks:")
    print(f"  - position available: {'yes' if preview['has_position'] else 'no'}")
    print(f"  - minimum size met: {'yes' if preview['meets_minimum_size'] else 'no'}")
    print(f"  - minimum cost met: {'yes' if preview['meets_minimum_cost'] else 'no'}")
    if validation is not None:
        print("Kraken validate=true response: accepted")
        print(f"  - description: {validation.get('description') or 'n/a'}")
    elif preview.get("validation_error"):
        print(f"Kraken validate=true response: rejected ({preview['validation_error']})")
    else:
        print("Kraken validate=true response: skipped (pre-checks failed)")
    return preview


def _run_kraken_order_submission(
    *,
    trade_logger: TradeLogger,
    symbol: str,
    side: str,
    quote_amount: float,
) -> dict[str, Any]:
    """Submit a manual Kraken order using the validated quote-order path."""
    adapter = KrakenExecutionAdapter()
    submission = adapter.submit_quote_order(symbol=symbol, quote_amount=quote_amount, side=side)

    persisted_trade_id: int | None = None
    tax_event_error: str | None = None
    timestamp = datetime.fromisoformat(submission["timestamp"])
    if submission.get("status") in {"FILLED", "PARTIALLY_FILLED"} and submission.get("fill_price") and submission.get("filled_size"):
        persisted_trade_id, tax_event_error = trade_logger.log_trade(
            timestamp=timestamp,
            source="cli_manual_live_order",
            exchange="kraken",
            pair=str(submission["symbol"]),
            side=str(submission["side"]),
            price=float(submission["fill_price"]),
            size=float(submission["filled_size"]),
            fee=float(submission.get("fee") or 0.0),
            record_tax_event=True,
            strategy_id="manual_cli",
        )

    trade_logger.log_event(
        timestamp=timestamp,
        level="WARNING",
        event_type="kraken_manual_order_submission",
        message="Manual Kraken order submitted from CLI",
        source="main",
        metadata={
            "symbol": submission.get("symbol"),
            "side": submission.get("side"),
            "requested_quote_amount": submission.get("requested_quote_amount"),
            "rounded_size": submission.get("rounded_size"),
            "order_id": submission.get("order_id"),
            "remote_order_id": submission.get("remote_order_id"),
            "status": submission.get("status"),
            "fill_price": submission.get("fill_price"),
            "filled_size": submission.get("filled_size"),
            "fee": submission.get("fee"),
            "persisted_trade_id": persisted_trade_id,
            "account_snapshot_error": submission.get("account_snapshot_error"),
        },
    )

    print("Kraken manual order submission")
    print("------------------------------")
    print("LIVE ACTION: order submitted to Kraken")
    print(f"Symbol: {submission['symbol']}")
    print(f"Side: {submission['side']}")
    print(f"Requested EUR notional: {submission['requested_quote_amount']:.8f}")
    print(f"Rounded base size: {submission['rounded_size']:.10f}")
    print(f"Reference ask price: {submission['reference_price']:.8f}")
    print(f"Estimated cost: {submission['estimated_cost']:.8f} {submission['quote_currency']}")
    print(f"Local order id: {submission['order_id']}")
    print(f"Kraken order id: {submission['remote_order_id'] or 'n/a'}")
    print(f"Submit description: {submission.get('submit_description') or 'n/a'}")
    print(f"Latest status: {submission['status']}")
    if submission.get("filled_size") is not None:
        print(f"Filled size: {submission['filled_size']:.10f}")
    if submission.get("fill_price") is not None:
        print(f"Fill price: {submission['fill_price']:.8f}")
    print(f"Fee: {float(submission.get('fee') or 0.0):.8f}")
    if persisted_trade_id is not None:
        print(f"Persisted trade id: {persisted_trade_id}")
        if tax_event_error is None:
            print("Tax logging: recorded for this fill")
        else:
            print("Tax logging: FAILED — this fill has NO tax-ledger rows yet")
            print(f"  reason: {tax_event_error}")
            print("  fix: seed the required EUR fiat pool / asset lot, then backfill via TradeLogger.record_trade_tax_events")
    else:
        print("Tax logging: deferred until a filled trade is observed")

    account_snapshot = submission.get("account_snapshot") or {}
    balances = account_snapshot.get("balances", {}) if isinstance(account_snapshot, dict) else {}
    if balances:
        eur_balance = balances.get("EUR")
        btc_balance = balances.get("BTC")
        if eur_balance is not None:
            print(f"Post-submit EUR balance: {float(eur_balance):.8f}")
        if btc_balance is not None:
            print(f"Post-submit BTC balance: {float(btc_balance):.10f}")
    if submission.get("account_snapshot_error"):
        print(f"Balance refresh: {submission['account_snapshot_error']}")

    print("Next operator step: inspect --report and --tax-report after the trade settles.")
    return submission


def _run_kraken_close_submission(
    *,
    trade_logger: TradeLogger,
    symbol: str,
) -> dict[str, Any]:
    """Submit a manual Kraken close order for the current full position."""
    adapter = KrakenExecutionAdapter()
    submission = adapter.submit_close_position(symbol=symbol)

    persisted_trade_id: int | None = None
    tax_event_error: str | None = None
    timestamp = datetime.fromisoformat(submission["timestamp"])
    if submission.get("status") in {"FILLED", "PARTIALLY_FILLED"} and submission.get("fill_price") and submission.get("filled_size"):
        persisted_trade_id, tax_event_error = trade_logger.log_trade(
            timestamp=timestamp,
            source="cli_manual_live_close",
            exchange="kraken",
            pair=str(submission["symbol"]),
            side="sell",
            price=float(submission["fill_price"]),
            size=float(submission["filled_size"]),
            fee=float(submission.get("fee") or 0.0),
            record_tax_event=True,
            strategy_id="manual_cli",
        )

    trade_logger.log_event(
        timestamp=timestamp,
        level="WARNING",
        event_type="kraken_manual_close_submission",
        message="Manual Kraken close order submitted from CLI",
        source="main",
        metadata={
            "symbol": submission.get("symbol"),
            "rounded_size": submission.get("rounded_size"),
            "order_id": submission.get("order_id"),
            "remote_order_id": submission.get("remote_order_id"),
            "status": submission.get("status"),
            "fill_price": submission.get("fill_price"),
            "filled_size": submission.get("filled_size"),
            "fee": submission.get("fee"),
            "persisted_trade_id": persisted_trade_id,
            "account_snapshot_error": submission.get("account_snapshot_error"),
        },
    )

    print("Kraken manual close submission")
    print("------------------------------")
    print("LIVE ACTION: close order submitted to Kraken")
    print(f"Symbol: {submission['symbol']}")
    print(f"Close size: {submission['rounded_size']:.10f}")
    print(f"Reference bid price: {submission['reference_price']:.8f}")
    print(f"Estimated proceeds: {submission['estimated_proceeds']:.8f}")
    print(f"Local order id: {submission['order_id']}")
    print(f"Kraken order id: {submission['remote_order_id'] or 'n/a'}")
    print(f"Submit description: {submission.get('submit_description') or 'n/a'}")
    print(f"Latest status: {submission['status']}")
    if submission.get("filled_size") is not None:
        print(f"Filled size: {submission['filled_size']:.10f}")
    if submission.get("fill_price") is not None:
        print(f"Fill price: {submission['fill_price']:.8f}")
    print(f"Fee: {float(submission.get('fee') or 0.0):.8f}")
    if persisted_trade_id is not None:
        print(f"Persisted trade id: {persisted_trade_id}")
        if tax_event_error is None:
            print("Tax logging: recorded for this fill")
        else:
            print("Tax logging: FAILED — this fill has NO tax-ledger rows yet")
            print(f"  reason: {tax_event_error}")
            print("  fix: seed the required EUR fiat pool / asset lot, then backfill via TradeLogger.record_trade_tax_events")
    else:
        print("Tax logging: deferred until a filled trade is observed")

    account_snapshot = submission.get("account_snapshot") or {}
    balances = account_snapshot.get("balances", {}) if isinstance(account_snapshot, dict) else {}
    positions = account_snapshot.get("positions", {}) if isinstance(account_snapshot, dict) else {}
    if balances:
        eur_balance = balances.get("EUR")
        if eur_balance is not None:
            print(f"Post-close EUR balance: {float(eur_balance):.8f}")
    if positions:
        base_asset = submission.get("base_asset")
        if base_asset in positions:
            print(f"Post-close {base_asset} position: {float(positions[base_asset]):.10f}")
    if submission.get("account_snapshot_error"):
        print(f"Balance refresh: {submission['account_snapshot_error']}")

    print("Next operator step: inspect --report and --tax-report after the close settles.")
    return submission


def main() -> None:
    """Initialize the runtime and run either the data pipeline or a demo backtest."""
    parser = argparse.ArgumentParser(description="CryptoQuantMFT runtime")
    parser.add_argument("--demo-backtest", action="store_true", help="Run a synthetic backtest and save plots")
    parser.add_argument("--plot-output-dir", default="plots", help="Directory for generated plots")
    parser.add_argument("--strategy", default="moving_average_crossover", help="Name of the strategy to run")
    parser.add_argument("--strategy-params", default="{}", help="Optional JSON object of strategy constructor parameters, e.g. '{\"lookback\": 5, \"threshold\": 0.01}'")
    parser.add_argument("--include-costs", action="store_true", help="Apply the fee and FX cost model")
    parser.add_argument("--compare-costs", action="store_true", help="Compare baseline and cost-adjusted backtests")
    parser.add_argument("--paper-trading", action="store_true", help="Run the paper-trading engine over bars and signals")
    parser.add_argument("--use-kraken-data", action="store_true", help="Backtest on recent Kraken OHLCV bars instead of synthetic demo bars")
    parser.add_argument("--kraken-symbol", default="BTC/EUR", help="Kraken symbol to fetch, e.g. BTC/EUR")
    parser.add_argument("--kraken-bars", type=int, default=200, help="Number of Kraken OHLCV bars to fetch")
    parser.add_argument("--taker-fee", type=float, default=0.4, help="Approximate taker fee as a percentage")
    parser.add_argument("--maker-fee", type=float, default=0.25, help="Approximate maker fee as a percentage")
    parser.add_argument("--fx-spread-bps", type=float, default=10.0, help="Approximate FX spread in bps")
    parser.add_argument("--report", action="store_true", help="Print recent trades, equity snapshots, and operational events from the SQLite logger")
    parser.add_argument("--report-limit", type=int, default=10, help="Number of recent rows to print in the report")
    parser.add_argument("--dashboard", action="store_true", help="Print a compact health dashboard based on recent runtime events and portfolio snapshots")
    parser.add_argument("--daily-summary", action="store_true", help="Print the persisted daily summary report for the selected day")
    parser.add_argument("--daily-summary-date", default=None, help="Optional report date in YYYY-MM-DD format")
    parser.add_argument("--l2-simulator", action="store_true", help="Run the lightweight event-driven L2 simulator over synthetic snapshots")
    parser.add_argument("--walk-forward", action="store_true", help="Run a simple walk-forward evaluation over the selected bars")
    parser.add_argument("--walk-forward-train-window", type=int, default=40, help="Number of bars to use as the warmup/training window")
    parser.add_argument("--walk-forward-test-window", type=int, default=20, help="Number of bars to use as the out-of-sample test window")
    parser.add_argument("--walk-forward-step", type=int, default=20, help="Number of bars to move between walk-forward windows")
    parser.add_argument("--runtime", choices=["paper", "live_dry_run", "live"], help="Run the runtime orchestrator with the requested mode")
    parser.add_argument("--runtime-iterations", type=int, default=3, help="Number of runtime cycles to execute")
    parser.add_argument("--runtime-interval", type=float, default=1.0, help="Delay in seconds between runtime cycles")
    parser.add_argument("--bar-interval", default=None, choices=["1m", "5m", "15m", "30m", "1h", "4h", "1d"], help="Bar length for the strategy, independent of --runtime-interval (how often prices are polled). The strategy only acts when a bar completes. Default: one bar per poll")
    parser.add_argument("--warmup-bars", type=int, default=200, help="With --bar-interval: completed historical bars loaded at startup (Kraken spot OHLC, or futures mark candles for kraken_futures) so long-window strategies can signal immediately. 0 disables")
    parser.add_argument("--risk-per-trade-pct", type=float, default=None, help="Override the fraction of equity risked per trade (default 0.10); needed for very small accounts to clear exchange minimum order sizes")
    parser.add_argument("--target-annual-vol", type=float, default=None, help="Size each entry so its forecast annualised volatility is this (0.5 = 50%%), using an EWMA forecast with a 10-day half-life; capped by the exchange's position and per-trade limits (0.5x equity on Kraken), not resized while open. Default: the older fixed-fraction sizing")
    parser.add_argument("--execution-exchange", choices=["auto", "sandbox", "kraken", "firi", "kraken_futures"], default="auto", help="Exchange routing target for the runtime execution adapter. kraken_futures trades perpetual futures: the in-process margin sandbox under live_dry_run, real Kraken Futures orders under live")
    parser.add_argument("--perp-sandbox-reset", action="store_true", help="Start the perpetual-futures dry-run account fresh; the previous saved state (data/perp_sandbox_<contract>.json) is moved aside, not deleted")
    parser.add_argument("--perp-max-leverage", type=float, default=2.0, help="Leverage cap this runtime enforces on a perpetual-futures account (default 2; live refuses more than 3)")
    parser.add_argument("--enable-live-trading", action="store_true", help=f"Required explicit opt-in before --runtime live is allowed. Pair with --live-confirmation {LIVE_TRADING_CONFIRMATION}")
    parser.add_argument("--live-confirmation", default=None, help=f"Exact confirmation token required with --runtime live: {LIVE_TRADING_CONFIRMATION}")
    parser.add_argument("--trading-symbol", default=None, help="Trading symbol for the runtime connector and execution context, e.g. BTC/EUR or BTC/NOK")
    parser.add_argument("--use-mock-connector", action="store_true", help="Use the mock exchange connector for the runtime loop")
    parser.add_argument("--watchdog-timeout", type=float, default=30.0, help="Seconds without a completed cycle or fresh data before the watchdog triggers")
    parser.add_argument("--watchdog-restarts", type=int, default=0, help="Number of times to restart the runtime after a watchdog timeout")
    parser.add_argument("--runtime-config-path", default=None, help="Optional JSON file path used to persist and reload the runtime config")
    parser.add_argument("--runtime-state-path", default=None, help="Optional JSON file path used to persist and reload the runtime checkpoint")
    parser.add_argument("--live-plot", action="store_true", help="Write a continuously updating equity/trade plot to disk during the runtime")
    parser.add_argument("--live-plot-path", default=None, help="Optional file path for the runtime live plot image")
    parser.add_argument("--resume-runtime", action="store_true", help="Load the runtime state from a checkpoint file before starting")
    parser.add_argument("--post-run-analysis", action="store_true", help="Print a post-session trade analysis: round-trip PnL, blocked/cancelled breakdown, equity stats")
    parser.add_argument("--since-today", action="store_true", help="Filter --post-run-analysis to today's data only")
    parser.add_argument("--since", default=None, help="Filter --post-run-analysis to data on or after this date (YYYY-MM-DD)")
    parser.add_argument("--kill-switch", action="store_true", help="Activate the runtime kill switch and cancel any open orders via the configured execution adapter")
    parser.add_argument("--kill-switch-reason", default="manual", help="Reason to record when activating the kill switch")
    parser.add_argument("--futures-venue-check", action="store_true", help="Non-destructive: fetch Kraken Futures public specs, fees, live mark price and funding history for --futures-symbol and print them (no credentials, no orders)")
    parser.add_argument("--futures-verify-credentials", action="store_true", help="Non-destructive: call Kraken Futures private read-only endpoints (accounts, open positions, open orders) with KRAKEN_FUTURES_API_KEY/SECRET and print the result. Places no orders")
    parser.add_argument("--futures-symbol", default="BTC/USD", help="Symbol for --futures-venue-check, e.g. BTC/USD, ETH/USD or SOL/USD")
    parser.add_argument("--record-market-data", action="store_true", help="Record public order flow until stopped (Ctrl-C): Kraken Futures and spot trades, order-book samples and perp tickers, Binance/Bybit liquidations. No credentials, no orders. Files go to data/market_data/")
    parser.add_argument("--record-config", default="config/market_data.json", help="JSON file with the symbols and sampling settings for --record-market-data")
    parser.add_argument("--record-duration-minutes", type=float, default=None, help="Stop --record-market-data after this many minutes (default: run until stopped)")
    parser.add_argument("--market-data-status", action="store_true", help="Print what --record-market-data has stored (rows, days, disk use per venue and channel) and recent recording gaps")
    parser.add_argument("--account-summary", action="store_true", help="Print a non-destructive Kraken account summary: balances, positions, open orders, exchange minimums, and recent live/manual actions")
    parser.add_argument("--account-summary-symbol", default=None, help="Symbol to use for --account-summary exchange-minimum checks, e.g. BTC/EUR")
    parser.add_argument("--account-summary-limit", type=int, default=10, help="Number of recent trades/actions to show with --account-summary")
    parser.add_argument("--kraken-verify-dry-run", action="store_true", help="Run a non-destructive Kraken private-endpoint verification for live_dry_run readiness")
    parser.add_argument("--kraken-verify-symbol", default=None, help="Symbol to use for Kraken validate-only order verification, e.g. BTC/EUR")
    parser.add_argument("--kraken-verify-size", type=float, default=0.0002, help="Order size used for Kraken validate-only verification")
    parser.add_argument("--kraken-verify-order-id", default=None, help="Optional Kraken order id to use for a real status-lookup probe during dry-run verification")
    parser.add_argument("--kraken-preview-order", action="store_true", help="Preview a Kraken order by quote notional using validate=true without submitting it")
    parser.add_argument("--kraken-preview-symbol", default=None, help="Symbol to use for Kraken order preview, e.g. BTC/EUR")
    parser.add_argument("--kraken-preview-side", choices=["buy"], default="buy", help="Order side for Kraken preview; currently buy only")
    parser.add_argument("--kraken-preview-quote-amount", type=float, default=3.0, help="Quote-currency notional for Kraken preview, e.g. 3.0 for 3 EUR on BTC/EUR")
    parser.add_argument("--kraken-preview-close-position", action="store_true", help="Preview closing the full current Kraken position with validate=true without submitting it")
    parser.add_argument("--kraken-close-symbol", default=None, help="Symbol to use for Kraken close-position preview or submission, e.g. BTC/EUR")
    parser.add_argument("--kraken-submit-order", action="store_true", help="Submit a manual Kraken market order by EUR notional after passing preview and confirmation gates")
    parser.add_argument("--kraken-submit-symbol", default=None, help="Symbol to use for Kraken manual submission, e.g. BTC/EUR")
    parser.add_argument("--kraken-submit-side", choices=["buy"], default="buy", help="Order side for Kraken manual submission; currently buy only")
    parser.add_argument("--kraken-submit-quote-amount", type=float, default=None, help="EUR notional to submit on Kraken for a manual order")
    parser.add_argument(
        "--kraken-submit-confirmation",
        default=None,
        help=f"Exact confirmation token required with --kraken-submit-order: {KRAKEN_MANUAL_ORDER_CONFIRMATION}",
    )
    parser.add_argument("--kraken-close-position", action="store_true", help="Submit a manual Kraken market sell that closes the full current position")
    parser.add_argument(
        "--kraken-close-confirmation",
        default=None,
        help=f"Exact confirmation token required with --kraken-close-position: {KRAKEN_MANUAL_ORDER_CONFIRMATION}",
    )
    parser.add_argument("--tax-report", action="store_true", help="Print the Norwegian tax summary for the selected year")
    parser.add_argument("--tax-year", type=int, default=datetime.now(timezone.utc).year, help="Tax year used by --tax-report and tax exports")
    parser.add_argument("--tax-export-path", default=None, help="Optional CSV/JSON path to export tax-ledger rows for the selected tax year")
    parser.add_argument("--tax-export-format", choices=["csv", "json"], default=None, help="Optional format override for --tax-export-path")
    parser.add_argument("--tax-log-fiat-eur", type=float, default=None, help="Manually record a EUR pool increase/decrease for tax basis tracking; positive acquires EUR, negative spends EUR")
    parser.add_argument("--tax-fx-rate", type=float, default=None, help="Optional EUR/NOK rate override used with --tax-log-fiat-eur")
    parser.add_argument("--tax-reference", default=None, help="Optional reference recorded with manual tax-ledger entries")
    args = parser.parse_args()
    if args.target_annual_vol is not None and not 0.0 < args.target_annual_vol <= 3.0:
        parser.error("--target-annual-vol must be above 0 and at most 3 (300% a year); 0.5 means 50%")
    runtime_config = build_runtime_config_from_args(args, argv=sys.argv[1:])

    logger.info("CryptoQuantMFT startup complete")
    logger.info("database_path={}", settings.database_path)
    logger.info("log_level={}", settings.log_level)

    logger_store = TradeLogger(database_path=settings.database_path)

    if args.kill_switch:
        controller = KillSwitchController(trade_logger=TradeLogger(database_path=settings.database_path))
        state = controller.activate(args.kill_switch_reason)
        print("Kill switch activated")
        print(f"Reason: {state['reason']}")
        print(f"Orders cancelled: {len(state['orders_cancelled'])}")
        return

    if args.futures_verify_credentials:
        _run_futures_verify_credentials(symbol=args.futures_symbol)
        return

    if args.futures_venue_check:
        _run_futures_venue_check(symbol=args.futures_symbol)
        return

    if args.record_market_data:
        _run_market_data_recorder(config_path=args.record_config, duration_minutes=args.record_duration_minutes)
        return

    if args.market_data_status:
        _run_market_data_status(config_path=args.record_config)
        return

    if args.account_summary:
        _run_account_summary(
            trade_logger=logger_store,
            symbol=args.account_summary_symbol or runtime_config.trading_symbol or "BTC/EUR",
            recent_limit=args.account_summary_limit,
        )
        return

    if args.kraken_verify_dry_run:
        _run_kraken_dry_run_verification(
            trade_logger=logger_store,
            symbol=args.kraken_verify_symbol or runtime_config.trading_symbol or "BTC/EUR",
            size=args.kraken_verify_size,
            probe_order_id=args.kraken_verify_order_id,
        )
        return

    if args.kraken_preview_order:
        _run_kraken_order_preview(
            trade_logger=logger_store,
            symbol=args.kraken_preview_symbol or runtime_config.trading_symbol or "BTC/EUR",
            side=args.kraken_preview_side,
            quote_amount=args.kraken_preview_quote_amount,
        )
        return

    if args.kraken_preview_close_position:
        _run_kraken_close_preview(
            trade_logger=logger_store,
            symbol=args.kraken_close_symbol or runtime_config.trading_symbol or "BTC/EUR",
        )
        return

    if args.kraken_submit_order:
        submission_symbol = args.kraken_submit_symbol or runtime_config.trading_symbol or "BTC/EUR"
        submission_quote_amount = args.kraken_submit_quote_amount
        if submission_quote_amount is None:
            raise SystemExit("refusing to submit a manual Kraken order without --kraken-submit-quote-amount")
        _validate_kraken_manual_submit_request(
            symbol=submission_symbol,
            side=args.kraken_submit_side,
            quote_amount=submission_quote_amount,
            enable_live_trading=args.enable_live_trading,
            live_confirmation=args.live_confirmation,
            submit_confirmation=args.kraken_submit_confirmation,
        )
        _run_kraken_order_submission(
            trade_logger=logger_store,
            symbol=submission_symbol,
            side=args.kraken_submit_side,
            quote_amount=submission_quote_amount,
        )
        return

    if args.kraken_close_position:
        close_symbol = args.kraken_close_symbol or runtime_config.trading_symbol or "BTC/EUR"
        _validate_kraken_manual_close_request(
            symbol=close_symbol,
            enable_live_trading=args.enable_live_trading,
            live_confirmation=args.live_confirmation,
            close_confirmation=args.kraken_close_confirmation,
        )
        _run_kraken_close_submission(
            trade_logger=logger_store,
            symbol=close_symbol,
        )
        return

    if args.tax_report:
        print_tax_report(
            tax_year=args.tax_year,
            export_path=args.tax_export_path,
            export_format=args.tax_export_format,
        )
        return

    if args.tax_log_fiat_eur is not None:
        record_tax_fiat_conversion(
            amount_eur=args.tax_log_fiat_eur,
            fx_rate=args.tax_fx_rate,
            reference=args.tax_reference,
        )
        return

    if args.l2_simulator:
        run_l2_simulation()
        return

    if args.walk_forward:
        bars = build_demo_bars()
        if args.use_kraken_data:
            bars = build_kraken_bars(symbol=args.kraken_symbol, count=args.kraken_bars)

        config = BacktestConfig(
            strategy_name=args.strategy,
            include_costs=args.include_costs,
            taker_fee=args.taker_fee,
            maker_fee=args.maker_fee,
            fx_spread_bps=args.fx_spread_bps,
        )
        result = evaluate_walk_forward(
            bars,
            config=config,
            train_window=args.walk_forward_train_window,
            test_window=args.walk_forward_test_window,
            step_size=args.walk_forward_step,
        )
        logger.info(
            "walk_forward_complete folds={} avg_return={} median_return={} positive_folds={} cumulative_return={}",
            result.summary["fold_count"],
            result.summary["avg_return"],
            result.summary["median_return"],
            result.summary["positive_folds"],
            result.summary["cumulative_return"],
        )
        return

    if args.runtime:
        _validate_live_runtime_request(
            runtime_config=runtime_config,
            use_mock_connector=args.use_mock_connector,
            enable_live_trading=args.enable_live_trading,
            live_confirmation=args.live_confirmation,
            perp_max_leverage=args.perp_max_leverage,
        )
        orchestrator = asyncio.run(
            run_runtime_orchestrator(
                config=runtime_config,
                mode=args.runtime,
                iterations=args.runtime_iterations,
                interval_seconds=args.runtime_interval,
                use_mock_connector=args.use_mock_connector,
                watchdog_timeout_seconds=args.watchdog_timeout,
                watchdog_restarts=args.watchdog_restarts,
                exchange=None if args.execution_exchange == "auto" else args.execution_exchange,
                resume_runtime=args.resume_runtime,
                risk_per_trade_pct=args.risk_per_trade_pct,
                perp_max_leverage=args.perp_max_leverage,
                perp_sandbox_reset=args.perp_sandbox_reset,
                target_annual_volatility=args.target_annual_vol,
            )
        )
        if args.dashboard:
            print_health_dashboard(limit=args.report_limit, runtime_config=runtime_config, orchestrator=orchestrator)
        if args.report:
            print_trade_report(limit=args.report_limit)
        if args.post_run_analysis:
            _since = args.since or (datetime.now().strftime("%Y-%m-%d") if args.since_today else None)
            print_post_run_analysis(limit=args.report_limit, since=_since)
        if args.daily_summary:
            summary_date = None
            if args.daily_summary_date:
                try:
                    summary_date = datetime.strptime(args.daily_summary_date, "%Y-%m-%d")
                except ValueError as exc:
                    raise SystemExit(f"invalid daily summary date: {args.daily_summary_date}") from exc
            summary = logger_store.get_daily_summary(report_date=summary_date)
            print("Daily summary report")
            print("-" * 22)
            print(f"Date: {summary['report_date']}")
            print(f"Trades: {summary['total_trades']}")
            print(f"Starting equity: {summary['starting_equity']:.4f}")
            print(f"Ending equity: {summary['ending_equity']:.4f}")
            print(f"PnL: {summary['total_pnl']:.4f}")
            print(f"Max drawdown: {summary['max_drawdown']:.4f} ({summary['max_drawdown_pct']:.2%})")
            print(f"Alerts: {summary['alert_count']}")
            print(f"Active alerts: {', '.join(summary['active_alerts']) if summary['active_alerts'] else 'none'}")
            print(f"Runtime status: {summary['runtime_status']}")
            print(f"Research status: {summary['research_status']}")
            print(f"Summary: {summary['summary_text']}")
        return

    if args.dashboard:
        print_health_dashboard(limit=args.report_limit, runtime_config=runtime_config)
        return

    if args.report:
        print_trade_report(limit=args.report_limit)
        return

    if args.post_run_analysis:
        _since = args.since or (datetime.now().strftime("%Y-%m-%d") if args.since_today else None)
        print_post_run_analysis(limit=args.report_limit, since=_since)
        return

    if args.daily_summary:
        summary_date = None
        if args.daily_summary_date:
            try:
                summary_date = datetime.strptime(args.daily_summary_date, "%Y-%m-%d")
            except ValueError as exc:
                raise SystemExit(f"invalid daily summary date: {args.daily_summary_date}") from exc
        summary = logger_store.get_daily_summary(report_date=summary_date)
        print("Daily summary report")
        print("-" * 22)
        print(f"Date: {summary['report_date']}")
        print(f"Trades: {summary['total_trades']}")
        print(f"Starting equity: {summary['starting_equity']:.4f}")
        print(f"Ending equity: {summary['ending_equity']:.4f}")
        print(f"PnL: {summary['total_pnl']:.4f}")
        print(f"Max drawdown: {summary['max_drawdown']:.4f} ({summary['max_drawdown_pct']:.2%})")
        print(f"Alerts: {summary['alert_count']}")
        print(f"Active alerts: {', '.join(summary['active_alerts']) if summary['active_alerts'] else 'none'}")
        print(f"Runtime status: {summary['runtime_status']}")
        print(f"Research status: {summary['research_status']}")
        print(f"Summary: {summary['summary_text']}")
        return

    if args.demo_backtest:
        bars = build_demo_bars()
        if args.use_kraken_data:
            bars = build_kraken_bars(symbol=args.kraken_symbol, count=args.kraken_bars)

        if args.paper_trading:
            run_paper_trading(bars, strategy_name=args.strategy, strategy_params=runtime_config.strategy_params)
            return

        config = BacktestConfig(
            strategy_name=args.strategy,
            include_costs=args.include_costs,
            taker_fee=args.taker_fee,
            maker_fee=args.maker_fee,
            fx_spread_bps=args.fx_spread_bps,
        )
        if args.compare_costs:
            comparison = compare_backtests(bars, config=config)
            logger.info(
                "demo_backtest_compare strategy={} baseline_return={} cost_return={} equity_delta={} baseline_sharpe={} cost_sharpe={} bars={}",
                config.strategy_name,
                comparison.baseline.total_return,
                comparison.with_costs.total_return,
                comparison.equity_delta,
                comparison.baseline.metrics.sharpe_ratio,
                comparison.with_costs.metrics.sharpe_ratio,
                len(bars),
            )
        else:
            result = run_backtest(bars, config=config)
            logger.info(
                "demo_backtest_complete strategy={} include_costs={} total_return={} trades={} final_equity={} bars={}",
                config.strategy_name,
                config.include_costs,
                result.total_return,
                result.trades,
                result.final_equity,
                len(bars),
            )
        return

    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
