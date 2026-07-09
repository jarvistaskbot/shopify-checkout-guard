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
    drop_pct = round((baseline_rate - current_rate) / baseline_rate * 100, 1) if baseline_rate else 0

    text = (
        f":rotating_light: *Revenue Drop Detected — {shop_domain}*\n"
        f"Order volume down *{drop_pct}%* vs 7-day baseline\n"
        f"• Baseline (30-min avg): {baseline_rate:.1f} orders → Now: {current_rate:.0f} orders\n"
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
        f":white_check_mark: *Revenue Recovery — {shop_domain}*\n"
        f"Order volume has returned to baseline.\n"
        f"• Incident #{incident_id} resolved after {duration_minutes} min."
    )
    payload = {"text": text}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(webhook_url, json=payload)
        resp.raise_for_status()
