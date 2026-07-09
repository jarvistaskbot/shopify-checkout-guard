"""
Slack alert messages for CheckoutGuard incidents.
"""

import httpx

_INCIDENT_LABELS = {
    "checkout_funnel_collapse": "Checkout Funnel Broken",
    "volume_drop": "Order Silence",
    "abandonment_spike": "Abandonment Spike",
    "payment_failure": "Payment Gateway Issue",
}


async def send_checkout_funnel_alert(
    webhook_url: str,
    shop_domain: str,
    incident_id: int,
    checkouts: int,
    orders: int,
    current_rate: float,
    baseline_rate: float,
    aov: float,
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
        f"• Incident #{incident_id}\n\n"
        f"*Action: Test your checkout NOW at https://{shop_domain}*"
    )
    await _post(webhook_url, text)


async def send_silence_alert(
    webhook_url: str,
    shop_domain: str,
    incident_id: int,
    baseline: float,
    current_volume: int,
    aov: float,
) -> None:
    drop_pct = round((baseline - current_volume) / baseline * 100, 1)
    revenue_per_hour = round(baseline * 2 * aov, 2)  # baseline is 30-min count

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
    await _post(webhook_url, text)


async def send_abandonment_alert(
    webhook_url: str,
    shop_domain: str,
    incident_id: int,
    abandoned: int,
    checkouts: int,
    current_rate: float,
    baseline_rate: float,
    aov: float,
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
    await _post(webhook_url, text)


async def send_payment_failure_alert(
    webhook_url: str,
    shop_domain: str,
    incident_id: int,
    pending_count: int,
    order_names: list,
    total_at_risk: float,
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
    await _post(webhook_url, text)


async def send_recovery_alert(
    webhook_url: str,
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
    await _post(webhook_url, text)


async def _post(webhook_url: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(webhook_url, json={"text": text})
        resp.raise_for_status()
