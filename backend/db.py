"""
db.py — Shared MongoDB client singleton.
Import `client`, `app_db`, `source_db` from here everywhere.
Never create a second MongoClient.
"""
import pymongo
from config import MONGODB_URI, APP_DB, SOURCE_DB

# Single client for the entire process
client    = pymongo.MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)

# Convenience database handles
app_db    = client[APP_DB]       # querysentinel — operational data
source_db = client[SOURCE_DB]    # sample_mflix  — monitored data

def ping() -> bool:
    """Returns True if Atlas is reachable."""
    try:
        client.admin.command("ping")
        return True
    except Exception:
        return False
