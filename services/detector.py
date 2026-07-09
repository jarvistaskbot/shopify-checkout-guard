"""
Anomaly detection engine — order volume based.

Algorithm:
  1. On every orders/create webhook, count orders in a 30-min sliding window.
  2. Fetch 7-day rolling baseline for the same hour-of-day (+-1 h).
  3. If current_volume < baseline * (1 - threshold/100) for 3+ consecutive
     triggers -> open an incident.
  4. Resolve when volume recovers within 10% of baseline for 2 consecutive checks.
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


async def process_event(shop_domain: str) -> None:
    """Called after every orders/create insertion. Runs detection in background."""
    asyncio.create_task(_run_check(shop_domain))


async def _run_check(shop_domain: str) -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            merchant = await conn.fetchrow(
                """
                SELECT alert_threshold_pct, active, drop_streak, recovery_streak, avg_order_value
                FROM merchants WHERE shop_domain = $1
                """,
                shop_domain,
            )
            if not merchant or not merchant["active"]:
                return

            threshold_pct = merchant["alert_threshold_pct"]
            drop_streak = merchant["drop_streak"]
            recovery_streak = merchant["recovery_streak"]
            avg_order_value = float(merchant["avg_order_value"])
            now = datetime.now(timezone.utc)

            current_volume = await _current_volume(conn, shop_domain, now)
            baseline_volume = await _baseline_volume(conn, shop_domain, now)

            if baseline_volume is None or baseline_volume == 0:
                return

            drop_pct = (baseline_volume - current_volume) / baseline_volume * 100
            is_dropping = drop_pct >= threshold_pct

            if is_dropping:
                drop_streak += 1
                recovery_streak = 0
            else:
                recovery_streak += 1
                drop_streak = 0

            await conn.execute(
                "UPDATE merchants SET drop_streak = $1, recovery_streak = $2 WHERE shop_domain = $3",
                drop_streak,
                recovery_streak,
                shop_domain,
            )

            active_incident = await conn.fetchrow(
                "SELECT id, started_at FROM incidents WHERE shop_domain = $1 AND resolved_at IS NULL",
                shop_domain,
            )

            if not active_incident:
                if drop_streak >= _CONSECUTIVE_DROPS_REQUIRED:
                    rev_loss_per_min = _revenue_loss_per_minute(
                        baseline_volume, current_volume, avg_order_value
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
                        float(baseline_volume),
                        float(current_volume),
                        rev_loss_per_min,
                        avg_order_value,
                    )
                    await _notify(conn, shop_domain, incident_id, baseline_volume, current_volume, rev_loss_per_min)
            else:
                recovery_threshold = baseline_volume * 0.9
                if current_volume >= recovery_threshold and recovery_streak >= _RECOVERY_CHECKS_REQUIRED:
                    await conn.execute(
                        "UPDATE incidents SET resolved_at = $1 WHERE id = $2",
                        now,
                        active_incident["id"],
                    )
                    await conn.execute(
                        "UPDATE merchants SET drop_streak = 0, recovery_streak = 0 WHERE shop_domain = $1",
                        shop_domain,
                    )
                    slack_row = await conn.fetchrow(
                        "SELECT slack_webhook_url FROM merchants WHERE shop_domain = $1",
                        shop_domain,
                    )
                    if slack_row and slack_row["slack_webhook_url"]:
                        from services.alerter import send_recovery_alert
                        duration_minutes = int(
                            (now - active_incident["started_at"]).total_seconds() / 60
                        )
                        await send_recovery_alert(
                            webhook_url=slack_row["slack_webhook_url"],
                            shop_domain=shop_domain,
                            incident_id=active_incident["id"],
                            duration_minutes=duration_minutes,
                        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("Detection error for %s: %s", shop_domain, exc)


async def _current_volume(
    conn: asyncpg.Connection, shop_domain: str, now: datetime
) -> int:
    """Orders received in the last 30 minutes."""
    since = now - timedelta(minutes=_WINDOW_MINUTES)
    return await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain = $1
          AND event_type = 'order_created'
          AND created_at >= $2
        """,
        shop_domain,
        since,
    ) or 0


async def _baseline_volume(
    conn: asyncpg.Connection, shop_domain: str, now: datetime
) -> Optional[float]:
    """Average 30-min order count for this hour-of-day over the last 7 days."""
    hour = now.hour
    since = now - timedelta(days=_BASELINE_DAYS)

    total = await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain = $1
          AND event_type = 'order_created'
          AND created_at >= $2
          AND EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC') BETWEEN $3 AND $4
        """,
        shop_domain,
        since,
        max(0, hour - 1),
        min(23, hour + 1),
    ) or 0

    if total == 0:
        return None

    # 30-min slots in a 3-hour window over 7 days = 7 * 6 = 42
    slots = _BASELINE_DAYS * 6
    return total / slots


def _revenue_loss_per_minute(
    baseline_volume: float,
    current_volume: int,
    avg_order_value: float,
) -> float:
    lost_orders = max(0.0, baseline_volume - current_volume)
    lost_orders_per_minute = lost_orders / _WINDOW_MINUTES
    return round(lost_orders_per_minute * avg_order_value, 2)


async def _notify(
    conn: asyncpg.Connection,
    shop_domain: str,
    incident_id: int,
    baseline_volume: float,
    current_volume: int,
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
            baseline_rate=baseline_volume,
            current_rate=float(current_volume),
            rev_loss_per_min=rev_loss_per_min,
        )
        await conn.execute(
            "UPDATE incidents SET notified = TRUE WHERE id = $1", incident_id
        )
