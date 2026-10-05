"""Import cleaned legacy CSV records into SQLite or PostgreSQL via DATABASE_URL.

Supported kinds and required CSV headers:
  students: student_number,first_name,last_name,email,program,year_level
  employees: employee_number,first_name,last_name,email,department,position
  subjects: code,title,units
  applicants: first_name,last_name,email,phone,program

Use --execute to commit. Otherwise this script validates and previews only.
"""
import argparse
import csv
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app  # noqa: E402
from models import Applicant, ApplicationRequirement, Employee, Student, Subject, db  # noqa: E402


HEADERS = {
    "students": {"student_number", "first_name", "last_name", "email", "program", "year_level"},
    "employees": {"employee_number", "first_name", "last_name", "email", "department", "position"},
    "subjects": {"code", "title", "units"},
    "applicants": {"first_name", "last_name", "email", "phone", "program"},
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=HEADERS)
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.csv_file.is_file():
        parser.error("CSV file does not exist.")
    with args.csv_file.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = HEADERS[args.kind] - set(reader.fieldnames or [])
        if missing:
            parser.error("Missing columns: " + ", ".join(sorted(missing)))
        rows = list(reader)
    errors = []
    keys = set()
    for number, row in enumerate(rows, 2):
        for field in HEADERS[args.kind]:
            if not (row.get(field) or "").strip():
                errors.append(f"row {number}: missing {field}")
        if args.kind == "subjects":
            try:
                if int(row["units"]) < 1:
                    raise ValueError()
            except ValueError:
                errors.append(f"row {number}: units must be a positive integer")
        key_field = {"students": "student_number", "employees": "employee_number", "subjects": "code", "applicants": "email"}[args.kind]
        key = (row.get(key_field) or "").strip().lower()
        if key in keys:
            errors.append(f"row {number}: duplicate {key_field} in CSV")
        keys.add(key)
    if errors:
        print("Validation failed:")
        print("\n".join(errors))
        raise SystemExit(1)
    print(f"Validated {len(rows)} {args.kind} rows.")
    if not args.execute:
        print("Preview only. Add --execute to import.")
        return
    app = create_app()
    with app.app_context():
        db.create_all()
        model, key_field = {
            "students": (Student, "student_number"), "employees": (Employee, "employee_number"),
            "subjects": (Subject, "code"), "applicants": (Applicant, "reference")}[args.kind]
        inserted = skipped = 0
        for row in rows:
            clean = {key: (value or "").strip() for key, value in row.items() if key}
            if "email" in clean:
                clean["email"] = clean["email"].lower()
            if args.kind == "subjects":
                clean["code"] = clean["code"].upper()
            if args.kind != "applicants":
                existing = db.session.scalar(db.select(model).filter(getattr(model, key_field) == clean[key_field]))
                if existing:
                    skipped += 1
                    continue
            if args.kind == "students":
                item = Student(**{key: clean[key] for key in HEADERS[args.kind]}, phone=clean.get("phone", ""))
            elif args.kind == "employees":
                item = Employee(**{key: clean[key] for key in HEADERS[args.kind]}, phone=clean.get("phone", ""))
            elif args.kind == "subjects":
                item = Subject(code=clean["code"].upper(), title=clean["title"], units=int(clean["units"]), description=clean.get("description", ""))
            else:
                item = Applicant(reference="APP-" + secrets.token_hex(6).upper(),
                                 **{key: clean[key] for key in HEADERS[args.kind]}, notes=clean.get("notes", ""))
                item.requirements = [ApplicationRequirement(name=name) for name in ("Application form", "Transcript of records", "Valid ID")]
            db.session.add(item)
            inserted += 1
        db.session.commit()
        print(f"Imported {inserted}; skipped {skipped} existing records.")


if __name__ == "__main__":
    main()
