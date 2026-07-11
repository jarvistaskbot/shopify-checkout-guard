-- CheckoutGuard v2 production hardening migration (004)
-- Safe to run repeatedly — all DDL is idempotent.
-- Apply after 003_fixes.sql; required before v2 production launch.

-- AI call rate-cap tracking per merchant per calendar month.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='merchants' AND column_name='ai_calls_month') THEN
        ALTER TABLE merchants ADD COLUMN ai_calls_month INTEGER NOT NULL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='merchants' AND column_name='ai_calls_reset_at') THEN
        ALTER TABLE merchants ADD COLUMN ai_calls_reset_at TIMESTAMPTZ;
    END IF;
END $$;

-- Change billing_status default from 'trialing' (never set by logic) to 'inactive'.
-- 'trialing' was only the column default; real post-billing values are 'active'/'pending'/'declined'.
-- Merchants who installed but never accepted a charge should have billing suppressed.
ALTER TABLE merchants ALTER COLUMN billing_status SET DEFAULT 'inactive';

-- Rename stale 'trialing' rows (no billing_charge_id means they never went through /billing/start).
UPDATE merchants
SET billing_status = 'inactive'
WHERE billing_status = 'trialing' AND billing_charge_id IS NULL;
