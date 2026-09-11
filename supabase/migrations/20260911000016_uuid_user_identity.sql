-- Ensure users.uid is a UUID with default gen_random_uuid()
-- and foreign keys have ON UPDATE CASCADE so UUID updates cascade cleanly.

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN (
        SELECT c.conname, c.conrelid::regclass as tbl, a.attname as col
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attnum = c.conkey[1] AND a.attrelid = c.conrelid
        WHERE c.confrelid = 'public.users'::regclass
    ) LOOP
        EXECUTE 'ALTER TABLE ' || r.tbl || ' DROP CONSTRAINT IF EXISTS ' || quote_ident(r.conname);
        EXECUTE 'ALTER TABLE ' || r.tbl || ' ADD CONSTRAINT ' || quote_ident(r.conname) ||
                ' FOREIGN KEY (' || quote_ident(r.col) || ') REFERENCES users(uid) ON DELETE CASCADE ON UPDATE CASCADE';
    END LOOP;
END $$;

-- Default new users to a random UUID
ALTER TABLE users ALTER COLUMN uid SET DEFAULT gen_random_uuid()::text;
