import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "worker"))

# worker.py connects to Postgres at import time only inside functions (lazy), so
# importing it doesn't require a live database - but set sane env defaults anyway.
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("REDIS_HOST", "localhost")
