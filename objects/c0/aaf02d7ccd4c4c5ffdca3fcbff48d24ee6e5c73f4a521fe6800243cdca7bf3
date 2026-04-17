-- sql/002_tables.sql
-- OlympusRepo schema — 17 tables
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT
--
-- Table creation order matters because of foreign keys:
--   1. repo_users                (no deps)
--   2. repo_repositories         (deps: repo_users)
--   3. everything else           (deps on users + repositories)

-- =========================================================================
-- IDENTITY & ACCESS
-- =========================================================================

CREATE TABLE repo_users (
    user_id         BIGSERIAL PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    full_name       TEXT,
    email           TEXT UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'mortal'
                    CHECK (role IN ('zeus','olympian','titan','mortal','prometheus','hermes')),
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_login      TIMESTAMPTZ
);

CREATE TABLE repo_sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES repo_users(user_id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    ip_address      INET,
    user_agent      TEXT
);

-- Reserved for AILang binary auth layer. Not used by the Python-only build.
-- Python layer authenticates via repo_sessions. You may drop this table if
-- you don't plan to ship the AILang binary.
CREATE TABLE repo_exec_tokens (
    token_id    BIGSERIAL PRIMARY KEY,
    token       TEXT UNIQUE NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used        BOOLEAN DEFAULT FALSE,
    used_by     TEXT,
    used_at     TIMESTAMPTZ
);

CREATE TABLE repo_server_config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_by      BIGINT REFERENCES repo_users(user_id)
);

-- =========================================================================
-- REPOSITORIES
-- =========================================================================

CREATE TABLE repo_repositories (
    repo_id         BIGSERIAL PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    description     TEXT,
    visibility      TEXT NOT NULL DEFAULT 'public'
                    CHECK (visibility IN ('public', 'private', 'internal')),
    owner_id        BIGINT NOT NULL REFERENCES repo_users(user_id),
    default_branch  TEXT DEFAULT 'main',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- =========================================================================
-- PERMISSIONS & ACCESS (depend on repositories)
-- =========================================================================

CREATE TABLE repo_permissions (
    perm_id         BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    user_id         BIGINT REFERENCES repo_users(user_id) ON DELETE CASCADE,
    role            TEXT,
    action          TEXT NOT NULL
                    CHECK (action IN (
                        'promote', 'branch_create', 'branch_delete',
                        'force_push', 'admin'
                    )),
    scope           TEXT DEFAULT '*',
    granted_by      BIGINT REFERENCES repo_users(user_id),
    granted_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT perm_target CHECK (user_id IS NOT NULL OR role IS NOT NULL)
);

CREATE TABLE repo_access (
    access_id       BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    user_id         BIGINT NOT NULL REFERENCES repo_users(user_id) ON DELETE CASCADE,
    access_level    TEXT NOT NULL DEFAULT 'read'
                    CHECK (access_level IN ('read', 'write', 'admin')),
    granted_by      BIGINT REFERENCES repo_users(user_id),
    granted_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(repo_id, user_id)
);

-- =========================================================================
-- CONTENT STORAGE
-- =========================================================================

CREATE TABLE repo_packs (
    pack_id         BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    pack_hash       TEXT UNIQUE,
    pack_path       TEXT NOT NULL,
    num_objects     BIGINT DEFAULT 0,
    size_bytes      BIGINT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE repo_objects (
    object_hash     TEXT PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    pack_id         BIGINT REFERENCES repo_packs(pack_id),
    byte_offset     BIGINT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    obj_type        TEXT NOT NULL
                    CHECK (obj_type IN ('blob', 'tree', 'commit'))
);

CREATE TABLE repo_commits (
    commit_hash     TEXT PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    tree_hash       TEXT NOT NULL,
    author_id       BIGINT REFERENCES repo_users(user_id),
    author_name     TEXT NOT NULL,
    committer_id    BIGINT REFERENCES repo_users(user_id),
    committer_name  TEXT NOT NULL,
    message         TEXT,
    authored_at     TIMESTAMPTZ,
    committed_at    TIMESTAMPTZ DEFAULT NOW(),
    rev             BIGSERIAL UNIQUE NOT NULL,
    parent_hashes   TEXT[],
    pack_id         BIGINT REFERENCES repo_packs(pack_id)
);

CREATE TABLE repo_changesets (
    commit_hash     TEXT NOT NULL REFERENCES repo_commits(commit_hash) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    change_type     TEXT NOT NULL
                    CHECK (change_type IN ('add', 'modify', 'delete', 'rename')),
    blob_before     TEXT,
    blob_after      TEXT,
    old_path        TEXT,
    lines_added     INTEGER DEFAULT 0,
    lines_removed   INTEGER DEFAULT 0,
    PRIMARY KEY (commit_hash, path)
);

CREATE TABLE repo_refs (
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    ref_name        TEXT NOT NULL,
    commit_hash     TEXT REFERENCES repo_commits(commit_hash),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_by      BIGINT REFERENCES repo_users(user_id),
    PRIMARY KEY (repo_id, ref_name)
);

-- =========================================================================
-- COLLABORATION
-- =========================================================================

CREATE TABLE repo_staging (
    staging_id      BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    user_id         BIGINT NOT NULL REFERENCES repo_users(user_id) ON DELETE CASCADE,
    branch_name     TEXT NOT NULL,
    status          TEXT DEFAULT 'active'
                    CHECK (status IN ('active', 'promoted', 'abandoned', 'archived')),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(repo_id, user_id, branch_name)
);

CREATE TABLE repo_staging_changes (
    change_id       BIGSERIAL PRIMARY KEY,
    staging_id      BIGINT NOT NULL REFERENCES repo_staging(staging_id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    change_type     TEXT NOT NULL
                    CHECK (change_type IN ('add', 'modify', 'delete', 'rename')),
    blob_hash       TEXT,
    old_path        TEXT,
    lines_added     INTEGER DEFAULT 0,
    lines_removed   INTEGER DEFAULT 0,
    staged_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE repo_promotions (
    promotion_id    BIGSERIAL PRIMARY KEY,
    staging_id      BIGINT NOT NULL REFERENCES repo_staging(staging_id),
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id),
    promoted_by     BIGINT NOT NULL REFERENCES repo_users(user_id),
    commit_hash     TEXT REFERENCES repo_commits(commit_hash),
    notes           TEXT,
    promoted_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE repo_messages (
    message_id      BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    channel         TEXT NOT NULL,
    username        TEXT NOT NULL,
    sender_id       BIGINT REFERENCES repo_users(user_id),
    content         TEXT NOT NULL,
    context_type    TEXT
                    CHECK (context_type IN ('file', 'commit', 'staging', 'branch', 'general', 'direct')),
    context_id      TEXT,
    is_private      BOOLEAN DEFAULT FALSE,
    recipient_id    BIGINT REFERENCES repo_users(user_id),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- =========================================================================
-- AUDITING
-- =========================================================================

CREATE TABLE repo_audit_log (
    log_id          BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT REFERENCES repo_repositories(repo_id) ON DELETE SET NULL,
    user_id         BIGINT REFERENCES repo_users(user_id),
    action          TEXT NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    details         JSONB,
    ip_address      INET,
    performed_at    TIMESTAMPTZ DEFAULT NOW()
);
