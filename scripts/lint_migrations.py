"""
P-02: Migration linter — CI guard
Fails with exit code 1 if any CREATE TABLE in a migration file
is not accompanied by ALTER TABLE ... ENABLE ROW LEVEL SECURITY.

Usage:
    python scripts/lint_migrations.py db/migrations/*.sql
    # Or in CI: python scripts/lint_migrations.py $(git diff --name-only HEAD~1 -- '*.sql')
"""
import re
import sys


def check_migration(path: str) -> bool:
    content = open(path).read()
    tables = re.findall(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)", content, re.IGNORECASE)
    ok = True
    for table in tables:
        rls = f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"
        if rls.lower() not in content.lower():
            print(f"FAIL [{path}]: table '{table}' is missing ENABLE ROW LEVEL SECURITY")
            ok = False
    if ok and tables:
        print(f"PASS [{path}]: {', '.join(tables)}")
    elif ok:
        print(f"PASS [{path}]: no CREATE TABLE statements")
    return ok


if __name__ == "__main__":
    paths = sys.argv[1:]
    if not paths:
        print("Usage: lint_migrations.py <migration.sql> [...]")
        sys.exit(1)
    results = [check_migration(p) for p in paths]
    sys.exit(0 if all(results) else 1)
