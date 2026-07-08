"""
Slack alert service for CheckoutGuard incidents.
"""

import httpx


async def send_alert(
    webhook_url: str,
    shop_domain: str,
    incident_id: int,
    baseline_rate: float,
    current_rate: float,
    rev_loss_per_min: float,
) -> None:
    baseline_pct = round(baseline_rate * 100, 1)
    current_pct = round(current_rate * 100, 1)
    drop_pct = round(baseline_pct - current_pct, 1)

    text = (
        f":rotating_light: *Checkout Alert — {shop_domain}*\n"
        f"Conversion rate dropped *{drop_pct}%* below baseline\n"
        f"• Normal: {baseline_pct}% → Now: {current_pct}%\n"
        f"• Estimated revenue loss: *${rev_loss_per_min:.2f}/min*\n"
        f"• Incident ID: #{incident_id}\n"
        f"Check your Shopify admin immediately."
    )

    payload = {"text": text}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(webhook_url, json=payload)
        resp.raise_for_status()


async def send_recovery_alert(
    webhook_url: str,
    shop_domain: str,
    incident_id: int,
    duration_minutes: int,
) -> None:
    text = (
        f":white_check_mark: *Checkout Recovery — {shop_domain}*\n"
        f"Conversion rate has returned to baseline.\n"
        f"• Incident #{incident_id} resolved after {duration_minutes} min."
    )
    payload = {"text": text}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(webhook_url, json=payload)
        resp.raise_for_status()
