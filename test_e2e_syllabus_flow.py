import os
import shutil
import tempfile

# Run against a throw-away copy of database.db and never touch Google Calendar.
_TEST_DB = os.path.join(tempfile.gettempdir(), "studyplanner_test.db")
if os.path.exists("database.db") and not os.environ.get("STUDYPLANNER_DB"):
    shutil.copyfile("database.db", _TEST_DB)
    os.environ["STUDYPLANNER_DB"] = _TEST_DB
os.environ["STUDYPLANNER_CALENDAR_SYNC"] = "0"
os.environ.setdefault("STUDYPLANNER_LOGIN", "0")

import unittest
import io
import json
import datetime
import sqlite3
import pymupdf
import app as flask_module


class TestEndToEndSyllabusFlow(unittest.TestCase):

    def setUp(self):
        self.app = flask_module.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_full_syllabus_lifecycle(self):
        # 1. Create a genuine, valid multi-subject PDF syllabus using PyMuPDF
        doc = pymupdf.open()
        page = doc.new_page()
        text = """SUBJECT: ADVANCED JAVA PROGRAMMING
COURSE CODE: CS501
Unit 1: Concurrency and Multithreading
Thread Lifecycle, Synchronization, Lock Interfaces, Thread Pools, Callable and Future
Unit 2: Java Networking and Web Services
Socket Programming, URL Connection, HTTP Client, RESTful APIs

SUBJECT: MODERN DATABASE SYSTEMS
COURSE CODE: CS502
Unit 1: Advanced SQL & Query Optimization
Complex Joins, Indexing Strategies, Execution Plans, B-Trees
Unit 2: NoSQL and Distributed Databases
MongoDB, Cassandra, CAP Theorem, Sharding, Replication
"""
        page.insert_text((50, 50), text, fontsize=11)
        pdf_bytes = doc.tobytes()
        doc.close()

        deadline_str = (datetime.date.today() + datetime.timedelta(days=14)).strftime("%Y-%m-%d")

        # 2. Upload via POST /add-syllabus
        data = {
            "syllabus": (io.BytesIO(pdf_bytes), "sample_course_syllabus.pdf"),
            "deadline": deadline_str,
            "study_hours": "3.5"
        }

        response = self.client.post(
            "/add-syllabus",
            data=data,
            content_type="multipart/form-data"
        )

        # Must redirect to /plan-preview/<plan_id>
        self.assertEqual(response.status_code, 302)
        location = response.headers.get("Location", "")
        self.assertIn("/plan-preview/", location)

        plan_id = int(location.split("/plan-preview/")[-1])

        # 3. View Plan Preview
        preview_res = self.client.get(f"/plan-preview/{plan_id}")
        self.assertEqual(preview_res.status_code, 200)
        self.assertIn(b"Advanced Java Programming", preview_res.data)
        self.assertIn(b"Modern Database Systems", preview_res.data)
        self.assertIn(b"Linear Regression (ML)", preview_res.data)
        self.assertIn(b"Approve Study Plan", preview_res.data)

        # 4. Approve Study Plan via POST /approve-plan/<plan_id>
        approve_res = self.client.post(f"/approve-plan/{plan_id}", follow_redirects=True)
        self.assertEqual(approve_res.status_code, 200)
        self.assertIn(b"Study Timetable", approve_res.data)

        # Verify database has approved plan and timetable entries
        conn = sqlite3.connect(os.environ["STUDYPLANNER_DB"])
        conn.row_factory = sqlite3.Row
        plan = conn.execute("SELECT * FROM generated_plans WHERE id = ?", (plan_id,)).fetchone()
        self.assertEqual(plan["status"], "approved")

        new_sessions = conn.execute("SELECT * FROM timetable WHERE subject = ?", ("Advanced Java Programming",)).fetchall()
        self.assertGreater(len(new_sessions), 0)
        first_session_id = new_sessions[0]["id"]
        conn.close()

        # 5. Mark a session as completed via POST /complete-session/<session_id>
        complete_res = self.client.post(f"/complete-session/{first_session_id}", follow_redirects=True)
        self.assertEqual(complete_res.status_code, 200)

        # Verify study_history record was created
        conn = sqlite3.connect(os.environ["STUDYPLANNER_DB"])
        conn.row_factory = sqlite3.Row
        history_record = conn.execute("SELECT * FROM study_history WHERE timetable_id = ?", (first_session_id,)).fetchone()
        self.assertIsNotNone(history_record)
        self.assertEqual(history_record["subject"], "Advanced Java Programming")
        self.assertEqual(history_record["completion_status"], "Completed")
        conn.close()

        # 6. Verify Dashboard shows updated metrics
        dash_res = self.client.get("/")
        self.assertEqual(dash_res.status_code, 200)
        self.assertIn(b"Hours Studied", dash_res.data)


if __name__ == "__main__":
    unittest.main()
