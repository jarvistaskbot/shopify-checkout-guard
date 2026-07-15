"""
Billing enforcement helpers for CheckoutGuard.

Active statuses: 'active' (accepted charge, will pay after trial) or 'pending' (acceptance in-flight).
Inactive statuses: 'inactive' (never visited /billing/start), 'declined', 'cancelled'.

Alerts are suppressed for non-active merchants — data collection continues so the baseline
is warm when they do subscribe.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from services.plans import PLANS, get_ai_cap, get_order_cap

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = frozenset({"active", "pending"})


def plan_allows(merchant_plan: Optional[str], feature: str) -> bool:
    """Return True if the merchant's plan permits the requested feature."""
    p = PLANS.get(merchant_plan or "starter", PLANS["starter"])
    return bool(p.get(feature, False))


def alerts_allowed(billing_status: Optional[str]) -> bool:
    """Return True if this merchant's billing permits alert dispatch."""
    return (billing_status or "inactive") in _ACTIVE_STATUSES


def get_billing_banner(
    billing_status: Optional[str],
    trial_ends_at,
    shop: str,
) -> Optional[tuple]:
    """
    Return (css_class, html_message) for a billing-state UI banner, or None if active + paid.
    Always rendered separately from the incident status banner.
    """
    from html import escape
    safe_shop = escape(shop)
    status = billing_status or "inactive"
    now = datetime.now(timezone.utc)

    if status in _ACTIVE_STATUSES:
        if trial_ends_at and trial_ends_at > now:
            days_left = max(0, (trial_ends_at - now).days)
            label = "day" if days_left == 1 else "days"
            return (
                "banner-trial",
                f"Trial &mdash; {days_left} {label} left. "
                f"<a href='/billing/plans?shop={safe_shop}'>Manage subscription</a>",
            )
        return None

    if status == "declined":
        msg = "Your billing was declined."
    elif status == "cancelled":
        msg = "Your subscription was cancelled."
    else:
        msg = "Alerts are paused."

    return (
        "banner-subscribe",
        f"{msg} <a href='/billing/plans?shop={safe_shop}'>Start your 14-day free trial</a> "
        f"to activate CheckoutGuard alerts.",
    )


async def track_order_for_cap(pool, shop_domain: str) -> None:
    """
    Atomically increment the per-merchant monthly order counter and send a
    one-time Slack upgrade notice the first time the plan cap is crossed each
    calendar month.

    SOFT enforcement: monitoring and alerts are never degraded regardless of
    whether the cap is exceeded.
    """
    try:
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT orders_month, orders_month_reset_at, orders_cap_notice_sent_at,
                          plan, slack_webhook_url
                   FROM merchants WHERE shop_domain=$1""",
                shop_domain,
            )
        if not row:
            return

        # Month rollover: reset counter and clear the notice flag.
        reset_at = row["orders_month_reset_at"]
        if reset_at is None or reset_at.year != now.year or reset_at.month != now.month:
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE merchants
                       SET orders_month = 0,
                           orders_month_reset_at = $1,
                           orders_cap_notice_sent_at = NULL
                       WHERE shop_domain = $2""",
                    now, shop_domain,
                )

        # Atomic increment; return the updated count in one round-trip.
        async with pool.acquire() as conn:
            new_count = await conn.fetchval(
                "UPDATE merchants SET orders_month = orders_month + 1 "
                "WHERE shop_domain=$1 RETURNING orders_month",
                shop_domain,
            )
        if new_count is None:
            return

        cap = get_order_cap(row["plan"])
        if cap is None:  # scale / unlimited
            return
        if new_count <= cap:
            return

        # Cap exceeded — check whether we've already notified this month.
        notice_sent = row["orders_cap_notice_sent_at"]
        if (
            notice_sent is not None
            and notice_sent.year == now.year
            and notice_sent.month == now.month
        ):
            return

        # Claim the notice slot atomically to prevent duplicate Slack messages
        # from concurrent webhook calls that all crossed the threshold at once.
        async with pool.acquire() as conn:
            claimed = await conn.fetchval(
                """UPDATE merchants SET orders_cap_notice_sent_at=$1
                   WHERE shop_domain=$2
                     AND (orders_cap_notice_sent_at IS NULL
                          OR EXTRACT(YEAR  FROM orders_cap_notice_sent_at) != $3
                          OR EXTRACT(MONTH FROM orders_cap_notice_sent_at) != $4)
                   RETURNING shop_domain""",
                now, shop_domain, now.year, now.month,
            )
        if not claimed:
            return  # Another concurrent call already sent the notice this month.

        slack_url = row["slack_webhook_url"]
        if slack_url:
            plan_name = PLANS.get(row["plan"] or "starter", PLANS["starter"])["name"]
            await _send_order_cap_slack_notice(slack_url, shop_domain, cap, new_count, plan_name)

    except Exception as exc:
        logger.warning("Order cap tracking failed for %s: %s", shop_domain, exc)


async def _send_order_cap_slack_notice(
    slack_url: str,
    shop_domain: str,
    cap: int,
    count: int,
    plan_name: str,
) -> None:
    try:
        import httpx
        text = (
            f":chart_with_upwards_trend: *CheckoutGuard — order volume limit reached*\n"
            f"Your store `{shop_domain}` has processed *{count:,} orders* this month "
            f"(plan limit: {cap:,} on {plan_name}).\n"
            f"Your store has outgrown the {plan_name} plan — upgrade at "
            f"https://{shop_domain}/admin/apps to keep full incident coverage.\n"
            f"_CheckoutGuard continues monitoring your store without interruption._"
        )
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(slack_url, json={"text": text})
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Order cap Slack notice failed for %s: %s", shop_domain, exc)


async def consume_ai_budget(pool, shop_domain: str, cap: int) -> bool:
    """
    Atomically check and increment the per-merchant monthly AI call counter.
    Returns True if the call is allowed, False if the cap is exceeded.
    Resets the counter when the calendar month turns.
    """
    try:
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ai_calls_month, ai_calls_reset_at FROM merchants WHERE shop_domain=$1",
                shop_domain,
            )
        if not row:
            return False

        reset_at = row["ai_calls_reset_at"]
        calls = row["ai_calls_month"] or 0

        # Reset if we've moved into a new calendar month.
        if reset_at is None or reset_at.year != now.year or reset_at.month != now.month:
            calls = 0
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE merchants SET ai_calls_month=0, ai_calls_reset_at=$1 WHERE shop_domain=$2",
                    now, shop_domain,
                )

        if calls >= cap:
            logger.info(
                "AI cap reached for %s (%d/%d this month) — skipping Anthropic call",
                shop_domain, calls, cap,
            )
            return False

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE merchants SET ai_calls_month = ai_calls_month + 1 WHERE shop_domain=$1",
                shop_domain,
            )
        return True
    except Exception as exc:
        logger.warning("AI budget check failed for %s: %s", shop_domain, exc)
        return True  # fail-open: don't suppress AI on DB errors
