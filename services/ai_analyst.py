"""
Haiku AI incident analysis.
Fail-silent: never raises, always returns None on any error.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "claude-haiku-4-5-20251001"
_TIMEOUT = 10
_MAX_CHARS = 600

_INCIDENT_DESCRIPTIONS = {
    "checkout_funnel_collapse": "checkout conversion rate dropped sharply",
    "volume_drop": "order volume dropped significantly below the day-of-week baseline",
    "abandonment_spike": "cart abandonment rate spiked to an unusually high level",
    "payment_failure": "multiple orders are stuck in 'pending' payment status",
    "js_error_spike": "a JavaScript error is occurring at high frequency on the storefront",
    "oos_hot_product": "a fast-selling product just hit zero inventory",
}


async def analyze_incident(
    incident_type: str,
    detail: dict,
    shop_domain: str,
    api_key: str,
    enabled: bool = True,
) -> Optional[str]:
    """Call Haiku to generate a 2-3 sentence diagnosis. Returns None on failure."""
    if not enabled or not api_key:
        return None

    description = _INCIDENT_DESCRIPTIONS.get(incident_type, incident_type.replace("_", " "))
    detail_summary = _summarize_detail(incident_type, detail)

    prompt = (
        f"You are a Shopify expert helping a merchant diagnose a store issue. "
        f"Their CheckoutGuard monitoring detected: {description}.\n"
        f"Details: {detail_summary}\n\n"
        f"Write exactly 2 sentences: (1) the most likely cause of this incident, "
        f"(2) the first thing the merchant should check right now. "
        f"Be specific and actionable. No preamble."
    )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _ANTHROPIC_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": _MODEL,
                    "max_tokens": 150,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                logger.warning("Anthropic API returned %d for %s incident", resp.status_code, incident_type)
                return None
            data = resp.json()
            text = data.get("content", [{}])[0].get("text", "").strip()
            return text[:_MAX_CHARS] if text else None
    except Exception as exc:
        logger.warning("AI analysis failed for %s on %s: %s", incident_type, shop_domain, exc)
        return None


def _summarize_detail(incident_type: str, detail: dict) -> str:
    if incident_type == "checkout_funnel_collapse":
        return (
            f"conversion rate dropped from {detail.get('baseline_rate', '?'):.1%} "
            f"to {detail.get('current_rate', '?'):.1%} "
            f"({detail.get('checkouts', '?')} checkouts, {detail.get('orders', '?')} orders)"
        )
    if incident_type == "volume_drop":
        return (
            f"volume dropped {detail.get('drop_pct', '?')}% vs baseline "
            f"(expected ~{detail.get('baseline', '?'):.1f} orders)"
        )
    if incident_type == "abandonment_spike":
        return (
            f"abandon rate {detail.get('current_rate', '?'):.1%} "
            f"vs baseline {detail.get('baseline_rate', '?'):.1%} "
            f"({detail.get('abandoned', '?')} of {detail.get('checkouts', '?')} checkouts abandoned)"
        )
    if incident_type == "payment_failure":
        names = ", ".join(detail.get("order_names", []))
        return f"{detail.get('pending_count', '?')} orders pending >15min (${detail.get('total_at_risk', 0):.2f} at risk). Orders: {names}"
    if incident_type == "js_error_spike":
        return (
            f"error '{detail.get('message', '?')[:100]}' "
            f"occurred {detail.get('count_10min', '?')} times in 10 min on {detail.get('page_url', 'unknown page')}"
        )
    if incident_type == "oos_hot_product":
        return (
            f"'{detail.get('product_title', 'unknown')}' hit zero inventory; "
            f"{detail.get('orders_last_7d', '?')} orders in the past 7 days, "
            f"~${detail.get('estimated_revenue_per_hour', 0):.2f}/hr at risk"
        )
    return str(detail)[:200]
