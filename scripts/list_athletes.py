"""List every athlete with their id, name, email, and role.

Handy before scripts/set_role.py or scripts/delete_athlete_data.py, which
select athletes by --id or --email.

    venv/bin/python scripts/list_athletes.py

Run from the project root.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from database import SessionLocal  # noqa: E402
from models import Athlete  # noqa: E402


def main() -> None:
    # No options (besides -h); the parser is here so --help prints the usage
    # notes above, matching the other scripts.
    argparse.ArgumentParser(description=__doc__).parse_args()

    db = SessionLocal()
    try:
        athletes = db.scalars(select(Athlete).order_by(Athlete.id)).all()
        if not athletes:
            print("No athletes yet.")
            return
        print(f"{'id':>4}  {'role':<8} {'name':<24} email")
        for a in athletes:
            print(f"{a.id:>4}  {a.role:<8} {a.name:<24} {a.email or '—'}")
        print(f"\n{len(athletes)} athlete(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
