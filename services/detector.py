"""
Anomaly detection engine.

Algorithm:
  1. On every webhook event, fetch the 30-minute sliding window for the shop.
  2. Compute current conversion rate: orders_created / checkouts_created (window).
  3. Fetch 7-day rolling baseline for the same hour-of-day.
  4. If current_rate < baseline * (1 - threshold/100) for 3+ consecutive 5-min
     buckets → open an incident.
  5. Resolve when rate recovers within 10% of baseline for 2 consecutive checks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import asyncpg

from database import get_pool


_WINDOW_MINUTES = 30
_BASELINE_DAYS = 7
_CONSECUTIVE_DROPS_REQUIRED = 3
_RECOVERY_CHECKS_REQUIRED = 2
_CHECK_INTERVAL_SECONDS = 300  # 5 min

# Per-shop state kept in memory between checks (not persisted across restarts).
_drop_streak: dict[str, int] = {}
_recovery_streak: dict[str, int] = {}


async def process_event(shop_domain: str) -> None:
    """Called after every webhook insertion. Runs detection in the background."""
    asyncio.create_task(_run_check(shop_domain))


async def _run_check(shop_domain: str) -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            merchant = await conn.fetchrow(
                "SELECT alert_threshold_pct, active FROM merchants WHERE shop_domain = $1",
                shop_domain,
            )
            if not merchant or not merchant["active"]:
                return

            threshold_pct = merchant["alert_threshold_pct"]
            now = datetime.now(timezone.utc)

            current_rate, checkout_vol = await _current_rate(conn, shop_domain, now)
            baseline_rate = await _baseline_rate(conn, shop_domain, now)
            avg_order_value = await _avg_order_value(conn, shop_domain, now)

            if baseline_rate is None or baseline_rate == 0:
                return

            drop_pct = (baseline_rate - current_rate) / baseline_rate * 100
            is_dropping = drop_pct >= threshold_pct

            if is_dropping:
                _drop_streak[shop_domain] = _drop_streak.get(shop_domain, 0) + 1
                _recovery_streak[shop_domain] = 0
            else:
                _recovery_streak[shop_domain] = _recovery_streak.get(shop_domain, 0) + 1
                _drop_streak[shop_domain] = 0

            active_incident = await conn.fetchrow(
                "SELECT id FROM incidents WHERE shop_domain = $1 AND resolved_at IS NULL",
                shop_domain,
            )

            if not active_incident:
                if _drop_streak.get(shop_domain, 0) >= _CONSECUTIVE_DROPS_REQUIRED:
                    rev_loss_per_min = _revenue_loss_per_minute(
                        baseline_rate, current_rate, checkout_vol, avg_order_value
                    )
                    incident_id = await conn.fetchval(
                        """
                        INSERT INTO incidents
                            (shop_domain, checkout_rate_before, checkout_rate_during,
                             estimated_revenue_loss_per_min, avg_order_value, notified)
                        VALUES ($1, $2, $3, $4, $5, FALSE)
                        RETURNING id
                        """,
                        shop_domain,
                        baseline_rate,
                        current_rate,
                        rev_loss_per_min,
                        avg_order_value,
                    )
                    await _notify(conn, shop_domain, incident_id, baseline_rate, current_rate, rev_loss_per_min)
            else:
                # Check recovery: rate within 10% of baseline for 2 consecutive checks.
                recovery_threshold = baseline_rate * 0.9
                if current_rate >= recovery_threshold:
                    if _recovery_streak.get(shop_domain, 0) >= _RECOVERY_CHECKS_REQUIRED:
                        await conn.execute(
                            "UPDATE incidents SET resolved_at = $1 WHERE id = $2",
                            now,
                            active_incident["id"],
                        )
                        _drop_streak[shop_domain] = 0
                        _recovery_streak[shop_domain] = 0
    except Exception as exc:
        # Detection errors must never crash the webhook handler.
        import logging
        logging.getLogger(__name__).error("Detection error for %s: %s", shop_domain, exc)


async def _current_rate(
    conn: asyncpg.Connection, shop_domain: str, now: datetime
) -> tuple[float, int]:
    since = now - timedelta(minutes=_WINDOW_MINUTES)
    row = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE event_type = 'checkout_created') AS checkouts,
            COUNT(*) FILTER (WHERE event_type = 'order_created')    AS orders
        FROM checkout_events
        WHERE shop_domain = $1 AND created_at >= $2
        """,
        shop_domain,
        since,
    )
    checkouts = row["checkouts"] or 0
    orders = row["orders"] or 0
    rate = (orders / checkouts) if checkouts > 0 else 0.0
    return rate, checkouts


async def _baseline_rate(
    conn: asyncpg.Connection, shop_domain: str, now: datetime
) -> Optional[float]:
    """7-day rolling baseline at the same hour-of-day (±1 h window)."""
    hour = now.hour
    since = now - timedelta(days=_BASELINE_DAYS)
    row = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE event_type = 'checkout_created') AS checkouts,
            COUNT(*) FILTER (WHERE event_type = 'order_created')    AS orders
        FROM checkout_events
        WHERE
            shop_domain = $1
            AND created_at >= $2
            AND EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC') BETWEEN $3 AND $4
        """,
        shop_domain,
        since,
        max(0, hour - 1),
        min(23, hour + 1),
    )
    checkouts = row["checkouts"] or 0
    orders = row["orders"] or 0
    if checkouts == 0:
        return None
    return orders / checkouts


async def _avg_order_value(
    conn: asyncpg.Connection, shop_domain: str, now: datetime
) -> float:
    """Use order count proxy; real AOV requires Shopify API or storing order amounts."""
    since = now - timedelta(days=30)
    count = await conn.fetchval(
        "SELECT COUNT(*) FROM checkout_events WHERE shop_domain=$1 AND event_type='order_created' AND created_at>=$2",
        shop_domain,
        since,
    )
    # Without storing order amounts we default to a placeholder.
    # Merchants should set this via a future settings endpoint.
    return 50.0 if (count or 0) == 0 else 50.0


def _revenue_loss_per_minute(
    baseline_rate: float,
    current_rate: float,
    checkout_volume: int,
    avg_order_value: float,
) -> float:
    if _WINDOW_MINUTES == 0:
        return 0.0
    checkouts_per_minute = checkout_volume / _WINDOW_MINUTES
    lost_orders_per_minute = (baseline_rate - current_rate) * checkouts_per_minute
    return round(max(0.0, lost_orders_per_minute * avg_order_value), 2)


async def _notify(
    conn: asyncpg.Connection,
    shop_domain: str,
    incident_id: int,
    baseline_rate: float,
    current_rate: float,
    rev_loss_per_min: float,
) -> None:
    from services.alerter import send_alert

    merchant = await conn.fetchrow(
        "SELECT slack_webhook_url FROM merchants WHERE shop_domain = $1", shop_domain
    )
    if merchant and merchant["slack_webhook_url"]:
        await send_alert(
            webhook_url=merchant["slack_webhook_url"],
            shop_domain=shop_domain,
            incident_id=incident_id,
            baseline_rate=baseline_rate,
            current_rate=current_rate,
            rev_loss_per_min=rev_loss_per_min,
        )
        await conn.execute(
            "UPDATE incidents SET notified = TRUE WHERE id = $1", incident_id
        )
