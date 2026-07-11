"""
Incidents dashboard — server-rendered HTML, last 7 days of incidents.
Auth: requires valid cg_session HttpOnly cookie (set at OAuth callback).
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from html import escape
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import settings
from database import get_pool
from session import COOKIE_NAME, verify_session_token

logger = logging.getLogger(__name__)
router = APIRouter()

_STYLE = """
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
    max-width: 820px;
    margin: 48px auto;
    padding: 0 20px;
    color: #1a1a1a;
}
h1 { font-size: 22px; margin-bottom: 4px; }
.shop { font-weight: 600; color: #008060; }
.sub { color: #666; font-size: 14px; margin-bottom: 28px; }
.banner {
    padding: 14px 18px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 28px;
}
.banner-ok { background: #e6f4ef; color: #006b45; border: 1px solid #b3d9c9; }
.banner-warn { background: #fff4e0; color: #7a4e00; border: 1px solid #f5c842; }
.banner-info { background: #f0f5ff; color: #1a3a6b; border: 1px solid #b3c9f0; }
.banner-trial { background: #fff8e6; color: #7a4e00; border: 1px solid #f5d87a; font-weight: normal; }
.banner-subscribe { background: #fff0f0; color: #7a0000; border: 1px solid #f5a0a0; font-weight: normal; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th {
    text-align: left; padding: 10px 12px; background: #f7f7f7;
    border-bottom: 2px solid #e0e0e0; font-weight: 600; color: #444;
}
td { padding: 10px 12px; border-bottom: 1px solid #eee; vertical-align: top; }
tr:last-child td { border-bottom: none; }
.badge {
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 12px; font-weight: 600;
}
.badge-active { background: #ffe5e5; color: #c00; }
.badge-resolved { background: #e6f4ef; color: #006b45; }
.stats { display: flex; gap: 24px; margin-bottom: 28px; flex-wrap: wrap; }
.stat {
    background: #f7f9ff; border: 1px solid #dde5f0; border-radius: 8px;
    padding: 16px 20px; min-width: 140px;
}
.stat-num { font-size: 26px; font-weight: 700; color: #1a1a1a; }
.stat-label { font-size: 13px; color: #666; margin-top: 4px; }
h2 { font-size: 16px; margin: 28px 0 12px; }
.empty { color: #999; font-size: 14px; padding: 20px 0; }
a { color: #008060; }
.ai-note { font-size: 12px; color: #555; font-style: italic; margin-top: 4px; }
"""

_INCIDENT_LABELS = {
    "checkout_funnel_collapse": "Checkout Funnel",
    "volume_drop": "Order Silence",
    "abandonment_spike": "Abandonment Spike",
    "payment_failure": "Payment Gateway",
    "js_error_spike": "JS Error Spike",
    "oos_hot_product": "Out-of-Stock Alert",
}


def _require_session(request: Request, shop: str):
    """Return shop from cookie or raise RedirectResponse to OAuth."""
    cookie_val = request.cookies.get(COOKIE_NAME)
    if not cookie_val:
        return None
    verified_shop = verify_session_token(cookie_val, settings.secret_key)
    if not verified_shop or verified_shop != shop:
        return None
    return verified_shop


def _fmt_dt(dt: datetime) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%b %d %H:%M UTC")


def _fmt_impact(row) -> str:
    itype = row["incident_type"]
    if itype == "js_error_spike":
        detail = row["detail"] or {}
        count = detail.get("count_10min", "?") if isinstance(detail, dict) else "?"
        return f"{count} errors in 10 min"
    if itype == "oos_hot_product":
        detail = row["detail"] or {}
        if isinstance(detail, dict) and "estimated_revenue_per_hour" in detail:
            return f"~${detail['estimated_revenue_per_hour']:.0f}/hr estimated"
        return "revenue impact pending"
    loss_per_min = float(row["estimated_revenue_loss_per_min"] or 0)
    if loss_per_min > 0:
        return f"~${loss_per_min * 60:.0f}/hr estimated"
    return "—"


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, shop: str = Query(...)) -> HTMLResponse:
    # Verify session cookie.
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    pool = await get_pool()
    async with pool.acquire() as conn:
        merchant = await conn.fetchrow(
            """SELECT shop_domain, installed_at, slack_webhook_url, alert_email,
                      billing_status, trial_ends_at, plan,
                      orders_month, orders_month_reset_at
               FROM merchants WHERE shop_domain = $1 AND active = TRUE""",
            shop,
        )
        if not merchant:
            raise HTTPException(status_code=404, detail="Shop not found or not active")

        installed_at = merchant["installed_at"]
        days_active = (datetime.now(timezone.utc) - installed_at).days
        calibrating = days_active < 7

        since = datetime.now(timezone.utc) - timedelta(days=7)

        active_incidents = await conn.fetch(
            """SELECT id, incident_type, started_at, resolved_at,
                      estimated_revenue_loss_per_min, avg_order_value, detail, ai_analysis
               FROM incidents WHERE shop_domain = $1 AND resolved_at IS NULL
               ORDER BY started_at DESC""",
            shop,
        )

        recent_incidents = await conn.fetch(
            """SELECT id, incident_type, started_at, resolved_at,
                      estimated_revenue_loss_per_min, avg_order_value, detail, ai_analysis
               FROM incidents WHERE shop_domain = $1 AND started_at >= $2
               ORDER BY started_at DESC LIMIT 50""",
            shop, since,
        )

        checkout_count = await conn.fetchval(
            """SELECT COUNT(*) FROM checkout_events
               WHERE shop_domain = $1 AND event_type = 'checkout_created' AND created_at >= $2""",
            shop, since,
        ) or 0

        order_count = await conn.fetchval(
            """SELECT COUNT(*) FROM checkout_events
               WHERE shop_domain = $1 AND event_type = 'order_created' AND created_at >= $2""",
            shop, since,
        ) or 0

    # Mask Slack webhook URL — show only last 6 chars, never the full URL.
    slack_masked = None
    if merchant["slack_webhook_url"]:
        slack_masked = "..." + merchant["slack_webhook_url"][-6:]

    from services.billing_guard import get_billing_banner
    from services.plans import PLANS, get_order_cap
    billing_banner = get_billing_banner(
        merchant["billing_status"],
        merchant["trial_ends_at"],
        shop,
    )
    merchant_plan = merchant["plan"] or "starter"
    plan_name = PLANS.get(merchant_plan, PLANS["starter"])["name"]

    # Determine whether this merchant has exceeded their monthly order cap.
    order_cap = get_order_cap(merchant_plan)
    orders_month = merchant["orders_month"] or 0
    orders_month_reset_at = merchant["orders_month_reset_at"]
    now = datetime.now(timezone.utc)
    if orders_month_reset_at is not None and (
        orders_month_reset_at.year != now.year or orders_month_reset_at.month != now.month
    ):
        orders_month = 0  # counter is from a prior month, not yet rolled over
    order_cap_exceeded = order_cap is not None and orders_month > order_cap

    return HTMLResponse(content=_render(
        shop=shop,
        calibrating=calibrating,
        days_active=days_active,
        active_incidents=list(active_incidents),
        recent_incidents=list(recent_incidents),
        checkout_count=checkout_count,
        order_count=order_count,
        slack_masked=slack_masked,
        alert_email=merchant["alert_email"],
        billing_banner=billing_banner,
        plan_name=plan_name,
        order_cap_exceeded=order_cap_exceeded,
        orders_month=orders_month,
        order_cap=order_cap,
    ))


def _render(
    shop: str,
    calibrating: bool,
    days_active: int,
    active_incidents: list,
    recent_incidents: list,
    checkout_count: int,
    order_count: int,
    slack_masked=None,
    alert_email=None,
    billing_banner=None,
    plan_name: str = "CheckoutGuard Starter",
    order_cap_exceeded: bool = False,
    orders_month: int = 0,
    order_cap: Optional[int] = None,
) -> str:
    safe_shop = escape(shop)
    conversion_rate = (
        f"{order_count / checkout_count * 100:.1f}%"
        if checkout_count > 0 else "—"
    )

    # Status banner
    if calibrating:
        banner_cls = "banner-info"
        banner_text = (
            "Calibrating your store&rsquo;s baseline &mdash; anomaly alerts begin after 7 days."
        )
    elif active_incidents:
        banner_cls = "banner-warn"
        banner_text = f"{len(active_incidents)} active incident(s) detected. See details below."
    else:
        banner_cls = "banner-ok"
        banner_text = "All clear — no active incidents in the last 7 days."

    stats_html = f"""
<div class="stats">
  <div class="stat">
    <div class="stat-num">{checkout_count}</div>
    <div class="stat-label">Checkouts started (7d)</div>
  </div>
  <div class="stat">
    <div class="stat-num">{order_count}</div>
    <div class="stat-label">Orders completed (7d)</div>
  </div>
  <div class="stat">
    <div class="stat-num">{conversion_rate}</div>
    <div class="stat-label">Conversion rate (7d)</div>
  </div>
  <div class="stat">
    <div class="stat-num">{len(recent_incidents)}</div>
    <div class="stat-label">Incidents this week</div>
  </div>
</div>"""

    # Incidents table
    if recent_incidents:
        rows = ""
        for row in recent_incidents:
            label = _INCIDENT_LABELS.get(row["incident_type"], row["incident_type"].replace("_", " ").title())
            status = (
                '<span class="badge badge-active">Active</span>'
                if row["resolved_at"] is None
                else '<span class="badge badge-resolved">Resolved</span>'
            )
            detail = row["detail"]
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except Exception:
                    detail = {}
            impact = _fmt_impact({**dict(row), "detail": detail})
            duration = ""
            if row["resolved_at"]:
                mins = int((row["resolved_at"] - row["started_at"]).total_seconds() / 60)
                duration = f"{mins} min"
            else:
                mins = int((datetime.now(timezone.utc) - row["started_at"]).total_seconds() / 60)
                duration = f"{mins} min (ongoing)"

            ai_note = ""
            if row.get("ai_analysis"):
                ai_note = f'<div class="ai-note">AI: {escape(row["ai_analysis"][:200])}</div>'

            rows += f"""
<tr>
  <td>{_fmt_dt(row["started_at"])}</td>
  <td>{label}{ai_note}</td>
  <td>{status}</td>
  <td>{duration}</td>
  <td>{impact}</td>
</tr>"""

        table_html = f"""
<table>
  <thead>
    <tr>
      <th>Started</th><th>Type</th><th>Status</th><th>Duration</th><th>Impact</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""
    else:
        table_html = '<p class="empty">No incidents in the last 7 days.</p>'

    # Alert settings summary (masked)
    settings_lines = []
    if slack_masked:
        settings_lines.append(f"Slack: connected ({escape(slack_masked)})")
    if alert_email:
        settings_lines.append(f"Email: {escape(alert_email)}")
    settings_summary = " &bull; ".join(settings_lines) if settings_lines else "No alert channels configured."

    billing_banner_html = ""
    if billing_banner:
        b_cls, b_text = billing_banner
        billing_banner_html = f'<div class="banner {b_cls}">{b_text}</div>'

    order_cap_banner_html = ""
    if order_cap_exceeded and order_cap is not None:
        order_cap_banner_html = (
            f'<div class="banner banner-subscribe">'
            f"You&rsquo;ve passed <strong>{orders_month:,} orders</strong> this month &mdash; "
            f"your store has outgrown the {escape(plan_name)} plan ({order_cap:,} order limit). "
            f"<a href='/billing/plans?shop={safe_shop}'>Upgrade your plan</a> "
            f"to continue with full coverage. CheckoutGuard keeps monitoring you in the meantime."
            f"</div>"
        )

    safe_plan = escape(plan_name)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>CheckoutGuard — Dashboard</title>
  <style>{_STYLE}</style>
</head>
<body>
  <h1>CheckoutGuard Dashboard</h1>
  <p class="sub">Monitoring <span class="shop">{safe_shop}</span>
    &bull; Plan: <strong>{safe_plan}</strong>
    &bull; <a href="/billing/plans?shop={safe_shop}">Upgrade</a></p>
  <div class="banner {banner_cls}">{banner_text}</div>
  {billing_banner_html}
  {order_cap_banner_html}
  {stats_html}
  <h2>Last 7 Days — Incidents</h2>
  {table_html}
  <p style="margin-top:32px; font-size:13px; color:#999;">
    {settings_summary}<br>
    <a href="/onboarding?shop={safe_shop}">Update alert settings</a>
    &bull; <a href="mailto:artomnats1996@gmail.com">Support</a>
  </p>
</body>
</html>"""
