"""Encrypted local PostgreSQL backup and isolated restore drill (Docker).

Set TA_BACKUP_KEY to a Fernet key from your secret manager. The key is never written
alongside the archive. Restore is restricted to an empty *_restore_test database.
No external destination, scheduler or credential is configured by this tool.
"""

import argparse
import os
import re
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet

MAGIC = b"TA_BACKUP_V1\n"
IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def cipher() -> Fernet:
    key = os.environ.get("TA_BACKUP_KEY")
    if not key:
        raise ValueError("TA_BACKUP_KEY must contain a Fernet key from your secret manager")
    return Fernet(key.encode("ascii"))


def docker_pg(container: str, command: list[str], data: bytes | None = None) -> bytes:
    if not container or container.startswith("-"):
        raise ValueError("invalid container name")
    result = subprocess.run(  # noqa: S603 - argument array, no shell or credentials in argv
        ["docker", "exec", "-i", container, *command],  # noqa: S607
        input=data,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        # PostgreSQL errors can include application data: do not print raw stderr.
        raise RuntimeError(f"PostgreSQL backup command failed (exit {result.returncode})")
    return result.stdout


def validate(database: str, user: str) -> None:
    if not IDENTIFIER.fullmatch(database) or not IDENTIFIER.fullmatch(user):
        raise ValueError("database and user must be simple PostgreSQL identifiers")


def backup(container: str, database: str, user: str, output: Path) -> None:
    validate(database, user)
    encryption = cipher()  # validate key before reading any database
    if output.exists():
        raise FileExistsError(output)
    archive = docker_pg(container, ["pg_dump", "-U", user, "-d", database, "-Fc"])
    if not archive.startswith(b"PGDMP"):
        raise ValueError("pg_dump did not return a custom-format archive")
    with output.open("xb") as handle:
        handle.write(MAGIC + encryption.encrypt(archive))


def restore_drill(container: str, database: str, user: str, archive: Path) -> None:
    validate(database, user)
    if not database.endswith("_restore_test"):
        raise ValueError("restore is restricted to an empty *_restore_test database")
    payload = archive.read_bytes()
    if not payload.startswith(MAGIC):
        raise ValueError("unsupported backup format")
    decrypted = cipher().decrypt(payload[len(MAGIC) :])
    count = docker_pg(
        container,
        [
            "psql",
            "-U",
            user,
            "-d",
            database,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','v','m','S','f')",
        ],
    )
    if count.strip() != b"0":
        raise ValueError("restore target must be empty")
    docker_pg(
        container,
        [
            "pg_restore",
            "-U",
            user,
            "-d",
            database,
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "--single-transaction",
        ],
        decrypted,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["backup", "restore-drill"])
    parser.add_argument("--container", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    operation = backup if args.operation == "backup" else restore_drill
    operation(args.container, args.database, args.user, args.archive)


if __name__ == "__main__":
    main()
