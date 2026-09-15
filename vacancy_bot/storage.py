import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from .models import Filters


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                  user_id INTEGER PRIMARY KEY, filters TEXT NOT NULL,
                  chat_id INTEGER, daily_time TEXT
                );
                CREATE TABLE IF NOT EXISTS seen (
                  url TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def filters(self, user_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT filters FROM settings WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return Filters()
        data = json.loads(row[0])
        data["companies"] = tuple(data["companies"])
        return Filters(**data)

    def save_filters(self, user_id, filters):
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO settings(user_id,filters) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET filters=excluded.filters",
                (user_id, json.dumps(asdict(filters))),
            )

    def subscribe(self, user_id, chat_id, daily_time):
        self.save_filters(user_id, self.filters(user_id))
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE settings SET chat_id=?,daily_time=? WHERE user_id=?",
                (chat_id, daily_time, user_id),
            )

    def unsubscribe(self, user_id):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE settings SET daily_time=NULL,chat_id=NULL WHERE user_id=?", (user_id,)
            )

    def subscriptions(self):
        with closing(self.connect()) as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM settings WHERE daily_time IS NOT NULL")
            ]

    def observe(self, vacancies, now):
        output = []
        with closing(self.connect()) as db, db:
            for v in vacancies:
                db.execute(
                    "INSERT INTO seen(url,first_seen,last_seen) VALUES(?,?,?) "
                    "ON CONFLICT(url) DO UPDATE SET last_seen=excluded.last_seen",
                    (v.url, now.isoformat(), now.isoformat()),
                )
                first = db.execute("SELECT first_seen FROM seen WHERE url=?", (v.url,)).fetchone()[
                    0
                ]
                output.append(replace(v, first_seen=datetime.fromisoformat(first)))
        return output
