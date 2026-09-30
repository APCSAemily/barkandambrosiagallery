# Ecological interactions: where the data lives

The Ecological Interactions page is built from the **database** (table `pathogen_interactions`). Its records, the
beetle summary, the reference list, the headline numbers and the category filter are all computed from that table each
time the page loads (`beetles_app/interaction_data.py`). Nothing on the page is read from a fixed file any more.

## How the published dataset (v1.0) gets in

`manage.py import_pathogen_interactions` reads `static/data/bark_beetle_pathogens_master.json` (the published v1.0 file, kept
in git as the baseline) and adds every record that is missing. **The deploy runs it after the migrations**
(`pixi run import-interactions`), so a fresh server fills itself and nothing has to be done by hand.

It never changes a row that is already in the database, so **corrections made through the upload page survive every deploy**.
Its options are for a person at a terminal, never for a deploy (a test enforces this):

* `--refresh` overwrites existing rows with the file's values (undoes corrections made since).
* `--clear` deletes the published-dataset rows and reloads them (proposals and uploads are kept).

## Changing the data in production

Sign in as staff or a superuser → **Ecological Interactions → Upload or Update Interactions**.

1. **Download current interactions (CSV)**. It has every row, the published v1.0 rows included, each with its `record_id`.
2. Edit cells, delete the columns you are not changing, or add rows with `record_id` blank (or `NEW`).
3. Upload it. Tick "Check the file only" first to see problems without saving. If any row is wrong nothing is saved and each
   problem is listed with its row number.

Any row can be corrected, including v1.0 rows (they stay marked as the published dataset). An accepted proposal from the
review page becomes a row too. The page shows every change straight away.

## Backups

The whole database is dumped by the **Dropbox Backup** workflow (`.github/workflows/backup.yaml`): `pg_dump` of the entire
`beetles_db` into `/opt/barkandambrosia_data/media/db_full_backup.sql`, which is then copied to Dropbox with the rest of
the data folder. The interactions are an ordinary table in that database, so they are in every dump, with no extra setup
(a test checks that the dump is never narrowed to certain tables and that the `.sql` file is not excluded).

The schedule is weekly (fast) and monthly (full), so edits made since the last run are not in Dropbox yet. After a large
upload, run **Actions → Dropbox Backup → Run workflow → fast** to back it up straight away. The published v1.0 file in git
is a second copy of the original dataset.

To restore, load the `.sql` dump into an empty Postgres (`psql -U beetles_user beetles_db < db_full_backup.sql`).

## Not changed

The download buttons on the page (CSV and Excel of the published dataset) are still switched off, as they were ("temporarily
commented out for public release during final dataset review/QC"). The "About" text that describes v1.0 (1,015 records,
281 publications, the per-group results) is still the published description of v1.0; the numbers in the tiles, tabs and filters
are live.
