CREATE TABLE IF NOT EXISTS friends (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    friend_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, friend_id),
    CHECK (user_id <> friend_id)
);

CREATE INDEX IF NOT EXISTS friends_friend_id_idx ON friends (friend_id);

CREATE TABLE IF NOT EXISTS posts (
    id UUID PRIMARY KEY,
    author_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS posts_author_updated_idx
    ON posts (author_user_id, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS author_post_versions (
    author_user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    version UUID NOT NULL
);

-- Store each version in the same transaction as the post change.
CREATE OR REPLACE FUNCTION bump_author_post_versions() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO author_post_versions (author_user_id, version)
    SELECT id, gen_random_uuid()
    FROM users
    WHERE id IN (
        CASE WHEN TG_OP <> 'INSERT' THEN OLD.author_user_id END,
        CASE WHEN TG_OP <> 'DELETE' THEN NEW.author_user_id END
    )
    ORDER BY id
    ON CONFLICT (author_user_id) DO UPDATE SET version = EXCLUDED.version;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE TRIGGER posts_bump_author_versions
AFTER INSERT OR UPDATE OR DELETE ON posts
FOR EACH ROW EXECUTE FUNCTION bump_author_post_versions();
