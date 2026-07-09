import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

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
