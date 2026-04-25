-- sql/018_anon_offerings.sql
-- Anonymous offerings on public repos. Lets drive-by contributors
-- submit small fixes (1-line typo, dead link, etc.) without creating
-- an account. Maintainer flow is unchanged — anon offerings show up
-- in /zeus/staging tagged 'anon' with the contributor's name + email
-- + IP for triage.
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering. MIT.

BEGIN;

-- Allow staging entries without a logged-in user. user_id NULL means
-- "anonymous" — the anon_* columns carry the attribution data the
-- maintainer needs to triage and (if promoted) credit the commit.
ALTER TABLE repo_staging
    ALTER COLUMN user_id DROP NOT NULL;

ALTER TABLE repo_staging
    ADD COLUMN IF NOT EXISTS anon_name    TEXT,
    ADD COLUMN IF NOT EXISTS anon_email   TEXT,
    ADD COLUMN IF NOT EXISTS anon_reason  TEXT,
    ADD COLUMN IF NOT EXISTS anon_ip      INET,
    ADD COLUMN IF NOT EXISTS public_token TEXT UNIQUE;

-- Sanity: every staging row must have either a user_id OR a public_token.
-- A NULL user_id with no token means the row was created broken; reject
-- with a constraint so the bug surfaces at the INSERT, not later.
ALTER TABLE repo_staging
    DROP CONSTRAINT IF EXISTS staging_attribution_required;
ALTER TABLE repo_staging
    ADD CONSTRAINT staging_attribution_required CHECK (
        user_id IS NOT NULL OR public_token IS NOT NULL
    );

-- Public-facing lookup index — bookmark URLs hit this constantly.
CREATE INDEX IF NOT EXISTS idx_staging_public_token
    ON repo_staging(public_token)
    WHERE public_token IS NOT NULL;

-- Per-repo opt-out: Zeus can disable anon offerings for a repo via the
-- settings page. Default ON (matches "drive-by-fixes welcome" model).
ALTER TABLE repo_repositories
    ADD COLUMN IF NOT EXISTS anon_offerings_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- Rate limiter ledger. Per-IP counter for the last hour. Simple SQL
-- approach beats keeping it in Python memory: survives restarts, works
-- across uvicorn workers, and easy to inspect/reset from psql.
CREATE TABLE IF NOT EXISTS repo_anon_rate_log (
    log_id      BIGSERIAL PRIMARY KEY,
    ip          INET NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_anon_rate_ip_time
    ON repo_anon_rate_log(ip, occurred_at DESC);

-- Cron-callable cleaner; logs older than 7 days are useless.
CREATE OR REPLACE FUNCTION prune_anon_rate_log(keep_days INTEGER DEFAULT 7)
RETURNS INTEGER AS $$
DECLARE
    n INTEGER;
BEGIN
    DELETE FROM repo_anon_rate_log
     WHERE occurred_at < NOW() - make_interval(days => keep_days);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$ LANGUAGE plpgsql;

COMMIT;
