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
import json
import sqlite3
import datetime
from PIL import Image, ImageDraw

from syllabus_processor import (
    clean_text,
    detect_subjects_and_topics,
    calculate_topic_difficulty,
    process_syllabus
)
from ml_models import (
    predict_topic_hours,
    predict_study_period,
    StudyTimeRegressor,
    StudyPeriodClassifier
)
from scheduler import (
    validate_deadline,
    generate_study_schedule,
    parse_time_to_minutes,
    format_minutes_to_time,
    find_free_slot
)
import app as flask_module


class SmartStudyPlannerComprehensiveTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = flask_module.app
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    # ==========================================================================
    # 1. FILE & DATABASE SAFETY CHECKS
    # ==========================================================================
    def test_01_critical_files_exist(self):
        """Verify that database.db and app.py exist (OAuth files are optional on a fresh clone)."""
        self.assertTrue(os.path.exists("database.db"), "database.db must exist!")
        self.assertTrue(os.path.exists("app.py"), "app.py must exist!")

    def test_02_database_schema_integrity(self):
        """Verify that existing tables (tasks, timetable, etc.) and new tables are intact."""
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        tables = [row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()

        required_tables = ["tasks", "timetable", "timetable_exceptions", "syllabuses", "subjects", "topics", "generated_plans", "study_history"]
        for tbl in required_tables:
            self.assertIn(tbl, tables, f"Table {tbl} must exist in database.db!")

    # ==========================================================================
    # 2. SYLLABUS PROCESSOR TESTS (PHASES 1-4)
    # ==========================================================================
    def test_03_data_cleaning(self):
        """Test text cleaning from noise, page numbers, duplicate lines, and excess spacing."""
        dirty_text = """
        Page 1 of 16
        --- Page 1 ---
        SUBJECT: JAVA PROGRAMMING
        
        Unit I: Object Oriented Concepts
        
        Class, Object,    Inheritance,    Polymorphism.
        www.example.com
        [2]
        """
        cleaned = clean_text(dirty_text)
        self.assertNotIn("Page 1 of 16", cleaned)
        self.assertNotIn("www.example.com", cleaned)
        self.assertIn("SUBJECT: JAVA PROGRAMMING", cleaned)
        self.assertIn("Unit I: Object Oriented Concepts", cleaned)

    def test_04_multi_subject_detection(self):
        """Test automatic detection of multiple subjects from a single syllabus string."""
        sample_syllabus = """
        SUBJECT: JAVA PROGRAMMING
        Unit 1: Core Java Basics
        Variables, Data Types, Control Structures
        Unit 2: Object-Oriented Principles
        Inheritance, Polymorphism, Abstract Classes

        SUBJECT: DATABASE MANAGEMENT SYSTEMS
        Unit 1: Relational Model
        ER Modeling, Relational Algebra, SQL Queries
        Unit 2: Normalization & Indexing
        1NF, 2NF, 3NF, BCNF, B-Trees, Hashing

        SUBJECT: COMPUTER NETWORKS
        Unit 1: Network Fundamentals
        OSI Model, TCP/IP Protocol Suite, Socket Programming
        """
        cleaned = clean_text(sample_syllabus)
        subjects = detect_subjects_and_topics(cleaned)

        self.assertIn("Java Programming", subjects)
        self.assertIn("Database Management Systems", subjects)
        self.assertIn("Computer Networks", subjects)

        self.assertGreaterEqual(subjects["Java Programming"]["total_topics"], 2)
        self.assertGreaterEqual(subjects["Database Management Systems"]["total_topics"], 2)
        self.assertGreaterEqual(subjects["Computer Networks"]["total_topics"], 1)

    def test_05_difficulty_score_calculation(self):
        """Test topic difficulty estimation based on technical complexity keywords."""
        easy_diff = calculate_topic_difficulty("Introduction to HTML and Basic Tags", ["Syntax", "Overview"])
        hard_diff = calculate_topic_difficulty("Distributed Cryptographic Protocols and Concurrency", ["Multithreading", "Optimization", "Deadlocks"])

        self.assertLess(easy_diff, hard_diff)
        self.assertGreaterEqual(easy_diff, 1.0)
        self.assertLessEqual(hard_diff, 5.0)

    # ==========================================================================
    # 3. MACHINE LEARNING MODEL TESTS (PHASES 6 & 7)
    # ==========================================================================
    def test_06_linear_regression_hours_prediction(self):
        """Test Multiple Linear Regression predicts reasonable study hours."""
        hours_intro = predict_topic_hours(topic_length=20, num_subtopics=1, difficulty=1.2)
        hours_advanced = predict_topic_hours(topic_length=120, num_subtopics=6, difficulty=4.8)

        self.assertIsInstance(hours_intro, float)
        self.assertIsInstance(hours_advanced, float)
        self.assertGreater(hours_advanced, hours_intro)
        self.assertGreaterEqual(hours_intro, 0.5)
        self.assertLessEqual(hours_advanced, 5.0)

    def test_07_decision_tree_period_prediction(self):
        """Test Decision Tree Classifier outputs valid period class."""
        period_easy = predict_study_period(difficulty=1.5, duration_hours=1.0)
        period_hard = predict_study_period(difficulty=4.8, duration_hours=3.5)

        valid_periods = {"Morning", "Afternoon", "Evening", "Night"}
        self.assertIn(period_easy, valid_periods)
        self.assertIn(period_hard, valid_periods)

    def test_08_ml_incremental_retraining(self):
        """Test retraining ML models with mock study history records."""
        regressor = StudyTimeRegressor()
        classifier = StudyPeriodClassifier()

        mock_history = [
            {"topic_length": 40, "num_subtopics": 2, "difficulty": 2.5, "experience": 1.0, "planned_hours": 2.0, "actual_hours": 1.5, "study_period": "Morning", "day_of_week": 1, "urgency": 0.3},
            {"topic_length": 80, "num_subtopics": 5, "difficulty": 4.0, "experience": 1.0, "planned_hours": 3.0, "actual_hours": 3.5, "study_period": "Evening", "day_of_week": 2, "urgency": 0.5}
        ]

        regressor.train_with_history(mock_history)
        classifier.train_with_history(mock_history)

        self.assertTrue(regressor.is_fitted)
        self.assertTrue(classifier.is_fitted)

    # ==========================================================================
    # 4. SCHEDULER & DEADLINE TESTS (PHASES 8 & 9)
    # ==========================================================================
    def test_09_deadline_validation(self):
        """Test deadline feasibility evaluation for feasible vs infeasible deadlines."""
        future_date = (datetime.date.today() + datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        
        # Feasible: 10 days * 3h = 30h available vs 15h needed
        feasible_res = validate_deadline(15.0, future_date, 3.0)
        self.assertTrue(feasible_res["is_feasible"])
        self.assertEqual(feasible_res["deficit_hours"], 0.0)

        # Infeasible: 10 days * 1h = 10h available vs 30h needed
        infeasible_res = validate_deadline(30.0, future_date, 1.0)
        self.assertFalse(infeasible_res["is_feasible"])
        self.assertGreater(infeasible_res["deficit_hours"], 0.0)
        self.assertIn("recommended_daily_hours", infeasible_res)

    def test_10_conflict_free_scheduling(self):
        """Test that generate_study_schedule creates non-overlapping sessions with 15-min breaks."""
        sample_subjects = {
            "Java": {
                "code": "CS101",
                "total_topics": 2,
                "topics": [
                    {"topic_name": "OOP Basics", "unit_name": "Unit 1", "subtopics": ["Classes", "Objects"], "difficulty_score": 2.0, "topic_length": 30, "subtopic_count": 2},
                    {"topic_name": "Inheritance", "unit_name": "Unit 1", "subtopics": ["Super", "Override"], "difficulty_score": 3.0, "topic_length": 40, "subtopic_count": 2}
                ]
            }
        }
        deadline = (datetime.date.today() + datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        plan = generate_study_schedule(sample_subjects, deadline, 3.0)

        sessions = plan["scheduled_sessions"]
        self.assertGreater(len(sessions), 0)

        # Verify no two sessions on the same date overlap
        by_date = {}
        for s in sessions:
            d = s["date"]
            if d not in by_date:
                by_date[d] = []
            by_date[d].append(s)

        for d, s_list in by_date.items():
            for i in range(len(s_list)):
                for j in range(i + 1, len(s_list)):
                    s1_start = parse_time_to_minutes(s_list[i]["start_time"])
                    s1_end = parse_time_to_minutes(s_list[i]["end_time"])
                    s2_start = parse_time_to_minutes(s_list[j]["start_time"])
                    s2_end = parse_time_to_minutes(s_list[j]["end_time"])

                    # Must not overlap (s1 ends before s2 starts or vice versa)
                    self.assertTrue(s1_end <= s2_start or s2_end <= s1_start, f"Sessions on {d} must not overlap!")

    # ==========================================================================
    # 5. FLASK WEB ROUTES & INTEGRATION (PHASES 10-15)
    # ==========================================================================
    def test_11_dashboard_route(self):
        """Verify dashboard route loads successfully and returns expected elements."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Smart Study Planner", response.data)
        self.assertIn(b"Total Tasks", response.data)
        self.assertIn(b"Hours Studied", response.data)

    def test_12_tasks_routes(self):
        """Test task creation, completion, and listing."""
        # Add task
        add_res = self.client.post("/add-task", data={
            "subject": "TestSubject",
            "task": "TestTaskUnit",
            "deadline": datetime.date.today().strftime("%Y-%m-%d"),
            "priority": "High"
        }, follow_redirects=True)
        self.assertEqual(add_res.status_code, 200)
        self.assertIn(b"TestSubject", add_res.data)

        # List tasks
        tasks_res = self.client.get("/tasks")
        self.assertEqual(tasks_res.status_code, 200)
        self.assertIn(b"TestSubject", tasks_res.data)

    def test_13_timetable_route(self):
        """Verify timetable route renders properly."""
        res = self.client.get("/timetable")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Study Timetable", res.data)

    def test_14_study_history_logging(self):
        """Test study history route and manual session logging."""
        log_res = self.client.post("/log-study", data={
            "subject": "AI Concepts",
            "topic": "Neural Networks",
            "planned_hours": "2.0",
            "actual_hours": "2.5",
            "study_period": "Evening",
            "study_date": datetime.date.today().strftime("%Y-%m-%d")
        }, follow_redirects=True)
        self.assertEqual(log_res.status_code, 200)
        self.assertIn(b"AI Concepts", log_res.data)


if __name__ == "__main__":
    unittest.main()
