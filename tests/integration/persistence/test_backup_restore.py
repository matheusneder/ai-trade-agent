from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from scripts.backup import backup, docker_pg, restore_drill
from testcontainers.community.postgres import PostgresContainer


def test_encrypted_postgres_restore_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TA_BACKUP_KEY", Fernet.generate_key().decode())
    with PostgresContainer("postgres:18", driver="asyncpg") as pg:
        container = pg.get_wrapped_container().id
        assert container is not None
        docker_pg(
            container,
            [
                "psql",
                "-U",
                pg.username,
                "-d",
                pg.dbname,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "CREATE TABLE audit(id integer PRIMARY KEY, amount numeric); "
                "INSERT INTO audit VALUES (1, 12.3456789)",
            ],
        )
        docker_pg(container, ["createdb", "-U", pg.username, "audit_restore_test"])
        archive = tmp_path / "db.enc"
        backup(container, pg.dbname, pg.username, archive)
        restore_drill(container, "audit_restore_test", pg.username, archive)
        result = docker_pg(
            container,
            [
                "psql",
                "-U",
                pg.username,
                "-d",
                "audit_restore_test",
                "-At",
                "-c",
                "SELECT amount FROM audit WHERE id=1",
            ],
        )
        assert result.strip() == b"12.3456789"
        with pytest.raises(ValueError, match="empty"):
            restore_drill(container, "audit_restore_test", pg.username, archive)
