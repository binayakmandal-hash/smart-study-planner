from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename
import sqlite3
import datetime
import os
import json
import re
import threading
import uuid

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from syllabus_processor import process_syllabus, needs_background_processing
from ml_models import (
    predict_topic_hours,
    predict_study_period,
    study_time_regressor,
    study_period_classifier
)
from scheduler import generate_study_schedule, validate_deadline

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.environ.get("STUDYPLANNER_DB") or os.path.join(BASE_DIR, "database.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")

ALLOWED_EXTENSIONS = {
    "pdf",
    "png",
    "jpg",
    "jpeg"
}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
# Set STUDYPLANNER_CALENDAR_SYNC=0 (tests do this) to never touch Google Calendar.
app.config["CALENDAR_SYNC"] = os.environ.get("STUDYPLANNER_CALENDAR_SYNC", "1") != "0"

# Public demo mode (Render): no Google Calendar, visible demo banner, sample syllabus.
DEMO_MODE = os.environ.get("STUDYPLANNER_DEMO", "0") == "1"
if DEMO_MODE:
    app.config["CALENDAR_SYNC"] = False
SAMPLE_SYLLABUS = os.path.join(BASE_DIR, "sample", "5th_sem_syllabus.pdf")


@app.context_processor
def inject_globals():
    return {
        "demo_mode": DEMO_MODE,
        "calendar_enabled": app.config.get("CALENDAR_SYNC", True),
        "sample_available": os.path.exists(SAMPLE_SYLLABUS),
    }


@app.errorhandler(413)
def upload_too_large(error):
    return render_template(
        "add_syllabus.html",
        error_msg="The uploaded file is too large. Please use a file smaller than 25 MB."
    ), 413

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events"
]

TOKEN_FILE = os.path.join(BASE_DIR, "token.json")

DAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday"
]


def get_database_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def ensure_database():
    connection = get_database_connection()

    # Bootstrap the two legacy tables as well as the newer planner tables so a
    # fresh clone can start without first running database.py manually.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            task TEXT NOT NULL,
            deadline TEXT NOT NULL,
            priority TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS timetable (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            topic TEXT NOT NULL,
            day TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            alarm INTEGER DEFAULT 1,
            google_event_id TEXT,
            date TEXT,
            schedule_type TEXT DEFAULT 'one-time',
            start_date TEXT,
            end_date TEXT,
            status TEXT DEFAULT 'Pending'
        )
        """
    )

    # Timetable column checks
    columns = [
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(timetable)"
        ).fetchall()
    ]

    if "schedule_type" not in columns:
        connection.execute(
            "ALTER TABLE timetable ADD COLUMN schedule_type TEXT DEFAULT 'one-time'"
        )

    if "alarm" not in columns:
        connection.execute(
            "ALTER TABLE timetable ADD COLUMN alarm INTEGER DEFAULT 1"
        )

    if "date" not in columns:
        connection.execute(
            "ALTER TABLE timetable ADD COLUMN date TEXT"
        )

    if "start_date" not in columns:
        connection.execute(
            "ALTER TABLE timetable ADD COLUMN start_date TEXT"
        )

    if "end_date" not in columns:
        connection.execute(
            "ALTER TABLE timetable ADD COLUMN end_date TEXT"
        )

    if "google_event_id" not in columns:
        connection.execute(
            "ALTER TABLE timetable ADD COLUMN google_event_id TEXT"
        )

    if "status" not in columns:
        connection.execute(
            "ALTER TABLE timetable ADD COLUMN status TEXT DEFAULT 'Pending'"
        )

    syllabus_columns = [
        row["name"]
        for row in connection.execute("PRAGMA table_info(syllabuses)").fetchall()
    ]
    if syllabus_columns and "daily_hours" not in syllabus_columns:
        connection.execute(
            "ALTER TABLE syllabuses ADD COLUMN daily_hours REAL DEFAULT 3.0"
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS timetable_exceptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timetable_id INTEGER NOT NULL,
            occurrence_date TEXT NOT NULL,
            UNIQUE(timetable_id, occurrence_date)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS syllabuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            filename TEXT NOT NULL,
            file_type TEXT NOT NULL,
            deadline TEXT NOT NULL,
            daily_hours REAL DEFAULT 3.0,
            extracted_text TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            syllabus_id INTEGER,
            name TEXT NOT NULL,
            code TEXT,
            total_topics INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(syllabus_id) REFERENCES syllabuses(id)
        )
        """
    )
    connection.execute(
        """
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
        """
    )
    connection.execute(
        """
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
        """
    )
    connection.execute(
        """
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
        """
    )
    # Safe, additive migrations for the richer curriculum structure
    def add_column(table, column, definition):
        existing = [row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]
        if existing and column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    add_column("topics", "description", "TEXT")
    add_column("topics", "syllabus_hours", "REAL")
    add_column("topics", "module_number", "INTEGER")
    add_column("subjects", "is_lab", "INTEGER DEFAULT 0")
    add_column("subjects", "contact_hours", "REAL")
    add_column("timetable", "notes", "TEXT")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS student_availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_of_week TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        )
        """
    )

    # Seed default student availability if empty
    avail_count = connection.execute(
        "SELECT COUNT(*) FROM student_availability"
    ).fetchone()[0]

    if avail_count == 0:
        default_slots = [
            ("Monday", "17:00", "20:00"),
            ("Monday", "21:00", "23:00"),
            ("Tuesday", "17:00", "20:00"),
            ("Tuesday", "21:00", "23:00"),
            ("Wednesday", "17:00", "20:00"),
            ("Wednesday", "21:00", "23:00"),
            ("Thursday", "17:00", "20:00"),
            ("Thursday", "21:00", "23:00"),
            ("Friday", "17:00", "20:00"),
            ("Friday", "21:00", "23:00"),
            ("Saturday", "09:00", "12:00"),
            ("Saturday", "14:00", "17:00"),
            ("Saturday", "18:00", "21:00"),
            ("Sunday", "09:00", "12:00"),
            ("Sunday", "14:00", "17:00"),
            ("Sunday", "18:00", "21:00"),
        ]
        for day, st, et in default_slots:
            connection.execute(
                """
                INSERT INTO student_availability (day_of_week, start_time, end_time, is_active)
                VALUES (?, ?, ?, 1)
                """,
                (day, st, et)
            )

    connection.commit()
    connection.close()


def get_google_credentials():
    if not os.path.exists(TOKEN_FILE):
        return None

    credentials = Credentials.from_authorized_user_file(
        TOKEN_FILE,
        SCOPES
    )

    if credentials.valid:
        return credentials

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())

            with open(TOKEN_FILE, "w") as token:
                token.write(credentials.to_json())

            return credentials
        except Exception as error:
            print("Google Token Refresh Error:", error)

    return None


def get_google_service():
    if not app.config.get("CALENDAR_SYNC", True):
        return None

    credentials = get_google_credentials()

    if not credentials:
        return None

    return build(
        "calendar",
        "v3",
        credentials=credentials
    )


def recurrence_until(end_date):
    return f"{end_date.replace('-', '')}T235959Z"


def create_google_calendar_event(
    subject,
    topic,
    schedule_type,
    event_date,
    start_time,
    end_time,
    alarm,
    end_date=None,
    day=None
):
    service = get_google_service()

    if not service:
        return None

    start_datetime = datetime.datetime.strptime(
        f"{event_date} {start_time}",
        "%Y-%m-%d %H:%M"
    )

    end_datetime = datetime.datetime.strptime(
        f"{event_date} {end_time}",
        "%Y-%m-%d %H:%M"
    )

    if end_datetime <= start_datetime:
        end_datetime += datetime.timedelta(days=1)

    event = {
        "summary": subject,
        "description": f"Study Topic: {topic}\nSmart Study Planner",
        "start": {
            "dateTime": start_datetime.isoformat(),
            "timeZone": "Asia/Kolkata"
        },
        "end": {
            "dateTime": end_datetime.isoformat(),
            "timeZone": "Asia/Kolkata"
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {
                    "method": "popup",
                    "minutes": 10
                }
            ] if alarm else []
        }
    }

    if schedule_type == "daily" and end_date:
        event["recurrence"] = [
            f"RRULE:FREQ=DAILY;UNTIL={recurrence_until(end_date)}"
        ]

    if schedule_type == "weekly" and end_date and day:
        day_codes = {
            "Monday": "MO",
            "Tuesday": "TU",
            "Wednesday": "WE",
            "Thursday": "TH",
            "Friday": "FR",
            "Saturday": "SA",
            "Sunday": "SU"
        }

        event["recurrence"] = [
            f"RRULE:FREQ=WEEKLY;BYDAY={day_codes[day]};UNTIL={recurrence_until(end_date)}"
        ]

    created_event = service.events().insert(
        calendarId="primary",
        body=event
    ).execute()

    return created_event.get("id")


def delete_google_event(event_id):
    if not event_id:
        return

    service = get_google_service()

    if not service:
        return

    try:
        service.events().delete(
            calendarId="primary",
            eventId=event_id
        ).execute()
    except Exception as error:
        print("Google Delete Error:", error)


def delete_google_occurrence(event_id, occurrence_date):
    service = get_google_service()

    if not service or not event_id:
        return

    try:
        start = datetime.datetime.strptime(
            occurrence_date,
            "%Y-%m-%d"
        )

        end = start + datetime.timedelta(days=1)
        instances = service.events().instances(
            calendarId="primary",
            eventId=event_id,
            timeMin=start.isoformat() + "Z",
            timeMax=end.isoformat() + "Z"
        ).execute()

        for instance in instances.get("items", []):
            instance_start = instance.get("start", {}).get("dateTime", "")

            if instance_start.startswith(occurrence_date):
                service.events().delete(
                    calendarId="primary",
                    eventId=instance["id"]
                ).execute()
                return

    except Exception as error:
        print("Google Occurrence Delete Error:", error)


def update_google_event(
    event_id,
    subject,
    topic,
    schedule_type,
    event_date,
    start_time,
    end_time,
    alarm,
    end_date=None,
    day=None
):
    if event_id:
        delete_google_event(event_id)

    return create_google_calendar_event(
        subject=subject,
        topic=topic,
        schedule_type=schedule_type,
        event_date=event_date,
        start_time=start_time,
        end_time=end_time,
        alarm=alarm,
        end_date=end_date,
        day=day
    )


def get_occurrences(session):
    schedule_type = session["schedule_type"]

    if schedule_type == "one-time":
        if session["date"]:
            return [session["date"]]
        return []

    if not session["start_date"] or not session["end_date"]:
        return []

    start = datetime.datetime.strptime(
        session["start_date"],
        "%Y-%m-%d"
    ).date()

    end = datetime.datetime.strptime(
        session["end_date"],
        "%Y-%m-%d"
    ).date()

    occurrences = []

    if schedule_type == "daily":
        current = start

        while current <= end:
            occurrences.append(
                current.strftime("%Y-%m-%d")
            )
            current += datetime.timedelta(days=1)

    elif schedule_type == "weekly":
        selected_weekday = DAYS.index(session["day"])

        days_until = (
            selected_weekday - start.weekday()
        ) % 7

        current = start + datetime.timedelta(
            days=days_until
        )

        while current <= end:
            occurrences.append(
                current.strftime("%Y-%m-%d")
            )
            current += datetime.timedelta(days=7)

    return occurrences


def get_visible_occurrences(connection, session):
    occurrences = get_occurrences(session)

    exceptions = connection.execute(
        """
        SELECT occurrence_date
        FROM timetable_exceptions
        WHERE timetable_id = ?
        """,
        (session["id"],)
    ).fetchall()

    exception_dates = {
        row["occurrence_date"]
        for row in exceptions
    }

    return [
        date
        for date in occurrences
        if date not in exception_dates
    ]


def expand_sessions(connection, sessions, date_from=None, date_to=None):
    """Turns timetable rows into (date, row) occurrences within an optional range."""
    occurrences = []
    for s in sessions:
        for d in get_visible_occurrences(connection, s):
            if date_from and d < date_from:
                continue
            if date_to and d > date_to:
                continue
            occurrences.append({"date": d, "session": s})
    occurrences.sort(key=lambda o: (o["date"], o["session"]["start_time"] or ""))
    return occurrences


def study_streak(connection):
    dates = {
        row["study_date"]
        for row in connection.execute("SELECT DISTINCT study_date FROM study_history").fetchall()
    }
    day = datetime.date.today()
    if day.strftime("%Y-%m-%d") not in dates:
        day -= datetime.timedelta(days=1)
    streak = 0
    while day.strftime("%Y-%m-%d") in dates:
        streak += 1
        day -= datetime.timedelta(days=1)
    return streak


@app.route("/")
def home():
    connection = get_database_connection()

    total_tasks = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    completed_tasks = connection.execute("SELECT COUNT(*) FROM tasks WHERE status = 'Completed'").fetchone()[0]
    pending_tasks = connection.execute("SELECT COUNT(*) FROM tasks WHERE status = 'Pending'").fetchone()[0]

    today_tasks = connection.execute(
        "SELECT * FROM tasks WHERE deadline = date('now', 'localtime') AND status = 'Pending'"
    ).fetchall()
    overdue_tasks = connection.execute(
        "SELECT * FROM tasks WHERE deadline < date('now', 'localtime') AND status = 'Pending'"
    ).fetchall()
    upcoming_tasks = connection.execute(
        "SELECT * FROM tasks WHERE status = 'Pending' ORDER BY deadline LIMIT 6"
    ).fetchall()
    subject_stats = connection.execute(
        """
        SELECT subject, COUNT(*) as total,
               SUM(CASE WHEN status = 'Completed' THEN 1 ELSE 0 END) as completed,
               SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) as pending
        FROM tasks GROUP BY subject
        """
    ).fetchall()

    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")
    week_end = (today + datetime.timedelta(days=6)).strftime("%Y-%m-%d")

    all_rows = connection.execute("SELECT * FROM timetable ORDER BY start_time").fetchall()
    pending_rows = [r for r in all_rows if r["status"] != "Completed"]
    today_sessions = [o["session"] for o in expand_sessions(connection, pending_rows, today_str, today_str)]
    done_today = [o["session"] for o in expand_sessions(
        connection, [r for r in all_rows if r["status"] == "Completed"], today_str, today_str)]
    upcoming = expand_sessions(connection, pending_rows,
                               (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d"), week_end)[:8]

    # Subject progress from the timetable (approved plans + manual sessions)
    progress_rows = connection.execute(
        """
        SELECT subject,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'Completed' THEN 1 ELSE 0 END) AS done
        FROM timetable
        WHERE schedule_type = 'one-time'
        GROUP BY subject
        ORDER BY total DESC
        """
    ).fetchall()
    colors = subject_color_map([r["subject"] for r in progress_rows])
    subject_progress = [
        {"subject": r["subject"], "total": r["total"], "done": r["done"] or 0,
         "percent": int(round(100.0 * (r["done"] or 0) / r["total"])) if r["total"] else 0,
         "color": colors.get(r["subject"], "#6366f1")}
        for r in progress_rows
    ][:8]

    hours_studied = round(connection.execute("SELECT SUM(actual_hours) FROM study_history").fetchone()[0] or 0.0, 1)
    total_topics = connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    completed_topics = connection.execute("SELECT COUNT(*) FROM topics WHERE status = 'Completed'").fetchone()[0]

    remaining_minutes = 0
    for occ in expand_sessions(connection, pending_rows, today_str):
        s = occ["session"]
        try:
            st = datetime.datetime.strptime(s["start_time"], "%H:%M")
            et = datetime.datetime.strptime(s["end_time"], "%H:%M")
            remaining_minutes += max(0, int((et - st).total_seconds() // 60))
        except (TypeError, ValueError):
            pass
    hours_remaining = round(remaining_minutes / 60.0, 1)

    # Last 7 days of logged study hours for the mini chart
    week_hours = []
    for i in range(6, -1, -1):
        d = today - datetime.timedelta(days=i)
        value = connection.execute(
            "SELECT SUM(actual_hours) FROM study_history WHERE study_date = ?", (d.strftime("%Y-%m-%d"),)
        ).fetchone()[0] or 0.0
        week_hours.append({"label": d.strftime("%a"), "hours": round(value, 1), "is_today": i == 0})
    max_week = max([w["hours"] for w in week_hours] + [1.0])

    exam = connection.execute(
        """
        SELECT deadline FROM generated_plans
        WHERE status = 'approved' AND deadline >= ?
        ORDER BY deadline LIMIT 1
        """,
        (today_str,)
    ).fetchone()
    exam_days = None
    exam_date = None
    if exam:
        exam_date = exam["deadline"]
        exam_days = (datetime.datetime.strptime(exam_date, "%Y-%m-%d").date() - today).days

    streak = study_streak(connection)
    latest_draft = connection.execute(
        "SELECT id, created_at FROM generated_plans WHERE status = 'draft' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    connection.close()

    progress = int((completed_tasks / total_tasks) * 100) if total_tasks else 0

    return render_template(
        "index.html",
        total_tasks=total_tasks,
        completed_tasks=completed_tasks,
        pending_tasks=pending_tasks,
        progress=progress,
        today_tasks=today_tasks,
        overdue_tasks=overdue_tasks,
        upcoming_tasks=upcoming_tasks,
        subject_stats=subject_stats,
        today_sessions=today_sessions,
        done_today=done_today,
        upcoming=upcoming,
        subject_progress=subject_progress,
        colors=colors,
        hours_studied=hours_studied,
        hours_remaining=hours_remaining,
        total_topics=total_topics,
        completed_topics=completed_topics,
        week_hours=week_hours,
        max_week=max_week,
        exam_days=exam_days,
        exam_date=exam_date,
        streak=streak,
        latest_draft=latest_draft,
        today=today_str,
        now_label=datetime.datetime.now().strftime("%A, %d %B"),
        hour=datetime.datetime.now().hour,
    )


@app.route("/add-task", methods=["GET", "POST"])
def add_task():
    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        task = request.form.get("task", "").strip()
        deadline = request.form.get("deadline")
        priority = request.form.get("priority")

        connection = get_database_connection()

        connection.execute(
            """
            INSERT INTO tasks
            (subject, task, deadline, priority, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                subject,
                task,
                deadline,
                priority,
                "Pending"
            )
        )

        connection.commit()
        connection.close()

        return redirect(url_for("tasks"))

    return render_template("add_task.html")


@app.route("/tasks")
def tasks():
    connection = get_database_connection()

    tasks_data = connection.execute(
        """
        SELECT *
        FROM tasks
        ORDER BY deadline
        """
    ).fetchall()

    connection.close()

    return render_template(
        "tasks.html",
        tasks=tasks_data
    )


@app.route("/complete/<int:task_id>", methods=["POST"])
def complete_task(task_id):
    connection = get_database_connection()

    connection.execute(
        """
        UPDATE tasks
        SET status = ?
        WHERE id = ?
        """,
        ("Completed", task_id)
    )

    connection.commit()
    connection.close()

    return redirect(url_for("tasks"))


@app.route("/delete/<int:task_id>", methods=["POST"])
def delete_task(task_id):
    connection = get_database_connection()

    connection.execute(
        """
        DELETE FROM tasks
        WHERE id = ?
        """,
        (task_id,)
    )

    connection.commit()
    connection.close()

    return redirect(url_for("tasks"))


@app.route("/timetable")
def timetable():
    connection = get_database_connection()

    sessions = connection.execute(
        """
        SELECT *
        FROM timetable
        ORDER BY
            CASE schedule_type
                WHEN 'one-time' THEN 1
                WHEN 'daily' THEN 2
                WHEN 'weekly' THEN 3
                ELSE 4
            END,
            start_date,
            date,
            start_time
        """
    ).fetchall()

    timetable_data = []
    for session in sessions:
        visible_dates = get_visible_occurrences(connection, session)
        if visible_dates:
            timetable_data.append({"session": session, "visible_dates": visible_dates})

    today = datetime.date.today()
    today_string = today.strftime("%Y-%m-%d")

    today_sessions = [item["session"] for item in timetable_data if today_string in item["visible_dates"]]
    today_sessions.sort(key=lambda s: s["start_time"] or "")

    # Agenda: one-time sessions grouped by date
    upcoming_days, past_days = [], []
    one_time = [item["session"] for item in timetable_data if item["session"]["schedule_type"] == "one-time"]
    one_time.sort(key=lambda s: (s["date"] or "", s["start_time"] or ""))
    for s in one_time:
        target = upcoming_days if (s["date"] or "") >= today_string else past_days
        if not target or target[-1]["date"] != s["date"]:
            date_obj = datetime.datetime.strptime(s["date"], "%Y-%m-%d").date() if s["date"] else today
            target.append({
                "date": s["date"],
                "label": date_obj.strftime("%a, %d %b"),
                "is_today": s["date"] == today_string,
                "relative": ("Today" if s["date"] == today_string else
                             "Tomorrow" if date_obj == today + datetime.timedelta(days=1) else
                             f"In {(date_obj - today).days} days" if date_obj > today else ""),
                "sessions": [],
            })
        target[-1]["sessions"].append(s)
    past_days.reverse()

    recurring = [item for item in timetable_data if item["session"]["schedule_type"] in ("daily", "weekly")]
    colors = subject_color_map(sorted({item["session"]["subject"] for item in timetable_data}))
    total_upcoming = sum(len(d["sessions"]) for d in upcoming_days)
    subject_counts = {}
    for d in upcoming_days:
        for sess in d["sessions"]:
            subject_counts[sess["subject"]] = subject_counts.get(sess["subject"], 0) + 1

    connection.close()

    return render_template(
        "timetable.html",
        timetable=timetable_data,
        today_sessions=today_sessions,
        upcoming_days=upcoming_days,
        past_days=past_days,
        recurring=recurring,
        colors=colors,
        total_upcoming=total_upcoming,
        subject_counts=sorted(subject_counts.items()),
        today=today_string,
        days=DAYS
    )


@app.route("/timetable/delete-subject", methods=["POST"])
def delete_subject_sessions():
    """Removes every one-time session of a subject (and its Google Calendar events)."""
    subject = request.form.get("subject", "").strip()
    scope = request.form.get("scope", "upcoming")
    if not subject:
        return redirect(url_for("timetable"))
    connection = get_database_connection()
    query = "SELECT id, google_event_id FROM timetable WHERE subject = ? AND schedule_type = 'one-time'"
    params = [subject]
    if scope == "upcoming":
        query += " AND date >= ? AND status != 'Completed'"
        params.append(datetime.date.today().strftime("%Y-%m-%d"))
    rows = connection.execute(query, params).fetchall()
    for row in rows:
        connection.execute("DELETE FROM timetable_exceptions WHERE timetable_id = ?", (row["id"],))
        connection.execute("DELETE FROM timetable WHERE id = ?", (row["id"],))
    connection.commit()
    connection.close()
    for row in rows:
        delete_google_event(row["google_event_id"])
    flash(f"Removed {len(rows)} session(s) of '{subject}'.")
    return redirect(url_for("timetable"))


@app.route("/add-timetable", methods=["GET", "POST"])
def add_timetable():
    if request.method == "POST":
        subject = request.form.get(
            "subject",
            ""
        ).strip()

        topic = request.form.get(
            "topic",
            ""
        ).strip()

        schedule_type = request.form.get(
            "schedule_type",
            "one-time"
        )

        event_date = request.form.get("date")

        daily_start_date = request.form.get(
            "daily_start_date"
        )

        daily_end_date = request.form.get(
            "daily_end_date"
        )

        weekly_start_date = request.form.get(
            "weekly_start_date"
        )

        weekly_end_date = request.form.get(
            "weekly_end_date"
        )

        day = request.form.get("day")

        start_time = request.form.get("start_time")
        end_time = request.form.get("end_time")

        alarm = 1 if "alarm" in request.form else 0

        if not subject or not topic or not start_time or not end_time:
            return redirect(url_for("add_timetable"))

        if schedule_type == "one-time":
            if not event_date:
                return redirect(url_for("add_timetable"))

            selected_date = datetime.datetime.strptime(
                event_date,
                "%Y-%m-%d"
            ).date()

            date_value = selected_date.strftime("%Y-%m-%d")
            start_date = date_value
            end_date = date_value
            database_day = selected_date.strftime("%A")

            google_start_date = date_value
            google_end_date = None

        elif schedule_type == "daily":
            if not daily_start_date or not daily_end_date:
                return redirect(url_for("add_timetable"))

            start = datetime.datetime.strptime(
                daily_start_date,
                "%Y-%m-%d"
            ).date()

            end = datetime.datetime.strptime(
                daily_end_date,
                "%Y-%m-%d"
            ).date()

            if start > end:
                return redirect(url_for("add_timetable"))

            date_value = start.strftime("%Y-%m-%d")
            start_date = start.strftime("%Y-%m-%d")
            end_date = end.strftime("%Y-%m-%d")
            database_day = "Every Day"

            google_start_date = start.strftime("%Y-%m-%d")
            google_end_date = end.strftime("%Y-%m-%d")

        elif schedule_type == "weekly":
            if not weekly_start_date or not weekly_end_date or not day:
                return redirect(url_for("add_timetable"))

            start = datetime.datetime.strptime(
                weekly_start_date,
                "%Y-%m-%d"
            ).date()

            end = datetime.datetime.strptime(
                weekly_end_date,
                "%Y-%m-%d"
            ).date()

            if start > end or day not in DAYS:
                return redirect(url_for("add_timetable"))

            selected_weekday = DAYS.index(day)

            days_until = (
                selected_weekday - start.weekday()
            ) % 7

            first_session = start + datetime.timedelta(
                days=days_until
            )

            if first_session > end:
                return redirect(url_for("add_timetable"))

            date_value = first_session.strftime("%Y-%m-%d")
            start_date = start.strftime("%Y-%m-%d")
            end_date = end.strftime("%Y-%m-%d")
            database_day = day

            google_start_date = first_session.strftime(
                "%Y-%m-%d"
            )

            google_end_date = end.strftime("%Y-%m-%d")

        else:
            return redirect(url_for("add_timetable"))

        connection = get_database_connection()

        cursor = connection.execute(
            """
            INSERT INTO timetable
            (
                subject,
                topic,
                day,
                date,
                start_time,
                end_time,
                alarm,
                google_event_id,
                schedule_type,
                start_date,
                end_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject,
                topic,
                database_day,
                date_value,
                start_time,
                end_time,
                alarm,
                None,
                schedule_type,
                start_date,
                end_date
            )
        )

        timetable_id = cursor.lastrowid

        connection.commit()
        connection.close()

        try:
            google_event_id = create_google_calendar_event(
                subject=subject,
                topic=topic,
                schedule_type=schedule_type,
                event_date=google_start_date,
                start_time=start_time,
                end_time=end_time,
                alarm=alarm,
                end_date=google_end_date,
                day=day
            )

            if google_event_id:
                connection = get_database_connection()

                connection.execute(
                    """
                    UPDATE timetable
                    SET google_event_id = ?
                    WHERE id = ?
                    """,
                    (
                        google_event_id,
                        timetable_id
                    )
                )

                connection.commit()
                connection.close()

        except Exception as error:
            print("Google Calendar Error:", error)

        return redirect(url_for("timetable"))

    return render_template("add_timetable.html")


@app.route("/edit-timetable/<int:timetable_id>", methods=["GET", "POST"])
def edit_timetable(timetable_id):
    connection = get_database_connection()

    session = connection.execute(
        """
        SELECT *
        FROM timetable
        WHERE id = ?
        """,
        (timetable_id,)
    ).fetchone()

    connection.close()

    if not session:
        return redirect(url_for("timetable"))

    if request.method == "POST":
        subject = request.form.get(
            "subject",
            ""
        ).strip()

        topic = request.form.get(
            "topic",
            ""
        ).strip()

        schedule_type = request.form.get(
            "schedule_type",
            "one-time"
        )

        start_time = request.form.get("start_time")
        end_time = request.form.get("end_time")

        alarm = 1 if "alarm" in request.form else 0

        if not subject or not topic or not start_time or not end_time:
            return redirect(
                url_for(
                    "edit_timetable",
                    timetable_id=timetable_id
                )
            )

        if schedule_type == "one-time":
            event_date = request.form.get("date")

            if not event_date:
                return redirect(
                    url_for(
                        "edit_timetable",
                        timetable_id=timetable_id
                    )
                )

            selected_date = datetime.datetime.strptime(
                event_date,
                "%Y-%m-%d"
            ).date()

            day = selected_date.strftime("%A")
            date_value = event_date
            start_date = event_date
            end_date = event_date
            google_start_date = event_date
            google_end_date = None

        elif schedule_type == "daily":
            start_date = request.form.get(
                "daily_start_date"
            )

            end_date = request.form.get(
                "daily_end_date"
            )

            if not start_date or not end_date:
                return redirect(
                    url_for(
                        "edit_timetable",
                        timetable_id=timetable_id
                    )
                )

            start = datetime.datetime.strptime(
                start_date,
                "%Y-%m-%d"
            ).date()

            end = datetime.datetime.strptime(
                end_date,
                "%Y-%m-%d"
            ).date()

            if start > end:
                return redirect(
                    url_for(
                        "edit_timetable",
                        timetable_id=timetable_id
                    )
                )

            day = "Every Day"
            date_value = start_date
            google_start_date = start_date
            google_end_date = end_date

        elif schedule_type == "weekly":
            day = request.form.get("day")

            start_date = request.form.get(
                "weekly_start_date"
            )

            end_date = request.form.get(
                "weekly_end_date"
            )

            if not day or not start_date or not end_date:
                return redirect(
                    url_for(
                        "edit_timetable",
                        timetable_id=timetable_id
                    )
                )

            start = datetime.datetime.strptime(
                start_date,
                "%Y-%m-%d"
            ).date()

            end = datetime.datetime.strptime(
                end_date,
                "%Y-%m-%d"
            ).date()

            if start > end or day not in DAYS:
                return redirect(
                    url_for(
                        "edit_timetable",
                        timetable_id=timetable_id
                    )
                )

            selected_weekday = DAYS.index(day)

            days_until = (
                selected_weekday - start.weekday()
            ) % 7

            first_session = start + datetime.timedelta(
                days=days_until
            )

            if first_session > end:
                return redirect(
                    url_for(
                        "edit_timetable",
                        timetable_id=timetable_id
                    )
                )

            date_value = first_session.strftime(
                "%Y-%m-%d"
            )

            google_start_date = date_value
            google_end_date = end_date

        else:
            return redirect(
                url_for(
                    "edit_timetable",
                    timetable_id=timetable_id
                )
            )

        old_google_event_id = session["google_event_id"]

        connection = get_database_connection()

        connection.execute(
            """
            UPDATE timetable
            SET
                subject = ?,
                topic = ?,
                schedule_type = ?,
                day = ?,
                date = ?,
                start_time = ?,
                end_time = ?,
                alarm = ?,
                start_date = ?,
                end_date = ?
            WHERE id = ?
            """,
            (
                subject,
                topic,
                schedule_type,
                day,
                date_value,
                start_time,
                end_time,
                alarm,
                start_date,
                end_date,
                timetable_id
            )
        )

        connection.execute(
            """
            DELETE FROM timetable_exceptions
            WHERE timetable_id = ?
            """,
            (timetable_id,)
        )

        connection.commit()
        connection.close()

        try:
            new_google_event_id = update_google_event(
                event_id=old_google_event_id,
                subject=subject,
                topic=topic,
                schedule_type=schedule_type,
                event_date=google_start_date,
                start_time=start_time,
                end_time=end_time,
                alarm=alarm,
                end_date=google_end_date,
                day=day if schedule_type == "weekly" else None
            )

            connection = get_database_connection()

            connection.execute(
                """
                UPDATE timetable
                SET google_event_id = ?
                WHERE id = ?
                """,
                (
                    new_google_event_id,
                    timetable_id
                )
            )

            connection.commit()
            connection.close()

        except Exception as error:
            print("Google Calendar Update Error:", error)

        return redirect(url_for("timetable"))

    return render_template(
        "edit_timetable.html",
        session=session
    )


@app.route("/delete-timetable/<int:timetable_id>", methods=["POST"])
def delete_timetable(timetable_id):
    mode = request.form.get(
        "delete_mode",
        "all"
    )

    occurrence_date = request.form.get(
        "occurrence_date"
    )

    connection = get_database_connection()

    session = connection.execute(
        """
        SELECT *
        FROM timetable
        WHERE id = ?
        """,
        (timetable_id,)
    ).fetchone()

    if not session:
        connection.close()
        return redirect(url_for("timetable"))

    schedule_type = session["schedule_type"]
    google_event_id = session["google_event_id"]

    if schedule_type == "one-time" or mode == "all":
        connection.execute(
            """
            DELETE FROM timetable_exceptions
            WHERE timetable_id = ?
            """,
            (timetable_id,)
        )

        connection.execute(
            """
            DELETE FROM timetable
            WHERE id = ?
            """,
            (timetable_id,)
        )

        connection.commit()
        connection.close()

        delete_google_event(google_event_id)

        return redirect(url_for("timetable"))

    if not occurrence_date:
        connection.close()
        return redirect(url_for("timetable"))

    if mode == "this":
        connection.execute(
            """
            INSERT OR IGNORE INTO timetable_exceptions
            (timetable_id, occurrence_date)
            VALUES (?, ?)
            """,
            (
                timetable_id,
                occurrence_date
            )
        )

        connection.commit()
        connection.close()

        delete_google_occurrence(
            google_event_id,
            occurrence_date
        )

        return redirect(url_for("timetable"))

    if mode == "following":
        selected_date = datetime.datetime.strptime(
            occurrence_date,
            "%Y-%m-%d"
        ).date()

        old_start = datetime.datetime.strptime(
            session["start_date"],
            "%Y-%m-%d"
        ).date()

        if selected_date <= old_start:
            connection.close()
            delete_google_event(google_event_id)

            connection = get_database_connection()

            connection.execute(
                """
                DELETE FROM timetable_exceptions
                WHERE timetable_id = ?
                """,
                (timetable_id,)
            )

            connection.execute(
                """
                DELETE FROM timetable
                WHERE id = ?
                """,
                (timetable_id,)
            )

            connection.commit()
            connection.close()

            return redirect(url_for("timetable"))

        new_end_date = (
            selected_date -
            datetime.timedelta(days=1)
        ).strftime("%Y-%m-%d")

        connection.execute(
            """
            UPDATE timetable
            SET end_date = ?
            WHERE id = ?
            """,
            (
                new_end_date,
                timetable_id
            )
        )

        connection.commit()
        connection.close()

        try:
            if google_event_id:
                service = get_google_service()

                if service:
                    event = service.events().get(
                        calendarId="primary",
                        eventId=google_event_id
                    ).execute()

                    event["recurrence"] = []

                    if schedule_type == "daily":
                        event["recurrence"] = [
                            f"RRULE:FREQ=DAILY;UNTIL={recurrence_until(new_end_date)}"
                        ]

                    elif schedule_type == "weekly":
                        day_codes = {
                            "Monday": "MO",
                            "Tuesday": "TU",
                            "Wednesday": "WE",
                            "Thursday": "TH",
                            "Friday": "FR",
                            "Saturday": "SA",
                            "Sunday": "SU"
                        }

                        event["recurrence"] = [
                            f"RRULE:FREQ=WEEKLY;BYDAY={day_codes[session['day']]};UNTIL={recurrence_until(new_end_date)}"
                        ]

                    service.events().update(
                        calendarId="primary",
                        eventId=google_event_id,
                        body=event
                    ).execute()

        except Exception as error:
            print("Google Following Delete Error:", error)

        return redirect(url_for("timetable"))

    connection.close()
    return redirect(url_for("timetable"))

# =============================================================================
# SYLLABUS UPLOAD -> BACKGROUND PROCESSING -> PLAN PREVIEW -> APPROVAL
# =============================================================================

PROCESSING_JOBS = {}
PROCESSING_LOCK = threading.Lock()

SUBJECT_COLORS = [
    "#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
    "#ec4899", "#14b8a6", "#f97316", "#84cc16",
]


def subject_color_map(names=None):
    """Stable colour per subject across every page (ordered by first appearance)."""
    ordered = []
    try:
        connection = get_database_connection()
        rows = connection.execute(
            """
            SELECT name FROM (
                SELECT subject AS name, MIN(id) AS first FROM timetable GROUP BY subject
                UNION ALL
                SELECT name, MIN(id) + 1000000 AS first FROM subjects GROUP BY name
            ) GROUP BY name ORDER BY MIN(first)
            """
        ).fetchall()
        connection.close()
        ordered = [r["name"] for r in rows]
    except Exception:
        pass
    for name in names or []:
        if name not in ordered:
            ordered.append(name)
    return {name: SUBJECT_COLORS[i % len(SUBJECT_COLORS)] for i, name in enumerate(ordered)}


def _parse_subtopics(value):
    if not value:
        return []
    if value.strip().startswith("["):
        try:
            return [s for s in json.loads(value) if s]
        except ValueError:
            pass
    return [s.strip() for s in value.split(",") if s.strip()]


def load_subjects_for_syllabus(connection, syllabus_id, include=None):
    """Rebuilds {subject: {code, is_lab, topics:[...]}} from the database (in syllabus order)."""
    subjects = {}
    for subj in connection.execute(
        "SELECT * FROM subjects WHERE syllabus_id = ? ORDER BY id", (syllabus_id,)
    ).fetchall():
        if include is not None and subj["name"] not in include:
            continue
        topics = []
        for t in connection.execute(
            "SELECT * FROM topics WHERE subject_id = ? ORDER BY id", (subj["id"],)
        ).fetchall():
            keys = t.keys()
            subs = _parse_subtopics(t["subtopics"])
            topics.append({
                "topic_name": t["topic_name"],
                "unit_name": t["unit_name"] or "Topics",
                "subtopics": subs,
                "subtopic_count": len(subs),
                "topic_length": len(t["topic_name"]) + sum(len(s) for s in subs),
                "difficulty_score": t["difficulty"],
                "syllabus_hours": t["syllabus_hours"] if "syllabus_hours" in keys else None,
                "description": t["description"] if "description" in keys else "",
                "predicted_hours": t["predicted_hours"],
                "preferred_period": t["preferred_period"],
                "status": t["status"],
            })
        subjects[subj["name"]] = {
            "code": subj["code"],
            "is_lab": bool(subj["is_lab"]) if "is_lab" in subj.keys() else False,
            "contact_hours": subj["contact_hours"] if "contact_hours" in subj.keys() else None,
            "total_topics": len(topics),
            "topics": topics,
        }
    return subjects


def save_syllabus_and_plan(processing_result, safe_filename, extension, deadline, study_hours):
    """Stores subjects/topics, generates the plan and returns the new plan id."""
    extracted_subjects = processing_result["subjects"]
    raw_text = processing_result.get("raw_text", "")

    connection = get_database_connection()
    cursor = connection.cursor()
    first_subject = list(extracted_subjects.keys())[0] if len(extracted_subjects) == 1 else "Multi-Subject Syllabus"
    cursor.execute(
        """
        INSERT INTO syllabuses
        (subject, filename, file_type, deadline, daily_hours, extracted_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (first_subject, safe_filename, extension, deadline, study_hours,
         raw_text[:20000], datetime.datetime.now().isoformat())
    )
    syllabus_id = cursor.lastrowid

    for subj_name, subj_info in extracted_subjects.items():
        cursor.execute(
            """
            INSERT INTO subjects (syllabus_id, name, code, total_topics, created_at, is_lab, contact_hours)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (syllabus_id, subj_name, subj_info.get("code", ""),
             subj_info.get("total_topics", len(subj_info.get("topics", []))),
             datetime.datetime.now().isoformat(),
             1 if subj_info.get("is_lab") else 0, subj_info.get("contact_hours"))
        )
        subject_id = cursor.lastrowid

        for topic in subj_info.get("topics", []):
            t_diff = topic.get("difficulty_score", 2.0)
            t_pred_hours = predict_topic_hours(topic.get("topic_length", 30), topic.get("subtopic_count", 0), t_diff)
            t_period = predict_study_period(t_diff, t_pred_hours)
            cursor.execute(
                """
                INSERT INTO topics
                (subject_id, syllabus_id, unit_name, topic_name, subtopics, difficulty,
                 predicted_hours, preferred_period, status, description, syllabus_hours, module_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (subject_id, syllabus_id, topic.get("unit_name", "Topics"), topic.get("topic_name", ""),
                 json.dumps(topic.get("subtopics", [])), t_diff, t_pred_hours, t_period, "Pending",
                 topic.get("description", ""), topic.get("syllabus_hours"), topic.get("module_number"))
            )
    connection.commit()

    subjects = load_subjects_for_syllabus(connection, syllabus_id)
    plan_data = generate_study_schedule(
        subjects_dict=subjects,
        deadline_date_str=deadline,
        daily_available_hours=study_hours,
        connection=connection
    )
    plan_data["options"] = {
        "included_subjects": list(subjects.keys()),
        "skipped_subjects": processing_result.get("skipped_subjects", []),
        "pages": processing_result.get("pages", 0),
        "ocr_pages": processing_result.get("ocr_pages", 0),
        "warnings": processing_result.get("warnings", []),
    }
    for topic in plan_data.get("topics_analysis", []):
        connection.execute(
            """
            UPDATE topics SET predicted_hours = ?, preferred_period = ?
            WHERE syllabus_id = ? AND topic_name = ?
              AND subject_id IN (SELECT id FROM subjects WHERE syllabus_id = ? AND name = ?)
            """,
            (topic["needed_hours"], topic["preferred_period"], syllabus_id, topic["topic"],
             syllabus_id, topic["subject"])
        )

    cursor.execute(
        """
        INSERT INTO generated_plans
        (syllabus_id, plan_data, deadline, daily_hours, total_hours, feasibility, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (syllabus_id, json.dumps(plan_data), deadline, study_hours,
         plan_data.get("total_predicted_hours", 0.0),
         "Feasible" if plan_data.get("validation", {}).get("is_feasible") else "Warning",
         "draft", datetime.datetime.now().isoformat())
    )
    plan_id = cursor.lastrowid
    connection.commit()
    connection.close()
    return plan_id


def _run_processing_job(job_id, file_path, extension, safe_filename, deadline, study_hours):
    def progress(done, total, message):
        with PROCESSING_LOCK:
            job = PROCESSING_JOBS[job_id]
            job["done"], job["total"], job["message"] = done, total, message

    try:
        result = process_syllabus(file_path, extension, progress_callback=progress)
        if not result.get("success") or not result.get("subjects"):
            raise ValueError(result.get("error") or "No usable curriculum content was detected.")
        with PROCESSING_LOCK:
            PROCESSING_JOBS[job_id]["message"] = "Building your study plan..."
        plan_id = save_syllabus_and_plan(result, safe_filename, extension, deadline, study_hours)
        with PROCESSING_LOCK:
            PROCESSING_JOBS[job_id].update({"status": "done", "plan_id": plan_id, "message": "Done"})
    except Exception as error:
        print("Syllabus job error:", error)
        with PROCESSING_LOCK:
            PROCESSING_JOBS[job_id].update({"status": "error", "error": str(error)})


@app.route("/add-syllabus", methods=["GET", "POST"])
def add_syllabus():
    if request.method == "POST":
        deadline = request.form.get("deadline")
        study_hours_raw = request.form.get("study_hours", "3")
        file = request.files.get("syllabus")

        if not deadline or not study_hours_raw or not file:
            return render_template("add_syllabus.html", error_msg="Please provide a deadline, daily study hours, and a valid syllabus file.")

        try:
            datetime.datetime.strptime(deadline, "%Y-%m-%d")
        except ValueError:
            return render_template("add_syllabus.html", error_msg="Please choose a valid deadline date.")

        try:
            study_hours = float(study_hours_raw)
            if study_hours < 0.5:
                study_hours = 3.0
        except ValueError:
            study_hours = 3.0

        filename = secure_filename(file.filename or "")
        if not filename:
            return render_template("add_syllabus.html", error_msg="No file was selected.")

        extension = filename.rsplit(".", 1)[-1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            return render_template("add_syllabus.html", error_msg=f"Unsupported file type '{extension}'. Please upload a PDF or image (.png, .jpg, .jpeg).")

        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        safe_filename = datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "_" + filename
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], safe_filename)
        file.save(file_path)

        # Scanned syllabi (OCR) run in the background with a live progress page
        if needs_background_processing(file_path, extension):
            job_id = uuid.uuid4().hex[:12]
            with PROCESSING_LOCK:
                PROCESSING_JOBS[job_id] = {
                    "status": "running", "done": 0, "total": 0, "message": "Starting...",
                    "plan_id": None, "error": None, "filename": filename,
                    "started": datetime.datetime.now().isoformat(),
                }
            worker = threading.Thread(
                target=_run_processing_job,
                args=(job_id, file_path, extension, safe_filename, deadline, study_hours),
                daemon=True,
            )
            worker.start()
            return redirect(url_for("processing_status_page", job_id=job_id))

        processing_result = process_syllabus(file_path, extension)
        if not processing_result.get("success") or not processing_result.get("subjects"):
            try:
                os.remove(file_path)
            except OSError:
                pass
            err = processing_result.get("error") or "No usable curriculum content was detected."
            return render_template("add_syllabus.html", error_msg=err)

        plan_id = save_syllabus_and_plan(processing_result, safe_filename, extension, deadline, study_hours)
        return redirect(url_for("plan_preview", plan_id=plan_id))

    connection = get_database_connection()
    from scheduler import get_student_availability, calculate_student_capacity
    capacity = calculate_student_capacity(get_student_availability(connection))
    connection.close()
    return render_template("add_syllabus.html", capacity=capacity)


@app.route("/add-syllabus/sample", methods=["POST"])
def add_sample_syllabus():
    """Builds a plan from the bundled sample syllabus (BCA Semester V)."""
    if not os.path.exists(SAMPLE_SYLLABUS):
        flash("The sample syllabus is not available on this server.")
        return redirect(url_for("add_syllabus"))
    deadline = request.form.get("deadline") or (datetime.date.today() + datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    try:
        study_hours = max(0.5, float(request.form.get("study_hours") or 3))
    except ValueError:
        study_hours = 3.0
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    safe_filename = datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "_sample_5th_sem_syllabus.pdf"
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], safe_filename)
    import shutil
    shutil.copyfile(SAMPLE_SYLLABUS, file_path)

    if needs_background_processing(file_path, "pdf"):
        job_id = uuid.uuid4().hex[:12]
        with PROCESSING_LOCK:
            PROCESSING_JOBS[job_id] = {
                "status": "running", "done": 0, "total": 0, "message": "Starting...",
                "plan_id": None, "error": None, "filename": "Sample: BCA Semester V syllabus",
                "started": datetime.datetime.now().isoformat(),
            }
        threading.Thread(target=_run_processing_job,
                         args=(job_id, file_path, "pdf", safe_filename, deadline, study_hours),
                         daemon=True).start()
        return redirect(url_for("processing_status_page", job_id=job_id))

    result = process_syllabus(file_path, "pdf")
    if not result.get("success"):
        return render_template("add_syllabus.html", error_msg=result.get("error"))
    plan_id = save_syllabus_and_plan(result, safe_filename, "pdf", deadline, study_hours)
    return redirect(url_for("plan_preview", plan_id=plan_id))


@app.route("/processing/<job_id>")
def processing_status_page(job_id):
    with PROCESSING_LOCK:
        job = dict(PROCESSING_JOBS.get(job_id) or {})
    if not job:
        flash("That processing job was not found. Please upload the syllabus again.")
        return redirect(url_for("add_syllabus"))
    return render_template("processing.html", job_id=job_id, job=job)


@app.route("/api/processing/<job_id>")
def processing_status_api(job_id):
    with PROCESSING_LOCK:
        job = dict(PROCESSING_JOBS.get(job_id) or {})
    if not job:
        return jsonify({"status": "missing"}), 404
    if job.get("status") == "done":
        job["redirect"] = url_for("plan_preview", plan_id=job["plan_id"])
    return jsonify(job)


def build_preview_tree(connection, syllabus_id, plan_data):
    """subject -> modules -> topics with needed / allocated hours for the preview page."""
    analysis = {(t["subject"], t["topic"]): t for t in plan_data.get("topics_analysis", [])}
    included = set(plan_data.get("options", {}).get("included_subjects") or [])
    tree = []
    subjects = load_subjects_for_syllabus(connection, syllabus_id)
    for name, data in subjects.items():
        modules = []
        for t in data["topics"]:
            info = analysis.get((name, t["topic_name"]), {})
            if not modules or modules[-1]["name"] != t["unit_name"]:
                modules.append({"name": t["unit_name"], "topics": [], "needed": 0.0, "allocated": 0.0,
                                "syllabus": 0.0})
            module = modules[-1]
            topic = {
                "name": t["topic_name"],
                "subtopics": t["subtopics"],
                "difficulty": t["difficulty_score"],
                "syllabus_hours": t.get("syllabus_hours"),
                "needed": info.get("needed_hours", t.get("predicted_hours") or 0),
                "ml_hours": info.get("ml_hours"),
                "allocated": info.get("allocated_hours", 0.0),
                "sessions": info.get("sessions", 0),
                "first_date": info.get("first_date"),
                "last_date": info.get("last_date"),
                "period": info.get("preferred_period", t.get("preferred_period")),
            }
            module["topics"].append(topic)
            module["needed"] = round(module["needed"] + (topic["needed"] or 0), 2)
            module["allocated"] = round(module["allocated"] + (topic["allocated"] or 0), 2)
            module["syllabus"] = round(module["syllabus"] + (topic["syllabus_hours"] or 0), 2)
        tree.append({
            "name": name,
            "code": data["code"],
            "is_lab": data["is_lab"],
            "included": (not included) or name in included,
            "modules": modules,
            "topic_count": len(data["topics"]),
            "needed": round(sum(m["needed"] for m in modules), 1),
            "allocated": round(sum(m["allocated"] for m in modules), 1),
        })
    return tree


@app.route("/plan-preview/<int:plan_id>")
def plan_preview(plan_id):
    connection = get_database_connection()
    plan_row = connection.execute("SELECT * FROM generated_plans WHERE id = ?", (plan_id,)).fetchone()

    if not plan_row:
        connection.close()
        return redirect(url_for("home"))

    if plan_row["status"] == "approved":
        connection.close()
        flash("This study plan has already been approved.")
        return redirect(url_for("timetable"))

    plan_data = json.loads(plan_row["plan_data"])
    tree = build_preview_tree(connection, plan_row["syllabus_id"], plan_data)
    connection.close()

    sessions = plan_data.get("scheduled_sessions", [])
    days = []
    for s in sessions:
        if not days or days[-1]["date"] != s["date"]:
            days.append({"date": s["date"], "day": s["day"], "sessions": [], "hours": 0.0})
        days[-1]["sessions"].append(s)
        days[-1]["hours"] = round(days[-1]["hours"] + s["duration_hours"], 2)
    weeks = []
    for d in days:
        date_obj = datetime.datetime.strptime(d["date"], "%Y-%m-%d").date()
        week_start = (date_obj - datetime.timedelta(days=date_obj.weekday())).strftime("%Y-%m-%d")
        if not weeks or weeks[-1]["start"] != week_start:
            weeks.append({"start": week_start, "days": [], "hours": 0.0})
        weeks[-1]["days"].append(d)
        weeks[-1]["hours"] = round(weeks[-1]["hours"] + d["hours"], 1)

    colors = subject_color_map([t["name"] for t in tree])
    subjects_dict = {t["name"]: {"code": t["code"], "total_topics": t["topic_count"]} for t in tree}

    return render_template(
        "plan_preview.html",
        plan_id=plan_id,
        plan=plan_row,
        plan_data=plan_data,
        subjects=subjects_dict,
        tree=tree,
        weeks=weeks,
        colors=colors,
        today=datetime.date.today().strftime("%Y-%m-%d"),
    )


@app.route("/plan-preview/<int:plan_id>/regenerate", methods=["POST"])
def regenerate_plan(plan_id):
    connection = get_database_connection()
    plan_row = connection.execute("SELECT * FROM generated_plans WHERE id = ?", (plan_id,)).fetchone()
    if not plan_row or plan_row["status"] == "approved":
        connection.close()
        return redirect(url_for("plan_preview", plan_id=plan_id))

    deadline = request.form.get("deadline") or plan_row["deadline"]
    try:
        datetime.datetime.strptime(deadline, "%Y-%m-%d")
    except ValueError:
        deadline = plan_row["deadline"]
    try:
        study_hours = max(0.5, float(request.form.get("study_hours") or plan_row["daily_hours"]))
    except ValueError:
        study_hours = plan_row["daily_hours"]
    include = request.form.getlist("include")

    old_data = json.loads(plan_row["plan_data"])
    subjects = load_subjects_for_syllabus(connection, plan_row["syllabus_id"], include=set(include) or None)
    if not subjects:
        connection.close()
        flash("Select at least one subject to plan.")
        return redirect(url_for("plan_preview", plan_id=plan_id))

    plan_data = generate_study_schedule(
        subjects_dict=subjects,
        deadline_date_str=deadline,
        daily_available_hours=study_hours,
        connection=connection
    )
    options = old_data.get("options", {})
    options["included_subjects"] = list(subjects.keys())
    plan_data["options"] = options

    connection.execute(
        """
        UPDATE generated_plans
        SET plan_data = ?, deadline = ?, daily_hours = ?, total_hours = ?, feasibility = ?
        WHERE id = ?
        """,
        (json.dumps(plan_data), deadline, study_hours, plan_data.get("total_predicted_hours", 0.0),
         "Feasible" if plan_data.get("validation", {}).get("is_feasible") else "Warning", plan_id)
    )
    connection.commit()
    connection.close()
    flash("Plan updated with your new settings.")
    return redirect(url_for("plan_preview", plan_id=plan_id))


@app.route("/approve-plan/<int:plan_id>", methods=["POST"])
def approve_plan(plan_id):
    connection = get_database_connection()

    plan_row = connection.execute(
        "SELECT * FROM generated_plans WHERE id = ?",
        (plan_id,)
    ).fetchone()

    if not plan_row:
        connection.close()
        return redirect(url_for("home"))

    if plan_row["status"] == "approved":
        connection.close()
        flash("This study plan has already been approved.")
        return redirect(url_for("timetable"))

    plan_data = json.loads(plan_row["plan_data"])
    checks = plan_data.get("checks", {})
    if checks and not checks.get("passed", True):
        connection.close()
        flash("This plan did not pass validation. Please regenerate it before approving.")
        return redirect(url_for("plan_preview", plan_id=plan_id))

    sessions = plan_data.get("scheduled_sessions", [])

    for session in sessions:
        subj = session["subject"]
        topic = session["topic"]
        day_name = session["day"]
        date_str = session["date"]
        start_time = session["start_time"]
        end_time = session["end_time"]
        alarm = session.get("alarm", 1)
        notes = "; ".join(session.get("covers", []))[:1000]

        cursor = connection.execute(
            """
            INSERT INTO timetable
            (
                subject,
                topic,
                day,
                date,
                start_time,
                end_time,
                alarm,
                google_event_id,
                schedule_type,
                start_date,
                end_date,
                status,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subj,
                topic,
                day_name,
                date_str,
                start_time,
                end_time,
                alarm,
                None,
                "one-time",
                date_str,
                date_str,
                "Pending",
                notes
            )
        )
        timetable_id = cursor.lastrowid

        # Sync to Google Calendar using the existing authenticated service
        try:
            g_event_id = create_google_calendar_event(
                subject=subj,
                topic=topic,
                schedule_type="one-time",
                event_date=date_str,
                start_time=start_time,
                end_time=end_time,
                alarm=alarm
            )
            if g_event_id:
                connection.execute(
                    "UPDATE timetable SET google_event_id = ? WHERE id = ?",
                    (g_event_id, timetable_id)
                )
        except Exception as cal_err:
            print("Google Calendar Sync Warning:", cal_err)

    connection.execute(
        "UPDATE generated_plans SET status = 'approved' WHERE id = ?",
        (plan_id,)
    )

    connection.commit()
    connection.close()

    flash(f"Study plan approved - {len(sessions)} sessions added to your timetable.")
    return redirect(url_for("timetable"))


def _safe_next():
    target = request.form.get("next") or request.args.get("next") or ""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("home")


@app.route("/complete-session/<int:session_id>", methods=["POST"])
def complete_session(session_id):
    connection = get_database_connection()

    session = connection.execute(
        "SELECT * FROM timetable WHERE id = ?",
        (session_id,)
    ).fetchone()

    if session and session["status"] != "Completed":
        study_date = request.form.get("study_date") or session["date"] or datetime.date.today().strftime("%Y-%m-%d")
        try:
            datetime.datetime.strptime(study_date, "%Y-%m-%d")
        except ValueError:
            connection.close()
            flash("Please provide a valid completion date.")
            return redirect(_safe_next())

        # A recurring timetable row represents many occurrences.  Completing
        # one occurrence must not hide every future session.
        if session["schedule_type"] == "one-time":
            connection.execute(
                "UPDATE timetable SET status = 'Completed' WHERE id = ?",
                (session_id,)
            )
        else:
            connection.execute(
                """
                INSERT OR IGNORE INTO timetable_exceptions
                (timetable_id, occurrence_date)
                VALUES (?, ?)
                """,
                (session_id, study_date)
            )

        already_logged = connection.execute(
            """
            SELECT 1 FROM study_history
            WHERE timetable_id = ? AND study_date = ? AND completion_status = 'Completed'
            """,
            (session_id, study_date)
        ).fetchone()

        if already_logged:
            connection.commit()
            connection.close()
            return redirect(_safe_next())

        # Calculate actual hours
        try:
            s_t = datetime.datetime.strptime(session["start_time"], "%H:%M")
            e_t = datetime.datetime.strptime(session["end_time"], "%H:%M")
            if e_t <= s_t:
                e_t += datetime.timedelta(days=1)
            duration_h = round((e_t - s_t).total_seconds() / 3600.0, 2)
        except Exception:
            duration_h = 1.5

        # Infer period
        start_hour = int(session["start_time"].split(":")[0]) if session["start_time"] else 18
        if start_hour < 12:
            period = "Morning"
        elif start_hour < 17:
            period = "Afternoon"
        elif start_hour < 21:
            period = "Evening"
        else:
            period = "Night"

        # Log into study_history for ML fine-tuning
        connection.execute(
            """
            INSERT INTO study_history
            (
                timetable_id,
                subject,
                topic,
                planned_hours,
                actual_hours,
                completion_status,
                study_period,
                study_date,
                difficulty,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                session["subject"],
                session["topic"],
                duration_h,
                duration_h,
                "Completed",
                period,
                study_date,
                2.5,
                datetime.datetime.now().isoformat()
            )
        )

        # Generated plans are one-time sessions.  Match their source topic so
        # dashboard topic progress reflects completed study work.
        if session["schedule_type"] == "one-time":
            topic_name = re.sub(r" \(Part \d+\)$", "", session["topic"])
            remaining_parts = connection.execute(
                """
                SELECT 1 FROM timetable
                WHERE subject = ?
                  AND (topic = ? OR topic LIKE ?)
                  AND status != 'Completed'
                LIMIT 1
                """,
                (session["subject"], topic_name, f"{topic_name} (Part %)")
            ).fetchone()
            if not remaining_parts:
                connection.execute(
                    """
                    UPDATE topics
                    SET status = 'Completed'
                    WHERE topic_name = ?
                      AND subject_id IN (SELECT id FROM subjects WHERE name = ?)
                    """,
                    (topic_name, session["subject"])
                )
        connection.commit()

        # Incrementally retrain ML models with new study history
        try:
            all_history = connection.execute("SELECT * FROM study_history").fetchall()
            hist_records = [dict(h) for h in all_history]
            study_time_regressor.train_with_history(hist_records)
            study_period_classifier.train_with_history(hist_records)
        except Exception as ml_err:
            print("ML Retraining Warning:", ml_err)

        flash(f"Nice work! '{session['topic']}' marked as done.")

    connection.close()
    return redirect(_safe_next())


@app.route("/study-history")
def study_history_view():
    connection = get_database_connection()
    history_records = connection.execute(
        "SELECT * FROM study_history ORDER BY study_date DESC, id DESC"
    ).fetchall()
    connection.close()

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    return render_template(
        "study_history.html",
        history_records=history_records,
        today=today_str
    )


@app.route("/log-study", methods=["POST"])
def log_study():
    subject = request.form.get("subject", "").strip()
    topic = request.form.get("topic", "").strip()
    try:
        planned_hours = float(request.form.get("planned_hours", 1.5))
        actual_hours = float(request.form.get("actual_hours", 1.5))
    except (TypeError, ValueError):
        flash("Study hours must be valid numbers.")
        return redirect(url_for("study_history_view"))

    if planned_hours <= 0 or actual_hours <= 0:
        flash("Study hours must be greater than zero.")
        return redirect(url_for("study_history_view"))

    study_period = request.form.get("study_period", "Evening")
    study_date = request.form.get("study_date", datetime.date.today().strftime("%Y-%m-%d"))

    try:
        datetime.datetime.strptime(study_date, "%Y-%m-%d")
    except ValueError:
        flash("Please provide a valid study date.")
        return redirect(url_for("study_history_view"))

    if subject and topic:
        connection = get_database_connection()
        connection.execute(
            """
            INSERT INTO study_history
            (
                timetable_id,
                subject,
                topic,
                planned_hours,
                actual_hours,
                completion_status,
                study_period,
                study_date,
                difficulty,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                None,
                subject,
                topic,
                planned_hours,
                actual_hours,
                "Completed",
                study_period,
                study_date,
                2.5,
                datetime.datetime.now().isoformat()
            )
        )
        connection.commit()

        # Retrain ML models
        try:
            all_history = connection.execute("SELECT * FROM study_history").fetchall()
            hist_records = [dict(h) for h in all_history]
            study_time_regressor.train_with_history(hist_records)
            study_period_classifier.train_with_history(hist_records)
        except Exception as ml_err:
            print("ML Retraining Error:", ml_err)

        connection.close()

    return redirect(url_for("study_history_view"))


@app.route("/availability", methods=["GET"])
def availability():
    ensure_database()
    connection = get_database_connection()
    slots = connection.execute(
        """
        SELECT * FROM student_availability
        ORDER BY
            CASE day_of_week
                WHEN 'Monday' THEN 1
                WHEN 'Tuesday' THEN 2
                WHEN 'Wednesday' THEN 3
                WHEN 'Thursday' THEN 4
                WHEN 'Friday' THEN 5
                WHEN 'Saturday' THEN 6
                WHEN 'Sunday' THEN 7
                ELSE 8
            END,
            start_time
        """
    ).fetchall()
    connection.close()

    grouped_slots = {d: [] for d in DAYS}
    total_minutes = 0
    for s in slots:
        day = s["day_of_week"]
        if day in grouped_slots:
            grouped_slots[day].append(s)
            if s["is_active"]:
                from scheduler import parse_time_to_minutes
                st = parse_time_to_minutes(s["start_time"])
                et = parse_time_to_minutes(s["end_time"])
                total_minutes += max(0, et - st)

    daily_avg_hours = round((total_minutes / 7.0) / 60.0, 1)

    return render_template(
        "availability.html",
        grouped_slots=grouped_slots,
        days=DAYS,
        daily_avg_hours=daily_avg_hours,
        total_weekly_hours=round(total_minutes / 60.0, 1)
    )


@app.route("/availability/add", methods=["POST"])
def add_availability_slot():
    day = request.form.get("day")
    start_time = request.form.get("start_time")
    end_time = request.form.get("end_time")

    if day in DAYS and start_time and end_time:
        from scheduler import parse_time_to_minutes
        if parse_time_to_minutes(end_time) > parse_time_to_minutes(start_time):
            ensure_database()
            connection = get_database_connection()
            connection.execute(
                """
                INSERT INTO student_availability (day_of_week, start_time, end_time, is_active)
                VALUES (?, ?, ?, 1)
                """,
                (day, start_time, end_time)
            )
            connection.commit()
            connection.close()

    return redirect(url_for("availability"))


@app.route("/availability/delete/<int:slot_id>", methods=["POST", "GET"])
def delete_availability_slot(slot_id):
    ensure_database()
    connection = get_database_connection()
    connection.execute(
        "DELETE FROM student_availability WHERE id = ?",
        (slot_id,)
    )
    connection.commit()
    connection.close()
    return redirect(url_for("availability"))


@app.route("/reset", methods=["GET", "POST"])
def reset_data():
    """Start fresh: clears the chosen data after saving a backup copy of database.db."""
    connection = get_database_connection()
    counts = {
        "tasks": connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
        "timetable": connection.execute("SELECT COUNT(*) FROM timetable").fetchone()[0],
        "calendar": connection.execute(
            "SELECT COUNT(*) FROM timetable WHERE google_event_id IS NOT NULL AND google_event_id != ''"
        ).fetchone()[0],
        "plans": connection.execute("SELECT COUNT(*) FROM syllabuses").fetchone()[0],
        "history": connection.execute("SELECT COUNT(*) FROM study_history").fetchone()[0],
    }

    if request.method == "GET":
        connection.close()
        return render_template("reset.html", counts=counts)

    if request.form.get("confirm_text", "").strip().upper() != "RESET":
        connection.close()
        flash("Type RESET in the box to confirm.")
        return redirect(url_for("reset_data"))

    chosen = set(request.form.getlist("clear"))
    if not chosen:
        connection.close()
        flash("Nothing selected, so nothing was deleted.")
        return redirect(url_for("reset_data"))

    # 1. Safety backup of the whole database before deleting anything
    backup_dir = os.path.join(BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, "database_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".db")
    backup_conn = sqlite3.connect(backup_path)
    connection.backup(backup_conn)
    backup_conn.close()

    # 2. Delete the chosen data (the database file and its tables are kept)
    event_ids = []
    summary = []
    if "timetable" in chosen:
        if request.form.get("remove_calendar"):
            event_ids = [
                row["google_event_id"]
                for row in connection.execute(
                    "SELECT google_event_id FROM timetable WHERE google_event_id IS NOT NULL AND google_event_id != ''"
                ).fetchall()
            ]
        connection.execute("DELETE FROM timetable_exceptions")
        connection.execute("DELETE FROM timetable")
        summary.append(f"{counts['timetable']} sessions")
    if "tasks" in chosen:
        connection.execute("DELETE FROM tasks")
        summary.append(f"{counts['tasks']} tasks")
    if "plans" in chosen:
        connection.execute("DELETE FROM generated_plans")
        connection.execute("DELETE FROM topics")
        connection.execute("DELETE FROM subjects")
        connection.execute("DELETE FROM syllabuses")
        summary.append(f"{counts['plans']} syllabus plans")
    if "history" in chosen:
        connection.execute("DELETE FROM study_history")
        summary.append(f"{counts['history']} history records")
    connection.commit()
    connection.close()

    # 3. Remove the matching Google Calendar events (only if asked)
    removed_events = 0
    for event_id in event_ids:
        try:
            delete_google_event(event_id)
            removed_events += 1
        except Exception as error:
            print("Google Calendar reset warning:", error)

    message = "Fresh start! Cleared " + ", ".join(summary) + "."
    if event_ids:
        message += f" Removed {removed_events} Google Calendar event(s)."
    message += f" A backup was saved in backups/{os.path.basename(backup_path)}."
    flash(message)
    return redirect(url_for("home"))


@app.route("/google-calendar/test")
def google_calendar_test():
    service = get_google_service()

    if not service:
        return "Google Calendar is not connected."

    events = service.events().list(
        calendarId="primary",
        maxResults=20,
        orderBy="startTime",
        singleEvents=True
    ).execute()

    result = []

    for event in events.get("items", []):
        result.append(
            event.get("summary", "No Title")
        )

    return "<br>".join(result)


@app.route("/google-calendar/add")
def google_calendar_add():
    start = datetime.datetime.now() + datetime.timedelta(
        minutes=5
    )

    end = start + datetime.timedelta(
        minutes=30
    )

    service = get_google_service()

    if not service:
        return "Google Calendar is not connected."

    event = {
        "summary": "Smart Study Planner Test",
        "description": "Test event from Smart Study Planner",
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": "Asia/Kolkata"
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": "Asia/Kolkata"
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {
                    "method": "popup",
                    "minutes": 10
                }
            ]
        }
    }

    created_event = service.events().insert(
        calendarId="primary",
        body=event
    ).execute()

    return f"Google Calendar event created: {created_event.get('id')}"


ensure_database()

if __name__ == "__main__":
    app.run(debug=True)
