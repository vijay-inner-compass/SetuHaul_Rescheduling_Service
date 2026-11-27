# Fresh production database (no sample data)

Status: **awaiting approval**. Nothing described here has been built or run on the server.

## Intent

Production should hold only real records. Today `deploy/start.py` creates a new server database as a copy of the sample data.

**Assumption, not yet confirmed:** the server holds no real drivers, shipments or other records worth keeping. If it does, stop: this design discards them.

## Goals and limits

- A new server database has the full schema and no sample rows.
- Real facilities, docks, carriers, vehicle types and vehicles can be loaded, because I found no production screen for most of them.
- The tracked `data/setuhaul.sqlite3` does not change. Tests, the demo and local scripts keep using it.
- Not included: screens for editing this data, or carrying users, settings or audit history across.
- Anti-goal: anything that deletes or overwrites an existing database on its own.

## Approaches

| Approach | Trade-off |
|---|---|
| **Fresh database (chosen)** | Easy to verify. Users, saved settings and the audit log on the server start over. |
| Remove sample rows in place | Keeps those, but append-only history tables can block deletion and real records may depend on sample ones. |

## Design (the details below are my proposals)

1. **Empty the sample while building the image.** A small script in the Dockerfile's first stage copies the sample, deletes every row and vacuums the file. Only that empty file goes into the final image, at the same path. `deploy/start.py` stays as it is and migrates it on first start. The sample file remains the single source of the base schema.
   - Not chosen: emptying at start-up (sample data would still ship in the image), or a separate schema file (a second copy of the schema to keep in step).
2. **Load real data with `scripts/load_reference_data.py`.** Input is a folder of CSV files, one per table, with headers matching the column names:
   - Required: `facilities`, `docks`, `carriers`, `vehicle_types`, `vehicles`.
   - Optional: `facility_rules`, `facility_contacts`.
   - All files load in one transaction. A bad row, unknown column, duplicate ID or missing parent loads nothing and says why.
   - It only inserts. It refuses to run on the sample database.
   - Blank templates are provided. Real data files are not committed to git.
3. **Entered in the app afterwards:** users, drivers (with approval), slot availability and shipments.

Facility latitude and longitude should be filled in: the GPS-assisted ETA depends on them.

## Switching the server over (run by you, once)

1. Check what is there: count users, drivers and shipments. If any are real, stop.
2. Take the usual backup and a Hostinger snapshot.
3. Deploy the new image. The old database stays in place and is untouched.
4. Stop the app. **Rename** (do not delete) `setuhaul.db`, any `-wal` and `-shm` files with it, and `escalations-fallback.jsonl` if present.
5. Recreate Redis, which clears old sessions and counters, and start the app. It creates the empty database.
6. Copy the CSV folder into the container and run the load script.
7. Sign in as the bootstrap admin, re-enter saved settings, re-register the Telegram webhook, then add users and drivers.

To go back: stop the app, restore the renamed files and start it.

## Checks

- Tests: the emptied database has no rows after migration (apart from migration records) and no foreign-key errors; the sample file is byte-for-byte unchanged.
- Tests: a valid CSV set loads; each kind of bad input loads nothing.
- Test: on an empty database plus loaded data, an admin can sign in, add a driver, open availability and create a shipment.
- Docs: update `docs/deployment-hostinger.md`, including the note that the supplied data is the dated sample.

A trial run on a scratch copy (emptied, then all 17 migrations) finished with no foreign-key errors. The app itself has not been started on an empty database.

## Left to the builder

Script names beyond those above, error wording, and test layout.
