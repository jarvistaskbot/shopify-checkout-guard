CREATE TABLE IF NOT EXISTS merchants (
    shop_domain          TEXT PRIMARY KEY,
    access_token         TEXT NOT NULL,
    slack_webhook_url    TEXT,
    alert_threshold_pct  INTEGER NOT NULL DEFAULT 20,
    installed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    active               BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS checkout_events (
    id              BIGSERIAL PRIMARY KEY,
    shop_domain     TEXT NOT NULL REFERENCES merchants(shop_domain) ON DELETE CASCADE,
    event_type      TEXT NOT NULL CHECK (event_type IN ('checkout_created', 'checkout_deleted', 'order_created')),
    checkout_token  TEXT,
    order_id        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_checkout_events_shop_created
    ON checkout_events (shop_domain, created_at);

CREATE INDEX IF NOT EXISTS idx_checkout_events_type
    ON checkout_events (shop_domain, event_type, created_at);

CREATE TABLE IF NOT EXISTS incidents (
    id                              BIGSERIAL PRIMARY KEY,
    shop_domain                     TEXT NOT NULL REFERENCES merchants(shop_domain) ON DELETE CASCADE,
    started_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at                     TIMESTAMPTZ,
    checkout_rate_before            NUMERIC(6, 4) NOT NULL,
    checkout_rate_during            NUMERIC(6, 4) NOT NULL,
    estimated_revenue_loss_per_min  NUMERIC(12, 2) NOT NULL,
    avg_order_value                 NUMERIC(10, 2) NOT NULL,
    notified                        BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_incidents_shop_active
    ON incidents (shop_domain, resolved_at)
    WHERE resolved_at IS NULL;
