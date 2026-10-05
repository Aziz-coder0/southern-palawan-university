"""Build a source ZIP without credentials, local data, caches, or previews."""

from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist" / "ISMS-Professional-SaaS.zip"
ROOT_FILES = (
    ".env.example", ".gitignore", ".python-version", "README.md", "app.py",
    "models.py", "requirements.txt", "requirements-dev.txt", "vercel.json",
)
SOURCE_DIRS = ("docs", "public/static", "scripts", "templates", "tests")
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".venv", ".git", "instance", "dist"}
EXCLUDED_SUFFIXES = {".pyc", ".db", ".sqlite", ".sqlite3", ".zip", ".tgz"}
REQUIRED_ARCHIVE_FILES = {
    "app.py", "models.py", "README.md", "vercel.json", ".env.example",
    "scripts/migrate_sqlite_to_postgres.py", "scripts/package_release.py",
    "public/static/vendor/fontawesome/LICENSE.txt",
    "public/static/vendor/fontawesome/webfonts/fa-solid-900.woff2",
    "templates/dashboard.html", "templates/notifications.html", "templates/login.html",
    "templates/apply.html", "templates/_application_fields.html", "templates/_academic_fields.html",
    "templates/academic_records.html", "docs/admissions_reference.md",
    "public/static/images/campus-administration.png",
}


def source_files():
    paths = [ROOT / name for name in ROOT_FILES]
    for dirname in SOURCE_DIRS:
        paths.extend(path for path in (ROOT / dirname).rglob("*") if path.is_file())
    files = []
    for path in paths:
        relative = path.relative_to(ROOT)
        if path.is_symlink():
            raise RuntimeError(f"Release source must not be a symlink: {relative}")
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES or path.name == ".env":
            continue
        if not path.is_file():
            raise FileNotFoundError(path)
        files.append((path, relative.as_posix()))
    return sorted(files, key=lambda item: item[1])


def main():
    files = source_files()
    names = {name for _, name in files}
    missing = REQUIRED_ARCHIVE_FILES - names
    if missing:
        raise RuntimeError(f"Required release files missing: {', '.join(sorted(missing))}")
    OUTPUT.parent.mkdir(exist_ok=True)
    with ZipFile(OUTPUT, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for path, name in files:
            archive.write(path, name)
    with ZipFile(OUTPUT) as archive:
        corrupt = archive.testzip()
        if corrupt:
            raise RuntimeError(f"Corrupt archive member: {corrupt}")
        if set(archive.namelist()) != names:
            raise RuntimeError("Archive manifest does not match source files")
    digest = sha256(OUTPUT.read_bytes()).hexdigest()
    print(f"Created: {OUTPUT}")
    print(f"Files: {len(files)} | Bytes: {OUTPUT.stat().st_size:,} | SHA-256: {digest}")


if __name__ == "__main__":
    main()
