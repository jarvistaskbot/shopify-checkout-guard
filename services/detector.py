"""
Multi-signal anomaly detection engine.

Detectors:
  1. checkout_funnel   — checkout→order conversion rate collapse (most powerful)
  2. order_silence     — no orders during expected peak, day-of-week aware
  3. abandonment_spike — sudden spike in unmatched checkouts
  4. payment_failure   — orders stuck in pending via Shopify API polling
  5. js_error_spike    — new JS error pattern >=10 occurrences in 10 min (v2)
  6. oos_hot_product   — hot product (>=5 orders/7d) hits inventory=0 (v2)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import asyncpg
import httpx

from database import get_pool

logger = logging.getLogger(__name__)

_WINDOW_MINUTES = 30
_BASELINE_DAYS = 7
_MIN_BASELINE_VOLUME = 1.0

# Funnel
_MIN_CHECKOUTS_FOR_FUNNEL = 5
_FUNNEL_ALERT_THRESHOLD = 0.50

# Silence
_CONSECUTIVE_DROPS_REQUIRED = 3
_RECOVERY_CHECKS_REQUIRED = 2

# Abandonment
_ABANDONMENT_SPIKE_MULTIPLIER = 3.0
_MIN_ABANDONMENTS_FOR_SPIKE = 5

# Payment
_PAYMENT_PENDING_MINUTES = 15
_MIN_PENDING_FOR_ALERT = 3
_PAYMENT_RECOVERY_CHECKS = 2


async def process_event(shop_domain: str, event_type: str = "order_created") -> None:
    asyncio.create_task(_run_realtime_checks(shop_domain, event_type))


async def run_proactive_checks_all_merchants() -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            shops = await conn.fetch(
                "SELECT shop_domain, access_token FROM merchants WHERE active = TRUE"
            )
        for row in shops:
            asyncio.create_task(
                _check_payment_failures(row["shop_domain"], row["access_token"])
            )
        asyncio.create_task(_resolve_stale_js_incidents())
        asyncio.create_task(run_slow_bleed_sweep())  # v3: hourly CUSUM (hour-guarded, cheap to re-call)
    except Exception as exc:
        logger.error("Proactive check loop error: %s", exc)


async def run_proactive_checks_fast_merchants() -> None:
    """Run proactive checks for pro/scale merchants only (1-min fast-check path)."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            shops = await conn.fetch(
                "SELECT shop_domain, access_token FROM merchants WHERE active = TRUE AND plan IN ('pro', 'scale')"
            )
        for row in shops:
            asyncio.create_task(
                _check_payment_failures(row["shop_domain"], row["access_token"])
            )
    except Exception as exc:
        logger.error("Fast proactive check loop error: %s", exc)


async def _run_realtime_checks(shop_domain: str, event_type: str) -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            merchant = await conn.fetchrow(
                """
                SELECT alert_threshold_pct, active, drop_streak, recovery_streak,
                       avg_order_value, slack_webhook_url, checkout_conversion_baseline,
                       alert_email, billing_status, plan, threshold_override
                FROM merchants WHERE shop_domain = $1
                """,
                shop_domain,
            )
            if not merchant or not merchant["active"]:
                return

            now = datetime.now(timezone.utc)
            await _check_checkout_funnel(conn, shop_domain, merchant, now)
            await _check_order_silence(conn, shop_domain, merchant, now)
            if event_type in ("checkout_created", "checkout_deleted"):
                await _check_abandonment_spike(conn, shop_domain, merchant, now)
    except Exception as exc:
        logger.error("Realtime detection error for %s: %s", shop_domain, exc)


# ---------------------------------------------------------------------------
# Detector 1: Checkout funnel collapse
# ---------------------------------------------------------------------------

async def _check_checkout_funnel(
    conn: asyncpg.Connection,
    shop_domain: str,
    merchant: asyncpg.Record,
    now: datetime,
) -> None:
    window_start = now - timedelta(minutes=_WINDOW_MINUTES)

    checkouts = await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain = $1 AND event_type = 'checkout_created' AND created_at >= $2
        """,
        shop_domain, window_start,
    ) or 0

    if checkouts < _MIN_CHECKOUTS_FOR_FUNNEL:
        return

    orders = await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain = $1 AND event_type = 'order_created' AND created_at >= $2
        """,
        shop_domain, window_start,
    ) or 0

    current_rate = orders / checkouts

    baseline_rate = merchant["checkout_conversion_baseline"]
    if not baseline_rate:
        baseline_rate = await _compute_conversion_baseline(conn, shop_domain, now)
        if baseline_rate:
            await conn.execute(
                "UPDATE merchants SET checkout_conversion_baseline = $1 WHERE shop_domain = $2",
                baseline_rate, shop_domain,
            )

    if not baseline_rate or float(baseline_rate) < 0.05:
        return

    is_broken = current_rate < float(baseline_rate) * _FUNNEL_ALERT_THRESHOLD

    active = await _get_active_incident(conn, shop_domain, "checkout_funnel_collapse")

    if not active and is_broken:
        aov = float(merchant["avg_order_value"])
        missed_orders = max(0, int(checkouts * float(baseline_rate)) - orders)
        revenue_at_risk = round(missed_orders * aov, 2)

        detail = {
            "checkouts": checkouts,
            "orders": orders,
            "current_rate": round(current_rate, 4),
            "baseline_rate": round(float(baseline_rate), 4),
        }

        incident_id = await conn.fetchval(
            """
            INSERT INTO incidents
                (shop_domain, checkout_rate_before, checkout_rate_during,
                 estimated_revenue_loss_per_min, avg_order_value, notified,
                 incident_type, detail)
            VALUES ($1, $2, $3, $4, $5, FALSE, 'checkout_funnel_collapse', $6::jsonb)
            RETURNING id
            """,
            shop_domain,
            float(baseline_rate),
            current_rate,
            round(revenue_at_risk / _WINDOW_MINUTES, 2),
            aov,
            json.dumps(detail),
        )
        from services.billing_guard import alerts_allowed
        if (merchant["slack_webhook_url"] or merchant["alert_email"]) and alerts_allowed(merchant.get("billing_status")):
            try:
                from services.alerter import send_checkout_funnel_alert
                ai_analysis = await _get_ai_analysis("checkout_funnel_collapse", detail, shop_domain)
                if ai_analysis:
                    await conn.execute("UPDATE incidents SET ai_analysis=$1 WHERE id=$2", ai_analysis, incident_id)
                await send_checkout_funnel_alert(
                    webhook_url=merchant["slack_webhook_url"],
                    shop_domain=shop_domain,
                    incident_id=incident_id,
                    checkouts=checkouts,
                    orders=orders,
                    current_rate=current_rate,
                    baseline_rate=float(baseline_rate),
                    aov=aov,
                    alert_email=merchant["alert_email"],
                    ai_analysis=ai_analysis,
                )
                await conn.execute("UPDATE incidents SET notified = TRUE WHERE id = $1", incident_id)
            except Exception as exc:
                logger.error("Funnel alert failed: %s", exc)

    elif active and not is_broken:
        await _resolve_incident(conn, shop_domain, active, now, merchant)


async def _compute_conversion_baseline(
    conn: asyncpg.Connection, shop_domain: str, now: datetime
) -> Optional[float]:
    since = now - timedelta(days=_BASELINE_DAYS)
    checkouts = await conn.fetchval(
        "SELECT COUNT(*) FROM checkout_events WHERE shop_domain=$1 AND event_type='checkout_created' AND created_at>=$2",
        shop_domain, since,
    ) or 0
    orders = await conn.fetchval(
        "SELECT COUNT(*) FROM checkout_events WHERE shop_domain=$1 AND event_type='order_created' AND created_at>=$2",
        shop_domain, since,
    ) or 0
    if checkouts < 10:
        return None
    return orders / checkouts


# ---------------------------------------------------------------------------
# Detector 2: Order silence (day-of-week aware)
# ---------------------------------------------------------------------------

async def _check_order_silence(
    conn: asyncpg.Connection,
    shop_domain: str,
    merchant: asyncpg.Record,
    now: datetime,
) -> None:
    window_start = now - timedelta(minutes=_WINDOW_MINUTES)
    current_volume = await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain = $1 AND event_type = 'order_created' AND created_at >= $2
        """,
        shop_domain, window_start,
    ) or 0

    baseline = await _compute_silence_baseline(conn, shop_domain, now)
    if baseline is None or baseline < _MIN_BASELINE_VOLUME:
        return

    # Scale tier: apply per-merchant threshold override if set.
    from services.billing_guard import plan_allows
    threshold_pct = merchant["alert_threshold_pct"]
    if plan_allows(merchant.get("plan"), "custom_thresholds"):
        override = merchant.get("threshold_override") or {}
        if isinstance(override, str):
            try:
                import json as _json
                override = _json.loads(override)
            except Exception:
                override = {}
        if override.get("alert_threshold_pct"):
            threshold_pct = int(override["alert_threshold_pct"])
    drop_pct = (baseline - current_volume) / baseline * 100
    is_dropping = drop_pct >= threshold_pct

    drop_streak = merchant["drop_streak"]
    recovery_streak = merchant["recovery_streak"]

    if is_dropping:
        drop_streak += 1
        recovery_streak = 0
    else:
        recovery_streak += 1
        drop_streak = 0

    await conn.execute(
        "UPDATE merchants SET drop_streak=$1, recovery_streak=$2 WHERE shop_domain=$3",
        drop_streak, recovery_streak, shop_domain,
    )

    active = await _get_active_incident(conn, shop_domain, "volume_drop")

    if not active and drop_streak >= _CONSECUTIVE_DROPS_REQUIRED:
        aov = float(merchant["avg_order_value"])
        rev_loss_per_min = round(max(0.0, baseline - current_volume) / _WINDOW_MINUTES * aov, 2)

        detail = {"drop_pct": round(drop_pct, 1), "baseline": round(float(baseline), 2)}

        incident_id = await conn.fetchval(
            """
            INSERT INTO incidents
                (shop_domain, checkout_rate_before, checkout_rate_during,
                 estimated_revenue_loss_per_min, avg_order_value, notified,
                 incident_type, detail)
            VALUES ($1, $2, $3, $4, $5, FALSE, 'volume_drop', $6::jsonb)
            RETURNING id
            """,
            shop_domain,
            float(baseline), float(current_volume),
            rev_loss_per_min, aov,
            json.dumps(detail),
        )
        from services.billing_guard import alerts_allowed as _alerts_allowed
        if (merchant["slack_webhook_url"] or merchant["alert_email"]) and _alerts_allowed(merchant.get("billing_status")):
            try:
                from services.alerter import send_silence_alert
                ai_analysis = await _get_ai_analysis("volume_drop", detail, shop_domain)
                if ai_analysis:
                    await conn.execute("UPDATE incidents SET ai_analysis=$1 WHERE id=$2", ai_analysis, incident_id)
                await send_silence_alert(
                    webhook_url=merchant["slack_webhook_url"],
                    shop_domain=shop_domain,
                    incident_id=incident_id,
                    baseline=baseline,
                    current_volume=current_volume,
                    aov=aov,
                    alert_email=merchant["alert_email"],
                    ai_analysis=ai_analysis,
                )
                await conn.execute("UPDATE incidents SET notified = TRUE WHERE id = $1", incident_id)
            except Exception as exc:
                logger.error("Silence alert failed: %s", exc)

    elif active and recovery_streak >= _RECOVERY_CHECKS_REQUIRED:
        await conn.execute(
            "UPDATE merchants SET drop_streak=0, recovery_streak=0 WHERE shop_domain=$1",
            shop_domain,
        )
        await _resolve_incident(conn, shop_domain, active, now, merchant)


async def _compute_silence_baseline(
    conn: asyncpg.Connection, shop_domain: str, now: datetime
) -> Optional[float]:
    weekday = now.weekday()
    hour = now.hour
    since = now - timedelta(days=28)

    total = await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain=$1 AND event_type='order_created' AND created_at>=$2
          AND EXTRACT(DOW FROM created_at AT TIME ZONE 'UTC')=$3
          AND EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC') BETWEEN $4 AND $5
        """,
        shop_domain, since, weekday,
        max(0, hour - 1), min(23, hour + 1),
    ) or 0

    if total > 0:
        return total / (4 * 6)

    total = await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain=$1 AND event_type='order_created'
          AND created_at >= $2
          AND EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC') BETWEEN $3 AND $4
        """,
        shop_domain, now - timedelta(days=_BASELINE_DAYS),
        max(0, hour - 1), min(23, hour + 1),
    ) or 0

    if total == 0:
        return None
    return total / (_BASELINE_DAYS * 6)


# ---------------------------------------------------------------------------
# Detector 3: Abandonment spike
# ---------------------------------------------------------------------------

async def _check_abandonment_spike(
    conn: asyncpg.Connection,
    shop_domain: str,
    merchant: asyncpg.Record,
    now: datetime,
) -> None:
    abandoned = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT ce.checkout_token) FROM checkout_events ce
        WHERE ce.shop_domain=$1 AND ce.event_type='checkout_created'
          AND ce.created_at BETWEEN $2 AND $3
          AND ce.checkout_token IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM checkout_events ord
              WHERE ord.shop_domain=$1 AND ord.event_type='order_created'
                AND ord.checkout_token = ce.checkout_token
          )
        """,
        shop_domain,
        now - timedelta(minutes=65),
        now - timedelta(minutes=35),
    ) or 0

    if abandoned < _MIN_ABANDONMENTS_FOR_SPIKE:
        return

    checkouts_same_window = await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain=$1 AND event_type='checkout_created'
          AND created_at BETWEEN $2 AND $3
        """,
        shop_domain,
        now - timedelta(minutes=65),
        now - timedelta(minutes=35),
    ) or 0

    current_abandon_rate = abandoned / max(1, checkouts_same_window)

    since = now - timedelta(days=_BASELINE_DAYS)
    bl_checkouts = await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain=$1 AND event_type='checkout_created' AND created_at BETWEEN $2 AND $3
        """,
        shop_domain, since, now - timedelta(minutes=35),
    ) or 0
    # Fixed: upper bound matches the same window used for bl_checkouts (excludes last 35 min)
    bl_orders = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT checkout_token) FROM checkout_events
        WHERE shop_domain=$1 AND event_type='order_created'
          AND created_at BETWEEN $2 AND $3 AND checkout_token IS NOT NULL
        """,
        shop_domain, since, now - timedelta(minutes=35),
    ) or 0

    if bl_checkouts == 0:
        return

    baseline_abandon_rate = max(0.0, (bl_checkouts - bl_orders) / bl_checkouts)
    is_spike = (
        current_abandon_rate > baseline_abandon_rate * _ABANDONMENT_SPIKE_MULTIPLIER
        and current_abandon_rate > 0.60
    )

    active = await _get_active_incident(conn, shop_domain, "abandonment_spike")

    if not active and is_spike:
        aov = float(merchant["avg_order_value"])
        detail = {
            "abandoned": abandoned,
            "checkouts": checkouts_same_window,
            "current_rate": round(current_abandon_rate, 4),
            "baseline_rate": round(baseline_abandon_rate, 4),
        }
        incident_id = await conn.fetchval(
            """
            INSERT INTO incidents
                (shop_domain, checkout_rate_before, checkout_rate_during,
                 estimated_revenue_loss_per_min, avg_order_value, notified,
                 incident_type, detail)
            VALUES ($1, $2, $3, 0, $4, FALSE, 'abandonment_spike', $5::jsonb)
            RETURNING id
            """,
            shop_domain,
            baseline_abandon_rate, current_abandon_rate, aov,
            json.dumps(detail),
        )
        from services.billing_guard import alerts_allowed as _ba
        if (merchant["slack_webhook_url"] or merchant["alert_email"]) and _ba(merchant.get("billing_status")):
            try:
                from services.alerter import send_abandonment_alert
                ai_analysis = await _get_ai_analysis("abandonment_spike", detail, shop_domain)
                if ai_analysis:
                    await conn.execute("UPDATE incidents SET ai_analysis=$1 WHERE id=$2", ai_analysis, incident_id)
                await send_abandonment_alert(
                    webhook_url=merchant["slack_webhook_url"],
                    shop_domain=shop_domain,
                    incident_id=incident_id,
                    abandoned=abandoned,
                    checkouts=checkouts_same_window,
                    current_rate=current_abandon_rate,
                    baseline_rate=baseline_abandon_rate,
                    aov=aov,
                    alert_email=merchant["alert_email"],
                    ai_analysis=ai_analysis,
                )
                await conn.execute("UPDATE incidents SET notified = TRUE WHERE id = $1", incident_id)
            except Exception as exc:
                logger.error("Abandonment alert failed: %s", exc)

    elif active and not is_spike:
        await _resolve_incident(conn, shop_domain, active, now, merchant)


# ---------------------------------------------------------------------------
# Detector 4: Payment failures (API polling)
# ---------------------------------------------------------------------------

async def _check_payment_failures(shop_domain: str, access_token: str) -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            merchant = await conn.fetchrow(
                "SELECT slack_webhook_url, alert_email, active, billing_status FROM merchants WHERE shop_domain=$1",
                shop_domain,
            )
            if not merchant or not merchant["active"]:
                return

            active = await _get_active_incident(conn, shop_domain, "payment_failure")

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=_PAYMENT_PENDING_MINUTES)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://{shop_domain}/admin/api/2024-10/orders.json",
                headers={"X-Shopify-Access-Token": access_token},
                params={
                    "financial_status": "pending",
                    "status": "open",
                    "created_at_max": cutoff.isoformat(),
                    "fields": "id,name,total_price,created_at",
                    "limit": 50,
                },
            )
            if resp.status_code != 200:
                return
            orders = resp.json().get("orders", [])

        pending_count = len(orders)

        # If an incident is already open, check for recovery.
        if active:
            if pending_count < _MIN_PENDING_FOR_ALERT:
                # Pending count back below threshold — resolve the incident.
                pool = await get_pool()
                async with pool.acquire() as conn:
                    now = datetime.now(timezone.utc)
                    await conn.execute(
                        "UPDATE incidents SET resolved_at=$1 WHERE id=$2", now, active["id"]
                    )
                    from services.billing_guard import alerts_allowed as _pf_ba
                    if (merchant["slack_webhook_url"] or merchant["alert_email"]) and _pf_ba(merchant.get("billing_status")):
                        try:
                            from services.alerter import send_recovery_alert
                            duration_minutes = int((now - active["started_at"]).total_seconds() / 60)
                            await send_recovery_alert(
                                webhook_url=merchant["slack_webhook_url"],
                                alert_email=merchant["alert_email"],
                                shop_domain=shop_domain,
                                incident_id=active["id"],
                                duration_minutes=duration_minutes,
                                incident_type="payment_failure",
                            )
                        except Exception as exc:
                            logger.error("Payment failure recovery alert failed: %s", exc)
            return

        # No active incident — check if we need to create one.
        if pending_count < _MIN_PENDING_FOR_ALERT:
            return

        order_names = [o["name"] for o in orders[:5]]
        total_at_risk = round(sum(float(o.get("total_price", 0)) for o in orders), 2)
        detail = {"pending_count": pending_count, "total_at_risk": total_at_risk, "order_names": order_names}

        async with pool.acquire() as conn:
            incident_id = await conn.fetchval(
                """
                INSERT INTO incidents
                    (shop_domain, checkout_rate_before, checkout_rate_during,
                     estimated_revenue_loss_per_min, avg_order_value, notified,
                     incident_type, detail)
                VALUES ($1, 0, 0, 0, 0, FALSE, 'payment_failure', $2::jsonb)
                RETURNING id
                """,
                shop_domain,
                json.dumps(detail),
            )
            from services.billing_guard import alerts_allowed as _pf_ba2
            if (merchant["slack_webhook_url"] or merchant["alert_email"]) and _pf_ba2(merchant.get("billing_status")):
                try:
                    from services.alerter import send_payment_failure_alert
                    ai_analysis = await _get_ai_analysis("payment_failure", detail, shop_domain)
                    if ai_analysis:
                        await conn.execute("UPDATE incidents SET ai_analysis=$1 WHERE id=$2", ai_analysis, incident_id)
                    await send_payment_failure_alert(
                        webhook_url=merchant["slack_webhook_url"],
                        shop_domain=shop_domain,
                        incident_id=incident_id,
                        pending_count=pending_count,
                        order_names=order_names,
                        total_at_risk=total_at_risk,
                        alert_email=merchant["alert_email"],
                        ai_analysis=ai_analysis,
                    )
                    await conn.execute("UPDATE incidents SET notified=TRUE WHERE id=$1", incident_id)
                except Exception as exc:
                    logger.error("Payment failure alert failed: %s", exc)
    except Exception as exc:
        logger.error("Payment failure check error for %s: %s", shop_domain, exc)


# ---------------------------------------------------------------------------
# Detector 5: JS error spike (v2)
# ---------------------------------------------------------------------------

_JS_SPIKE_MIN_COUNT = 10
_JS_SPIKE_WINDOW_MIN = 10
_JS_RESOLVE_QUIET_MIN = 60
_JS_RESOLVE_MAX_COUNT = 3
_JS_BASELINE_LOOKBACK_H = 24


async def check_js_error_spike(shop_domain: str, error_hash: str) -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            merchant = await conn.fetchrow(
                "SELECT slack_webhook_url, alert_email, active, billing_status, plan FROM merchants WHERE shop_domain = $1",
                shop_domain,
            )
            if not merchant or not merchant["active"]:
                return

            # JS error monitoring is a growth+ feature.
            from services.billing_guard import plan_allows as _pa
            if not _pa(merchant.get("plan"), "js_errors"):
                return

            now = datetime.now(timezone.utc)
            window_start = now - timedelta(minutes=_JS_SPIKE_WINDOW_MIN)
            baseline_start = now - timedelta(hours=_JS_BASELINE_LOOKBACK_H) - timedelta(minutes=_JS_SPIKE_WINDOW_MIN)

            count_10min = await conn.fetchval(
                """
                SELECT COUNT(*) FROM js_error_events
                WHERE shop_domain = $1 AND error_hash = $2 AND occurred_at >= $3
                """,
                shop_domain, error_hash, window_start,
            ) or 0

            if count_10min < _JS_SPIKE_MIN_COUNT:
                return

            prior_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM js_error_events
                WHERE shop_domain = $1 AND error_hash = $2
                  AND occurred_at >= $3 AND occurred_at < $4
                """,
                shop_domain, error_hash, baseline_start, window_start,
            ) or 0

            if prior_count > 0:
                return

            active = await _get_active_incident(conn, shop_domain, "js_error_spike")

            if active:
                detail = active.get("detail") or {}
                if isinstance(detail, str):
                    try:
                        detail = json.loads(detail)
                    except Exception:
                        detail = {}
                if detail.get("error_hash") == error_hash:
                    return

            sample = await conn.fetchrow(
                """
                SELECT error_message, page_url FROM js_error_events
                WHERE shop_domain = $1 AND error_hash = $2
                ORDER BY occurred_at DESC LIMIT 1
                """,
                shop_domain, error_hash,
            )
            message = sample["error_message"] if sample else "unknown"
            page_url = sample["page_url"] if sample else ""

            detail = {
                "error_hash": error_hash,
                "count_10min": count_10min,
                "message": message[:200],
                "page_url": page_url,
            }
            incident_id = await conn.fetchval(
                """
                INSERT INTO incidents
                    (shop_domain, checkout_rate_before, checkout_rate_during,
                     estimated_revenue_loss_per_min, avg_order_value, notified,
                     incident_type, detail)
                VALUES ($1, 0, 0, 0, 0, FALSE, 'js_error_spike', $2::jsonb)
                RETURNING id
                """,
                shop_domain,
                json.dumps(detail),
            )

            from services.billing_guard import alerts_allowed as _js_ba
            webhook = merchant["slack_webhook_url"]
            email = merchant["alert_email"]
            if (webhook or email) and _js_ba(merchant.get("billing_status")):
                try:
                    from services.alerter import send_js_error_alert
                    ai_analysis = await _get_ai_analysis("js_error_spike", detail, shop_domain)
                    if ai_analysis:
                        await conn.execute("UPDATE incidents SET ai_analysis=$1 WHERE id=$2", ai_analysis, incident_id)
                    await send_js_error_alert(
                        webhook_url=webhook,
                        alert_email=email,
                        shop_domain=shop_domain,
                        incident_id=incident_id,
                        count_10min=count_10min,
                        message=message,
                        page_url=page_url,
                        ai_analysis=ai_analysis,
                    )
                    await conn.execute("UPDATE incidents SET notified=TRUE WHERE id=$1", incident_id)
                except Exception as exc:
                    logger.error("JS error alert failed for %s: %s", shop_domain, exc)
    except Exception as exc:
        logger.error("JS error spike check error for %s: %s", shop_domain, exc)


async def _resolve_stale_js_incidents() -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            open_js = await conn.fetch(
                """
                SELECT id, shop_domain, detail, started_at FROM incidents
                WHERE incident_type = 'js_error_spike' AND resolved_at IS NULL
                """,
            )
            for row in open_js:
                detail = row["detail"] or {}
                if isinstance(detail, str):
                    try:
                        detail = json.loads(detail)
                    except Exception:
                        continue
                error_hash = detail.get("error_hash")
                if not error_hash:
                    continue

                quiet_start = datetime.now(timezone.utc) - timedelta(minutes=_JS_RESOLVE_QUIET_MIN)
                count_last_hour = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM js_error_events
                    WHERE shop_domain = $1 AND error_hash = $2 AND occurred_at >= $3
                    """,
                    row["shop_domain"], error_hash, quiet_start,
                ) or 0

                if count_last_hour < _JS_RESOLVE_MAX_COUNT:
                    merchant = await conn.fetchrow(
                        "SELECT slack_webhook_url, alert_email FROM merchants WHERE shop_domain = $1",
                        row["shop_domain"],
                    )
                    now = datetime.now(timezone.utc)
                    await conn.execute(
                        "UPDATE incidents SET resolved_at=$1 WHERE id=$2", now, row["id"]
                    )
                    if merchant and (merchant["slack_webhook_url"] or merchant["alert_email"]):
                        try:
                            from services.alerter import send_recovery_alert
                            duration_minutes = int((now - row["started_at"]).total_seconds() / 60)
                            await send_recovery_alert(
                                webhook_url=merchant["slack_webhook_url"],
                                alert_email=merchant["alert_email"],
                                shop_domain=row["shop_domain"],
                                incident_id=row["id"],
                                duration_minutes=duration_minutes,
                                incident_type="js_error_spike",
                            )
                        except Exception as exc:
                            logger.error("JS recovery alert failed: %s", exc)
    except Exception as exc:
        logger.error("Stale JS incident resolution error: %s", exc)


# ---------------------------------------------------------------------------
# Detector 6: OOS hot product (v2, gated by OOS_ENABLED)
# ---------------------------------------------------------------------------

_OOS_HOT_THRESHOLD = 5
_OOS_LOOKBACK_DAYS = 7


async def check_oos_hot_product(
    shop_domain: str,
    inventory_item_id: int,
    available: int,
) -> None:
    from config import settings
    if not settings.oos_enabled:
        return

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            merchant = await conn.fetchrow(
                "SELECT slack_webhook_url, alert_email, avg_order_value, active, billing_status, plan FROM merchants WHERE shop_domain = $1",
                shop_domain,
            )
            if not merchant or not merchant["active"]:
                return

            # OOS monitoring is a pro+ feature.
            from services.billing_guard import plan_allows as _pa2
            if not _pa2(merchant.get("plan"), "oos"):
                return

            product_id = await conn.fetchval(
                "SELECT product_id FROM inventory_levels WHERE shop_domain=$1 AND inventory_item_id=$2",
                shop_domain, inventory_item_id,
            )

            if product_id is None:
                return

            now = datetime.now(timezone.utc)
            since = now - timedelta(days=_OOS_LOOKBACK_DAYS)

            order_count = await conn.fetchval(
                """
                SELECT COALESCE(SUM(quantity), 0) FROM order_line_items
                WHERE shop_domain = $1 AND product_id = $2 AND created_at >= $3
                """,
                shop_domain, product_id, since,
            ) or 0

            if order_count < _OOS_HOT_THRESHOLD:
                return

            product_title = await conn.fetchval(
                """
                SELECT product_title FROM order_line_items
                WHERE shop_domain = $1 AND product_id = $2
                ORDER BY created_at DESC LIMIT 1
                """,
                shop_domain, product_id,
            ) or f"product #{product_id}"

            unit_price = await conn.fetchval(
                """
                SELECT price FROM order_line_items
                WHERE shop_domain = $1 AND product_id = $2 AND price IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                shop_domain, product_id,
            )
            if unit_price is None:
                unit_price = float(merchant["avg_order_value"])

            units_per_hour = order_count / (_OOS_LOOKBACK_DAYS * 24)
            revenue_per_hour = round(units_per_hour * float(unit_price), 2)

            if available > 0:
                active = await conn.fetchrow(
                    """
                    SELECT id, started_at FROM incidents
                    WHERE shop_domain=$1 AND incident_type='oos_hot_product' AND resolved_at IS NULL
                      AND detail->>'product_id' = $2::text
                    LIMIT 1
                    """,
                    shop_domain, str(product_id),
                )
                if active:
                    await conn.execute(
                        "UPDATE incidents SET resolved_at=NOW() WHERE id=$1", active["id"]
                    )
                    from services.billing_guard import alerts_allowed as _oos_ba
                    if (merchant["slack_webhook_url"] or merchant["alert_email"]) and _oos_ba(merchant.get("billing_status")):
                        try:
                            from services.alerter import send_recovery_alert
                            duration_minutes = int(
                                (now - active["started_at"]).total_seconds() / 60
                            )
                            await send_recovery_alert(
                                webhook_url=merchant["slack_webhook_url"],
                                alert_email=merchant["alert_email"],
                                shop_domain=shop_domain,
                                incident_id=active["id"],
                                duration_minutes=duration_minutes,
                                incident_type="oos_hot_product",
                            )
                        except Exception as exc:
                            logger.error("OOS recovery alert failed: %s", exc)
                return

            already_open = await conn.fetchrow(
                """
                SELECT id FROM incidents
                WHERE shop_domain=$1 AND incident_type='oos_hot_product' AND resolved_at IS NULL
                  AND detail->>'product_id' = $2::text
                LIMIT 1
                """,
                shop_domain, str(product_id),
            )
            if already_open:
                return

            detail = {
                "product_id": product_id,
                "product_title": product_title,
                "inventory_item_id": inventory_item_id,
                "orders_last_7d": order_count,
                "estimated_revenue_per_hour": revenue_per_hour,
                "unit_price": float(unit_price),
            }
            incident_id = await conn.fetchval(
                """
                INSERT INTO incidents
                    (shop_domain, checkout_rate_before, checkout_rate_during,
                     estimated_revenue_loss_per_min, avg_order_value, notified,
                     incident_type, detail)
                VALUES ($1, 0, 0, $2, $3, FALSE, 'oos_hot_product', $4::jsonb)
                RETURNING id
                """,
                shop_domain,
                round(revenue_per_hour / 60, 4),
                float(unit_price),
                json.dumps(detail),
            )

            from services.billing_guard import alerts_allowed as _oos_ba2
            if (merchant["slack_webhook_url"] or merchant["alert_email"]) and _oos_ba2(merchant.get("billing_status")):
                try:
                    from services.alerter import send_oos_alert
                    ai_analysis = await _get_ai_analysis("oos_hot_product", detail, shop_domain)
                    if ai_analysis:
                        await conn.execute("UPDATE incidents SET ai_analysis=$1 WHERE id=$2", ai_analysis, incident_id)
                    await send_oos_alert(
                        webhook_url=merchant["slack_webhook_url"],
                        alert_email=merchant["alert_email"],
                        shop_domain=shop_domain,
                        incident_id=incident_id,
                        product_title=product_title,
                        orders_last_7d=order_count,
                        revenue_per_hour=revenue_per_hour,
                        unit_price=float(unit_price),
                        ai_analysis=ai_analysis,
                    )
                    await conn.execute("UPDATE incidents SET notified=TRUE WHERE id=$1", incident_id)
                except Exception as exc:
                    logger.error("OOS alert failed for %s: %s", shop_domain, exc)
    except Exception as exc:
        logger.error("OOS check error for %s/%d: %s", shop_domain, inventory_item_id, exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_active_incident(
    conn: asyncpg.Connection, shop_domain: str, incident_type: str
) -> Optional[asyncpg.Record]:
    return await conn.fetchrow(
        """
        SELECT id, started_at, detail FROM incidents
        WHERE shop_domain=$1 AND incident_type=$2 AND resolved_at IS NULL
        ORDER BY started_at DESC LIMIT 1
        """,
        shop_domain, incident_type,
    )


async def _resolve_incident(
    conn: asyncpg.Connection,
    shop_domain: str,
    active: asyncpg.Record,
    now: datetime,
    merchant: asyncpg.Record,
) -> None:
    await conn.execute(
        "UPDATE incidents SET resolved_at=$1 WHERE id=$2", now, active["id"]
    )
    alert_email = dict(merchant).get("alert_email")
    if merchant["slack_webhook_url"] or alert_email:
        try:
            from services.alerter import send_recovery_alert
            duration_minutes = int((now - active["started_at"]).total_seconds() / 60)
            incident_type = await conn.fetchval(
                "SELECT incident_type FROM incidents WHERE id=$1", active["id"]
            )
            await send_recovery_alert(
                webhook_url=merchant["slack_webhook_url"],
                alert_email=alert_email,
                shop_domain=shop_domain,
                incident_id=active["id"],
                duration_minutes=duration_minutes,
                incident_type=incident_type or "unknown",
            )
        except Exception as exc:
            logger.error("Recovery alert failed: %s", exc)


async def _get_ai_analysis(incident_type: str, detail: dict, shop_domain: str) -> Optional[str]:
    try:
        from config import settings
        from services.ai_analyst import analyze_incident
        from services.billing_guard import consume_ai_budget, plan_allows
        from services.plans import get_ai_cap
        pool = await get_pool()
        # Fetch merchant plan to gate AI and use per-plan cap.
        async with pool.acquire() as conn:
            merchant_plan = await conn.fetchval(
                "SELECT plan FROM merchants WHERE shop_domain=$1", shop_domain
            ) or "starter"
        if not plan_allows(merchant_plan, "ai_analysis"):
            return None
        cap = get_ai_cap(merchant_plan)
        if not await consume_ai_budget(pool, shop_domain, cap):
            return None
        return await analyze_incident(
            incident_type=incident_type,
            detail=detail,
            shop_domain=shop_domain,
            api_key=settings.ai_api_key,
            enabled=settings.ai_analysis_enabled,
        )
    except Exception as exc:
        logger.warning("AI analysis wrapper error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Detector 7: Slow bleed (CUSUM on hourly checkout-start shortfall) — v3
# ---------------------------------------------------------------------------
# Single-window thresholds miss slow bleed: 25-35% under baseline never trips a
# 50% threshold in any window. CUSUM accumulates shortfall across completed
# hours, so "~30% under expectation for ~6 straight hours" fires even though no
# single hour looks alarming. Signal is checkout-starts (densest ingested
# signal); orders stay the slow confirmation. See docs/12-V3-DIRECTION.md §3.

_SB_SLACK = 0.15            # ignore shortfall within 15% of expectation (jitter)
_SB_ALERT_S = 0.90          # ~30% under for ~6h: 6 x (0.30 - 0.15) = 0.90
_SB_RESOLVE_S = 0.20        # statistic decayed back to normal -> resolve
_SB_RECOVERY_DECAY = 0.25   # decay per at/above-expectation hour
_SB_STAT_CAP = 3.0          # cap so recovery after a long outage isn't endless
_SB_MIN_EXPECTED = 1.0      # skip hours with expected < 1 checkout-start (too sparse)


def _cusum_update(s: float, ratio: float) -> float:
    """One-sided CUSUM step for one completed hour.

    ratio = observed/expected checkout-starts. Accumulates only the shortfall
    beyond the slack allowance; decays when the hour was at/above expectation.
    Pure function so the accumulation math is unit-testable.
    """
    if ratio < 1.0 - _SB_SLACK:
        s += (1.0 - _SB_SLACK) - ratio
    else:
        s = max(0.0, s - _SB_RECOVERY_DECAY)
    return min(s, _SB_STAT_CAP)


async def run_slow_bleed_sweep() -> None:
    """Hourly CUSUM sweep over all active merchants (called from the proactive
    loop; the per-merchant hour guard makes repeat calls within an hour free)."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            merchants = await conn.fetch(
                """
                SELECT shop_domain, cusum_stat, cusum_updated_at, avg_order_value,
                       slack_webhook_url, alert_email, billing_status, plan
                FROM merchants WHERE active = TRUE
                """
            )
            now = datetime.now(timezone.utc)
            for m in merchants:
                try:
                    await _check_slow_bleed(conn, m["shop_domain"], m, now)
                except Exception as exc:
                    logger.error("Slow-bleed check failed for %s: %s", m["shop_domain"], exc)
    except Exception as exc:
        logger.error("Slow-bleed sweep error: %s", exc)


async def _check_slow_bleed(
    conn: asyncpg.Connection,
    shop_domain: str,
    merchant: asyncpg.Record,
    now: datetime,
) -> None:
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    prev_hour_start = hour_start - timedelta(hours=1)

    # Process each completed hour exactly once.
    last = merchant["cusum_updated_at"]
    if last is not None and last >= hour_start:
        return

    expected = await _compute_start_rate_baseline(conn, shop_domain, prev_hour_start)
    if expected is None or expected < _SB_MIN_EXPECTED:
        # Too sparse to judge this hour — stamp it processed, leave S unchanged.
        await conn.execute(
            "UPDATE merchants SET cusum_updated_at=$1 WHERE shop_domain=$2",
            hour_start, shop_domain,
        )
        return

    observed = await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain=$1 AND event_type='checkout_created'
          AND created_at >= $2 AND created_at < $3
        """,
        shop_domain, prev_hour_start, hour_start,
    ) or 0

    ratio = observed / expected
    s = _cusum_update(float(merchant["cusum_stat"] or 0.0), ratio)

    await conn.execute(
        "UPDATE merchants SET cusum_stat=$1, cusum_updated_at=$2 WHERE shop_domain=$3",
        s, hour_start, shop_domain,
    )

    active = await _get_active_incident(conn, shop_domain, "slow_bleed")

    if not active and s >= _SB_ALERT_S:
        aov = float(merchant["avg_order_value"] or 0.0)
        detail = {
            "cusum": round(s, 2),
            "last_hour_observed": int(observed),
            "expected_per_hour": round(float(expected), 2),
            "last_ratio": round(ratio, 2),
        }
        incident_id = await conn.fetchval(
            """
            INSERT INTO incidents
                (shop_domain, checkout_rate_before, checkout_rate_during,
                 estimated_revenue_loss_per_min, avg_order_value, notified,
                 incident_type, detail)
            VALUES ($1, $2, $3, $4, $5, FALSE, 'slow_bleed', $6::jsonb)
            RETURNING id
            """,
            shop_domain,
            float(expected), float(observed),
            round(max(0.0, float(expected) - observed) / 60.0 * aov, 2), aov,
            json.dumps(detail),
        )
        from services.billing_guard import alerts_allowed as _alerts_allowed
        if (merchant["slack_webhook_url"] or merchant["alert_email"]) and _alerts_allowed(merchant.get("billing_status")):
            try:
                from services.alerter import send_slow_bleed_alert
                ai_analysis = await _get_ai_analysis("slow_bleed", detail, shop_domain)
                if ai_analysis:
                    await conn.execute("UPDATE incidents SET ai_analysis=$1 WHERE id=$2", ai_analysis, incident_id)
                await send_slow_bleed_alert(
                    webhook_url=merchant["slack_webhook_url"],
                    shop_domain=shop_domain,
                    incident_id=incident_id,
                    observed=int(observed),
                    expected=float(expected),
                    cusum=s,
                    aov=aov,
                    alert_email=merchant["alert_email"],
                    ai_analysis=ai_analysis,
                )
                await conn.execute("UPDATE incidents SET notified = TRUE WHERE id = $1", incident_id)
            except Exception as exc:
                logger.error("Slow-bleed alert failed: %s", exc)

    elif active and s <= _SB_RESOLVE_S:
        await _resolve_incident(conn, shop_domain, active, now, merchant)


async def _compute_start_rate_baseline(
    conn: asyncpg.Connection, shop_domain: str, hour_dt: datetime
) -> Optional[float]:
    """Expected checkout-starts for the hour beginning at hour_dt: 28-day
    same-weekday +-1h band (like the silence baseline), 7-day hour-band fallback.
    Divides by the ACTUAL band width so midnight/23:00 edges aren't inflated."""
    # Postgres EXTRACT(DOW) is 0=Sunday..6=Saturday; Python weekday() is
    # 0=Monday..6=Sunday — convert, or the weekday match is shifted by one day.
    weekday = (hour_dt.weekday() + 1) % 7
    hour = hour_dt.hour
    lo, hi = max(0, hour - 1), min(23, hour + 1)
    band_hours = hi - lo + 1

    total = await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain=$1 AND event_type='checkout_created' AND created_at>=$2
          AND EXTRACT(DOW FROM created_at AT TIME ZONE 'UTC')=$3
          AND EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC') BETWEEN $4 AND $5
        """,
        shop_domain, hour_dt - timedelta(days=28), weekday, lo, hi,
    ) or 0
    if total > 0:
        return total / (4 * band_hours)

    total = await conn.fetchval(
        """
        SELECT COUNT(*) FROM checkout_events
        WHERE shop_domain=$1 AND event_type='checkout_created' AND created_at>=$2
          AND EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC') BETWEEN $3 AND $4
        """,
        shop_domain, hour_dt - timedelta(days=_BASELINE_DAYS), lo, hi,
    ) or 0
    if total == 0:
        return None
    return total / (_BASELINE_DAYS * band_hours)
