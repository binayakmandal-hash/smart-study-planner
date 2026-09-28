"""
Tests for the new syllabus reader and deadline-aware scheduler (PRP section 31).
Runs fully offline: temporary database copy, Google Calendar disabled.
"""
import os
import shutil
import tempfile

_TEST_DB = os.path.join(tempfile.gettempdir(), "studyplanner_test.db")
if os.path.exists("database.db") and not os.environ.get("STUDYPLANNER_DB"):
    shutil.copyfile("database.db", _TEST_DB)
    os.environ["STUDYPLANNER_DB"] = _TEST_DB
os.environ["STUDYPLANNER_CALENDAR_SYNC"] = "0"

import datetime
import unittest

from syllabus_processor import (
    clean_text, detect_subjects_and_topics, parse_pages, parse_module_label, page_items
)
from scheduler import (
    generate_study_schedule, get_time_period_label, parse_time_to_minutes, validate_plan
)


def _row(module, content, hours="", y=0, first=False):
    return {"kind": "row", "module": module, "content": content, "hours": hours, "y": y, "first_on_page": first}


class SyllabusReaderTests(unittest.TestCase):

    SAMPLE = """
SEMESTER - V
SUBJECT NAME: PHP WITH MYSQL
SUBJECT CODE: BCA24-CC-501
CREDIT: 4
CONTACT HOURS: 48
COURSE OBJECTIVE:
1. To understand the fundamentals of PHP programming
COURSE OUTCOME:
CO1: Explain the basic concepts of server-side scripting
DETAILED SYLLABUS:
MODU
LE NO:
NAME OF THE TOPIC
UNIT I
Introduction to Web Development:
Overview of web development, server vs. client-side, HTML basics.
UNIT II
PHP Basics:
PHP syntax, variables, data types, operators, and control structures.
Functions in PHP: Defining and using functions, function scope.
UNIT III
Content Management System (CMS):
Case Study: A blog site requires a CMS that allows authors to wrile, edit
and publish articles. Design CMS using PHP and MySQL where authors can log in
creale nCw posts, edit existing posts, and delete existing posts.
SUGGESTED READING:
1. Gilmore, W. J. (2010). Beginning PHP and MySQL.
"""

    def setUp(self):
        self.subjects = detect_subjects_and_topics(clean_text(self.SAMPLE))

    def test_subject_detected_without_false_modul(self):
        self.assertIn("PHP with MySQL", self.subjects)
        for name in self.subjects:
            self.assertFalse(name.lower().startswith("modu"), name)
        self.assertEqual(self.subjects["PHP with MySQL"]["code"], "BCA24-CC-501")

    def test_metadata_not_topics(self):
        topics = [t["topic_name"].upper() for t in self.subjects["PHP with MySQL"]["topics"]]
        for meta in ["SUBJECT CODE", "CREDIT", "CONTACT HOURS", "COURSE OBJECTIVE",
                     "COURSE OUTCOMES", "SUGGESTED READING", "NAME OF THE TOPIC", "DETAILED SYLLABUS"]:
            self.assertFalse(any(t.startswith(meta) for t in topics), meta)
        self.assertFalse(any("GILMORE" in t for t in topics))

    def test_units_are_separate(self):
        units = [m["number"] for m in self.subjects["PHP with MySQL"]["modules"]]
        self.assertEqual(units, [1, 2, 3])

    def test_case_study_stays_with_topic_and_ocr_fixed(self):
        cms = [t for t in self.subjects["PHP with MySQL"]["topics"] if t["topic_name"].startswith("Content Management")]
        self.assertEqual(len(cms), 1)
        text = " ".join(cms[0]["subtopics"])
        self.assertIn("Case Study", text)
        self.assertIn("write", text)
        self.assertIn("create new posts", text)

    def test_inline_heading_becomes_topic(self):
        names = [t["topic_name"] for t in self.subjects["PHP with MySQL"]["topics"]]
        self.assertIn("PHP Basics", names)
        self.assertIn("Functions in PHP", names)

    def test_module_labels(self):
        self.assertEqual(parse_module_label("UNIT II")[0], 2)
        self.assertEqual(parse_module_label("Module 3: Trees"), (3, "Trees"))
        self.assertEqual(parse_module_label("M4")[0], 4)

    def test_table_rows_with_hours(self):
        """Layout rows (module | topic | hours) as produced from a scanned table."""
        page = {"width": 1000, "height": 1400, "tokens": [], "hlines": [], "vsegs": []}
        items = [
            {"kind": "text", "text": "SUBJECT NAME: Entrepreneurship", "y": 1, "x0": 0, "x1": 400, "width": 1000},
            {"kind": "text", "text": "SUBJECT CODE: BCA24-DSE-503", "y": 2, "x0": 0, "x1": 400, "width": 1000},
            _row("MOD ULE NO.", ["COURSE CONTENT"], "Hours", 3),
            _row("1", ["Introduction to Entrepreneurship:", "Meaning and concept of entrepreneurship."], "4", 4),
            _row("", ["Introduction to entrepreneur:", "Meaning, types of entrepreneur."], "", 5),
            _row("3", ["Entrepreneurial Motivation:", "Maslow's theory, McGregor's Theory."], "6", 6),
        ]
        import syllabus_processor as sp
        original = sp.page_items
        sp.page_items = lambda p: items
        try:
            result = parse_pages([page])
        finally:
            sp.page_items = original
        subj = result["Entrepreneurship"]
        self.assertEqual([m["number"] for m in subj["modules"]], [1, 2, 3])
        self.assertEqual(subj["modules"][0]["hours"], 4)
        self.assertEqual(subj["modules"][2]["hours"], 6)
        self.assertEqual(subj["total_topics"], 3)


class SchedulerTests(unittest.TestCase):

    def _subjects(self, n_topics=6, hours=2.0):
        return {
            "Java": {"is_lab": False, "topics": [
                {"topic_name": f"Java topic {i}", "unit_name": f"Module {i}: X", "subtopics": ["a", "b", "c"],
                 "difficulty_score": 2.5, "topic_length": 40, "subtopic_count": 3, "syllabus_hours": hours}
                for i in range(1, n_topics + 1)]},
            "PHP": {"is_lab": False, "topics": [
                {"topic_name": f"PHP topic {i}", "unit_name": f"Module {i}: Y", "subtopics": ["a", "b"],
                 "difficulty_score": 3.0, "topic_length": 40, "subtopic_count": 2, "syllabus_hours": hours}
                for i in range(1, n_topics + 1)]},
        }

    def _plan(self, availability, days=14, daily=6, subjects=None):
        start = datetime.date(2026, 10, 5)
        return generate_study_schedule(
            subjects or self._subjects(), (start + datetime.timedelta(days=days)).strftime("%Y-%m-%d"), daily,
            start_date_str=start.strftime("%Y-%m-%d"), student_availability_dict=availability,
            now=datetime.datetime(2026, 10, 1, 8, 0))

    def test_single_window_respected(self):
        avail = {d: [(17 * 60, 20 * 60)] for d in
                 ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
        plan = self._plan(avail)
        self.assertGreater(len(plan["scheduled_sessions"]), 0)
        for s in plan["scheduled_sessions"]:
            self.assertGreaterEqual(parse_time_to_minutes(s["start_time"]), 17 * 60)
            self.assertLessEqual(parse_time_to_minutes(s["end_time"]), 20 * 60)

    def test_multiple_windows_never_in_gap(self):
        avail = {d: [(17 * 60, 20 * 60), (21 * 60, 23 * 60)] for d in
                 ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
        plan = self._plan(avail)
        for s in plan["scheduled_sessions"]:
            start, end = parse_time_to_minutes(s["start_time"]), parse_time_to_minutes(s["end_time"])
            self.assertFalse(start < 21 * 60 and end > 20 * 60, f"{s['start_time']}-{s['end_time']} crosses 20:00-21:00")
        self.assertTrue(plan["checks"]["passed"], plan["checks"])

    def test_period_label_from_actual_time(self):
        self.assertEqual(get_time_period_label("18:00"), "Evening")
        self.assertEqual(get_time_period_label("09:30"), "Morning")
        self.assertEqual(get_time_period_label("22:15"), "Night")

    def test_chronological_and_no_overlap(self):
        avail = {d: [(9 * 60, 12 * 60), (18 * 60, 22 * 60)] for d in
                 ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
        plan = self._plan(avail)
        keys = [(s["date"], s["start_time"]) for s in plan["scheduled_sessions"]]
        self.assertEqual(keys, sorted(keys))
        self.assertTrue(plan["checks"]["no_session_overlap"])

    def test_overlap_is_detected(self):
        avail = {"Monday": [(0, 1440)]}
        bad = [
            {"date": "2026-10-05", "day": "Monday", "start_time": "10:00", "end_time": "11:00", "preferred_period": "Morning"},
            {"date": "2026-10-05", "day": "Monday", "start_time": "10:30", "end_time": "11:30", "preferred_period": "Morning"},
        ]
        self.assertFalse(validate_plan(bad, avail)["no_session_overlap"])

    def test_every_topic_scheduled_before_deadline_even_when_tight(self):
        avail = {d: [(18 * 60, 20 * 60)] for d in
                 ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
        plan = self._plan(avail, days=10, daily=2, subjects=self._subjects(n_topics=5, hours=3.0))
        scheduled_topics = {s["base_topic"] for s in plan["scheduled_sessions"]}
        self.assertEqual(len(scheduled_topics), 10)
        self.assertTrue(plan["validation"]["compressed"])
        self.assertTrue(all(s["date"] < plan["deadline"] for s in plan["scheduled_sessions"]))

    def test_impossible_deadline_reported_honestly(self):
        avail = {d: [(18 * 60, 19 * 60)] for d in
                 ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
        plan = self._plan(avail, days=3, daily=1, subjects=self._subjects(n_topics=5, hours=3.0))
        self.assertFalse(plan["validation"]["is_feasible"])
        self.assertGreater(len(plan["validation"]["unscheduled_topics"]), 0)
        self.assertTrue(plan["checks"]["all_sessions_inside_availability"])

    def test_daily_limit(self):
        avail = {d: [(8 * 60, 22 * 60)] for d in
                 ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
        plan = self._plan(avail, daily=2)
        per_day = {}
        for s in plan["scheduled_sessions"]:
            per_day[s["date"]] = per_day.get(s["date"], 0) + s["duration_hours"]
        self.assertTrue(all(h <= 2.0 + 1e-6 for h in per_day.values()), per_day)


if __name__ == "__main__":
    unittest.main()
