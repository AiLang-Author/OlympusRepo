-- sql/015_git_import.sql
-- Schema additions needed for full-fidelity git import and Phase 2
-- round-trip to git remotes.
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT
--
-- What's already in place (from 002_tables.sql):
--   * commit_hash TEXT PRIMARY KEY      — reused as the original git SHA
--   * tree_hash   TEXT NOT NULL         — reused as the original git tree SHA
--   * parent_hashes TEXT[]              — multi-parent array, no join table needed
--   * author_name, committer_name       — present
--   * authored_at, committed_at         — present
--   * author_id, committer_id (nullable FKs) — importer may not match a user
--
-- What this migration adds:
--   * author_email, committer_email on repo_commits (needed to round-trip)
--   * imported_from + imported_at on repo_repositories (provenance)
--   * is_imported marker on repo_commits (fast filter during push-to-git)
--   * Index on (repo_id, is_imported) for the push planner

BEGIN;

-- -------------------------------------------------------------------------
-- Commit-level additions
-- -------------------------------------------------------------------------
-- Git identity is "Name <email>". Storing email separately from name means
-- we can reconstruct a byte-identical git commit object later, which is
-- what lets Phase 2 push back with matching SHAs.
ALTER TABLE repo_commits
    ADD COLUMN IF NOT EXISTS author_email    TEXT,
    ADD COLUMN IF NOT EXISTS committer_email TEXT,
    ADD COLUMN IF NOT EXISTS is_imported     BOOLEAN NOT NULL DEFAULT FALSE;

-- Fast path for "give me all git-imported commits in this repo that we
-- haven't pushed back yet" — the core query for push-to-git-remote.
CREATE INDEX IF NOT EXISTS idx_commits_repo_imported
    ON repo_commits(repo_id, is_imported)
    WHERE is_imported = TRUE;

-- -------------------------------------------------------------------------
-- Repository-level additions
-- -------------------------------------------------------------------------
-- Provenance so the push planner knows this repo has a git origin it
-- can push back to. NULL = native Olympus repo, never touched git.
ALTER TABLE repo_repositories
    ADD COLUMN IF NOT EXISTS imported_from TEXT,
    ADD COLUMN IF NOT EXISTS imported_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS imported_by   BIGINT REFERENCES repo_users(user_id);

-- -------------------------------------------------------------------------
-- Helper: bulk-insert an imported commit
-- -------------------------------------------------------------------------
-- Why a SQL function instead of doing this from Python: import loops hit
-- the DB thousands of times on medium repos. Collapsing the commit insert
-- + rev assignment into one round-trip saves real time, and keeps the
-- transaction boundary inside Postgres where it belongs.
--
-- Returns the rev assigned to the new commit. Raises if commit_hash
-- already exists in this repo (which is correct — re-importing should
-- be an explicit UPDATE path, not a silent overwrite).
CREATE OR REPLACE FUNCTION repo_insert_imported_commit(
    p_repo_id          BIGINT,
    p_commit_hash      TEXT,
    p_tree_hash        TEXT,
    p_parent_hashes    TEXT[],
    p_author_name      TEXT,
    p_author_email     TEXT,
    p_authored_at      TIMESTAMPTZ,
    p_committer_name   TEXT,
    p_committer_email  TEXT,
    p_committed_at     TIMESTAMPTZ,
    p_message          TEXT,
    p_author_id        BIGINT DEFAULT NULL,
    p_committer_id     BIGINT DEFAULT NULL
) RETURNS BIGINT AS $$
DECLARE
    new_rev BIGINT;
BEGIN
    INSERT INTO repo_commits (
        commit_hash, repo_id, tree_hash, parent_hashes,
        author_id, author_name, author_email, authored_at,
        committer_id, committer_name, committer_email, committed_at,
        message, is_imported
    ) VALUES (
        p_commit_hash, p_repo_id, p_tree_hash, p_parent_hashes,
        p_author_id, p_author_name, p_author_email, p_authored_at,
        p_committer_id, p_committer_name, p_committer_email, p_committed_at,
        p_message, TRUE
    )
    RETURNING rev INTO new_rev;
    RETURN new_rev;
END;
$$ LANGUAGE plpgsql;

-- Note: rev is a BIGSERIAL UNIQUE column, so Postgres assigns it from the
-- sequence automatically on insert. 010_fix_fk_cascades.sql made it
-- nullable; we don't rely on that here (we always want a rev for local
-- imports), but we also don't re-add NOT NULL since the connector's
-- master/slave flow still needs rev to be nullable on offers.

COMMIT;