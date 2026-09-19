-- 012: review-banner state per merchant (Fix C).
-- Tracks how many times the review request banner has been shown and whether
-- the merchant dismissed it. Cap = 3 impressions; dismissed = permanent hide.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='merchants' AND column_name='review_banner_impressions') THEN
        ALTER TABLE merchants ADD COLUMN review_banner_impressions INTEGER NOT NULL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='merchants' AND column_name='review_banner_dismissed') THEN
        ALTER TABLE merchants ADD COLUMN review_banner_dismissed BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
END $$;
