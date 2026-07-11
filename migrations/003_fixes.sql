-- CheckoutGuard v2 fixes migration (003)
-- Safe to run repeatedly — all DDL is idempotent.
-- Apply locally after 002_v2.sql; required before v2 deploy.

-- Nonce store for OAuth state (replaces in-memory set, survives restarts)
CREATE TABLE IF NOT EXISTS pending_nonces (
    nonce      TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- AI incident analysis column
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='incidents' AND column_name='ai_analysis') THEN
        ALTER TABLE incidents ADD COLUMN ai_analysis TEXT;
    END IF;
END $$;

-- Weekly digest tracking column
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='merchants' AND column_name='last_digest_sent_at') THEN
        ALTER TABLE merchants ADD COLUMN last_digest_sent_at TIMESTAMPTZ;
    END IF;
END $$;

-- Performance: index for abandonment spike NOT EXISTS join
CREATE INDEX IF NOT EXISTS idx_checkout_events_token
    ON checkout_events (shop_domain, checkout_token)
    WHERE checkout_token IS NOT NULL;

-- Nonce auto-cleanup: stale nonces older than 15 minutes are invalid
-- (cleaned by data retention loop; index speeds that DELETE)
CREATE INDEX IF NOT EXISTS idx_pending_nonces_created
    ON pending_nonces (created_at);
