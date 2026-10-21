from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from scripts import backup as module


def test_encrypted_archive_and_restore_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TA_BACKUP_KEY", Fernet.generate_key().decode())
    calls: list[list[str]] = []

    def command(container: str, args: list[str], data: bytes | None = None) -> bytes:
        calls.append(args)
        if args[0] == "pg_dump":
            return b"PGDMP-private-financial-data"
        if args[0] == "psql":
            return b"0\n"
        assert data == b"PGDMP-private-financial-data"
        return b""

    monkeypatch.setattr(module, "docker_pg", command)
    output = tmp_path / "backup.enc"
    module.backup("pg", "trades", "agent", output)
    assert b"private-financial-data" not in output.read_bytes()
    with pytest.raises(FileExistsError):
        module.backup("pg", "trades", "agent", output)
    with pytest.raises(ValueError, match="restricted"):
        module.restore_drill("pg", "production", "agent", output)
    module.restore_drill("pg", "audit_restore_test", "agent", output)
    assert calls[-1][0] == "pg_restore"
    monkeypatch.setenv("TA_BACKUP_KEY", Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        module.restore_drill("pg", "audit_restore_test", "agent", output)
