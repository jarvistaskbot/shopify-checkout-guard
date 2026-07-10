-- CheckoutGuard migration 005 — 4-tier pricing
-- Idempotent: safe to run multiple times.
-- Apply after 004_production.sql.

DO $$ BEGIN
    -- plan key (maps to services/plans.py PLANS dict)
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='merchants' AND column_name='plan') THEN
        ALTER TABLE merchants ADD COLUMN plan TEXT NOT NULL DEFAULT 'starter';
    END IF;
    -- per-merchant threshold overrides (scale tier only)
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='merchants' AND column_name='threshold_override') THEN
        ALTER TABLE merchants ADD COLUMN threshold_override JSONB;
    END IF;
END $$;
