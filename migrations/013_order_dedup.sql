-- 013: deduplicate order_created events and enforce uniqueness.
-- Shopify redelivers orders/create webhooks (retries observed 2ms apart);
-- the handler stored every delivery, inflating order counts and line items.

-- Remove existing duplicate order_created rows, keeping the earliest.
DELETE FROM checkout_events ce
USING checkout_events keeper
WHERE ce.event_type = 'order_created'
  AND ce.order_id <> ''
  AND keeper.event_type = 'order_created'
  AND keeper.shop_domain = ce.shop_domain
  AND keeper.order_id = ce.order_id
  AND keeper.id < ce.id;

-- Remove duplicated line items from the same double-deliveries, keeping the
-- earliest copy of each identical row.
DELETE FROM order_line_items oli
USING order_line_items keeper
WHERE keeper.shop_domain = oli.shop_domain
  AND keeper.shopify_order_id = oli.shopify_order_id
  AND keeper.product_id IS NOT DISTINCT FROM oli.product_id
  AND keeper.variant_id IS NOT DISTINCT FROM oli.variant_id
  AND keeper.quantity = oli.quantity
  AND keeper.price IS NOT DISTINCT FROM oli.price
  AND keeper.id < oli.id;

-- One order_created event per (shop, order). Partial: pixel/checkout events
-- share this table and must not be constrained.
CREATE UNIQUE INDEX IF NOT EXISTS uq_checkout_events_order
    ON checkout_events (shop_domain, order_id)
    WHERE event_type = 'order_created' AND order_id <> '';
