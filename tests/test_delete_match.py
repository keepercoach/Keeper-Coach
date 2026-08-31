import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException


class DeleteMatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["KEEPERCOACH_DATA_DIR"] = self.tmp.name
        sys.modules.pop("app.main", None)
        self.main = importlib.import_module("app.main")
        with self.main.db() as con:
            user = con.execute(
                "SELECT * FROM users WHERE email=?", ("demo@keepercoach.app",)
            ).fetchone()
            self.user = dict(user)
            keeper = con.execute(
                "SELECT id FROM keepers WHERE owner_user_id=?", (user["id"],)
            ).fetchone()
            self.match_id = self.main.uid()
            self.video_name = f"{self.match_id}.mp4"
            con.execute(
                "INSERT INTO matches(id,keeper_id,opponent,match_date,video_path,status,created_at) "
                "VALUES(?,?,?,?,?,'ready',?)",
                (
                    self.match_id,
                    keeper["id"],
                    "Delete FC",
                    "2026-08-31",
                    self.video_name,
                    self.main.now(),
                ),
            )
            con.execute(
                "INSERT INTO events(id,match_id,event_type,created_at) VALUES(?,?,?,?)",
                (self.main.uid(), self.match_id, "Save", self.main.now()),
            )
        (self.main.UPLOADS / self.video_name).write_bytes(b"video")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("KEEPERCOACH_DATA_DIR", None)
        sys.modules.pop("app.main", None)

    def test_delete_removes_match_events_and_video(self):
        self.assertEqual(
            self.main.delete_match(self.match_id, user=self.user), {"ok": True}
        )
        with self.main.db() as con:
            self.assertIsNone(
                con.execute("SELECT id FROM matches WHERE id=?", (self.match_id,)).fetchone()
            )
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM events WHERE match_id=?", (self.match_id,)
                ).fetchone()[0],
                0,
            )
        self.assertFalse((self.main.UPLOADS / self.video_name).exists())

    def test_delete_rejects_non_owner_without_changing_data(self):
        with self.assertRaises(HTTPException) as caught:
            self.main.delete_match(self.match_id, user={"id": "someone-else"})
        self.assertEqual(caught.exception.status_code, 404)
        with self.main.db() as con:
            self.assertIsNotNone(
                con.execute("SELECT id FROM matches WHERE id=?", (self.match_id,)).fetchone()
            )
        self.assertTrue((self.main.UPLOADS / self.video_name).exists())


if __name__ == "__main__":
    unittest.main()

