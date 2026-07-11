-- CheckoutGuard migration 006 — monthly order-volume caps
-- Idempotent: safe to run multiple times.
-- Apply after 005_pricing_tiers.sql.

DO $$ BEGIN
    -- Running total of orders for the current calendar month.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='merchants' AND column_name='orders_month') THEN
        ALTER TABLE merchants ADD COLUMN orders_month INT NOT NULL DEFAULT 0;
    END IF;
    -- Timestamp of the last counter reset; used to detect month rollover.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='merchants' AND column_name='orders_month_reset_at') THEN
        ALTER TABLE merchants ADD COLUMN orders_month_reset_at TIMESTAMPTZ;
    END IF;
    -- Records when we last sent the over-cap Slack notice so we fire it only once per month.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='merchants' AND column_name='orders_cap_notice_sent_at') THEN
        ALTER TABLE merchants ADD COLUMN orders_cap_notice_sent_at TIMESTAMPTZ;
    END IF;
END $$;
