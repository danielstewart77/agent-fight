from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from tests.test_migration_initial import REPO_ROOT


def test_queue_migration_preserves_production_review_history(tmp_path):
    cfg = Config(str(REPO_ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(REPO_ROOT / 'migrations'))
    url = f"sqlite:///{tmp_path / 'production.db'}"
    cfg.set_main_option('sqlalchemy.url', url)
    command.upgrade(cfg, '0011_review_created_by')
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO settings (id, ado_organization) VALUES (1, 'preserve')")
            connection.exec_driver_sql("INSERT INTO reviews (pr_id, repo_name, status) VALUES (42, 'widgets', 'converged')")
        command.upgrade(cfg, 'head')
        with engine.connect() as connection:
            assert connection.exec_driver_sql('SELECT ado_organization FROM settings').scalar() == 'preserve'
            assert connection.exec_driver_sql('SELECT count(*) FROM reviews').scalar() == 1
        assert 'review_requests' in inspect(engine).get_table_names()
        command.downgrade(cfg, '0011_review_created_by')
        assert 'review_requests' not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.exec_driver_sql('SELECT count(*) FROM reviews').scalar() == 1
    finally:
        engine.dispose()
