-- A migration squawk flags: an index built without CONCURRENTLY and a NOT NULL column added without a default,
-- each also without the timeouts and IF NOT EXISTS its default rules ask for.
CREATE INDEX users_email_idx ON users (email);
ALTER TABLE users ADD COLUMN nickname text NOT NULL;
