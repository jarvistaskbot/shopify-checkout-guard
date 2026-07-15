import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from config import settings
from database import create_pool, get_pool
from routes.auth import is_valid_shop_domain
from routes.auth import router as auth_router
from routes.billing import router as billing_router
from routes.dashboard import router as dashboard_router
from routes.events import router as events_router
from routes.onboarding import router as onboarding_router
from routes.org import router as org_router
from routes.webhooks import router as webhook_router
from services.detector import run_proactive_checks_all_merchants, run_proactive_checks_fast_merchants

logger = logging.getLogger(__name__)

# Alert after this many consecutive loop failures (prevents silent infinite failure loops).
_MAX_CONSECUTIVE_ERRORS = 5


async def _proactive_monitor_loop() -> None:
    """Run payment failure checks every 5 minutes for all active merchants."""
    consecutive_errors = 0
    while True:
        await asyncio.sleep(300)
        try:
            await run_proactive_checks_all_merchants()
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            logger.error(
                "Proactive monitor loop error (%d consecutive): %s",
                consecutive_errors, exc,
            )
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                logger.critical(
                    "Proactive monitor has failed %d times in a row — possible DB outage",
                    consecutive_errors,
                )


async def _fast_proactive_monitor_loop() -> None:
    """Run payment failure checks every 1 minute for pro/scale merchants (fast_checks feature)."""
    consecutive_errors = 0
    while True:
        await asyncio.sleep(60)
        try:
            await run_proactive_checks_fast_merchants()
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            logger.error(
                "Fast proactive monitor loop error (%d consecutive): %s",
                consecutive_errors, exc,
            )


async def _token_refresh_loop() -> None:
    """Proactively refresh expiring tokens every 20 minutes."""
    consecutive_errors = 0
    while True:
        await asyncio.sleep(1200)
        try:
            from services.token_manager import get_valid_token
            # Fetch list, then release connection before per-merchant HTTP calls.
            pool = await get_pool()
            rows = await pool.fetch(
                "SELECT shop_domain, token_expires_at FROM merchants WHERE active = TRUE"
            )
            for row in rows:
                if row["token_expires_at"] is None:
                    continue
                try:
                    async with pool.acquire() as conn:
                        await get_valid_token(conn, row["shop_domain"], settings.shopify_api_key, settings.shopify_api_secret)
                except Exception as exc:
                    logger.error("Token refresh failed for %s: %s", row["shop_domain"], exc)
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            logger.error("Token refresh loop error (%d consecutive): %s", consecutive_errors, exc)


async def _data_retention_loop() -> None:
    """Purge old events nightly. Prevents unbounded table growth."""
    while True:
        await asyncio.sleep(3600)  # check hourly, actual purge is daily
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Purge events older than 90 days (privacy policy says "purged after 90 days").
                cutoff_90 = datetime.now(timezone.utc) - timedelta(days=90)
                cutoff_7 = datetime.now(timezone.utc) - timedelta(days=7)
                cutoff_15m = datetime.now(timezone.utc) - timedelta(minutes=15)

                deleted_ce = await conn.fetchval(
                    """WITH del AS (
                           DELETE FROM checkout_events WHERE created_at < $1 RETURNING 1
                       ) SELECT COUNT(*) FROM del""",
                    cutoff_90,
                ) or 0
                deleted_js = await conn.fetchval(
                    """WITH del AS (
                           DELETE FROM js_error_events WHERE occurred_at < $1 RETURNING 1
                       ) SELECT COUNT(*) FROM del""",
                    cutoff_90,
                ) or 0
                deleted_li = await conn.fetchval(
                    """WITH del AS (
                           DELETE FROM order_line_items WHERE created_at < $1 RETURNING 1
                       ) SELECT COUNT(*) FROM del""",
                    cutoff_7,
                ) or 0
                # Clean expired nonces.
                await conn.execute(
                    "DELETE FROM pending_nonces WHERE created_at < $1", cutoff_15m
                )

                if deleted_ce or deleted_js or deleted_li:
                    logger.info(
                        "Data retention: purged %d checkout_events, %d js_errors, %d line_items",
                        deleted_ce, deleted_js, deleted_li,
                    )
        except Exception as exc:
            logger.error("Data retention loop error: %s", exc)


async def _weekly_digest_loop() -> None:
    """Send weekly digest emails to merchants who have alert_email configured."""
    while True:
        await asyncio.sleep(3600)  # check hourly
        try:
            await _send_pending_digests()
        except Exception as exc:
            logger.error("Weekly digest loop error: %s", exc)


async def _send_pending_digests() -> None:
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        merchants = await conn.fetch(
            """SELECT shop_domain, alert_email, avg_order_value, installed_at, last_digest_sent_at
               FROM merchants
               WHERE active = TRUE AND alert_email IS NOT NULL
                 AND billing_status IN ('active', 'pending')
                 AND plan IN ('growth', 'pro', 'scale')
                 AND installed_at <= NOW() - INTERVAL '7 days'
                 AND (last_digest_sent_at IS NULL OR last_digest_sent_at < NOW() - INTERVAL '7 days')"""
        )

    for m in merchants:
        try:
            await _send_digest_for_merchant(m, now, pool)
        except Exception as exc:
            logger.error("Digest failed for %s: %s", m["shop_domain"], exc)


async def _send_digest_for_merchant(merchant, now: datetime, pool) -> None:
    shop = merchant["shop_domain"]
    since = now - timedelta(days=7)

    async with pool.acquire() as conn:
        checkout_count = await conn.fetchval(
            """SELECT COUNT(*) FROM checkout_events
               WHERE shop_domain=$1 AND event_type='checkout_created' AND created_at >= $2""",
            shop, since,
        ) or 0
        order_count = await conn.fetchval(
            """SELECT COUNT(*) FROM checkout_events
               WHERE shop_domain=$1 AND event_type='order_created' AND created_at >= $2""",
            shop, since,
        ) or 0
        incident_count = await conn.fetchval(
            "SELECT COUNT(*) FROM incidents WHERE shop_domain=$1 AND started_at >= $2",
            shop, since,
        ) or 0
        estimated_protected = await conn.fetchval(
            """SELECT COALESCE(SUM(estimated_revenue_loss_per_min * 60), 0)
               FROM incidents
               WHERE shop_domain=$1 AND started_at >= $2 AND resolved_at IS NOT NULL""",
            shop, since,
        ) or 0.0

    conversion_rate_pct = (order_count / checkout_count * 100) if checkout_count > 0 else 0.0

    # Baseline = conversion rate of the week BEFORE this digest window.
    prev_since = since - timedelta(days=7)
    async with pool.acquire() as conn:
        prev_checkouts = await conn.fetchval(
            """SELECT COUNT(*) FROM checkout_events
               WHERE shop_domain=$1 AND event_type='checkout_created'
                 AND created_at >= $2 AND created_at < $3""",
            shop, prev_since, since,
        ) or 0
        prev_orders = await conn.fetchval(
            """SELECT COUNT(*) FROM checkout_events
               WHERE shop_domain=$1 AND event_type='order_created'
                 AND created_at >= $2 AND created_at < $3""",
            shop, prev_since, since,
        ) or 0
    baseline_pct = (prev_orders / prev_checkouts * 100) if prev_checkouts > 0 else 0.0

    ai_summary = None
    if settings.ai_analysis_enabled and settings.ai_api_key:
        from services.ai_analyst import generate_text
        prompt = (
            f"You are a Shopify expert writing a brief weekly digest for a merchant. "
            f"Their store {shop} had these metrics last week: "
            f"{checkout_count} checkouts, {order_count} orders "
            f"({conversion_rate_pct:.1f}% conversion), {incident_count} incident(s). "
            f"Write exactly 2 friendly, concise sentences summarizing the week. "
            f"If incident_count=0, be encouraging. No preamble."
        )
        ai_summary = await generate_text(prompt, settings.ai_api_key, max_tokens=100)
        if ai_summary:
            ai_summary = ai_summary[:300]

    from services.alerter import send_weekly_digest
    await send_weekly_digest(
        shop_domain=shop,
        to_email=merchant["alert_email"],
        checkout_count=checkout_count,
        order_count=order_count,
        conversion_rate_pct=conversion_rate_pct,
        baseline_rate_pct=baseline_pct,
        incident_count=incident_count,
        estimated_protected_usd=float(estimated_protected),
        ai_summary=ai_summary,
    )

    pool2 = await get_pool()
    async with pool2.acquire() as conn:
        await conn.execute(
            "UPDATE merchants SET last_digest_sent_at=$1 WHERE shop_domain=$2",
            now, shop,
        )
    logger.info("Weekly digest sent to %s", shop)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.secret_key == "dev-secret-change-in-prod" and not settings.billing_test_mode:
        raise RuntimeError(
            "SECRET_KEY is still the default dev value in a non-test environment. "
            "Set SECRET_KEY before serving production traffic."
        )

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

    tasks = [
        asyncio.create_task(_token_refresh_loop()),
        asyncio.create_task(_proactive_monitor_loop()),
        asyncio.create_task(_fast_proactive_monitor_loop()),
        asyncio.create_task(_data_retention_loop()),
        asyncio.create_task(_weekly_digest_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="CheckoutGuard", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(dashboard_router)
app.include_router(events_router)
app.include_router(onboarding_router)
app.include_router(org_router)
app.include_router(webhook_router)


_LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CheckoutGuard</title>
  <style>
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
        max-width: 560px;
        margin: 80px auto;
        padding: 0 20px;
        color: #1a1a1a;
    }
    h1 { font-size: 22px; margin-bottom: 6px; }
    .sub { color: #666; font-size: 15px; line-height: 1.6; }
    .brand { color: #008060; }
    a { color: #008060; }
  </style>
</head>
<body>
  <h1><span class="brand">CheckoutGuard</span></h1>
  <p class="sub">
    CheckoutGuard monitors your Shopify store's checkout funnel around the clock
    and alerts you in Slack or by email the moment checkout conversion drops,
    payments start failing, or a hot product sells out &mdash; so silent revenue
    bleed never goes unnoticed. Install CheckoutGuard from the Shopify App Store
    to get started.
  </p>
  <p class="sub" style="margin-top:32px; font-size:13px; color:#999;">
    Questions? <a href="mailto:artomnats1996@gmail.com">Contact support</a>
    &bull; <a href="/privacy">Privacy policy</a>
  </p>
</body>
</html>"""


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "CheckoutGuard"}


@app.get("/")
async def root(shop: Optional[str] = Query(default=None)):
    # Shopify may open the app root with ?shop=... — forward into OAuth.
    if shop and is_valid_shop_domain(shop):
        return RedirectResponse(url=f"/auth/shopify?shop={quote(shop)}", status_code=302)
    return HTMLResponse(content=_LANDING_HTML)
