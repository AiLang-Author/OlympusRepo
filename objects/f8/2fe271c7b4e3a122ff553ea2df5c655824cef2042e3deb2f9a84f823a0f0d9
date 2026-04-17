-- sql/011_file_revisions.sql
-- File-level revision tracking
-- Uses committed_at timestamp as the version identifier

CREATE TABLE repo_file_revisions (
    frev_id      BIGSERIAL PRIMARY KEY,
    repo_id      BIGINT NOT NULL REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    blob_hash    TEXT NOT NULL,
    commit_hash  TEXT NOT NULL REFERENCES repo_commits(commit_hash) ON DELETE CASCADE,
    global_rev   BIGINT NOT NULL,
    change_type  TEXT NOT NULL CHECK (change_type IN ('add','modify','delete')),
    committed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    author_name  TEXT,
    message      TEXT
);

CREATE INDEX idx_frev_repo_path    ON repo_file_revisions(repo_id, path, committed_at DESC);
CREATE INDEX idx_frev_commit       ON repo_file_revisions(commit_hash);
CREATE INDEX idx_frev_global       ON repo_file_revisions(repo_id, global_rev DESC);
CREATE INDEX idx_frev_blob         ON repo_file_revisions(blob_hash);

CREATE TABLE repo_archive_log (
    archive_id   BIGSERIAL PRIMARY KEY,
    repo_id      BIGINT NOT NULL REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    pruned_by    BIGINT REFERENCES repo_users(user_id),
    strategy     TEXT NOT NULL,
    files_pruned BIGINT NOT NULL DEFAULT 0,
    revs_pruned  BIGINT NOT NULL DEFAULT 0,
    pruned_at    TIMESTAMPTZ DEFAULT NOW(),
    notes        TEXT
);