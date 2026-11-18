"""Exercise both default store owners without reading or touching operational DBs."""

from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.write import backup_database, initialize_database
from baibai_engine.storage.sqlite.machine_analysis import initialize_run_store, run_store_path


def test_default_db_writers_stay_in_a_lazy_test_namespace(tmp_path_factory) -> None:
    app, runs = database_path(), run_store_path()
    assert app.parent == runs.parent
    assert app.parent.is_relative_to(tmp_path_factory.getbasetemp())
    assert not app.parent.exists()
    initialize_database()
    assert app.is_file()
    assert not runs.exists()
    initialize_run_store()
    assert runs.is_file()
    backup = backup_database()
    assert backup is not None
    assert backup.parent == app.parent / "backups"
