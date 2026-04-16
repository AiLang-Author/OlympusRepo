-- sql/003_indexes.sql
-- Indexes for query performance
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT

-- Users
CREATE INDEX idx_users_role       ON repo_users(role);
CREATE INDEX idx_users_username   ON repo_users(username);

-- Sessions
CREATE INDEX idx_sessions_user    ON repo_sessions(user_id);
CREATE INDEX idx_sessions_expires ON repo_sessions(expires_at);

-- Exec tokens
CREATE INDEX idx_exec_tokens_token   ON repo_exec_tokens(token) WHERE used = FALSE;
CREATE INDEX idx_exec_tokens_expires ON repo_exec_tokens(expires_at);

-- Repositories
CREATE INDEX idx_repos_owner      ON repo_repositories(owner_id);
CREATE INDEX idx_repos_visibility ON repo_repositories(visibility);

-- Permissions & access
CREATE INDEX idx_permissions_repo ON repo_permissions(repo_id);
CREATE INDEX idx_permissions_user ON repo_permissions(user_id);
CREATE INDEX idx_permissions_role ON repo_permissions(role);
CREATE INDEX idx_access_repo      ON repo_access(repo_id);
CREATE INDEX idx_access_user      ON repo_access(user_id);

-- Content
CREATE INDEX idx_packs_repo       ON repo_packs(repo_id);
CREATE INDEX idx_objects_repo     ON repo_objects(repo_id);
CREATE INDEX idx_objects_pack     ON repo_objects(pack_id);
CREATE INDEX idx_objects_type     ON repo_objects(obj_type);

-- Commits
CREATE INDEX idx_commits_repo     ON repo_commits(repo_id);
CREATE INDEX idx_commits_rev      ON repo_commits(rev DESC);
CREATE INDEX idx_commits_author   ON repo_commits(author_id);
CREATE INDEX idx_commits_date     ON repo_commits(committed_at DESC);

-- Changesets
CREATE INDEX idx_changesets_path  ON repo_changesets(path);
CREATE INDEX idx_changesets_type  ON repo_changesets(change_type);

-- Refs
CREATE INDEX idx_refs_repo        ON repo_refs(repo_id);

-- Staging
CREATE INDEX idx_staging_repo     ON repo_staging(repo_id);
CREATE INDEX idx_staging_user     ON repo_staging(user_id);
CREATE INDEX idx_staging_status   ON repo_staging(status);
CREATE INDEX idx_staging_changes_staging ON repo_staging_changes(staging_id);
CREATE INDEX idx_staging_changes_path    ON repo_staging_changes(path);

-- Promotions
CREATE INDEX idx_promotions_repo     ON repo_promotions(repo_id);
CREATE INDEX idx_promotions_staging  ON repo_promotions(staging_id);
CREATE INDEX idx_promotions_promoter ON repo_promotions(promoted_by);

-- Messages (mana)
CREATE INDEX idx_messages_repo    ON repo_messages(repo_id);
CREATE INDEX idx_messages_channel ON repo_messages(channel, created_at DESC);
CREATE INDEX idx_messages_context ON repo_messages(context_type, context_id);
CREATE INDEX idx_messages_sender  ON repo_messages(sender_id);
CREATE INDEX idx_messages_private ON repo_messages(is_private, sender_id, recipient_id)
    WHERE is_private = TRUE;

-- Audit log
CREATE INDEX idx_audit_repo   ON repo_audit_log(repo_id);
CREATE INDEX idx_audit_user   ON repo_audit_log(user_id);
CREATE INDEX idx_audit_action ON repo_audit_log(action);
CREATE INDEX idx_audit_date   ON repo_audit_log(performed_at DESC);
