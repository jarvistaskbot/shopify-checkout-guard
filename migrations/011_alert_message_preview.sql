-- 011: store alert message content so alerts are fully reviewable in-app,
-- without access to any external delivery channel.

ALTER TABLE alert_deliveries ADD COLUMN IF NOT EXISTS message_preview TEXT;
