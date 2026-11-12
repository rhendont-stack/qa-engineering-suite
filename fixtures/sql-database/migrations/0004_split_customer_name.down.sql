ALTER TABLE customers ADD COLUMN name TEXT NOT NULL DEFAULT '';
ALTER TABLE customers DROP COLUMN given_name;
ALTER TABLE customers DROP COLUMN family_name;
