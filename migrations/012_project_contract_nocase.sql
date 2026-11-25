-- Contract references are unique ignoring case ('Acme-1' and 'ACME-1' are one contract).

-- Guard: stop with a readable message if existing rows already collide. Nothing is changed or deleted; the
-- named constraint below is what the error reports. Rename the colliding contract_ref values and run again.
CREATE TEMP TABLE migration_012_guard (
    collisions INTEGER NOT NULL,
    CONSTRAINT projects_contract_ref_values_differ_only_by_case_rename_them_then_rerun CHECK (collisions = 0)
);
INSERT INTO migration_012_guard
    SELECT count(*) FROM (SELECT 1 FROM projects GROUP BY lower(contract_ref) HAVING count(*) > 1);
DROP TABLE migration_012_guard;

-- The database backstop for the application's lower-cased duplicate check.
CREATE UNIQUE INDEX IF NOT EXISTS ux_projects_contract_ref_nocase ON projects(lower(contract_ref));
