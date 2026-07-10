-- CheckoutGuard v2 migration
-- Apply ONLY after v2 is deployed (post App Store approval).
-- Safe to run repeatedly (all DDL is idempotent).

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='merchants' AND column_name='alert_email') THEN
        ALTER TABLE merchants ADD COLUMN alert_email TEXT;
    END IF;
END $$;

-- JS error events: one row per occurrence, indexed for 10-min window counts.
CREATE TABLE IF NOT EXISTS js_error_events (
    id              BIGSERIAL PRIMARY KEY,
    shop_domain     TEXT NOT NULL REFERENCES merchants(shop_domain) ON DELETE CASCADE,
    error_hash      VARCHAR(64) NOT NULL,
    error_message   TEXT NOT NULL,
    page_url        TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_js_error_events_shop_hash_time
    ON js_error_events (shop_domain, error_hash, occurred_at);

CREATE INDEX IF NOT EXISTS idx_js_error_events_shop_time
    ON js_error_events (shop_domain, occurred_at);

-- Order line items: stored from orders/create payloads for hot product detection.
CREATE TABLE IF NOT EXISTS order_line_items (
    id               BIGSERIAL PRIMARY KEY,
    shop_domain      TEXT NOT NULL REFERENCES merchants(shop_domain) ON DELETE CASCADE,
    shopify_order_id BIGINT NOT NULL,
    product_id       BIGINT,
    product_title    TEXT,
    variant_id       BIGINT,
    quantity         INTEGER NOT NULL DEFAULT 1,
    price            NUMERIC(10,2),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_order_line_items_shop_product_time
    ON order_line_items (shop_domain, product_id, created_at);

-- Inventory levels: current state per inventory_item, upserted on each webhook.
CREATE TABLE IF NOT EXISTS inventory_levels (
    id                BIGSERIAL PRIMARY KEY,
    shop_domain       TEXT NOT NULL REFERENCES merchants(shop_domain) ON DELETE CASCADE,
    inventory_item_id BIGINT NOT NULL,
    product_id        BIGINT,
    available         INTEGER NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_levels_shop_item
    ON inventory_levels (shop_domain, inventory_item_id);

CREATE INDEX IF NOT EXISTS idx_inventory_levels_shop_product
    ON inventory_levels (shop_domain, product_id);
