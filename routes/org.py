"""
Multi-store organization dashboard (Scale plan).

Routes:
  GET  /org         — View org dashboard, or create/join options if not in one yet.
  POST /org/create  — Create a new org (generates link_token). Scale + active billing only.
  POST /org/join    — Join an existing org via link_token. Scale + active billing only.

Session-protected with the same cg_session cookie mechanism as /dashboard.
plan_allows(plan, 'multi_store') gates all org operations.
"""

import secrets
from datetime import datetime, timezone, timedelta
from html import escape
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import settings
from database import get_pool
from session import COOKIE_NAME, csrf_token_for, verify_session_token
from services.plans import PLANS, plan_allows

router = APIRouter()

_ACTIVE_BILLING = frozenset({"active", "pending"})

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
    padding: 14px 18px; border-radius: 8px; font-size: 14px;
    font-weight: 600; margin-bottom: 28px;
}
.banner-warn { background: #fff4e0; color: #7a4e00; border: 1px solid #f5c842; }
.banner-subscribe { background: #fff0f0; color: #7a0000; border: 1px solid #f5a0a0; font-weight: normal; }
.banner-info { background: #f0f5ff; color: #1a3a6b; border: 1px solid #b3c9f0; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th {
    text-align: left; padding: 10px 12px; background: #f7f7f7;
    border-bottom: 2px solid #e0e0e0; font-weight: 600; color: #444;
}
td { padding: 10px 12px; border-bottom: 1px solid #eee; vertical-align: top; }
tr:last-child td { border-bottom: none; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 600; }
.badge-active { background: #ffe5e5; color: #c00; }
.badge-ok { background: #e6f4ef; color: #006b45; }
h2 { font-size: 16px; margin: 28px 0 12px; }
.card {
    background: #f6faf8; border: 1px solid #d4e9e2; border-radius: 8px;
    padding: 20px 24px; margin-bottom: 20px;
}
.card h3 { font-size: 15px; margin: 0 0 10px; color: #008060; }
.token-box {
    font-family: monospace; background: #f0f0f0; border: 1px solid #ccc;
    padding: 8px 12px; border-radius: 4px; font-size: 14px; word-break: break-all;
}
label { display: block; font-weight: 600; font-size: 14px; margin-bottom: 6px; }
.hint { font-size: 13px; color: #666; margin-bottom: 10px; }
input[type=text] {
    width: 100%; padding: 10px 12px; font-size: 14px; border: 1px solid #ccc;
    border-radius: 6px; box-sizing: border-box; margin-bottom: 4px;
}
button {
    margin-top: 12px; padding: 10px 24px; background: #008060;
    color: white; border: none; border-radius: 6px; font-size: 15px;
    cursor: pointer; font-weight: 600;
}
button:hover { background: #006e52; }
a { color: #008060; }
.error { color: #c00; font-size: 14px; margin-bottom: 16px; font-weight: 600; }
"""


def _require_session(request: Request, shop: str) -> Optional[str]:
    cookie_val = request.cookies.get(COOKIE_NAME)
    if not cookie_val:
        return None
    verified = verify_session_token(cookie_val, settings.secret_key)
    if not verified or verified != shop:
        return None
    return verified


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>CheckoutGuard — {escape(title)}</title>
  <style>{_STYLE}</style>
</head>
<body>
{body}
</body>
</html>"""


def _render_upgrade_required(shop: str) -> str:
    safe_shop = escape(shop)
    return _page("Multi-Store", f"""
  <h1>Multi-Store Overview</h1>
  <p class="sub">Store: <span class="shop">{safe_shop}</span></p>
  <div class="banner banner-subscribe">
    Multi-store organization is a <strong>Scale plan</strong> feature.
    <a href="/billing/plans?shop={safe_shop}">Upgrade to Scale</a> to link multiple stores.
  </div>
  <p><a href="/dashboard?shop={safe_shop}">&larr; Back to Dashboard</a></p>
""")


def _render_create_or_join(request: Request, shop: str, error: str = "") -> str:
    safe_shop = escape(shop)
    cookie_val = request.cookies.get(COOKIE_NAME, "")
    csrf = csrf_token_for(cookie_val, settings.secret_key)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    return _page("Multi-Store", f"""
  <h1>Multi-Store Overview</h1>
  <p class="sub">Store: <span class="shop">{safe_shop}</span></p>
  {error_html}
  <div class="card">
    <h3>Create a new organization</h3>
    <p class="hint">Start an org for your stores. A shareable link token will be generated
    so other Scale stores can join.</p>
    <form method="POST" action="/org/create">
      <input type="hidden" name="shop" value="{safe_shop}" />
      <input type="hidden" name="csrf_token" value="{csrf}" />
      <label for="org_name">Organization name</label>
      <input type="text" id="org_name" name="org_name"
             placeholder="My Brand Organization" required />
      <button type="submit">Create organization &rarr;</button>
    </form>
  </div>
  <div class="card">
    <h3>Join an existing organization</h3>
    <p class="hint">Enter the link token shared by another Scale store in your org.</p>
    <form method="POST" action="/org/join">
      <input type="hidden" name="shop" value="{safe_shop}" />
      <input type="hidden" name="csrf_token" value="{csrf}" />
      <label for="link_token">Link token</label>
      <input type="text" id="link_token" name="link_token" placeholder="Paste link token here" required />
      <button type="submit">Join organization &rarr;</button>
    </form>
  </div>
  <p><a href="/dashboard?shop={safe_shop}">&larr; Back to Dashboard</a></p>
""")


def _render_org_dashboard(
    shop: str,
    org: dict,
    member_stats: list,
    request: Request,
) -> str:
    safe_shop = escape(shop)
    cookie_val = request.cookies.get(COOKIE_NAME, "")
    csrf = csrf_token_for(cookie_val, settings.secret_key)
    org_name = escape(org["name"] if hasattr(org, "__getitem__") else "")
    link_token = escape(org["link_token"])

    rows = ""
    for m in member_stats:
        is_self = m["shop_domain"] == shop
        plan_label = PLANS.get(m["plan"], PLANS["starter"])["name"]
        incidents_badge = (
            f'<span class="badge badge-active">{m["open_incidents"]} open</span>'
            if m["open_incidents"] > 0
            else '<span class="badge badge-ok">All clear</span>'
        )
        self_label = " (this store)" if is_self else ""
        rows += f"""
<tr>
  <td><strong>{escape(m["shop_domain"])}</strong>{escape(self_label)}</td>
  <td>{escape(plan_label)}</td>
  <td>{m["orders_7d"]:,}</td>
  <td>{incidents_badge}</td>
</tr>"""

    table_html = f"""
<table>
  <thead>
    <tr>
      <th>Store</th><th>Plan</th><th>Orders (7d)</th><th>Incidents</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""

    return _page("Multi-Store", f"""
  <h1>Multi-Store Overview</h1>
  <p class="sub">Organization: <strong>{org_name}</strong>
    &bull; <span class="shop">{safe_shop}</span></p>

  <h2>Linked Stores</h2>
  {table_html}

  <h2>Invite another store</h2>
  <div class="card">
    <h3>Link token</h3>
    <p class="hint">Share this token with another Scale store owner. They enter it on their
    <a href="/org?shop=their-store.myshopify.com">/org page</a> to join this organization.
    Both stores must have active billing.</p>
    <div class="token-box">{link_token}</div>
  </div>

  <p style="margin-top:24px;">
    <a href="/dashboard?shop={safe_shop}">&larr; Back to Dashboard</a>
  </p>
""")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

@router.get("/org", response_class=HTMLResponse)
async def org_dashboard(request: Request, shop: str = Query(...)) -> HTMLResponse:
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    pool = await get_pool()
    async with pool.acquire() as conn:
        merchant = await conn.fetchrow(
            """SELECT shop_domain, plan, billing_status, organization_id
               FROM merchants WHERE shop_domain=$1 AND active=TRUE""",
            shop,
        )
    if not merchant:
        raise HTTPException(status_code=404, detail="Shop not found or not active")

    merchant_plan = merchant["plan"] or "starter"
    if not plan_allows(merchant_plan, "multi_store"):
        return HTMLResponse(content=_render_upgrade_required(shop))

    org_id = merchant["organization_id"]
    if org_id is None:
        return HTMLResponse(content=_render_create_or_join(request, shop))

    since = datetime.now(timezone.utc) - timedelta(days=7)
    async with pool.acquire() as conn:
        org = await conn.fetchrow(
            "SELECT id, name, link_token FROM organizations WHERE id=$1",
            org_id,
        )
        members = await conn.fetch(
            """SELECT shop_domain, plan, billing_status
               FROM merchants WHERE organization_id=$1 AND active=TRUE
               ORDER BY shop_domain""",
            org_id,
        )

    member_stats = []
    async with pool.acquire() as conn:
        for m in members:
            order_count_7d = await conn.fetchval(
                """SELECT COUNT(*) FROM checkout_events
                   WHERE shop_domain=$1 AND event_type='order_created' AND created_at>=$2""",
                m["shop_domain"], since,
            ) or 0
            open_incidents = await conn.fetchval(
                "SELECT COUNT(*) FROM incidents WHERE shop_domain=$1 AND resolved_at IS NULL",
                m["shop_domain"],
            ) or 0
            member_stats.append({
                "shop_domain": m["shop_domain"],
                "plan": m["plan"] or "starter",
                "billing_status": m["billing_status"],
                "orders_7d": order_count_7d,
                "open_incidents": open_incidents,
            })

    return HTMLResponse(content=_render_org_dashboard(shop, org, member_stats, request))


@router.post("/org/create")
async def org_create(
    request: Request,
    shop: str = Form(...),
    org_name: str = Form(...),
    csrf_token: Optional[str] = Form(default=None),
) -> HTMLResponse:
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    cookie_val = request.cookies.get(COOKIE_NAME, "")
    if not csrf_token or csrf_token != csrf_token_for(cookie_val, settings.secret_key):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    pool = await get_pool()
    async with pool.acquire() as conn:
        merchant = await conn.fetchrow(
            "SELECT plan, billing_status, organization_id FROM merchants WHERE shop_domain=$1 AND active=TRUE",
            shop,
        )
    if not merchant:
        raise HTTPException(status_code=404)

    merchant_plan = merchant["plan"] or "starter"
    if not plan_allows(merchant_plan, "multi_store"):
        raise HTTPException(status_code=403, detail="Scale plan required to create an organization")
    if merchant["billing_status"] not in _ACTIVE_BILLING:
        raise HTTPException(status_code=403, detail="Active billing required")

    if merchant["organization_id"] is not None:
        # Already in an org — redirect to view.
        return RedirectResponse(url=f"/org?shop={escape(shop)}", status_code=303)

    safe_name = (org_name or "").strip()[:100] or shop
    link_token = secrets.token_urlsafe(24)

    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (name, link_token) VALUES ($1, $2) RETURNING id",
            safe_name, link_token,
        )
        await conn.execute(
            "UPDATE merchants SET organization_id=$1 WHERE shop_domain=$2",
            org_id, shop,
        )

    return RedirectResponse(url=f"/org?shop={escape(shop)}", status_code=303)


@router.post("/org/join")
async def org_join(
    request: Request,
    shop: str = Form(...),
    link_token: str = Form(...),
    csrf_token: Optional[str] = Form(default=None),
) -> HTMLResponse:
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    cookie_val = request.cookies.get(COOKIE_NAME, "")
    if not csrf_token or csrf_token != csrf_token_for(cookie_val, settings.secret_key):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    pool = await get_pool()
    async with pool.acquire() as conn:
        merchant = await conn.fetchrow(
            "SELECT plan, billing_status, organization_id FROM merchants WHERE shop_domain=$1 AND active=TRUE",
            shop,
        )
        org = await conn.fetchrow(
            "SELECT id FROM organizations WHERE link_token=$1",
            link_token.strip(),
        )

    if not merchant:
        raise HTTPException(status_code=404)

    merchant_plan = merchant["plan"] or "starter"
    if not plan_allows(merchant_plan, "multi_store"):
        raise HTTPException(status_code=403, detail="Scale plan required to join an organization")
    if merchant["billing_status"] not in _ACTIVE_BILLING:
        raise HTTPException(status_code=403, detail="Active billing required")

    if not org:
        return HTMLResponse(
            content=_render_create_or_join(
                request, shop, error="Invalid link token — double-check the code and try again."
            ),
            status_code=200,
        )

    if merchant["organization_id"] is not None:
        # Already in an org.
        return RedirectResponse(url=f"/org?shop={escape(shop)}", status_code=303)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE merchants SET organization_id=$1 WHERE shop_domain=$2",
            org["id"], shop,
        )

    return RedirectResponse(url=f"/org?shop={escape(shop)}", status_code=303)
