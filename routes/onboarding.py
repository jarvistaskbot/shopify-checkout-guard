from html import escape
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import settings
from database import get_pool
from session import COOKIE_NAME, csrf_token_for, verify_session_token

router = APIRouter()

_STYLE = """
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
    max-width: 560px;
    margin: 80px auto;
    padding: 0 20px;
    color: #1a1a1a;
}
h1 { font-size: 22px; margin-bottom: 6px; }
.sub { color: #666; margin-bottom: 32px; font-size: 15px; }
.shop { font-weight: 600; color: #008060; }
label { display: block; font-weight: 600; font-size: 14px; margin-bottom: 6px; }
.hint { font-size: 13px; color: #666; margin-bottom: 10px; }
input[type=text] {
    width: 100%;
    padding: 10px 12px;
    font-size: 14px;
    border: 1px solid #ccc;
    border-radius: 6px;
    box-sizing: border-box;
    margin-bottom: 4px;
}
button {
    margin-top: 16px;
    padding: 10px 24px;
    background: #008060;
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 15px;
    cursor: pointer;
    font-weight: 600;
}
button:hover { background: #006e52; }
"""


def _require_session(request: Request, shop: str) -> Optional[str]:
    """Return verified shop from cookie, or None if invalid/missing."""
    cookie_val = request.cookies.get(COOKIE_NAME)
    if not cookie_val:
        return None
    verified = verify_session_token(cookie_val, settings.secret_key)
    if not verified or verified != shop:
        return None
    return verified


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, shop: str = Query(...)) -> HTMLResponse:
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    # Derive CSRF token from session cookie.
    cookie_val = request.cookies.get(COOKIE_NAME, "")
    csrf = csrf_token_for(cookie_val, settings.secret_key)
    safe_shop = escape(shop)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>CheckoutGuard — Setup</title>
  <style>{_STYLE}</style>
</head>
<body>
  <h1>CheckoutGuard installed</h1>
  <p class="sub">
    Connected to <span class="shop">{safe_shop}</span>.<br>
    Enter your Slack Incoming Webhook URL to receive revenue drop alerts.
  </p>
  <form method="POST" action="/onboarding">
    <input type="hidden" name="shop" value="{safe_shop}" />
    <input type="hidden" name="csrf_token" value="{csrf}" />
    <label for="slack_webhook_url">Slack Incoming Webhook URL</label>
    <p class="hint">
      In Slack: Apps &rarr; Incoming Webhooks &rarr; Add to Slack &rarr; copy the webhook URL.
    </p>
    <input
      type="text"
      id="slack_webhook_url"
      name="slack_webhook_url"
      placeholder="https://hooks.slack.com/services/T.../B.../..."
      required
    />
    <label for="alert_email" style="margin-top:20px;">Alert Email Address (optional)</label>
    <p class="hint">Receive the same alerts by email via SendGrid.</p>
    <input
      type="text"
      id="alert_email"
      name="alert_email"
      placeholder="you@yourstore.com"
    />
    <button type="submit">Continue to billing &rarr;</button>
  </form>
  <p style="margin-top:40px; font-size:13px; color:#999;">
    Questions? <a href="mailto:artomnats1996@gmail.com" style="color:#008060;">Contact support</a>
  </p>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.post("/onboarding")
async def onboarding_save(
    request: Request,
    shop: str = Form(...),
    slack_webhook_url: str = Form(...),
    alert_email: Optional[str] = Form(default=None),
    csrf_token: Optional[str] = Form(default=None),
) -> RedirectResponse:
    # Verify session cookie and CSRF token.
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    cookie_val = request.cookies.get(COOKIE_NAME, "")
    expected_csrf = csrf_token_for(cookie_val, settings.secret_key)
    if not csrf_token or csrf_token != expected_csrf:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE merchants SET slack_webhook_url = $1, alert_email = $2 WHERE shop_domain = $3",
            slack_webhook_url,
            alert_email or None,
            shop,
        )
    return RedirectResponse(url=f"/billing/start?shop={escape(shop)}", status_code=303)


@router.get("/demo", response_class=HTMLResponse)
async def demo_page(success: str = Query(default="")) -> HTMLResponse:
    if success == "1":
        body = """
  <h1>&#10003; CheckoutGuard is active</h1>
  <p class="sub">Your Slack channel is now connected. We'll notify you the moment your order volume drops below normal.</p>
  <div class="card">
    <div class="card-title">What happens next</div>
    <ul>
      <li>CheckoutGuard monitors your order flow every <strong>30 minutes</strong></li>
      <li>We compare against a <strong>7-day rolling baseline</strong></li>
      <li>If volume drops &gt;50%, a Slack alert fires immediately</li>
      <li>When volume recovers, a "Resolved" message is sent automatically</li>
    </ul>
  </div>
  <p style="margin-top:32px;"><a href="/demo" style="color:#008060;font-weight:600;">&#8592; Back to setup demo</a></p>
"""
    else:
        body = """
  <h1>CheckoutGuard installed</h1>
  <p class="sub">
    Connected to <span class="shop">your-store.myshopify.com</span>.<br>
    Enter your Slack Incoming Webhook URL to receive revenue drop alerts.
  </p>
  <form method="GET" action="/demo">
    <input type="hidden" name="success" value="1" />
    <label for="slack_webhook_url">Slack Incoming Webhook URL</label>
    <p class="hint">
      In Slack: Apps &rarr; Incoming Webhooks &rarr; Add to Slack &rarr; copy the webhook URL.
    </p>
    <input
      type="text"
      id="slack_webhook_url"
      name="slack_webhook_url"
      placeholder="https://hooks.slack.com/services/T.../B.../..."
    />
    <button type="submit">Connect Slack &rarr;</button>
  </form>
  <p class="demo-note">&#9432; This is a demo page for app review purposes.</p>
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>CheckoutGuard — Setup</title>
  <style>
{_STYLE}
.card {{
  background: #f6faf8;
  border: 1px solid #d4e9e2;
  border-radius: 8px;
  padding: 20px 24px;
  margin-top: 24px;
}}
.card-title {{ font-weight: 700; font-size: 14px; margin-bottom: 12px; color: #008060; }}
.card ul {{ margin: 0; padding-left: 20px; }}
.card li {{ font-size: 14px; color: #333; margin-bottom: 8px; }}
.demo-note {{ font-size: 12px; color: #999; margin-top: 24px; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""
    return HTMLResponse(content=html)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_policy() -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>CheckoutGuard — Privacy Policy</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      max-width: 720px; margin: 60px auto; padding: 0 20px; color: #1a1a1a; line-height: 1.7;
    }
    h1 { font-size: 24px; }
    h2 { font-size: 17px; margin-top: 32px; }
    p, li { color: #333; font-size: 15px; }
  </style>
</head>
<body>
  <h1>Privacy Policy &mdash; CheckoutGuard</h1>
  <p><em>Last updated: July 2026</em></p>

  <h2>What we collect</h2>
  <p>CheckoutGuard collects the minimum data required to detect revenue anomalies:</p>
  <ul>
    <li>Your Shopify store domain (e.g. <code>your-store.myshopify.com</code>)</li>
    <li>Order creation timestamps and order IDs (not customer names, emails, or payment details)</li>
    <li>Your Slack Incoming Webhook URL (used only to send you alerts)</li>
  </ul>

  <h2>What we do not collect</h2>
  <ul>
    <li>Customer names, email addresses, or any PII</li>
    <li>Payment or billing card data</li>
  </ul>

  <h2>How we use your data</h2>
  <p>
    Order timestamps are used solely to compute a rolling baseline of your normal order volume.
    When the volume drops significantly, we send an alert to your configured Slack channel.
    We do not share, sell, or use your data for any other purpose.
  </p>

  <h2>Data retention</h2>
  <p>
    Checkout event records older than 90 days are automatically purged.
    When you uninstall CheckoutGuard, your store data is deleted within 48 hours upon
    receipt of the Shopify <code>shop/redact</code> webhook.
  </p>

  <h2>Contact</h2>
  <p>For data requests or questions, contact:
    <a href="mailto:support@checkoutguard.io">support@checkoutguard.io</a></p>
</body>
</html>"""
    return HTMLResponse(content=html)
