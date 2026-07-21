-- 010: alert delivery history.
-- Records every Slack alert send attempt so delivery is verifiable from the
-- dashboard without access to the receiving Slack workspace.

CREATE TABLE IF NOT EXISTS alert_deliveries (
    id            BIGSERIAL PRIMARY KEY,
    shop_domain   TEXT NOT NULL REFERENCES merchants(shop_domain) ON DELETE CASCADE,
    alert_type    TEXT NOT NULL,
    incident_id   BIGINT,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    success       BOOLEAN NOT NULL,
    status_detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_alert_deliveries_shop_sent
    ON alert_deliveries (shop_domain, sent_at DESC);
