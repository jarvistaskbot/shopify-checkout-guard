import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from config import settings
from database import create_pool, get_pool
from routes.auth import router as auth_router
from routes.billing import router as billing_router
from routes.onboarding import router as onboarding_router
from routes.webhooks import router as webhook_router
from services.detector import run_proactive_checks_all_merchants

logger = logging.getLogger(__name__)


async def _proactive_monitor_loop() -> None:
    """Run payment failure checks every 5 minutes for all active merchants."""
    while True:
        await asyncio.sleep(300)
        try:
            await run_proactive_checks_all_merchants()
        except Exception as exc:
            logger.error("Proactive monitor loop error: %s", exc)


async def _token_refresh_loop() -> None:
    """Proactively refresh expiring tokens every 20 minutes."""
    while True:
        await asyncio.sleep(1200)
        try:
            from services.token_manager import get_valid_token
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT shop_domain, token_expires_at FROM merchants WHERE active = TRUE"
                )
                for row in rows:
                    if row["token_expires_at"] is None:
                        continue
                    try:
                        await get_valid_token(conn, row["shop_domain"], settings.shopify_api_key, settings.shopify_api_secret)
                    except Exception as exc:
                        logger.error("Token refresh failed for %s: %s", row["shop_domain"], exc)
        except Exception as exc:
            logger.error("Token refresh loop error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.database_url:
        for attempt in range(10):
            try:
                await create_pool(settings.database_url)
                logger.info("Database pool created")
                break
            except Exception as exc:
                if attempt == 9:
                    logger.error("Database unavailable after 10 attempts: %s", exc)
                    break
                await asyncio.sleep(3)
    else:
        logger.warning("DATABASE_URL not set — skipping DB pool creation")
    task = asyncio.create_task(_token_refresh_loop())
    task2 = asyncio.create_task(_proactive_monitor_loop())
    yield
    task.cancel()
    task2.cancel()


app = FastAPI(title="CheckoutGuard", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(onboarding_router)
app.include_router(webhook_router)


@app.get("/")
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "CheckoutGuard"}


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Privacy Policy — CheckoutGuard</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:800px;margin:0 auto;padding:40px 24px;color:#1e293b;line-height:1.7}h1{color:#0f172a;font-size:2rem;margin-bottom:8px}h2{color:#0f172a;margin-top:2rem}p,li{color:#374151}a{color:#10b981}.updated{color:#6b7280;font-size:0.9rem}</style>
</head>
<body>
<h1>Privacy Policy</h1>
<p class="updated">Last updated: July 9, 2026</p>

<p>CheckoutGuard ("we", "our", "us") operates the CheckoutGuard Shopify app. This Privacy Policy explains how we collect, use, and protect information when you install and use our app.</p>

<h2>Information We Collect</h2>
<p>When you install CheckoutGuard, we collect and store:</p>
<ul>
  <li><strong>Shop domain</strong> — to identify your store and route alerts correctly</li>
  <li><strong>Shopify access token</strong> — to read order data from your store via the Shopify API</li>
  <li><strong>Slack webhook URL</strong> — the URL you provide to receive alert notifications</li>
  <li><strong>Order timestamps and counts</strong> — anonymized order volume data used to detect revenue drops. We do not store order contents, customer names, emails, addresses, or payment information.</li>
</ul>

<h2>How We Use Your Information</h2>
<ul>
  <li>To monitor your store's order volume in real time</li>
  <li>To detect and alert you about significant drops in order activity</li>
  <li>To send resolved notifications when order volume recovers</li>
</ul>
<p>We do not sell, share, or use your data for advertising or any purpose other than providing the CheckoutGuard service.</p>

<h2>Data Storage and Security</h2>
<p>Your data is stored on secured servers with encrypted connections. Access tokens are stored with encryption at rest. We retain data only as long as your app installation is active.</p>

<h2>Data Deletion</h2>
<p>When you uninstall CheckoutGuard, we process a mandatory data deletion webhook from Shopify and permanently delete all data associated with your shop within 48 hours.</p>

<h2>Third-Party Services</h2>
<p>CheckoutGuard sends alert notifications to Slack using the webhook URL you provide. We do not share your Shopify data with Slack or any other third party.</p>

<h2>Contact</h2>
<p>For privacy questions or data deletion requests, contact us at <a href="mailto:artomnats1996@gmail.com">artomnats1996@gmail.com</a>.</p>
</body>
</html>"""
