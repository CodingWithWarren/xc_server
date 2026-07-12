"""Change an athlete's role (promote to coach, or demote back to athlete).

Coaches can read every athlete's data and see the roster; promotion is a
manual admin operation by design (see CLAUDE.md "Auth"). The role is stamped
into the JWT at sign-in, so the change takes effect on the user's NEXT
sign-in — an already-signed-in session keeps its old role until then.

    venv/bin/python scripts/set_role.py --email you@gmail.com          # -> coach
    venv/bin/python scripts/set_role.py --id 4 --role athlete          # demote

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
    p = argparse.ArgumentParser(description=__doc__)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", type=int, help="athlete id")
    target.add_argument("--email", help="athlete email")
    p.add_argument("--role", choices=("coach", "athlete"), default="coach",
                   help="role to set (default: coach)")
    args = p.parse_args()

    db = SessionLocal()
    try:
        if args.email:
            athlete = db.scalar(select(Athlete).where(Athlete.email == args.email))
            if athlete is None:
                sys.exit(f"No athlete with email {args.email!r}.")
        else:
            athlete = db.get(Athlete, args.id)
            if athlete is None:
                sys.exit(f"No athlete with id {args.id}.")

        if athlete.role == args.role:
            print(f"Athlete {athlete.id} ({athlete.name}, {athlete.email}) "
                  f"is already {args.role!r} — nothing to do.")
            return

        old = athlete.role
        athlete.role = args.role
        db.commit()
        print(f"Athlete {athlete.id} ({athlete.name}, {athlete.email}): "
              f"{old} -> {args.role}")
        print("Takes effect on their next sign-in (the role rides in the JWT).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
