-- sql/017_phase4_and_tightening.sql
-- Phase 4: git protocol server + tightening for v2.0.0 beta
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT

BEGIN;

-- -------------------------------------------------------------------------
-- TIGHTENING: niggles from Phase 2 review
-- -------------------------------------------------------------------------

-- Niggle 1+2: byte_offset only makes sense for packed objects. Loose
-- objects (the common case) should have NULL offset. The CHECK makes
-- the rule explicit so no one accidentally inserts half-valid rows.
ALTER TABLE repo_objects ALTER COLUMN byte_offset DROP NOT NULL;
ALTER TABLE repo_objects
    DROP CONSTRAINT IF EXISTS byte_offset_matches_pack;

-- Backfill: existing loose-object rows were inserted with byte_offset = 0
-- under the old NOT NULL schema. Set those to NULL so the new constraint
-- can be enforced. pack_id IS NULL identifies the loose-object case.
UPDATE repo_objects SET byte_offset = NULL
 WHERE pack_id IS NULL AND byte_offset IS NOT NULL;

ALTER TABLE repo_objects
    ADD CONSTRAINT byte_offset_matches_pack CHECK (
        (pack_id IS NULL AND byte_offset IS NULL) OR
        (pack_id IS NOT NULL AND byte_offset IS NOT NULL)
    );

-- Niggle 4: file_mode on changesets. Without this, executable scripts
-- and symlinks round-trip as regular files, which breaks Unix tooling
-- downstream. The four values are git's full set of regular modes;
-- gitlinks (160000) are for submodules and we accept them though we
-- don't recurse into submodule content.
ALTER TABLE repo_changesets
    ADD COLUMN IF NOT EXISTS file_mode TEXT NOT NULL DEFAULT '100644';
ALTER TABLE repo_changesets
    DROP CONSTRAINT IF EXISTS valid_file_mode;
ALTER TABLE repo_changesets
    ADD CONSTRAINT valid_file_mode CHECK (
        file_mode IN ('100644','100755','120000','160000')
    );

-- Niggle 9: GPG signature preservation. Nullable because most commits
-- aren't signed; when present, we pass it through to fast-import so
-- signatures survive round-trip.
ALTER TABLE repo_commits
    ADD COLUMN IF NOT EXISTS gpg_signature TEXT;

-- Niggle 3: dangling parent integrity view. parent_hashes[] can't be
-- FK-enforced (shallow clones are legitimate), but ops should have a
-- way to find orphaned references. Cheap query, run on a cron.
CREATE OR REPLACE VIEW repo_dangling_parents AS
SELECT c.repo_id, c.commit_hash AS child, p AS missing_parent
FROM repo_commits c, unnest(c.parent_hashes) AS p
WHERE NOT EXISTS (
    SELECT 1 FROM repo_commits pc WHERE pc.commit_hash = p
);

-- Niggle 5: log retention. Push/pull/protocol logs grow unbounded.
-- Call this from a cron; returns number of rows deleted.
CREATE OR REPLACE FUNCTION prune_git_logs(keep_days INTEGER DEFAULT 90)
RETURNS INTEGER AS $$
DECLARE
    total INTEGER := 0;
    n INTEGER;
BEGIN
    DELETE FROM repo_git_push_log
     WHERE started_at < NOW() - make_interval(days => keep_days);
    GET DIAGNOSTICS n = ROW_COUNT; total := total + n;

    DELETE FROM repo_git_pull_log
     WHERE started_at < NOW() - make_interval(days => keep_days);
    GET DIAGNOSTICS n = ROW_COUNT; total := total + n;

    DELETE FROM repo_git_protocol_log
     WHERE occurred_at < NOW() - make_interval(days => keep_days);
    GET DIAGNOSTICS n = ROW_COUNT; total := total + n;

    RETURN total;
END;
$$ LANGUAGE plpgsql;

-- -------------------------------------------------------------------------
-- PHASE 4: gateway repos
-- -------------------------------------------------------------------------
-- Each Olympus repo has a bare git repo on disk that git-upload-pack
-- and git-receive-pack operate against. The gateway is treated as
-- derived state — it can be rebuilt from repo_commits at any time.
CREATE TABLE IF NOT EXISTS repo_git_gateways (
    repo_id                 BIGINT PRIMARY KEY
                            REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    gateway_path            TEXT NOT NULL,
    last_synced_commit_hash TEXT,
    last_synced_at          TIMESTAMPTZ,
    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','rebuilding','error')),
    error_message           TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- -------------------------------------------------------------------------
-- PHASE 4: Personal Access Tokens (PATs) for git CLI auth
-- -------------------------------------------------------------------------
-- token_hash is bcrypt of the full raw token. token_prefix is the first
-- 12 chars ("olyp_" + 7 random) used as an index so auth is O(1) lookup
-- plus one bcrypt verify, not O(users) bcrypts per request.
CREATE TABLE IF NOT EXISTS repo_pats (
    pat_id        BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES repo_users(user_id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL,
    token_prefix  TEXT NOT NULL,
    scopes        TEXT[] NOT NULL DEFAULT ARRAY['git:read','git:write'],
    expires_at    TIMESTAMPTZ,
    last_used_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    revoked_at    TIMESTAMPTZ,
    UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_pats_prefix
    ON repo_pats(token_prefix) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_pats_user
    ON repo_pats(user_id) WHERE revoked_at IS NULL;

-- -------------------------------------------------------------------------
-- PHASE 4: git protocol access log
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS repo_git_protocol_log (
    log_id        BIGSERIAL PRIMARY KEY,
    repo_id       BIGINT REFERENCES repo_repositories(repo_id) ON DELETE SET NULL,
    user_id       BIGINT REFERENCES repo_users(user_id) ON DELETE SET NULL,
    operation     TEXT NOT NULL CHECK (operation IN (
                      'info-refs-upload', 'info-refs-receive',
                      'upload-pack', 'receive-pack'
                  )),
    ref_updates   JSONB,
    bytes_in      BIGINT DEFAULT 0,
    bytes_out     BIGINT DEFAULT 0,
    status_code   INTEGER,
    error_message TEXT,
    user_agent    TEXT,
    ip_address    INET,
    occurred_at   TIMESTAMPTZ DEFAULT NOW(),
    duration_ms   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_git_protocol_log_repo
    ON repo_git_protocol_log(repo_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_git_protocol_log_user
    ON repo_git_protocol_log(user_id, occurred_at DESC);

COMMIT;
