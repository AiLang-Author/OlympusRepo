-- sql/016_git_push.sql
-- Phase 2: Git remote push/pull support.
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT
--
-- Adds:
--   * timezone offset columns on repo_commits (for SHA round-trip fidelity)
--   * repo_git_remotes: per-repo git remote configuration
--   * repo_git_commit_map: olympus_commit_hash <-> git_sha per remote
--   * repo_git_push_log / repo_git_pull_log: audit trail
--   * server-side credential encryption key in repo_server_config

BEGIN;

-- -------------------------------------------------------------------------
-- Timezone fidelity on commits
-- -------------------------------------------------------------------------
-- Git commit SHAs are computed over commit text that includes author
-- and committer lines like:
--     author Alice <a@x> 1700000000 +0200
-- The "+0200" is part of the hashed text. TIMESTAMPTZ normalizes to UTC
-- on read-back, so we need to store the offset string separately to
-- reconstruct byte-identical commit text at push time.
--
-- Format: exactly +HHMM or -HHMM (what git log --date=raw emits).
ALTER TABLE repo_commits
    ADD COLUMN IF NOT EXISTS author_tz_offset    TEXT,
    ADD COLUMN IF NOT EXISTS committer_tz_offset TEXT;

-- Commits that predate this migration have NULL offsets. Export code
-- falls back to "+0000" in that case, which will produce a different
-- SHA than the original. To reclaim SHA fidelity on older imports,
-- re-run the importer against the original git source.

-- -------------------------------------------------------------------------
-- Git remote configuration
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS repo_git_remotes (
    remote_id       BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id)
                    ON DELETE CASCADE,
    name            TEXT NOT NULL,            -- e.g. 'origin', 'github'
    url             TEXT NOT NULL,            -- https://... or git@...
    auth_type       TEXT NOT NULL DEFAULT 'none'
                    CHECK (auth_type IN ('none','token','ssh_key')),
    -- Credentials are encrypted server-side with pgp_sym_encrypt using
    -- the master key stored in repo_server_config['git_creds_key'].
    -- Never write plaintext credentials here.
    auth_credential_enc  BYTEA,
    -- Cached bare mirror for pull/incremental push. Managed by the
    -- pull code; safe to delete, will be re-fetched on next pull.
    mirror_path     TEXT,
    last_push_at    TIMESTAMPTZ,
    last_pull_at    TIMESTAMPTZ,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      BIGINT REFERENCES repo_users(user_id),
    UNIQUE (repo_id, name)
);

CREATE INDEX IF NOT EXISTS idx_git_remotes_repo ON repo_git_remotes(repo_id);

-- -------------------------------------------------------------------------
-- Olympus commit -> git SHA mapping per remote
-- -------------------------------------------------------------------------
-- For commits imported from git: olympus_commit_hash == git_sha, and we
-- pre-populate on import (one row per known remote).
-- For native commits: git_sha is assigned on first push to each remote
-- and recorded here.
--
-- Per-remote keys because the same native commit pushed to two different
-- remotes will have identical git_sha (SHA is a function of commit text,
-- not destination), but tracking per-remote lets us answer "has this
-- commit been pushed to X?" without joining through push_log.
CREATE TABLE IF NOT EXISTS repo_git_commit_map (
    olympus_commit_hash TEXT NOT NULL
                        REFERENCES repo_commits(commit_hash) ON DELETE CASCADE,
    remote_id           BIGINT NOT NULL
                        REFERENCES repo_git_remotes(remote_id) ON DELETE CASCADE,
    git_sha             TEXT NOT NULL,
    pushed_at           TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (olympus_commit_hash, remote_id)
);

CREATE INDEX IF NOT EXISTS idx_git_commit_map_remote
    ON repo_git_commit_map(remote_id, git_sha);

-- -------------------------------------------------------------------------
-- Push/pull audit
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS repo_git_push_log (
    push_id         BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id)
                    ON DELETE CASCADE,
    remote_id       BIGINT REFERENCES repo_git_remotes(remote_id)
                    ON DELETE SET NULL,
    ref_name        TEXT NOT NULL,            -- e.g. 'refs/heads/main'
    from_sha        TEXT,                     -- remote's previous tip (NULL on initial)
    to_sha          TEXT,                     -- new tip after push
    commits_pushed  INTEGER DEFAULT 0,
    blobs_pushed    INTEGER DEFAULT 0,
    bytes_pushed    BIGINT DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','success','failed')),
    error_message   TEXT,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    started_by      BIGINT REFERENCES repo_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_git_push_log_repo
    ON repo_git_push_log(repo_id, started_at DESC);

CREATE TABLE IF NOT EXISTS repo_git_pull_log (
    pull_id         BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id)
                    ON DELETE CASCADE,
    remote_id       BIGINT REFERENCES repo_git_remotes(remote_id)
                    ON DELETE SET NULL,
    ref_name        TEXT NOT NULL,
    from_sha        TEXT,                     -- local tip before pull
    to_sha          TEXT,                     -- local tip after pull
    commits_fetched INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','success','failed')),
    error_message   TEXT,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    started_by      BIGINT REFERENCES repo_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_git_pull_log_repo
    ON repo_git_pull_log(repo_id, started_at DESC);

-- -------------------------------------------------------------------------
-- Bootstrap the credential-encryption key
-- -------------------------------------------------------------------------
-- Generated once on first run. All stored auth_credential_enc values
-- are encrypted with this key via pgp_sym_encrypt. If this key is lost
-- or rotated, all stored credentials must be re-entered.
--
-- The key lives in repo_server_config rather than in a file so that
-- backups of the database are self-contained. Operators who want
-- defense-in-depth should additionally encrypt the database at rest.
INSERT INTO repo_server_config (key, value)
SELECT 'git_creds_key', encode(gen_random_bytes(32), 'hex')
WHERE NOT EXISTS (
    SELECT 1 FROM repo_server_config WHERE key = 'git_creds_key'
);

-- -------------------------------------------------------------------------
-- repo_insert_imported_commit, v2 — adds tz offset parameters
-- -------------------------------------------------------------------------
-- Drop the 015 (13-arg) version and replace with a 15-arg version that
-- interleaves author_tz_offset after authored_at and committer_tz_offset
-- after committed_at. Drop is unconditional because Postgres treats
-- different argument lists as distinct functions; without the drop both
-- signatures coexist and overload resolution depends on ambient cast
-- rules — fine until it isn't.
DROP FUNCTION IF EXISTS repo_insert_imported_commit(
    BIGINT, TEXT, TEXT, TEXT[],
    TEXT, TEXT, TIMESTAMPTZ,
    TEXT, TEXT, TIMESTAMPTZ,
    TEXT, BIGINT, BIGINT
);

CREATE OR REPLACE FUNCTION repo_insert_imported_commit(
    p_repo_id              BIGINT,
    p_commit_hash          TEXT,
    p_tree_hash            TEXT,
    p_parent_hashes        TEXT[],
    p_author_name          TEXT,
    p_author_email         TEXT,
    p_authored_at          TIMESTAMPTZ,
    p_author_tz_offset     TEXT,
    p_committer_name       TEXT,
    p_committer_email      TEXT,
    p_committed_at         TIMESTAMPTZ,
    p_committer_tz_offset  TEXT,
    p_message              TEXT,
    p_author_id            BIGINT DEFAULT NULL,
    p_committer_id         BIGINT DEFAULT NULL
) RETURNS BIGINT AS $$
DECLARE
    new_rev BIGINT;
BEGIN
    INSERT INTO repo_commits (
        commit_hash, repo_id, tree_hash, parent_hashes,
        author_id,    author_name,    author_email,    authored_at,    author_tz_offset,
        committer_id, committer_name, committer_email, committed_at,   committer_tz_offset,
        message, is_imported
    ) VALUES (
        p_commit_hash, p_repo_id, p_tree_hash, p_parent_hashes,
        p_author_id,    p_author_name,    p_author_email,    p_authored_at,    p_author_tz_offset,
        p_committer_id, p_committer_name, p_committer_email, p_committed_at,   p_committer_tz_offset,
        p_message, TRUE
    )
    RETURNING rev INTO new_rev;
    RETURN new_rev;
END;
$$ LANGUAGE plpgsql;

COMMIT;
