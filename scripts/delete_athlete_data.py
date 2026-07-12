"""Delete everything an athlete has uploaded (the athlete account stays).

Wipes the same tables as DELETE /me/data — raw sample streams, workouts,
detected sessions, route tracks, sync rows — but for any athlete, picked by id
or email. Useful when a phone needs to re-upload from scratch: afterwards the
watermark endpoint reports the season start again, so the next sync backfills
everything. The account and its role are untouched; tokens stay valid.

    venv/bin/python scripts/delete_athlete_data.py --email you@gmail.com
    venv/bin/python scripts/delete_athlete_data.py --id 4
    venv/bin/python scripts/delete_athlete_data.py --id 4 --dry-run
    venv/bin/python scripts/delete_athlete_data.py --id 4 --yes   # skip prompt

Run from the project root.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402

from database import SessionLocal  # noqa: E402
from models import (Athlete, DetectedSession, HeartRateSample,  # noqa: E402
                    IntervalSample, RouteTrack, Sync, Workout)

DATA_TABLES = [Sync, Workout, HeartRateSample, IntervalSample, DetectedSession,
               RouteTrack]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", type=int, help="athlete id")
    target.add_argument("--email", help="athlete email")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be deleted without changing anything")
    p.add_argument("--yes", action="store_true",
                   help="delete without asking for confirmation")
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

        print(f"Athlete {athlete.id} ({athlete.name}, {athlete.email}, "
              f"role={athlete.role}) — data to delete:")
        counts = {}
        for model in DATA_TABLES:
            counts[model] = db.scalar(select(func.count()).select_from(model)
                                      .where(model.athlete_id == athlete.id))
            print(f"  {model.__tablename__:22} {counts[model]:>8} rows")

        if args.dry_run:
            print("Dry run — nothing changed.")
            return
        if sum(counts.values()) == 0:
            print("No data — nothing to do.")
            return
        if not args.yes:
            answer = input("Delete all of it? The account itself is kept. [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                sys.exit("Aborted — nothing changed.")

        for model in DATA_TABLES:
            db.execute(delete(model).where(model.athlete_id == athlete.id))
        db.commit()
        print("Done. The athlete's next sync re-uploads from the season start.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
