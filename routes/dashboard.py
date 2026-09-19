"""
Incidents dashboard — server-rendered HTML, last 7 days of incidents.
Auth: requires valid cg_session HttpOnly cookie (set at OAuth callback).
"""

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from html import escape
from typing import Dict, Optional

from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import settings
from database import get_pool
from session import COOKIE_NAME, csrf_token_for, verify_session_token, verify_shopify_session_token

# In-memory rate limiter for test-alert: maps shop_domain → unix timestamp of last send.
_test_alert_last_sent: Dict[str, float] = {}
_TEST_ALERT_COOLDOWN_SECS = 60

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
    "slow_bleed": "Slow Checkout Bleed",
}

_ALERT_TYPE_LABELS = {
    **_INCIDENT_LABELS,
    "test": "Test Alert",
    "recovery": "Incident Resolved",
}


def _require_session(request: Request, shop: str):
    """Return shop if authenticated via cookie (standalone) OR App Bridge JWT (embedded)."""
    # Standalone mode: HMAC-signed session cookie.
    cookie_val = request.cookies.get(COOKIE_NAME)
    if cookie_val:
        verified_shop = verify_session_token(cookie_val, settings.secret_key)
        if verified_shop and verified_shop == shop:
            return verified_shop

    # Embedded mode: App Bridge session token (JWT sent in Authorization header
    # or id-token query param).
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        verified_shop = verify_shopify_session_token(
            token, settings.shopify_api_secret, settings.shopify_api_key
        )
        if verified_shop and verified_shop == shop:
            return verified_shop

    id_token = request.query_params.get("id-token", "")
    if id_token:
        verified_shop = verify_shopify_session_token(
            id_token, settings.shopify_api_secret, settings.shopify_api_key
        )
        if verified_shop and verified_shop == shop:
            return verified_shop

    return None


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
async def dashboard(
    request: Request,
    shop: str = Query(...),
    ta: str = Query(default=""),
    w: int = Query(default=0),
    host: str = Query(default=""),
) -> HTMLResponse:
    # Verify session (cookie for standalone, JWT for embedded).
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    pool = await get_pool()
    async with pool.acquire() as conn:
        merchant = await conn.fetchrow(
            """SELECT shop_domain, installed_at, slack_webhook_url, alert_email,
                      billing_status, trial_ends_at, plan,
                      orders_month, orders_month_reset_at,
                      review_banner_impressions, review_banner_dismissed
               FROM merchants WHERE shop_domain = $1 AND active = TRUE""",
            shop,
        )
        if not merchant:
            # Unknown/uninstalled shop (e.g. stale cookie after uninstall) —
            # restart OAuth instead of showing a raw JSON 404.
            return RedirectResponse(url=f"/auth/shopify?shop={quote(shop)}", status_code=302)

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

        recent_alerts = await conn.fetch(
            """SELECT alert_type, incident_id, sent_at, success, status_detail, message_preview
               FROM alert_deliveries WHERE shop_domain = $1
               ORDER BY sent_at DESC LIMIT 10""",
            shop,
        )

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

    cookie_val = request.cookies.get(COOKIE_NAME, "")
    csrf = csrf_token_for(cookie_val, settings.secret_key)

    # Review banner: show if merchant is >14 days old, impressions < 3, not dismissed.
    impressions = merchant["review_banner_impressions"] or 0
    dismissed = merchant["review_banner_dismissed"] or False
    show_review_banner = (
        days_active >= 14
        and not dismissed
        and impressions < 3
    )
    if show_review_banner:
        import asyncio
        asyncio.create_task(_increment_review_banner(shop))

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
        csrf_token=csrf,
        test_alert_status=ta,
        test_alert_wait_secs=w,
        recent_alerts=list(recent_alerts),
        show_review_banner=show_review_banner,
        host=host,
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
    csrf_token: str = "",
    test_alert_status: str = "",
    test_alert_wait_secs: int = 0,
    recent_alerts: Optional[list] = None,
    show_review_banner: bool = False,
    host: str = "",
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

    slack_missing_banner_html = ""
    if not slack_masked:
        slack_missing_banner_html = (
            f'<div class="banner banner-warn">'
            f'Slack is not connected &mdash; alerts cannot be delivered yet. '
            f'<a href="/onboarding?shop={safe_shop}" style="color:#7a4e00;">Connect Slack</a>'
            f'</div>'
        )

    billing_banner_html = ""
    if billing_banner:
        b_cls, b_text = billing_banner
        billing_banner_html = f'<div class="banner {b_cls}">{b_text}</div>'

    # Test-alert result banner
    test_alert_banner_html = ""
    if test_alert_status == "sent":
        test_alert_banner_html = (
            '<div class="banner banner-ok" style="margin-top:12px;">'
            'Test alert sent to your Slack channel.'
            '</div>'
        )
    elif test_alert_status == "limit":
        wait_secs = test_alert_wait_secs if test_alert_wait_secs > 0 else 60
        test_alert_banner_html = (
            '<div class="banner banner-warn" style="margin-top:12px;">'
            f'Rate limit reached &mdash; you can send another test alert in about {wait_secs} seconds.'
            '</div>'
        )
    elif test_alert_status == "inapp":
        test_alert_banner_html = (
            '<div class="banner banner-ok" style="margin-top:12px;">'
            'Test alert generated &mdash; see it in Recent Alerts below. '
            f'<a href="/onboarding?shop={escape(shop)}">Connect Slack</a> '
            'to also deliver alerts externally.'
            '</div>'
        )
    elif test_alert_status == "error":
        test_alert_banner_html = (
            '<div class="banner banner-subscribe" style="margin-top:12px;">'
            'Test alert failed — check that your Slack webhook URL is still valid.'
            '</div>'
        )

    test_alert_section = f"""
<div style="margin-top:28px;padding:16px 20px;background:#f7f9ff;border:1px solid #dde5f0;border-radius:8px;">
  <strong style="font-size:14px;">Test your alert integration</strong>
  <p style="font-size:13px;color:#666;margin:6px 0 12px;">
    Send a [TEST] message to your Slack channel to verify the connection.
  </p>
  {test_alert_banner_html}
  <form method="POST" action="/dashboard/test-alert" style="margin-top:8px;">
    <input type="hidden" name="shop" value="{escape(shop)}" />
    <input type="hidden" name="csrf_token" value="{escape(csrf_token)}" />
    <button type="submit"
      style="padding:8px 18px;background:#008060;color:white;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;">
      Send test alert
    </button>
  </form>
</div>"""

    # Recent alerts (delivery history) — lets anyone verify alert delivery
    # from inside the app, without access to the receiving Slack workspace.
    if recent_alerts:
        alert_rows = ""
        for a in recent_alerts:
            type_label = _ALERT_TYPE_LABELS.get(
                a["alert_type"], a["alert_type"].replace("_", " ").title()
            )
            detail = a["status_detail"] or ""
            if a["success"] and detail == "delivered":
                delivery = '<span class="badge badge-resolved">Delivered to Slack</span>'
            elif a["success"]:
                delivery = '<span class="badge badge-resolved">Generated</span>'
            else:
                delivery = (
                    f'<span class="badge badge-active" title="{escape(detail[:80])}">Failed</span>'
                )
            incident_ref = f'#{a["incident_id"]}' if a["incident_id"] else "&mdash;"
            preview_html = ""
            if a["message_preview"]:
                preview_html = (
                    '<details style="margin-top:4px;"><summary style="cursor:pointer;'
                    'font-size:12px;color:#008060;">View alert content</summary>'
                    f'<pre style="white-space:pre-wrap;font-size:12px;background:#f7f7f7;'
                    f'padding:8px;border-radius:6px;margin:6px 0 0;">'
                    f'{escape(a["message_preview"])}</pre></details>'
                )
            alert_rows += f"""
<tr>
  <td>{_fmt_dt(a["sent_at"])}</td>
  <td>{type_label}{preview_html}</td>
  <td>{incident_ref}</td>
  <td>{delivery}</td>
</tr>"""
        recent_alerts_html = f"""
<h2>Recent Alerts</h2>
<table>
  <thead>
    <tr><th>Sent</th><th>Type</th><th>Incident</th><th>Delivery</th></tr>
  </thead>
  <tbody>{alert_rows}</tbody>
</table>"""
    else:
        recent_alerts_html = (
            '<h2>Recent Alerts</h2>'
            '<p class="empty">No alerts sent yet &mdash; press &ldquo;Send test alert&rdquo; '
            'below and it will appear here with its delivery status.</p>'
        )

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

    # App Bridge CDN: only inject when embedded params are present so the
    # standalone dashboard is completely unaffected by the script.
    app_bridge_script = ""
    if host and safe_shop:
        _CLIENT_ID = "4e9e166367e80c062d31c73303a085bc"
        app_bridge_script = (
            f'<script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"'
            f' data-api-key="{_CLIENT_ID}"></script>'
        )

    # Review-request banner (Fix C). App Store handle must be confirmed by Arto
    # from his listing URL at https://apps.shopify.com/<handle>.
    _APP_STORE_HANDLE = "YOUR_APP_STORE_HANDLE"  # TODO: Arto must confirm this
    review_banner_html = ""
    if show_review_banner:
        review_url = f"https://apps.shopify.com/{_APP_STORE_HANDLE}/reviews"
        review_banner_html = f"""
<div class="banner banner-info" id="review-banner"
     style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
  <span>
    Enjoying CheckoutGuard? A quick review on the App Store helps other merchants find us
    &mdash; it takes 30 seconds and means a lot.
    <a href="{review_url}" target="_blank" rel="noopener"
       style="color:#1a3a6b;font-weight:700;">Leave a review &rarr;</a>
  </span>
  <form method="POST" action="/dashboard/dismiss-review-banner" style="margin:0;flex-shrink:0;">
    <input type="hidden" name="shop" value="{safe_shop}" />
    <input type="hidden" name="csrf_token" value="{escape(csrf_token)}" />
    <button type="submit"
      style="background:none;border:none;cursor:pointer;color:#1a3a6b;font-size:18px;
             line-height:1;padding:0 4px;" title="Dismiss">&times;</button>
  </form>
</div>"""

    safe_plan = escape(plan_name)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>CheckoutGuard — Dashboard</title>
  <style>{_STYLE}</style>
  {app_bridge_script}
</head>
<body>
  <h1>CheckoutGuard Dashboard</h1>
  <p class="sub">Monitoring <span class="shop">{safe_shop}</span>
    &bull; Plan: <strong>{safe_plan}</strong>
    &bull; <a href="/billing/plans?shop={safe_shop}">Upgrade</a></p>
  <div class="banner {banner_cls}">{banner_text}</div>
  {slack_missing_banner_html}
  {billing_banner_html}
  {order_cap_banner_html}
  {review_banner_html}
  {stats_html}
  <h2>Last 7 Days — Incidents</h2>
  {table_html}
  {recent_alerts_html}
  {test_alert_section}
  <p style="margin-top:24px; font-size:13px; color:#999;">
    {settings_summary}<br>
    <a href="/onboarding?shop={safe_shop}">Update alert settings</a>
    &bull; <a href="mailto:artomnats1996@gmail.com">Support</a>
  </p>
</body>
</html>"""


@router.post("/dashboard/test-alert")
async def dashboard_test_alert(
    request: Request,
    shop: str = Form(...),
    csrf_token: Optional[str] = Form(default=None),
) -> RedirectResponse:
    """Send a [TEST] Slack alert; rate-limited to one per 10 minutes per shop."""
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    cookie_val = request.cookies.get(COOKIE_NAME, "")
    expected_csrf = csrf_token_for(cookie_val, settings.secret_key)
    if not csrf_token or csrf_token != expected_csrf:
        return RedirectResponse(url=f"/dashboard?shop={quote(shop)}", status_code=302)

    # Rate limit: one test alert per shop per cooldown window.
    now = time.time()
    last_sent = _test_alert_last_sent.get(shop, 0.0)
    if now - last_sent < _TEST_ALERT_COOLDOWN_SECS:
        remaining = max(1, int(_TEST_ALERT_COOLDOWN_SECS - (now - last_sent)))
        return RedirectResponse(
            url=f"/dashboard?shop={quote(shop)}&ta=limit&w={remaining}", status_code=303
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT slack_webhook_url FROM merchants WHERE shop_domain=$1 AND active=TRUE",
            shop,
        )

    if not row or not row["slack_webhook_url"]:
        # No Slack configured: still generate the test alert and record it so
        # the complete alert pipeline is verifiable in-app, with no external
        # account or credential required.
        _test_alert_last_sent[shop] = now
        from services.alerter import build_test_alert_text, record_in_app_alert
        await record_in_app_alert(shop, "test", build_test_alert_text(shop))
        return RedirectResponse(url=f"/dashboard?shop={quote(shop)}&ta=inapp", status_code=303)

    # Stamp rate limiter before attempting send so errors also consume the cooldown.
    _test_alert_last_sent[shop] = now
    try:
        from services.alerter import send_test_alert
        await send_test_alert(row["slack_webhook_url"], shop)
    except Exception as exc:
        logger.error("Test alert failed for %s: %s", shop, exc)
        return RedirectResponse(url=f"/dashboard?shop={quote(shop)}&ta=error", status_code=303)

    return RedirectResponse(url=f"/dashboard?shop={quote(shop)}&ta=sent", status_code=303)


async def _increment_review_banner(shop: str) -> None:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE merchants
                   SET review_banner_impressions = review_banner_impressions + 1
                   WHERE shop_domain = $1""",
                shop,
            )
    except Exception as exc:
        logger.error("review_banner impression increment failed for %s: %s", shop, exc)


@router.post("/dashboard/dismiss-review-banner")
async def dismiss_review_banner(
    request: Request,
    shop: str = Form(...),
    csrf_token: Optional[str] = Form(default=None),
) -> RedirectResponse:
    """Permanently dismiss the review-request banner for this merchant."""
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    cookie_val = request.cookies.get(COOKIE_NAME, "")
    expected_csrf = csrf_token_for(cookie_val, settings.secret_key)
    if not csrf_token or csrf_token != expected_csrf:
        return RedirectResponse(url=f"/dashboard?shop={quote(shop)}", status_code=302)

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE merchants SET review_banner_dismissed = TRUE WHERE shop_domain = $1",
                shop,
            )
    except Exception as exc:
        logger.error("review_banner dismiss failed for %s: %s", shop, exc)

    return RedirectResponse(url=f"/dashboard?shop={quote(shop)}", status_code=303)
