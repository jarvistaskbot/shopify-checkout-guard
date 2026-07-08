from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import settings
from database import create_pool
from routes.auth import router as auth_router
from routes.webhooks import router as webhook_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_pool(settings.database_url)
    yield


app = FastAPI(title="CheckoutGuard", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(webhook_router)


@app.get("/")
async def health() -> dict:
    return {"status": "ok", "service": "CheckoutGuard"}
