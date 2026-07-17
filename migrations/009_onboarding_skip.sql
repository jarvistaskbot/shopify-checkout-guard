-- Idempotent: adds onboarding_seen flag so merchants who skip Slack setup
-- are not bounced back to /onboarding on every re-auth.
ALTER TABLE merchants ADD COLUMN IF NOT EXISTS onboarding_seen BOOLEAN NOT NULL DEFAULT FALSE;
