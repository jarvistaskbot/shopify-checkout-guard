"""
Alert dispatchers for CheckoutGuard incidents.
Sends Slack messages (always) and email via SendGrid (when configured).
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_INCIDENT_LABELS = {
    "checkout_funnel_collapse": "Checkout Funnel Broken",
    "volume_drop": "Order Silence",
    "abandonment_spike": "Abandonment Spike",
    "payment_failure": "Payment Gateway Issue",
    "js_error_spike": "JS Error Spike",
    "oos_hot_product": "Hot Product Out of Stock",
    "slow_bleed": "Slow Checkout Bleed",
}


def _append_ai(text: str, ai_analysis: Optional[str]) -> str:
    if ai_analysis:
        return text + f"\n\n*AI Analysis:* {ai_analysis}"
    return text


async def send_checkout_funnel_alert(
    webhook_url: str,
    shop_domain: str,
    incident_id: int,
    checkouts: int,
    orders: int,
    current_rate: float,
    baseline_rate: float,
    aov: float,
    alert_email: str = None,
    ai_analysis: Optional[str] = None,
) -> None:
    missed = max(0, int(checkouts * baseline_rate) - orders)
    revenue_at_risk = round(missed * aov, 2)
    current_pct = round(current_rate * 100, 1)
    baseline_pct = round(baseline_rate * 100, 1)

    text = (
        f":rotating_light: *CHECKOUT BROKEN — {shop_domain}*\n"
        f"{checkouts} customers started checkout but only {orders} orders completed (last 30 min)\n"
        f"• Conversion rate: *{current_pct}%* (normal: {baseline_pct}%)\n"
        f"• Estimated revenue at risk: *${revenue_at_risk:,.2f}*\n"
        f"  ↳ {missed} likely missed conversions × ${aov:.0f} avg order value\n"
        f"  ↳ Checkout rate dropped from {baseline_pct}% → {current_pct}% (last 30 min)\n"
        f"  ↳ Confidence: High\n"
        f"• Incident #{incident_id}\n\n"
        f"*Action: Test your checkout NOW at https://{shop_domain}*"
    )
    text = _append_ai(text, ai_analysis)
    if webhook_url:
        await _post(webhook_url, text, shop_domain=shop_domain, alert_type="checkout_funnel_collapse", incident_id=incident_id)
    if alert_email:
        subject = f"[CheckoutGuard] Checkout Broken on {shop_domain}"
        await _send_email(alert_email, subject, text.replace("*", "").replace(":rotating_light:", "🚨"))


async def send_silence_alert(
    webhook_url: str,
    shop_domain: str,
    incident_id: int,
    baseline: float,
    current_volume: int,
    aov: float,
    alert_email: str = None,
    ai_analysis: Optional[str] = None,
) -> None:
    drop_pct = round((baseline - current_volume) / baseline * 100, 1)
    revenue_per_hour = round(baseline * 2 * aov, 2)

    text = (
        f":warning: *UNUSUAL SILENCE — {shop_domain}*\n"
        f"No orders for 90+ min during expected peak hours\n"
        f"• Expected: ~{baseline:.1f} orders per 30 min -> Received: {current_volume}\n"
        f"• Volume down *{drop_pct}%* vs baseline\n"
        f"• Revenue at risk: *${revenue_per_hour:,.2f}/hr*\n"
        f"• Incident #{incident_id}\n\n"
        f"Possible causes: store down, checkout error, payment failure.\n"
        f"Check https://{shop_domain}"
    )
    text = _append_ai(text, ai_analysis)
    if webhook_url:
        await _post(webhook_url, text, shop_domain=shop_domain, alert_type="volume_drop", incident_id=incident_id)
    if alert_email:
        subject = f"[CheckoutGuard] Unusual Silence on {shop_domain}"
        await _send_email(alert_email, subject, text.replace("*", "").replace(":warning:", "⚠️"))


async def send_abandonment_alert(
    webhook_url: str,
    shop_domain: str,
    incident_id: int,
    abandoned: int,
    checkouts: int,
    current_rate: float,
    baseline_rate: float,
    aov: float,
    alert_email: str = None,
    ai_analysis: Optional[str] = None,
) -> None:
    current_pct = round(current_rate * 100, 1)
    baseline_pct = round(baseline_rate * 100, 1)
    multiplier = round(current_rate / max(0.01, baseline_rate), 1)
    revenue_at_risk = round(abandoned * aov, 2)

    text = (
        f":warning: *ABANDONMENT SPIKE — {shop_domain}*\n"
        f"{abandoned} of {checkouts} recent checkouts were abandoned ({current_pct}% abandon rate)\n"
        f"• Normal abandon rate: {baseline_pct}% -> Current: {current_pct}% ({multiplier}x spike)\n"
        f"• Revenue at risk: *${revenue_at_risk:,.2f}*\n"
        f"• Incident #{incident_id}\n\n"
        f"Possible cause: broken checkout step, failed payment method, confusing shipping rates.\n"
        f"Test your checkout: https://{shop_domain}"
    )
    text = _append_ai(text, ai_analysis)
    if webhook_url:
        await _post(webhook_url, text, shop_domain=shop_domain, alert_type="abandonment_spike", incident_id=incident_id)
    if alert_email:
        subject = f"[CheckoutGuard] Abandonment Spike on {shop_domain}"
        await _send_email(alert_email, subject, text.replace("*", "").replace(":warning:", "⚠️"))


async def send_payment_failure_alert(
    webhook_url: str,
    shop_domain: str,
    incident_id: int,
    pending_count: int,
    order_names: list,
    total_at_risk: float,
    alert_email: str = None,
    ai_analysis: Optional[str] = None,
) -> None:
    orders_str = ", ".join(order_names)
    if pending_count > 5:
        orders_str += f" (and {pending_count - 5} more)"

    text = (
        f":rotating_light: *PAYMENT GATEWAY ISSUE — {shop_domain}*\n"
        f"{pending_count} orders stuck in 'pending' for >15 minutes\n"
        f"• Orders: {orders_str}\n"
        f"• Revenue stuck: *${total_at_risk:,.2f}*\n"
        f"• Incident #{incident_id}\n\n"
        f"*Action: Check your payment gateway immediately.*\n"
        f"Shopify admin -> Orders -> filter by 'Payment pending'"
    )
    text = _append_ai(text, ai_analysis)
    if webhook_url:
        await _post(webhook_url, text, shop_domain=shop_domain, alert_type="payment_failure", incident_id=incident_id)
    if alert_email:
        subject = f"[CheckoutGuard] Payment Gateway Issue on {shop_domain}"
        await _send_email(alert_email, subject, text.replace("*", "").replace(":rotating_light:", "🚨"))


async def send_js_error_alert(
    webhook_url: str,
    alert_email: str,
    shop_domain: str,
    incident_id: int,
    count_10min: int,
    message: str,
    page_url: str,
    ai_analysis: Optional[str] = None,
) -> None:
    text = (
        f":warning: *JS ERROR SPIKE — {shop_domain}*\n"
        f"New error seen {count_10min} times in 10 minutes\n"
        f"• Error: `{message[:120]}`\n"
        f"• Page: {page_url or 'unknown'}\n"
        f"• Incident #{incident_id}\n\n"
        f"This may be causing checkout friction. Check your browser console."
    )
    text = _append_ai(text, ai_analysis)
    subject = f"[CheckoutGuard] JS Error Spike on {shop_domain}"
    body = text.replace("*", "").replace("`", '"').replace(":warning:", "⚠️")
    if webhook_url:
        await _post(webhook_url, text, shop_domain=shop_domain, alert_type="js_error_spike", incident_id=incident_id)
    if alert_email:
        await _send_email(alert_email, subject, body)


async def send_oos_alert(
    webhook_url: str,
    alert_email: str,
    shop_domain: str,
    incident_id: int,
    product_title: str,
    orders_last_7d: int,
    revenue_per_hour: float,
    unit_price: float,
    ai_analysis: Optional[str] = None,
) -> None:
    text = (
        f":rotating_light: *HOT PRODUCT OUT OF STOCK — {shop_domain}*\n"
        f"*{product_title}* just hit zero inventory\n"
        f"• {orders_last_7d} orders in the last 7 days\n"
        f"• ~${revenue_per_hour:.2f}/hr estimated revenue at risk\n"
        f"  ↳ {orders_last_7d}/7 days × ${unit_price:.2f} price × 1/24 per hour\n"
        f"  ↳ Confidence: Medium-High\n"
        f"• Incident #{incident_id}\n\n"
        f"*Action: Restock or hide the product until inventory is back.*"
    )
    text = _append_ai(text, ai_analysis)
    subject = f"[CheckoutGuard] Hot Product Out of Stock: {product_title}"
    body = text.replace("*", "").replace(":rotating_light:", "🚨")
    if webhook_url:
        await _post(webhook_url, text, shop_domain=shop_domain, alert_type="oos_hot_product", incident_id=incident_id)
    if alert_email:
        await _send_email(alert_email, subject, body)


async def send_recovery_alert(
    webhook_url: str,
    alert_email: str,
    shop_domain: str,
    incident_id: int,
    duration_minutes: int,
    incident_type: str = "unknown",
) -> None:
    label = _INCIDENT_LABELS.get(incident_type, incident_type.replace("_", " ").title())
    text = (
        f":white_check_mark: *RESOLVED — {shop_domain}*\n"
        f"{label} incident resolved after {duration_minutes} min.\n"
        f"• Incident #{incident_id} closed."
    )
    subject = f"[CheckoutGuard] Resolved: {label} on {shop_domain}"
    body = text.replace("*", "").replace(":white_check_mark:", "✅")
    if webhook_url:
        await _post(webhook_url, text, shop_domain=shop_domain, alert_type="recovery", incident_id=incident_id)
    if alert_email:
        await _send_email(alert_email, subject, body)


async def send_weekly_digest(
    shop_domain: str,
    to_email: str,
    checkout_count: int,
    order_count: int,
    conversion_rate_pct: float,
    baseline_rate_pct: float,
    incident_count: int,
    estimated_protected_usd: float,
    ai_summary: Optional[str] = None,
) -> None:
    """Weekly digest email — sent every 7 days per merchant."""
    trend = ""
    if baseline_rate_pct > 0:
        delta = conversion_rate_pct - baseline_rate_pct
        trend = f" ({'+' if delta >= 0 else ''}{delta:.1f}pp vs baseline)"

    if incident_count == 0:
        status_line = "Your store ran cleanly — no incidents detected this week."
    else:
        status_line = f"{incident_count} incident(s) detected this week."
        if estimated_protected_usd > 0:
            status_line += f" Estimated revenue protected: ~${estimated_protected_usd:,.0f}."

    intro = ai_summary if ai_summary else status_line

    body = (
        f"CheckoutGuard Weekly Digest — {shop_domain}\n\n"
        f"{intro}\n\n"
        f"Last 7 days:\n"
        f"  Checkouts started:   {checkout_count}\n"
        f"  Orders completed:    {order_count}\n"
        f"  Conversion rate:     {conversion_rate_pct:.1f}%{trend}\n"
        f"  Incidents:           {incident_count}\n\n"
        f"— CheckoutGuard\n"
        f"Manage alerts: https://checkoutguardalerts.com/dashboard?shop={shop_domain}"
    )

    subject = f"[CheckoutGuard] Weekly digest — {shop_domain}"
    await _send_email(to_email, subject, body)


async def send_test_alert(webhook_url: str, shop_domain: str) -> None:
    """Send a clearly-labelled [TEST] alert to verify the Slack integration."""
    text = (
        f":white_check_mark: *[TEST] CheckoutGuard is connected — {shop_domain}*\n"
        f"This is a test alert. Your Slack integration is working correctly.\n"
        f"You will receive real alerts here when CheckoutGuard detects revenue anomalies."
    )
    await _post(webhook_url, text, shop_domain=shop_domain, alert_type="test")


async def _record_delivery(
    shop_domain: Optional[str],
    alert_type: Optional[str],
    incident_id: Optional[int],
    success: bool,
    status_detail: str,
) -> None:
    """Persist an alert delivery attempt. Never raises — history recording
    must not break alert delivery or the detection loop."""
    if not shop_domain:
        return
    try:
        from database import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO alert_deliveries
                       (shop_domain, alert_type, incident_id, success, status_detail)
                   VALUES ($1, $2, $3, $4, $5)""",
                shop_domain, alert_type or "unknown", incident_id, success, status_detail[:200],
            )
    except Exception as exc:
        logger.warning("Failed to record alert delivery for %s: %s", shop_domain, exc)


async def _post(
    webhook_url: str,
    text: str,
    shop_domain: Optional[str] = None,
    alert_type: Optional[str] = None,
    incident_id: Optional[int] = None,
) -> None:
    if not webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json={"text": text})
            resp.raise_for_status()
    except Exception as exc:
        await _record_delivery(shop_domain, alert_type, incident_id, False, str(exc))
        raise
    await _record_delivery(shop_domain, alert_type, incident_id, True, "delivered")


async def _send_email(to_email: str, subject: str, body: str) -> None:
    from config import settings
    if not settings.sendgrid_api_key or not to_email:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={
                    "Authorization": f"Bearer {settings.sendgrid_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "personalizations": [{"to": [{"email": to_email}]}],
                    "from": {"email": "alerts@checkoutguardalerts.com", "name": "CheckoutGuard"},
                    "subject": subject,
                    "content": [{"type": "text/plain", "value": body}],
                },
            )
            if resp.status_code not in (200, 202):
                logger.warning("SendGrid returned %d for %s: %s", resp.status_code, to_email, resp.text[:200])
    except Exception as exc:
        logger.error("Email send failed to %s: %s", to_email, exc)


async def send_slow_bleed_alert(
    webhook_url: str,
    shop_domain: str,
    incident_id: int,
    observed: int,
    expected: float,
    cusum: float,
    aov: float,
    alert_email: str = None,
    ai_analysis: Optional[str] = None,
) -> None:
    """v3 slow-bleed alert: sustained under-expectation checkout-start volume.

    No single window looked alarming — the accumulated shortfall did. Framed
    accordingly: this is a "check for silent breakage" alert, not an outage."""
    shortfall_pct = round(max(0.0, 1.0 - (observed / expected)) * 100, 1) if expected else 0.0

    text = (
        f":small_red_triangle_down: *SLOW CHECKOUT BLEED — {shop_domain}*\n"
        f"Checkout starts have run persistently below normal for hours\n"
        f"• Last hour: *{observed}* checkout starts (normal for this hour: ~{expected:.1f})\n"
        f"• Latest shortfall: *{shortfall_pct}%* under expectation\n"
        f"• No single hour looked broken — the sustained accumulation did\n"
        f"• Incident #{incident_id}\n\n"
        f"*Action: check for silent breakage — recent theme/app changes, discount or "
        f"shipping misconfig, or a broken product page at https://{shop_domain}*"
    )
    text = _append_ai(text, ai_analysis)
    if webhook_url:
        await _post(webhook_url, text, shop_domain=shop_domain, alert_type="slow_bleed", incident_id=incident_id)
    if alert_email:
        subject = f"[CheckoutGuard] Slow Checkout Bleed on {shop_domain}"
        await _send_email(alert_email, subject, text.replace("*", "").replace(":small_red_triangle_down:", "\u26a0\ufe0f"))
