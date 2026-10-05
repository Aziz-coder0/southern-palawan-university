"""Copy this ISMS SQLite database to an empty PostgreSQL/Neon database.

Preview: python scripts/migrate_sqlite_to_postgres.py --sqlite instance/isms.db
Copy:    python scripts/migrate_sqlite_to_postgres.py --sqlite instance/isms.db --execute
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, func, inspect, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app  # noqa: E402
from models import db  # noqa: E402


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, type=Path, help="Source ISMS SQLite file")
    parser.add_argument("--execute", action="store_true", help="Perform the copy after preview")
    args = parser.parse_args()
    source_path = args.sqlite.expanduser().resolve()
    if not source_path.is_file():
        parser.error(f"Source does not exist: {source_path}")
    target_url = os.environ.get("DATABASE_URL", "")
    if not target_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
        parser.error("Set DATABASE_URL to the target PostgreSQL/Neon connection string.")
    source = create_engine(f"sqlite:///{source_path.as_posix()}")
    app = create_app()
    with app.app_context():
        tables = db.metadata.sorted_tables
        source_inspector = inspect(source)
        with source.connect() as source_connection:
            counts = {}
            for table in tables:
                if not source_inspector.has_table(table.name):
                    parser.error(f"Source is missing the {table.name} table.")
                source_columns = {column["name"] for column in source_inspector.get_columns(table.name)}
                missing_columns = set(table.c.keys()) - source_columns
                if missing_columns:
                    parser.error(f"Source {table.name} is missing columns: {', '.join(sorted(missing_columns))}")
                try:
                    counts[table.name] = source_connection.scalar(select(func.count()).select_from(table))
                except Exception as exc:
                    parser.error(f"Source is not a compatible ISMS database ({table.name}): {exc}")
            print(f"Source: {source_path}")
            for name, count in counts.items():
                print(f"  {name}: {count}")
            if not args.execute:
                print("Preview only. Add --execute to copy into PostgreSQL.")
                return
            db.create_all()
            target = db.engine
            with target.begin() as target_connection:
                occupied = [table.name for table in tables if target_connection.scalar(select(func.count()).select_from(table))]
                if occupied:
                    parser.error("Target must be empty. Existing rows found in: " + ", ".join(occupied))
                for table in tables:
                    rows = [dict(row._mapping) for row in source_connection.execute(select(table))]
                    if rows:
                        target_connection.execute(table.insert(), rows)
                    print(f"Copied {table.name}: {len(rows)}")
                for table in tables:
                    if "id" in table.c:
                        # Names come only from our trusted SQLAlchemy model metadata.
                        target_connection.execute(text("SELECT setval(pg_get_serial_sequence(quote_ident(:table_name), 'id'), "
                                                       "COALESCE((SELECT MAX(id) FROM \"" + table.name + "\"), 1), "
                                                       "EXISTS(SELECT 1 FROM \"" + table.name + "\"))"),
                                                  {"table_name": table.name})
            with target.connect() as verification:
                for table in tables:
                    actual = verification.scalar(select(func.count()).select_from(table))
                    if actual != counts[table.name]:
                        raise RuntimeError(f"Count mismatch in {table.name}: {actual} != {counts[table.name]}")
            print("Migration complete. All table counts match.")


if __name__ == "__main__":
    main()
