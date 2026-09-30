"""
Guards for what a production deploy can do to existing data.

A deploy (see .github/workflows/deploy.yml) rebuilds the containers and runs
``migrate`` against the live database and media volume. These tests fail when a
change would make that unsafe:

* a new migration that drops or deletes data,
* a deploy step that calls a command that does not exist, or one that purges data.

They are static checks (no database is touched), so they run in the normal suite.
"""
import re
import tomllib

from django.conf import settings
from django.core.management import get_commands
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import DeleteModel, RemoveField, RunPython, RunSQL
from django.test import SimpleTestCase

# Production was on migration 0017 when this guard was added. The older destructive
# migrations (0003, 0008, 0014) ran long ago and are not re-run, so only newer ones are checked.
BASELINE = 17

# A migration that has to remove or delete data on purpose sets, on its Migration class,
#     DESTRUCTIVE_OK = "why this is safe / who signed it off"
# A data migration (RunPython) sets
#     DATA_MIGRATION_REVIEWED = "what it reads and writes"
# Either makes the reviewer look at it twice, which is the point.

DESTRUCTIVE_SQL = re.compile(r"\b(DROP|DELETE|TRUNCATE)\b", re.I)

# Things a deploy must never run against production.
DEPLOY_DENYLIST = [
    "migrate_taxonomy_to_db",       # purges and reloads the taxonomy
    "import_pathogen_interactions",  # can clear the interactions table (--clear)
    "--clear",
    "flush",
    "sqlflush",
    "reset_db",
    "dropdb",
    "docker compose down -v",
    "docker-compose down -v",
    "docker volume rm",
    "docker volume prune",
    "rm -rf /opt/barkandambrosia_data",
]


def new_beetles_migrations():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    for (app, name), migration in sorted(loader.disk_migrations.items()):
        if app != "beetles_app":
            continue
        number = int(name.split("_", 1)[0])
        if number > BASELINE:
            yield name, migration


class MigrationSafetyTests(SimpleTestCase):
    def test_there_are_migrations_to_check(self):
        # If the loader finds nothing, the checks below pass vacuously.
        self.assertTrue(list(new_beetles_migrations()))

    def test_new_migrations_do_not_destroy_data(self):
        problems = []
        for name, migration in new_beetles_migrations():
            if getattr(migration, "DESTRUCTIVE_OK", None):
                continue
            for op in migration.operations:
                if isinstance(op, (RemoveField, DeleteModel)):
                    problems.append(f"{name}: {op.__class__.__name__} {op.describe()}")
                elif isinstance(op, RunSQL):
                    statements = op.sql if isinstance(op.sql, (list, tuple)) else [op.sql]
                    if any(DESTRUCTIVE_SQL.search(str(s)) for s in statements):
                        problems.append(f"{name}: RunSQL with DROP/DELETE/TRUNCATE")
        self.assertEqual(
            problems, [],
            "These migrations remove data on the next deploy. If that is intended, set "
            "DESTRUCTIVE_OK = '<why it is safe>' on the migration's Migration class.",
        )

    def test_new_data_migrations_are_marked_as_reviewed(self):
        unreviewed = [
            name for name, migration in new_beetles_migrations()
            if any(isinstance(op, RunPython) for op in migration.operations)
            and not getattr(migration, "DATA_MIGRATION_REVIEWED", None)
        ]
        self.assertEqual(
            unreviewed, [],
            "RunPython migrations rewrite production rows. Set "
            "DATA_MIGRATION_REVIEWED = '<what it reads and writes>' on the Migration class once someone has checked.",
        )


class DeployScriptTests(SimpleTestCase):
    def setUp(self):
        self.deploy = (settings.BASE_DIR / ".github" / "workflows" / "deploy.yml").read_text()
        self.pixi_tasks = tomllib.loads((settings.BASE_DIR / "pixi.toml").read_text())["tasks"]

    def test_pixi_tasks_the_deploy_runs_exist(self):
        # Steps that run inside the production web container. (The Modal job runs on
        # GitHub's runner and calls the 'modal' program, not a pixi task.)
        wanted = set(re.findall(r"exec -T web pixi run ([\w-]+)", self.deploy))
        self.assertTrue(wanted, "found no production 'pixi run' steps in deploy.yml")
        self.assertEqual(sorted(t for t in wanted if t not in self.pixi_tasks), [])

    def test_management_commands_behind_pixi_tasks_exist(self):
        available = set(get_commands())
        missing = []
        for task, command in self.pixi_tasks.items():
            for name in re.findall(r"manage\.py (\w+)", str(command)):
                if name not in available:
                    missing.append(f"pixi task '{task}' runs 'manage.py {name}'")
        self.assertEqual(missing, [])

    def test_deploy_never_runs_data_destroying_commands(self):
        # Only look at the commands themselves, not comments that mention them.
        lines = [ln for ln in self.deploy.splitlines() if not ln.strip().startswith("#")]
        found = [item for item in DEPLOY_DENYLIST if any(item in ln for ln in lines)]
        self.assertEqual(found, [], "deploy.yml must not run these against production")

    def test_deploy_keeps_the_data_volumes(self):
        prod = (settings.BASE_DIR / "docker-compose.prod.yml").read_text()
        # Database and uploaded images live on the host, outside the containers.
        self.assertIn("/opt/barkandambrosia_data/postgres:/var/lib/postgresql/data", prod)
        self.assertIn("/opt/barkandambrosia_data/media:/app/media", prod)
