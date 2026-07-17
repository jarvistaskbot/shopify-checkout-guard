-- 008: CUSUM slow-bleed detector state (v3, docs/12-V3-DIRECTION.md §3).
-- One-sided CUSUM statistic on hourly checkout-start shortfall, updated once per
-- completed hour. Idempotent, like all migrations.

ALTER TABLE merchants ADD COLUMN IF NOT EXISTS cusum_stat NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE merchants ADD COLUMN IF NOT EXISTS cusum_updated_at TIMESTAMPTZ;
