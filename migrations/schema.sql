CREATE TABLE IF NOT EXISTS merchants (
    shop_domain          TEXT PRIMARY KEY,
    access_token         TEXT NOT NULL,
    refresh_token        TEXT,
    token_expires_at     TIMESTAMPTZ,
    slack_webhook_url    TEXT,
    alert_threshold_pct  INTEGER NOT NULL DEFAULT 20,
    installed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    active               BOOLEAN NOT NULL DEFAULT TRUE
);

-- Idempotent column additions for deployments against an existing schema.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='merchants' AND column_name='refresh_token') THEN
        ALTER TABLE merchants ADD COLUMN refresh_token TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='merchants' AND column_name='token_expires_at') THEN
        ALTER TABLE merchants ADD COLUMN token_expires_at TIMESTAMPTZ;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='merchants' AND column_name='drop_streak') THEN
        ALTER TABLE merchants ADD COLUMN drop_streak INTEGER NOT NULL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='merchants' AND column_name='recovery_streak') THEN
        ALTER TABLE merchants ADD COLUMN recovery_streak INTEGER NOT NULL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='merchants' AND column_name='avg_order_value') THEN
        ALTER TABLE merchants ADD COLUMN avg_order_value NUMERIC(10,2) NOT NULL DEFAULT 50.00;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='merchants' AND column_name='billing_charge_id') THEN
        ALTER TABLE merchants ADD COLUMN billing_charge_id TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='merchants' AND column_name='billing_status') THEN
        ALTER TABLE merchants ADD COLUMN billing_status TEXT NOT NULL DEFAULT 'trialing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='merchants' AND column_name='billing_activated_at') THEN
        ALTER TABLE merchants ADD COLUMN billing_activated_at TIMESTAMPTZ;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='merchants' AND column_name='trial_ends_at') THEN
        ALTER TABLE merchants ADD COLUMN trial_ends_at TIMESTAMPTZ;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='incidents' AND column_name='incident_type') THEN
        ALTER TABLE incidents ADD COLUMN incident_type TEXT NOT NULL DEFAULT 'volume_drop';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='incidents' AND column_name='detail') THEN
        ALTER TABLE incidents ADD COLUMN detail JSONB;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='merchants' AND column_name='checkout_conversion_baseline') THEN
        ALTER TABLE merchants ADD COLUMN checkout_conversion_baseline NUMERIC(5,4);
    END IF;
END $$;

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
