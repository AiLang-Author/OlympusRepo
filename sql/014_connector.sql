-- sql/014_connector.sql
-- OlympusRepo Connector: remote instances, sync state, offers
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT
--
-- The connector implements a master/slave sync model:
--   Canonical (master) owns the truth. Global rev is authoritative.
--   Local (slave) pulls from canonical. Pushes are OFFERS, not writes.
--   Nothing enters canonical without Zeus/Olympian promotion.

-- Remote instance registry
CREATE TABLE repo_remotes (
    remote_id       BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,  -- e.g. 'origin', 'upstream'
    url             TEXT NOT NULL,         -- e.g. 'http://canonical:8000'
    role            TEXT NOT NULL DEFAULT 'canonical'
                    CHECK (role IN ('canonical', 'mirror', 'fork')),
    api_token       TEXT,                  -- auth token for this remote
    last_sync_at    TIMESTAMPTZ,
    last_sync_rev   BIGINT DEFAULT 0,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Sync log: every pull/offer operation recorded
CREATE TABLE repo_sync_log (
    sync_id         BIGSERIAL PRIMARY KEY,
    remote_id       BIGINT NOT NULL REFERENCES repo_remotes(remote_id) ON DELETE CASCADE,
    repo_id         BIGINT REFERENCES repo_repositories(repo_id) ON DELETE SET NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('pull', 'offer', 'promote')),
    status          TEXT NOT NULL CHECK (status IN ('pending','running','success','failed')),
    from_rev        BIGINT,
    to_rev          BIGINT,
    commits_synced  INTEGER DEFAULT 0,
    blobs_synced    INTEGER DEFAULT 0,
    error_message   TEXT,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ
);

-- Offers: staging realms pushed from a slave to canonical for review
CREATE TABLE repo_offers (
    offer_id        BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    remote_id       BIGINT REFERENCES repo_remotes(remote_id) ON DELETE SET NULL,
    branch_name     TEXT NOT NULL,
    from_rev        BIGINT NOT NULL,   -- slave's current rev
    base_rev        BIGINT NOT NULL,   -- canonical rev this was based on
    offered_by      TEXT NOT NULL,     -- username on the slave instance
    message         TEXT,              -- why this should be accepted
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','reviewing','promoted','rejected')),
    reviewed_by     BIGINT REFERENCES repo_users(user_id),
    review_notes    TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Offer changesets: what files changed in this offer
CREATE TABLE repo_offer_changes (
    change_id       BIGSERIAL PRIMARY KEY,
    offer_id        BIGINT NOT NULL REFERENCES repo_offers(offer_id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    change_type     TEXT NOT NULL CHECK (change_type IN ('add','modify','delete')),
    blob_hash       TEXT,
    lines_added     INTEGER DEFAULT 0,
    lines_removed   INTEGER DEFAULT 0
);

-- Indexes
CREATE INDEX idx_remotes_name      ON repo_remotes(name);
CREATE INDEX idx_sync_log_remote   ON repo_sync_log(remote_id);
CREATE INDEX idx_sync_log_repo     ON repo_sync_log(repo_id);
CREATE INDEX idx_offers_repo       ON repo_offers(repo_id, status);
CREATE INDEX idx_offer_changes     ON repo_offer_changes(offer_id);
