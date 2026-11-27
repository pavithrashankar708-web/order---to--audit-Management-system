"""Seed / reset / test helper.
  python manage.py reset               masters + users + BOM + OPENING stock only  (do the walkthrough yourself)
  python manage.py reset --benchmark   same, then replays all 10 benchmark transactions
  python manage.py test                run the automated test-suite
"""
import os, subprocess, sys
from app import create_app
from app.seed import reset_database

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "sri_murugan.db")


def main(argv):
    if not argv:
        print(__doc__); return 1
    if argv[0] == "reset":
        os.makedirs(os.path.dirname(DB), exist_ok=True)
        reset_database(DB, benchmark="--benchmark" in argv)
        print("Database reset:", DB, "(with benchmark transactions)" if "--benchmark" in argv else "(opening stock only)")
        print("Demo users: salesman_a, salesman_b, admin, supervisor, auditor, owner  - password: demo123")
        return 0
    if argv[0] == "test":
        return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    print(__doc__); return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
