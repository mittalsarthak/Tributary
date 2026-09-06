CREATE SCHEMA IF NOT EXISTS _tributary;

CREATE TABLE IF NOT EXISTS _tributary.branches (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name         text UNIQUE NOT NULL,
  head_commit  uuid,
  base_commit  uuid,
  schema_name  text UNIQUE NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS _tributary.commits (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  branch_id  uuid NOT NULL REFERENCES _tributary.branches(id) ON DELETE CASCADE,
  parent_id  uuid REFERENCES _tributary.commits(id),
  -- Second parent, set only on a merge commit. Without it the DAG cannot
  -- represent a merge at all: a merge would create a commit on the target
  -- whose sole parent is the target's previous head, so the merged branch's
  -- commits never enter the target's ancestry -- the branch looks unmerged
  -- forever, and merge_base rewinds to the original fork point on a re-merge.
  merge_parent_id uuid REFERENCES _tributary.commits(id),
  message    text NOT NULL,
  author     text NOT NULL DEFAULT 'you',
  snapshot   jsonb NOT NULL,
  ops        jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_commits_branch ON _tributary.commits (branch_id, created_at DESC);

CREATE TABLE IF NOT EXISTS _tributary.merges (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_branch text NOT NULL,
  target_branch text NOT NULL,
  base_commit   uuid,
  source_head   uuid,
  target_head   uuid,
  status        text NOT NULL DEFAULT 'pending',
  conflicts     jsonb NOT NULL DEFAULT '[]',
  resolutions   jsonb NOT NULL DEFAULT '{}',
  plan          jsonb NOT NULL DEFAULT '[]',
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);

CREATE TABLE IF NOT EXISTS _tributary.migration_steps (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  merge_id    uuid NOT NULL REFERENCES _tributary.merges(id) ON DELETE CASCADE,
  seq         int NOT NULL,
  sql         text NOT NULL,
  kind        text NOT NULL,
  safety      text NOT NULL,
  note        text NOT NULL DEFAULT '',
  status      text NOT NULL DEFAULT 'pending',
  rows_done   bigint NOT NULL DEFAULT 0,
  rows_total  bigint,
  cursor_val  text,
  error       text,
  started_at  timestamptz,
  finished_at timestamptz,
  UNIQUE (merge_id, seq)
);

-- Existing workspaces predate merge_parent_id; CREATE TABLE IF NOT EXISTS
-- above will not add it to a table that already exists.
ALTER TABLE _tributary.commits
  ADD COLUMN IF NOT EXISTS merge_parent_id uuid REFERENCES _tributary.commits(id);
