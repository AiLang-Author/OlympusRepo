-- sql/020_security.sql
-- Security hardening (see docs/SECURITY_AUDIT.md).
-- Idempotent. License: MIT

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Default credentials. 007_seed.sql created zeus/changeme on every
--    install, alongside the real admin setup.sh creates. Disable any account
--    still using that password and drop its sessions.
-- ---------------------------------------------------------------------------
DO $$
DECLARE n INT;
BEGIN
    UPDATE repo_users SET is_active = FALSE
     WHERE password_hash = crypt('changeme', password_hash) AND is_active;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
        DELETE FROM repo_sessions WHERE user_id IN
            (SELECT user_id FROM repo_users WHERE password_hash = crypt('changeme', password_hash));
        RAISE NOTICE 'Disabled % account(s) still using the default password ''changeme''.', n;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Sessions die with the account: a deactivated user's cookie stops
--    working immediately instead of living out its 24h TTL.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION repo_validate_session(p_session_id TEXT) RETURNS BIGINT AS $$
DECLARE
    found_user BIGINT;
BEGIN
    SELECT s.user_id INTO found_user
      FROM repo_sessions s JOIN repo_users u ON u.user_id = s.user_id
     WHERE s.session_id = p_session_id
       AND s.expires_at > NOW()
       AND u.is_active;
    RETURN found_user;
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION repo_kill_sessions_on_deactivate() RETURNS trigger AS $$
BEGIN
    IF OLD.is_active AND NOT NEW.is_active THEN
        DELETE FROM repo_sessions WHERE user_id = NEW.user_id;
        UPDATE repo_pats SET revoked_at = NOW() WHERE user_id = NEW.user_id AND revoked_at IS NULL;
    END IF;
    IF NEW.password_hash IS DISTINCT FROM OLD.password_hash THEN
        DELETE FROM repo_sessions WHERE user_id = NEW.user_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_kill_sessions ON repo_users;
CREATE TRIGGER trg_kill_sessions AFTER UPDATE OF is_active, password_hash ON repo_users
    FOR EACH ROW EXECUTE FUNCTION repo_kill_sessions_on_deactivate();

-- PATs must also stop working for deactivated users even if a revoke was missed.
-- (pats.verify_pat joins repo_users.is_active in the app layer as well.)

-- Expired-session cleanup, cron-callable (was a DELETE on every request).
CREATE OR REPLACE FUNCTION prune_sessions() RETURNS INTEGER AS $$
DECLARE n INTEGER;
BEGIN
    DELETE FROM repo_sessions WHERE expires_at < NOW();
    GET DIAGNOSTICS n = ROW_COUNT;
    DELETE FROM repo_password_resets WHERE expires_at < NOW() - INTERVAL '1 day';
    RETURN n;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 3. Password reset tokens: store only a SHA-256 digest.
-- ---------------------------------------------------------------------------
ALTER TABLE repo_password_resets ADD COLUMN IF NOT EXISTS token_sha256 TEXT;
UPDATE repo_password_resets SET used = TRUE WHERE token_sha256 IS NULL AND NOT used;
CREATE INDEX IF NOT EXISTS idx_password_resets_sha ON repo_password_resets(token_sha256);

-- ---------------------------------------------------------------------------
-- 4. Federation offers: who may submit. 'trusted' (default) accepts only
--    signer public keys listed in offer_trusted_keys; 'open' accepts any
--    valid signature (old behaviour); 'closed' refuses all offers.
-- ---------------------------------------------------------------------------
INSERT INTO repo_server_config (key, value)
VALUES ('offer_policy', 'trusted'), ('offer_trusted_keys', '')
ON CONFLICT (key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 5. 010_fix_fk_cascades.sql ran before 011 created repo_file_revisions, so
--    on most installs this statement silently failed. Re-apply.
-- ---------------------------------------------------------------------------
ALTER TABLE repo_file_revisions ALTER COLUMN global_rev DROP NOT NULL;

-- Blob membership lookups for sync_blob's per-repo authorization check.
CREATE INDEX IF NOT EXISTS idx_changesets_blob_after ON repo_changesets(blob_after);

COMMIT;
