-- CheckoutGuard migration 007 — multi-store organizations (Scale plan)
-- Idempotent: safe to run multiple times.
-- Apply after 006_order_caps.sql.

-- Organizations group multiple Scale-plan stores under a single view.
CREATE TABLE IF NOT EXISTS organizations (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    link_token  TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='merchants' AND column_name='organization_id') THEN
        ALTER TABLE merchants ADD COLUMN organization_id INT REFERENCES organizations(id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_merchants_org
    ON merchants(organization_id) WHERE organization_id IS NOT NULL;
