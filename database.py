import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
connection = sqlite3.connect(os.path.join(BASE_DIR, "database.db"))
cursor = connection.cursor()

# 1. Create tasks table
cursor.execute("""
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    task TEXT NOT NULL,
    deadline TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL
)
""")

# 2. Create timetable table
cursor.execute("""
CREATE TABLE IF NOT EXISTS timetable (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    topic TEXT NOT NULL,
    day TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL
)
""")

# Safe column migrations for timetable
columns = [col[1] for col in cursor.execute("PRAGMA table_info(timetable)").fetchall()]

if "alarm" not in columns:
    cursor.execute("ALTER TABLE timetable ADD COLUMN alarm INTEGER DEFAULT 1")

if "google_event_id" not in columns:
    cursor.execute("ALTER TABLE timetable ADD COLUMN google_event_id TEXT")

if "date" not in columns:
    cursor.execute("ALTER TABLE timetable ADD COLUMN date TEXT")

if "schedule_type" not in columns:
    cursor.execute("ALTER TABLE timetable ADD COLUMN schedule_type TEXT DEFAULT 'one-time'")

if "start_date" not in columns:
    cursor.execute("ALTER TABLE timetable ADD COLUMN start_date TEXT")

if "end_date" not in columns:
    cursor.execute("ALTER TABLE timetable ADD COLUMN end_date TEXT")

if "status" not in columns:
    cursor.execute("ALTER TABLE timetable ADD COLUMN status TEXT DEFAULT 'Pending'")

# 3. Create timetable_exceptions table
cursor.execute("""
CREATE TABLE IF NOT EXISTS timetable_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timetable_id INTEGER NOT NULL,
    occurrence_date TEXT NOT NULL,
    UNIQUE(timetable_id, occurrence_date)
)
""")

# 4. Create syllabuses table
cursor.execute("""
CREATE TABLE IF NOT EXISTS syllabuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL,
    deadline TEXT NOT NULL,
    extracted_text TEXT,
    created_at TEXT NOT NULL
)
""")

# Safe column migrations for syllabuses
syllabus_cols = [col[1] for col in cursor.execute("PRAGMA table_info(syllabuses)").fetchall()]
if "daily_hours" not in syllabus_cols:
    cursor.execute("ALTER TABLE syllabuses ADD COLUMN daily_hours REAL DEFAULT 3.0")

# 5. Create subjects table
cursor.execute("""
CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    syllabus_id INTEGER,
    name TEXT NOT NULL,
    code TEXT,
    total_topics INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(syllabus_id) REFERENCES syllabuses(id)
)
""")

# 6. Create topics table
cursor.execute("""
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER,
    syllabus_id INTEGER,
    unit_name TEXT,
    topic_name TEXT NOT NULL,
    subtopics TEXT,
    difficulty REAL DEFAULT 2.0,
    predicted_hours REAL DEFAULT 1.5,
    preferred_period TEXT DEFAULT 'Evening',
    status TEXT DEFAULT 'Pending',
    FOREIGN KEY(subject_id) REFERENCES subjects(id),
    FOREIGN KEY(syllabus_id) REFERENCES syllabuses(id)
)
""")

# 7. Create generated_plans table
cursor.execute("""
CREATE TABLE IF NOT EXISTS generated_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    syllabus_id INTEGER,
    plan_data TEXT NOT NULL,
    deadline TEXT NOT NULL,
    daily_hours REAL NOT NULL,
    total_hours REAL NOT NULL,
    feasibility TEXT NOT NULL,
    status TEXT DEFAULT 'draft',
    created_at TEXT NOT NULL,
    FOREIGN KEY(syllabus_id) REFERENCES syllabuses(id)
)
""")

# 8. Create study_history table
cursor.execute("""
CREATE TABLE IF NOT EXISTS study_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timetable_id INTEGER,
    subject TEXT NOT NULL,
    topic TEXT NOT NULL,
    planned_hours REAL NOT NULL,
    actual_hours REAL NOT NULL,
    completion_status TEXT NOT NULL,
    study_period TEXT NOT NULL,
    study_date TEXT NOT NULL,
    difficulty REAL DEFAULT 2.5,
    created_at TEXT NOT NULL
)
""")

connection.commit()
connection.close()

print("Database verified and safely migrated successfully!")
