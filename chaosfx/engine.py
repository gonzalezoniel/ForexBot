import time
import logging
from datetime import datetime, date, timezone
from typing import Dict, Any, List, Optional

from chaosfx.config import settings
from chaosfx.oanda_client import OandaClient
from chaosfx.strategy import generate_signal, Signal
from chaosfx.risk import (
    compute_position_size,
    daily_drawdown_exceeded,
    validate_risk_reward,
    compute_portfolio_risk,
    get_effective_max_trades,
    check_consecutive_loss_kill_switch,
    compute_currency_exposure,
    get_usd_directional_bias,
    would_stack_usd_exposure,
    compute_trade_risk_pct,
)

logger = logging.getLogger("chaosfx")
logger.setLevel(settings.LOG_LEVEL)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
    )
    logger.addHandler(handler)


def _is_forex_market_open() -> bool:
    """
    Returns True when the forex market is actually open.
    Forex trades Sun 17:00 ET (22:00 UTC) through Fri 17:00 ET (22:00 UTC).
    Closed Fri 22:00 UTC → Sun 22:00 UTC.
    """
    now = datetime.now(timezone.utc)
    wd = now.weekday()  # Mon=0 … Sun=6
    hour = now.hour

    if wd == 5:                       # Saturday
        return False
    if wd == 6 and hour < 22:         # Sunday before open
        return False
    if wd == 4 and hour >= 22:        # Friday after close
        return False
    return True


def _in_trading_session() -> bool:
    """
    Rough session filter: by default trade only between SESSION_UTC_START_HOUR
    and SESSION_UTC_END_HOUR, which roughly covers London+NY.
    """
    if not settings.SESSION_ONLY:
        return True

    now = datetime.now(timezone.utc)
    hour = now.hour
    return settings.SESSION_UTC_START_HOUR <= hour < settings.SESSION_UTC_END_HOUR


class ChaosEngineFX:
    """
    Chaos-style Forex engine:

    - Scans multiple FX pairs
    - Computes volatility and ranks pairs
    - Generates signals using pattern + volatility strategy
    - Applies risk management
    - Places market orders via Oanda
    - Tracks history and recent trades for dashboard
    """

    def __init__(self):
        self.client = OandaClient()
        self.last_day: date = date.today()
        self.start_of_day_equity: float = self._get_equity()

        # Dashboard / status
        self.last_summary: Optional[Dict[str, Any]] = None
        self.recent_runs: List[Dict[str, Any]] = []
        self.recent_trades: List[Dict[str, Any]] = []

        # AGGRESSIVE MODE: Track closed trade outcomes for kill switch
        self.closed_trade_results: List[Dict[str, Any]] = []

        logger.info("ChaosEngine-FX AGGRESSIVE MODE initialized")

    def _get_equity(self) -> float:
        summary = self.client.get_account_summary()
        return float(summary["NAV"])

    def _refresh_daily_equity_anchor(self):
        today = date.today()
        if today != self.last_day:
            self.last_day = today
            self.start_of_day_equity = self._get_equity()
            logger.info(
                f"New trading day detected. Start-of-day equity: {self.start_of_day_equity:.2f}"
            )

    def _can_trade(self) -> bool:
        equity = self._get_equity()
        exceeded, dd = daily_drawdown_exceeded(equity, self.start_of_day_equity)
        if exceeded:
            logger.warning(
                f"Daily drawdown limit exceeded: {dd*100:.2f}% (equity {equity:.2f}). "
                "No more trades for today."
            )
            return False
        return True

    def _record_run(self, summary: Dict[str, Any]) -> None:
        """Keep a rolling history in memory (for dashboard endpoints)."""
        self.last_summary = summary
        self.recent_runs.append(summary)
        if len(self.recent_runs) > 100:
            self.recent_runs = self.recent_runs[-100:]

    def _record_trade(self, trade: Dict[str, Any]) -> None:
        """
        Keep a small recent trade history for dashboard.
        """
        self.recent_trades.append(trade)
        if len(self.recent_trades) > settings.RECENT_TRADES_LIMIT:
            self.recent_trades = self.recent_trades[-settings.RECENT_TRADES_LIMIT :]

    def _update_closed_trades(self) -> None:
        """
        Fetch recent closed trades from OANDA and update the kill switch tracker.
        Logs R-multiple outcome per closed trade.
        """
        try:
            # Use the account's transaction history to detect closed trades
            # For simplicity, we track based on the account summary changes
            account = self.client.get_account_summary()
            # Store the latest PL info for kill switch tracking
            recent_pl = float(account.get("pl", 0))
            if self.closed_trade_results:
                last_pl = self.closed_trade_results[-1].get("cumulative_pl", 0)
                if recent_pl != last_pl:
                    trade_pl = recent_pl - last_pl
                    self.closed_trade_results.append({
                        "cumulative_pl": recent_pl,
                        "pl": trade_pl,
                        "realizedPL": trade_pl,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    logger.info(
                        f"[AGGRESSIVE LOG] Closed trade detected: PL={trade_pl:.2f} "
                        f"cumulative_PL={recent_pl:.2f}"
                    )
            else:
                self.closed_trade_results.append({
                    "cumulative_pl": recent_pl,
                    "pl": 0,
                    "realizedPL": 0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            logger.debug(f"Could not update closed trades: {e}")

    def run_once(self) -> Dict[str, Any]:
        """
        AGGRESSIVE MODE: Run a single scan-execute cycle across all pairs.

        Flow:
        1. Market hours & daily drawdown check
        2. Kill switch check (3 consecutive losses OR -6% drawdown)
        3. Portfolio risk & exposure analysis
        4. Scan ALL instruments for signals
        5. Rank by opportunity score (ATR expansion + trend + breakout)
        6. Execute top 1-2 signals with exposure controls
        7. Enhanced logging (risk %, portfolio risk, currency exposure, R multiple)

        Returns summary for logging / API.
        """
        self._refresh_daily_equity_anchor()
        self._update_closed_trades()

        # --- Market hours guard (weekend) ---
        if not _is_forex_market_open():
            logger.info("Forex market is closed (weekend), skipping cycle.")
            summary = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "equity": 0.0,
                "actions": [],
                "reason": "market_closed",
                "surge_mode": False,
                "portfolio_risk_pct": 0.0,
                "currency_exposure": {},
            }
            self._record_run(summary)
            return summary

        account = self.client.get_account_summary()
        equity = float(account["NAV"])
        open_trades = self.client.get_open_trades()

        # -----------------------------------------------------------------------
        # AGGRESSIVE MODE: Compute portfolio risk & currency exposure
        # -----------------------------------------------------------------------
        portfolio_risk = compute_portfolio_risk(open_trades, equity)
        currency_exposure = compute_currency_exposure(open_trades)
        usd_bias = get_usd_directional_bias(currency_exposure)
        effective_max_trades = get_effective_max_trades(portfolio_risk)

        logger.info(
            f"[AGGRESSIVE] Run cycle - equity: {equity:.2f}, "
            f"open_trades: {len(open_trades)}, "
            f"portfolio_risk: {portfolio_risk*100:.2f}%, "
            f"max_trades_allowed: {effective_max_trades}, "
            f"USD_bias: {usd_bias}"
        )

        # Log currency exposure
        if currency_exposure:
            exposure_str = ", ".join(
                f"{ccy}={exp:+.0f}" for ccy, exp in sorted(currency_exposure.items())
            )
            logger.info(f"[AGGRESSIVE LOG] Currency exposure: {exposure_str}")

        in_session = _in_trading_session()

        # -----------------------------------------------------------------------
        # AGGRESSIVE MODE: Daily drawdown kill switch (-6%)
        # -----------------------------------------------------------------------
        if not self._can_trade():
            summary = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "equity": equity,
                "actions": [],
                "reason": "kill_switch_daily_drawdown_exceeded",
                "surge_mode": False,
                "portfolio_risk_pct": portfolio_risk * 100,
                "currency_exposure": currency_exposure,
            }
            self._record_run(summary)
            logger.warning("[AGGRESSIVE KILL SWITCH] Daily drawdown limit exceeded. Trading halted.")
            return summary

        # -----------------------------------------------------------------------
        # AGGRESSIVE MODE: Consecutive loss kill switch (3 losses)
        # -----------------------------------------------------------------------
        if check_consecutive_loss_kill_switch(self.closed_trade_results):
            logger.warning(
                f"[AGGRESSIVE KILL SWITCH] {settings.KILL_SWITCH_CONSECUTIVE_LOSSES} "
                "consecutive losses detected. Trading halted for this cycle."
            )
            summary = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "equity": equity,
                "actions": [],
                "reason": "kill_switch_consecutive_losses",
                "surge_mode": False,
                "portfolio_risk_pct": portfolio_risk * 100,
                "currency_exposure": currency_exposure,
            }
            self._record_run(summary)
            return summary

        actions: List[Dict[str, Any]] = []

        # -----------------------------------------------------------------------
        # AGGRESSIVE MODE: Dynamic max trades based on portfolio risk
        # -----------------------------------------------------------------------
        if len(open_trades) >= effective_max_trades:
            logger.info(
                f"Max open trades reached ({len(open_trades)}/{effective_max_trades}), "
                f"portfolio_risk={portfolio_risk*100:.2f}%"
            )
            summary = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "equity": equity,
                "actions": [],
                "reason": "max_open_trades_reached",
                "surge_mode": False,
                "portfolio_risk_pct": portfolio_risk * 100,
                "currency_exposure": currency_exposure,
            }
            self._record_run(summary)
            return summary

        # ---- ANALYZE ALL PAIRS (volatility + signal + confidence) ----
        analyses: List[Dict[str, Any]] = []
        for pair in settings.FOREX_PAIRS[: settings.MAX_PAIRS]:
            try:
                candles = self.client.get_candles(pair, granularity="M1", count=200)
                signal, df, meta = generate_signal(
                    instrument=pair,
                    candles=candles,
                    sl_pips=settings.DEFAULT_SL_PIPS,
                    tp_pips=settings.DEFAULT_TP_PIPS,
                )
                vol_score = float(meta.get("volatility", 0.0))
                confidence = float(meta.get("confidence", 0.0))
                opportunity_score = float(meta.get("opportunity_score", 0.0))

                analyses.append(
                    {
                        "pair": pair,
                        "signal": signal,
                        "df": df,
                        "volatility": vol_score,
                        "confidence": confidence,
                        "opportunity_score": opportunity_score,
                        "meta": meta,
                    }
                )
                logger.debug(
                    f"{pair}: signal={signal.side} conf={confidence:.2f} "
                    f"reason={signal.reason} vol={vol_score:.6f} "
                    f"opp_score={opportunity_score:.4f} "
                    f"atr_exp={meta.get('atr_expanding')} "
                    f"breakout={meta.get('breakout_confirmed')} "
                    f"trend={meta.get('trend_aligned')} "
                    f"rr={meta.get('risk_reward', 0):.2f}"
                )
            except Exception as e:
                logger.exception(f"Error analyzing {pair}: {e}")

        # Vol thresholds
        extreme_threshold = settings.VOLATILITY_MIN_SCORE * settings.VOLATILITY_EXTREME_MULTIPLIER
        extreme_pairs = [a for a in analyses if a["volatility"] >= extreme_threshold]

        # Session handling with surge mode
        surge_mode = False
        if not in_session and not extreme_pairs:
            summary = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "equity": equity,
                "actions": [],
                "reason": "outside_session_hours",
                "surge_mode": False,
                "portfolio_risk_pct": portfolio_risk * 100,
                "currency_exposure": currency_exposure,
            }
            self._record_run(summary)
            return summary
        elif not in_session and extreme_pairs:
            surge_mode = True
            logger.info(
                f"Volatility surge mode active: {len(extreme_pairs)} extreme pairs"
            )

        # Filter by basic volatility threshold
        hot = [
            a
            for a in analyses
            if a["volatility"] >= settings.VOLATILITY_MIN_SCORE
        ]

        if not hot:
            summary = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "equity": equity,
                "actions": [],
                "reason": "no_pairs_above_vol_threshold",
                "surge_mode": surge_mode,
                "portfolio_risk_pct": portfolio_risk * 100,
                "currency_exposure": currency_exposure,
            }
            self._record_run(summary)
            return summary

        # -----------------------------------------------------------------------
        # AGGRESSIVE MODE: Opportunity ranking
        # Rank by opportunity_score (ATR expansion + trend strength + breakout)
        # Take only top 1-2 signals
        # -----------------------------------------------------------------------
        if surge_mode and not in_session:
            candidate_list = sorted(
                extreme_pairs, key=lambda x: x["opportunity_score"], reverse=True
            )
        else:
            candidate_list = sorted(
                hot, key=lambda x: x["opportunity_score"], reverse=True
            )

        # Log the ranking
        for rank, a in enumerate(candidate_list[:5], 1):
            logger.info(
                f"[AGGRESSIVE RANK #{rank}] {a['pair']}: "
                f"opp_score={a['opportunity_score']:.4f} "
                f"signal={a['signal'].side} "
                f"vol={a['volatility']:.6f} "
                f"conf={a['confidence']:.2f} "
                f"rr={a['meta'].get('risk_reward', 0):.2f}"
            )

        # ---- EXECUTE ON TOP-K CANDIDATES WITH REAL SIGNALS ----
        for a in candidate_list[: settings.VOLATILITY_TOP_K]:
            pair = a["pair"]
            signal: Signal = a["signal"]
            df = a["df"]
            confidence = a["confidence"]
            vol_score = a["volatility"]

            try:
                if any(t["instrument"] == pair for t in open_trades):
                    logger.debug(f"Skipping {pair}: trade already open")
                    continue

                # Require confidence
                if signal.side == "FLAT" or confidence < settings.CONFIDENCE_MIN:
                    continue

                # ---------------------------------------------------------------
                # AGGRESSIVE MODE: Currency exposure check
                # Block stacking of multiple USD-directional trades
                # ---------------------------------------------------------------
                if would_stack_usd_exposure(pair, signal.side, open_trades):
                    logger.info(
                        f"[AGGRESSIVE EXPOSURE] Blocked {signal.side} {pair}: "
                        f"would stack USD exposure (current bias: {usd_bias})"
                    )
                    continue

                last_price = float(df["close"].iloc[-1])

                # Scale risk in surge mode
                effective_equity = equity
                surge_for_this_pair = surge_mode and vol_score >= extreme_threshold
                if surge_for_this_pair:
                    effective_equity = equity * settings.EXTREME_RISK_FACTOR

                units = compute_position_size(
                    instrument=pair,
                    account_balance=effective_equity,
                    stop_loss_price=signal.stop_loss,
                    entry_price=last_price,
                )

                if units <= 0:
                    logger.debug(f"{pair}: position size <= 0, skip")
                    continue

                if signal.side == "SHORT":
                    units = -abs(units)
                else:
                    units = abs(units)

                # ---------------------------------------------------------------
                # AGGRESSIVE MODE: Check portfolio risk before entry
                # ---------------------------------------------------------------
                trade_risk_pct = compute_trade_risk_pct(
                    last_price, signal.stop_loss, units, equity
                )
                new_total_risk = portfolio_risk + trade_risk_pct

                if new_total_risk > settings.MAX_TOTAL_PORTFOLIO_RISK:
                    logger.info(
                        f"[AGGRESSIVE RISK] Blocked {pair}: "
                        f"would push portfolio risk to {new_total_risk*100:.2f}% "
                        f"(max {settings.MAX_TOTAL_PORTFOLIO_RISK*100:.0f}%)"
                    )
                    continue

                order_resp = self.client.create_market_order(
                    instrument=pair,
                    units=units,
                    stop_loss_price=signal.stop_loss,
                    take_profit_price=signal.take_profit,
                )

                trade_time = datetime.now(timezone.utc).isoformat()
                actual_rr = float(a["meta"].get("risk_reward", 0))

                action_info = {
                    "pair": pair,
                    "side": signal.side,
                    "units": units,
                    "entry_price": last_price,
                    "stop_loss": signal.stop_loss,
                    "take_profit": signal.take_profit,
                    "reason": signal.reason,
                    "volatility": vol_score,
                    "confidence": confidence,
                    "surge_mode": surge_for_this_pair,
                    "timestamp": trade_time,
                    "order_response": order_resp,
                    # AGGRESSIVE MODE: Enhanced logging fields
                    "risk_pct": trade_risk_pct * 100,
                    "portfolio_risk_pct": new_total_risk * 100,
                    "risk_reward": actual_rr,
                    "opportunity_score": a["opportunity_score"],
                    "currency_exposure": compute_currency_exposure(
                        self.client.get_open_trades()
                    ),
                }
                actions.append(action_info)
                self._record_trade(action_info)

                # Update portfolio risk for next iteration
                portfolio_risk = new_total_risk

                logger.info(
                    f"[AGGRESSIVE TRADE] Opened {signal.side} {pair} units={units} "
                    f"SL={signal.stop_loss:.5f} TP={signal.take_profit:.5f} "
                    f"R:R={actual_rr:.2f} risk={trade_risk_pct*100:.2f}% "
                    f"portfolio_risk={new_total_risk*100:.2f}% "
                    f"vol={vol_score:.6f} conf={confidence:.2f} "
                    f"opp_score={a['opportunity_score']:.4f} "
                    f"surge={surge_for_this_pair} reason={signal.reason}"
                )

                open_trades = self.client.get_open_trades()
                if len(open_trades) >= effective_max_trades:
                    logger.info(
                        f"Reached max open trades ({effective_max_trades}) "
                        f"during execution; stopping"
                    )
                    break

            except Exception as e:
                logger.exception(f"Error executing on {pair}: {e}")

        reason = "completed" if actions else "no_valid_signals_in_hot_pairs"
        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "equity": equity,
            "actions": actions,
            "reason": reason,
            "surge_mode": surge_mode,
            "portfolio_risk_pct": portfolio_risk * 100,
            "currency_exposure": currency_exposure,
        }
        self._record_run(summary)
        return summary

    def run_forever_blocking(self):
        logger.info("Starting ChaosEngine-FX loop")
        while True:
            summary = self.run_once()
            logger.debug(f"Cycle summary: {summary}")
            time.sleep(settings.LOOP_INTERVAL_SECONDS)
