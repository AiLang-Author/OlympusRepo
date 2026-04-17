-- sql/013_issues.sql
-- Bug tracker: issues, file attachments, comments
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT

CREATE TABLE repo_issues (
    issue_id        BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    number          BIGINT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','in_progress','resolved','closed','wontfix')),
    priority        TEXT NOT NULL DEFAULT 'normal'
                    CHECK (priority IN ('critical','high','normal','low')),
    issue_type      TEXT NOT NULL DEFAULT 'bug'
                    CHECK (issue_type IN ('bug','feature','task','question','security')),
    reported_by     BIGINT REFERENCES repo_users(user_id),
    assigned_to     BIGINT REFERENCES repo_users(user_id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    UNIQUE (repo_id, number)
);

CREATE TABLE repo_issue_files (
    issue_id        BIGINT NOT NULL REFERENCES repo_issues(issue_id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    line_start      INTEGER,
    line_end        INTEGER,
    blob_hash       TEXT,
    added_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (issue_id, path)
);

CREATE TABLE repo_issue_commits (
    issue_id        BIGINT NOT NULL REFERENCES repo_issues(issue_id) ON DELETE CASCADE,
    commit_hash     TEXT NOT NULL REFERENCES repo_commits(commit_hash) ON DELETE CASCADE,
    link_type       TEXT NOT NULL
                    CHECK (link_type IN ('introduced','fixed','related','mentioned')),
    linked_at       TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (issue_id, commit_hash)
);

CREATE TABLE repo_issue_comments (
    comment_id      BIGSERIAL PRIMARY KEY,
    issue_id        BIGINT NOT NULL REFERENCES repo_issues(issue_id) ON DELETE CASCADE,
    user_id         BIGINT REFERENCES repo_users(user_id),
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    edited_at       TIMESTAMPTZ
);

-- Indexes
CREATE INDEX idx_issues_repo        ON repo_issues(repo_id, status);
CREATE INDEX idx_issues_assigned    ON repo_issues(assigned_to);
CREATE INDEX idx_issues_reporter    ON repo_issues(reported_by);
CREATE INDEX idx_issue_files_path   ON repo_issue_files(path);
CREATE INDEX idx_issue_commits      ON repo_issue_commits(commit_hash);
CREATE INDEX idx_issue_comments     ON repo_issue_comments(issue_id);