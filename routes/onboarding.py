from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse

from database import get_pool

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
.success-icon { font-size: 40px; margin-bottom: 16px; }
"""


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(shop: str = Query(...)) -> HTMLResponse:
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
    Connected to <span class="shop">{shop}</span>.<br>
    Enter your Slack Incoming Webhook URL to receive revenue drop alerts.
  </p>
  <form method="POST" action="/onboarding">
    <input type="hidden" name="shop" value="{shop}" />
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
    <button type="submit">Save and activate alerts</button>
  </form>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.post("/onboarding", response_class=HTMLResponse)
async def onboarding_save(
    shop: str = Form(...),
    slack_webhook_url: str = Form(...),
) -> HTMLResponse:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE merchants SET slack_webhook_url = $1 WHERE shop_domain = $2",
            slack_webhook_url,
            shop,
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>CheckoutGuard — Active</title>
  <style>{_STYLE}</style>
</head>
<body>
  <div class="success-icon">&#x2705;</div>
  <h1>You&rsquo;re all set</h1>
  <p class="sub">
    CheckoutGuard is monitoring <strong>{shop}</strong>.<br>
    You&rsquo;ll receive a Slack alert if order volume drops &ge;20% below your 7-day baseline.
  </p>
  <p class="sub">
    Baseline builds automatically over the first 7 days of traffic.
    Alerts fire after 3 consecutive drops are detected.
  </p>
</body>
</html>"""
    return HTMLResponse(content=html)
