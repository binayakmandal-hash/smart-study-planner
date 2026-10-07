"""
Smart Study Planner - scheduling engine.

Priority order (never violated from top to bottom):
    1. Student availability windows  (hard constraint)
    2. Deadline                       (plan is compressed to fit when needed)
    3. No conflicts with existing timetable / 15-minute breaks
    4. Required duration              (syllabus hours x ML difficulty + ML regression)
    5. ML preferred period            (soft preference only)
"""

import datetime
import math

from ml_models import predict_topic_hours, predict_study_period


PERIOD_TIME_WINDOWS = {
    "Morning": {"start": "08:00", "end": "12:00", "min_start": 300, "max_end": 720},
    "Afternoon": {"start": "13:00", "end": "17:00", "min_start": 720, "max_end": 1020},
    "Evening": {"start": "17:00", "end": "21:00", "min_start": 1020, "max_end": 1260},
    "Night": {"start": "21:00", "end": "23:30", "min_start": 1260, "max_end": 1440},
}

DEFAULT_AVAILABILITY = {
    "Monday": [(1020, 1200), (1260, 1380)],
    "Tuesday": [(1020, 1200), (1260, 1380)],
    "Wednesday": [(1020, 1200), (1260, 1380)],
    "Thursday": [(1020, 1200), (1260, 1380)],
    "Friday": [(1020, 1200), (1260, 1380)],
    "Saturday": [(540, 720), (840, 1020), (1080, 1260)],
    "Sunday": [(540, 720), (840, 1020), (1080, 1260)],
}

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

BREAK_MINUTES = 15
MIN_SESSION_MINUTES = 30
MAX_SESSION_MINUTES = 90
MIN_TOPIC_MINUTES = 30
MIN_COMPRESSION = 0.5          # never plan less than half of the needed time
LAB_STUDY_RATIO = 0.6          # labs: part of the practice happens in college
THEORY_STUDY_RATIO = 1.0       # self-study hours ~ syllabus contact hours


# =============================================================================
# Small helpers
# =============================================================================

def parse_time_to_minutes(time_str):
    """Converts 'HH:MM' to integer minutes from midnight."""
    parts = time_str.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])


def format_minutes_to_time(minutes):
    """Converts integer minutes from midnight to 'HH:MM'."""
    hours = (minutes // 60) % 24
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


def get_time_period_label(start_time_str):
    """Morning 05:00-11:59, Afternoon 12:00-16:59, Evening 17:00-20:59, Night 21:00-04:59."""
    mins = parse_time_to_minutes(start_time_str)
    if 300 <= mins < 720:
        return "Morning"
    if 720 <= mins < 1020:
        return "Afternoon"
    if 1020 <= mins < 1260:
        return "Evening"
    return "Night"


def _period_of_minutes(minutes):
    return get_time_period_label(format_minutes_to_time(minutes))


# =============================================================================
# Availability
# =============================================================================

def get_student_availability(connection=None, user_id=None):
    """
    Returns {day: [(start_min, end_min), ...]} from the student_availability table.
    If the student has configured ANY window, days without windows are treated as
    unavailable (no silent defaults).  Defaults are used only when nothing is set.
    """
    if not connection:
        return DEFAULT_AVAILABILITY
    try:
        if user_id is None:
            rows = connection.execute(
                "SELECT day_of_week, start_time, end_time FROM student_availability WHERE is_active = 1"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT day_of_week, start_time, end_time FROM student_availability WHERE is_active = 1 AND user_id = ?",
                (user_id,)
            ).fetchall()
        if not rows:
            return DEFAULT_AVAILABILITY
        avail = {day: [] for day in DAY_NAMES}
        for row in rows:
            day = row["day_of_week"]
            if day in avail:
                s_min = parse_time_to_minutes(row["start_time"])
                e_min = parse_time_to_minutes(row["end_time"])
                if e_min > s_min:
                    avail[day].append((s_min, e_min))
        for day in avail:
            avail[day].sort()
        return avail
    except Exception as err:
        print("Error fetching student availability:", err)
        return DEFAULT_AVAILABILITY


def calculate_student_capacity(availability_dict):
    """Average daily study capacity (hours) from the weekly availability windows."""
    total_weekly_minutes = sum(max(0, e - s) for slots in availability_dict.values() for s, e in slots)
    return round((total_weekly_minutes / 7.0) / 60.0, 1)


def validate_deadline(total_predicted_hours, deadline_date_str, daily_available_hours, start_date_str=None):
    """Simple capacity check (kept for compatibility and quick feasibility maths)."""
    start_date = (datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
                  if start_date_str else datetime.date.today())
    deadline_date = datetime.datetime.strptime(deadline_date_str, "%Y-%m-%d").date()
    days_available = max(1, (deadline_date - start_date).days)

    total_available_hours = round(days_available * daily_available_hours, 1)
    is_feasible = total_available_hours >= total_predicted_hours
    deficit = max(0.0, round(total_predicted_hours - total_available_hours, 1))
    surplus = max(0.0, round(total_available_hours - total_predicted_hours, 1))
    recommended_daily_hours = round(total_predicted_hours / days_available, 1)
    recommended_days_needed = int(round(total_predicted_hours / max(1.0, daily_available_hours) + 0.5))
    recommended_extended_deadline = (start_date + datetime.timedelta(days=recommended_days_needed)).strftime("%Y-%m-%d")

    return {
        "is_feasible": is_feasible,
        "total_predicted_hours": round(total_predicted_hours, 1),
        "total_available_hours": total_available_hours,
        "days_available": days_available,
        "daily_available_hours": daily_available_hours,
        "deficit_hours": deficit,
        "surplus_hours": surplus,
        "recommended_daily_hours": recommended_daily_hours,
        "recommended_extended_deadline": recommended_extended_deadline,
        "message": (
            "Plan can be comfortably completed before the deadline within your availability!"
            if is_feasible
            else f"Available study capacity ({total_available_hours}h) is less than required curriculum duration ({total_predicted_hours}h) before {deadline_date_str}."
        ),
    }


def get_existing_day_bookings(connection, date_str, user_id=None):
    """Booked (start_min, end_min) intervals on a date from the timetable (one-time, daily, weekly, exceptions)."""
    if not connection:
        return []
    target_weekday = datetime.datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
    bookings = []
    try:
        if user_id is None:
            rows = connection.execute("SELECT * FROM timetable").fetchall()
        else:
            rows = connection.execute("SELECT * FROM timetable WHERE user_id = ?", (user_id,)).fetchall()
        exceptions = {
            (row["timetable_id"], row["occurrence_date"])
            for row in connection.execute("SELECT timetable_id, occurrence_date FROM timetable_exceptions").fetchall()
        }
        for row in rows:
            sched_type = (row["schedule_type"] if "schedule_type" in row.keys() else "one-time") or "one-time"
            if not row["start_time"] or not row["end_time"]:
                continue
            if (row["id"], date_str) in exceptions:
                continue
            booked = False
            if sched_type == "one-time" and row["date"] == date_str:
                booked = True
            elif sched_type == "daily":
                s_date, e_date = row["start_date"] or row["date"], row["end_date"]
                booked = bool(s_date and e_date and s_date <= date_str <= e_date)
            elif sched_type == "weekly":
                s_date, e_date = row["start_date"] or row["date"], row["end_date"]
                booked = bool(s_date and e_date and s_date <= date_str <= e_date and row["day"] == target_weekday)
            if booked:
                start_min = parse_time_to_minutes(row["start_time"])
                end_min = parse_time_to_minutes(row["end_time"])
                if end_min <= start_min:
                    end_min += 24 * 60
                bookings.append((start_min, end_min))
    except Exception as err:
        print("Error fetching existing bookings:", err)
    return sorted(bookings)


def find_free_slot_in_availability(day_availability_windows, existing_bookings, preferred_period,
                                   duration_minutes, break_minutes=BREAK_MINUTES):
    """
    Finds a non-overlapping slot strictly inside availability windows.  Windows that
    overlap the preferred period are tried first.  Never schedules outside availability.
    """
    if not day_availability_windows:
        return None, None, None
    pref = PERIOD_TIME_WINDOWS.get(preferred_period, PERIOD_TIME_WINDOWS["Evening"])
    ordered = sorted(day_availability_windows,
                     key=lambda w: 0 if not (w[1] <= pref["min_start"] or w[0] >= pref["max_end"]) else 1)
    for win_start, win_end in ordered:
        candidate = win_start
        while candidate + duration_minutes <= win_end:
            end = candidate + duration_minutes
            clash = next(((bs, be) for bs, be in existing_bookings
                          if not (end + break_minutes <= bs or candidate >= be + break_minutes)), None)
            if clash is None:
                start_str = format_minutes_to_time(candidate)
                return start_str, format_minutes_to_time(end), get_time_period_label(start_str)
            candidate = max(candidate + 15, clash[1] + break_minutes)
    return None, None, None


def find_free_slot(existing_bookings, preferred_period, duration_minutes, break_minutes=BREAK_MINUTES):
    """Backward-compatible wrapper using the standard period windows."""
    for period in [preferred_period] + [p for p in ["Evening", "Morning", "Afternoon", "Night"] if p != preferred_period]:
        info = PERIOD_TIME_WINDOWS.get(period, PERIOD_TIME_WINDOWS["Evening"])
        start, end, _ = find_free_slot_in_availability([(info["min_start"], info["max_end"])],
                                                       existing_bookings, period, duration_minutes, break_minutes)
        if start and end:
            return start, end
    return None, None


# =============================================================================
# Hours estimation
# =============================================================================

def estimate_topic_hours(topic, is_lab=False):
    """
    Needed self-study hours for one topic.
      * syllabus hours (share of the module's hours) x ML difficulty factor  (60 %)
      * Linear Regression (ML) prediction                                     (40 %)
    Without syllabus hours the regression prediction is used alone.
    """
    t_len = topic.get("topic_length", len(topic.get("topic_name", "")))
    n_sub = topic.get("subtopic_count", len(topic.get("subtopics", [])))
    diff = float(topic.get("difficulty_score", topic.get("difficulty", 2.5)) or 2.5)
    ml_hours = predict_topic_hours(t_len, n_sub, diff)

    syllabus_hours = topic.get("syllabus_hours")
    if syllabus_hours:
        ratio = LAB_STUDY_RATIO if is_lab else THEORY_STUDY_RATIO
        difficulty_factor = 0.85 + 0.1 * (diff - 1.0)          # 0.85 .. 1.25
        syllabus_based = float(syllabus_hours) * ratio * difficulty_factor
        hours = 0.6 * syllabus_based + 0.4 * ml_hours
    else:
        hours = ml_hours
    hours = max(0.5, round(hours * 4) / 4.0)
    return hours, ml_hours


# =============================================================================
# Free-time map
# =============================================================================

def _subtract(intervals, blocks, pad):
    """intervals minus blocks (each block padded by `pad` minutes on both sides)."""
    result = []
    for s, e in intervals:
        pieces = [(s, e)]
        for bs, be in blocks:
            bs, be = bs - pad, be + pad
            nxt = []
            for ps, pe in pieces:
                if be <= ps or bs >= pe:
                    nxt.append((ps, pe))
                else:
                    if bs > ps:
                        nxt.append((ps, bs))
                    if be < pe:
                        nxt.append((be, pe))
            pieces = nxt
        result.extend(pieces)
    return [(s, e) for s, e in result if e - s >= MIN_SESSION_MINUTES]


def build_free_time(start_date, end_date_exclusive, availability, connection, now=None, user_id=None):
    """[(date, day_name, [(start,end), ...]), ...] of usable study time."""
    days = []
    current = start_date
    now = now or datetime.datetime.now()
    while current < end_date_exclusive:
        date_str = current.strftime("%Y-%m-%d")
        day_name = current.strftime("%A")
        windows = list(availability.get(day_name, []))
        bookings = get_existing_day_bookings(connection, date_str, user_id=user_id)
        free = _subtract(windows, bookings, BREAK_MINUTES)
        if current == now.date():
            cutoff = now.hour * 60 + now.minute + 15
            free = [(max(s, cutoff), e) for s, e in free if e - max(s, cutoff) >= MIN_SESSION_MINUTES]
        days.append((date_str, day_name, free))
        current += datetime.timedelta(days=1)
    return days


# =============================================================================
# Placement
# =============================================================================

def _place(work_by_subject, subject_order, free_days, daily_limit_minutes):
    """
    Greedy placement across days / windows with subject interleaving.
    work_by_subject: {subject: [topic_work dict, ...]}  (mutated: remaining minutes)
    Returns list of placed sessions (dicts).
    """
    sessions = []
    rotation = 0
    for date_str, day_name, windows in free_days:
        used_today = 0
        subjects_today = {}
        last_subject = None
        for win_start, win_end in windows:
            cursor = win_start
            while True:
                room = min(win_end - cursor, daily_limit_minutes - used_today)
                if room < MIN_SESSION_MINUTES:
                    break
                active = [s for s in subject_order if work_by_subject.get(s)]
                if not active:
                    return sessions
                period = _period_of_minutes(cursor)
                n = len(subject_order)
                ranked = []
                for offset in range(n):
                    subj = subject_order[(rotation + offset) % n]
                    if not work_by_subject.get(subj):
                        continue
                    head = work_by_subject[subj][0]
                    score = offset
                    if subj == last_subject:
                        score += 100
                    score += 30 * subjects_today.get(subj, 0)
                    if head["preferred_period"] == period:
                        score -= 5          # soft ML preference
                    ranked.append((score, subj))
                ranked.sort()
                choice = None
                for _, candidate in ranked:
                    head = work_by_subject[candidate][0]
                    length = _session_length(head["remaining"], room)
                    if length:
                        choice = (candidate, head, length)
                        break
                if choice is None:
                    break
                subj, head, length = choice

                sessions.append({
                    "date": date_str,
                    "day": day_name,
                    "start_min": cursor,
                    "end_min": cursor + length,
                    "work": head,
                    "subject": subj,
                })
                head["remaining"] -= length
                head["placed"] += length
                if head["remaining"] <= 0:
                    work_by_subject[subj].pop(0)
                used_today += length
                subjects_today[subj] = subjects_today.get(subj, 0) + 1
                last_subject = subj
                rotation = (subject_order.index(subj) + 1) % len(subject_order)
                cursor += length + BREAK_MINUTES
    return sessions


def _session_length(remaining, room):
    """
    Length of the next session for a topic with `remaining` minutes when `room`
    minutes are free.  Never leaves a tail shorter than MIN_SESSION_MINUTES.
    Returns 0 when the topic cannot be placed sensibly in this gap.
    """
    cap = min(MAX_SESSION_MINUTES, room)
    if remaining <= cap:
        return remaining
    if remaining <= min(room, MAX_SESSION_MINUTES + 15):
        return remaining                       # finish the topic in one go
    length = cap
    if remaining - length < MIN_SESSION_MINUTES:
        length = remaining - MIN_SESSION_MINUTES
    length = int(length // 5 * 5)
    return length if length >= MIN_SESSION_MINUTES else 0


def _make_work(topic_items, scale):
    work_by_subject = {}
    for item in topic_items:
        minutes = max(MIN_TOPIC_MINUTES, int(round(item["needed_hours"] * 60 * scale / 5.0)) * 5)
        work_by_subject.setdefault(item["subject"], []).append({
            "item": item,
            "minutes": minutes,
            "remaining": minutes,
            "placed": 0,
            "preferred_period": item["preferred_period"],
        })
    return work_by_subject


def validate_plan(sessions, availability):
    """PRP validation: inside availability, no overlap, chronological, correct labels."""
    checks = {
        "all_sessions_inside_availability": True,
        "no_session_overlap": True,
        "chronological_timetable": True,
        "valid_period_labels": True,
        "valid_times": True,
    }
    previous = None
    by_date = {}
    for s in sessions:
        start = parse_time_to_minutes(s["start_time"])
        end = parse_time_to_minutes(s["end_time"])
        if end <= start:
            checks["valid_times"] = False
        windows = availability.get(s["day"], [])
        if not any(ws <= start and end <= we for ws, we in windows):
            checks["all_sessions_inside_availability"] = False
        if s["preferred_period"] != get_time_period_label(s["start_time"]):
            checks["valid_period_labels"] = False
        key = (s["date"], s["start_time"])
        if previous and key < previous:
            checks["chronological_timetable"] = False
        previous = key
        for os_, oe in by_date.get(s["date"], []):
            if not (end <= os_ or start >= oe):
                checks["no_session_overlap"] = False
        by_date.setdefault(s["date"], []).append((start, end))
    checks["passed"] = all(checks.values())
    return checks


# =============================================================================
# Main entry point
# =============================================================================

def generate_study_schedule(subjects_dict, deadline_date_str, daily_available_hours=None, connection=None,
                            start_date_str=None, student_availability_dict=None, include_revision=True,
                            now=None, user_id=None):
    """
    Builds a complete, deadline-aware study plan for every subject -> module -> topic.
    Returns a dict (JSON-serialisable) used by the plan preview and approval.
    """
    now = now or datetime.datetime.now()
    start_date = (datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
                  if start_date_str else now.date())
    deadline_date = datetime.datetime.strptime(deadline_date_str, "%Y-%m-%d").date()
    # study up to and including the day before the deadline (exam day is free)
    end_exclusive = deadline_date if deadline_date > start_date else start_date + datetime.timedelta(days=1)

    availability = (student_availability_dict if student_availability_dict is not None
                    else get_student_availability(connection, user_id=user_id))
    student_capacity = calculate_student_capacity(availability)
    try:
        daily_cap_hours = float(daily_available_hours) if daily_available_hours else 0.0
    except (TypeError, ValueError):
        daily_cap_hours = 0.0
    if daily_cap_hours <= 0:
        daily_cap_hours = 24.0          # availability alone limits the day
    daily_limit_minutes = int(daily_cap_hours * 60)

    # ---- 1. topics with needed hours ------------------------------------------
    topic_items = []
    subject_order = []
    for subj_name, subj_data in subjects_dict.items():
        is_lab = bool(subj_data.get("is_lab"))
        topics = subj_data.get("topics", [])
        if not topics:
            continue
        subject_order.append(subj_name)
        for t in topics:
            needed, ml_hours = estimate_topic_hours(t, is_lab)
            diff = float(t.get("difficulty_score", t.get("difficulty", 2.5)) or 2.5)
            topic_items.append({
                "subject": subj_name,
                "topic": t.get("topic_name", ""),
                "unit_name": t.get("unit_name") or "Topics",
                "subtopics": list(t.get("subtopics", [])),
                "difficulty": diff,
                "is_lab": is_lab,
                "syllabus_hours": t.get("syllabus_hours"),
                "ml_hours": ml_hours,
                "needed_hours": needed,
                "predicted_hours": needed,
                "preferred_period": predict_study_period(diff, needed),
            })

    total_needed_hours = round(sum(i["needed_hours"] for i in topic_items), 1)
    free_days = build_free_time(start_date, end_exclusive, availability, connection, now=now, user_id=user_id)
    capacity_minutes = sum(
        min(daily_limit_minutes, sum(e - s for s, e in windows)) for _, _, windows in free_days
    )

    # ---- 2. fit to deadline (largest scale that places everything) ---------------
    def attempt(scale):
        work = _make_work(topic_items, scale)
        placed = _place(work, subject_order, free_days, daily_limit_minutes)
        unplaced = sum(w["remaining"] for lst in work.values() for w in lst)
        return placed, unplaced, work

    scale = 1.0
    placed, unplaced, work = attempt(scale)
    if unplaced > 0:
        lo, hi = MIN_COMPRESSION, 1.0
        best = None
        for _ in range(12):
            mid = (lo + hi) / 2.0
            p, u, w = attempt(mid)
            if u == 0:
                best, lo = (mid, p, u, w), mid
            else:
                hi = mid
        if best:
            scale, placed, unplaced, work = best
        else:
            scale = MIN_COMPRESSION
            placed, unplaced, work = attempt(scale)

    # ---- 3. revision sessions with any spare time -------------------------------
    revision_sessions = []
    if include_revision and unplaced == 0 and placed and scale >= 0.999:
        used = {}
        for p in placed:
            used.setdefault(p["date"], []).append((p["start_min"], p["end_min"]))
        spare_days = []
        for date_str, day_name, windows in free_days:
            taken = used.get(date_str, [])
            left = _subtract(windows, taken, BREAK_MINUTES)
            used_today = sum(e - s for s, e in taken)
            if left and used_today + 45 <= daily_limit_minutes:
                spare_days.append((date_str, day_name, left, daily_limit_minutes - used_today))
        # revise each subject once in the last spare days before the deadline
        targets = list(subject_order)
        for date_str, day_name, left, room_today in reversed(spare_days):
            if not targets:
                break
            s, e = left[-1]
            length = min(60, e - s, room_today)
            if length >= 45:
                subj = targets.pop()
                revision_sessions.append({"date": date_str, "day": day_name, "start_min": e - length,
                                          "end_min": e, "subject": subj, "revision": True})

    # ---- 4. turn placements into sessions ---------------------------------------
    parts_count = {}
    for p in placed:
        parts_count[id(p["work"])] = parts_count.get(id(p["work"]), 0) + 1
    part_index = {}
    scheduled_sessions = []
    for p in placed:
        w = p["work"]
        item = w["item"]
        key = id(w)
        part_index[key] = part_index.get(key, 0) + 1
        k, n = part_index[key], parts_count[key]
        subs = item["subtopics"]
        if n > 1 and subs:
            per = math.ceil(len(subs) / n)
            covers = subs[(k - 1) * per:k * per] or subs[-1:]
        else:
            covers = subs
        start_str = format_minutes_to_time(p["start_min"])
        end_str = format_minutes_to_time(p["end_min"])
        duration = round((p["end_min"] - p["start_min"]) / 60.0, 2)
        scheduled_sessions.append({
            "date": p["date"],
            "day": p["day"],
            "start_time": start_str,
            "end_time": end_str,
            "duration_hours": duration,
            "subject": item["subject"],
            "topic": item["topic"] if n == 1 else f"{item['topic']} (Part {k}/{n})",
            "base_topic": item["topic"],
            "unit_name": item["unit_name"],
            "covers": covers,
            "difficulty": item["difficulty"],
            "preferred_period": get_time_period_label(start_str),
            "ml_period": item["preferred_period"],
            "kind": "lab" if item["is_lab"] else "study",
            "alarm": 1,
        })
    for r in revision_sessions:
        start_str = format_minutes_to_time(r["start_min"])
        scheduled_sessions.append({
            "date": r["date"],
            "day": r["day"],
            "start_time": start_str,
            "end_time": format_minutes_to_time(r["end_min"]),
            "duration_hours": round((r["end_min"] - r["start_min"]) / 60.0, 2),
            "subject": r["subject"],
            "topic": f"Revision - {r['subject']}",
            "base_topic": "Revision",
            "unit_name": "Revision",
            "covers": ["Quick recap of all modules", "Practice important questions"],
            "difficulty": 2.5,
            "preferred_period": get_time_period_label(start_str),
            "ml_period": get_time_period_label(start_str),
            "kind": "revision",
            "alarm": 1,
        })
    scheduled_sessions.sort(key=lambda s: (s["date"], s["start_time"]))

    # ---- 5. per-topic allocation summary ----------------------------------------
    allocation = {}
    unscheduled = []
    for subj, lst in work.items():
        for w in lst:
            if w["remaining"] > 0:
                unscheduled.append({"subject": subj, "topic": w["item"]["topic"],
                                    "missing_hours": round(w["remaining"] / 60.0, 1)})
    for s in scheduled_sessions:
        if s["kind"] == "revision":
            continue
        key = f"{s['subject']}||{s['base_topic']}"
        entry = allocation.setdefault(key, {"hours": 0.0, "sessions": 0, "first": s["date"], "last": s["date"]})
        entry["hours"] = round(entry["hours"] + s["duration_hours"], 2)
        entry["sessions"] += 1
        entry["last"] = s["date"]
    for item in topic_items:
        a = allocation.get(f"{item['subject']}||{item['topic']}", {})
        item["allocated_hours"] = a.get("hours", 0.0)
        item["sessions"] = a.get("sessions", 0)
        item["first_date"] = a.get("first")
        item["last_date"] = a.get("last")

    subject_summary = []
    for subj in subject_order:
        items = [i for i in topic_items if i["subject"] == subj]
        sess = [s for s in scheduled_sessions if s["subject"] == subj and s["kind"] != "revision"]
        subject_summary.append({
            "subject": subj,
            "is_lab": bool(items and items[0]["is_lab"]),
            "topics": len(items),
            "modules": len({i["unit_name"] for i in items}),
            "needed_hours": round(sum(i["needed_hours"] for i in items), 1),
            "allocated_hours": round(sum(s["duration_hours"] for s in sess), 1),
            "sessions": len(sess),
            "finish_date": sess[-1]["date"] if sess else None,
        })

    # ---- 6. feasibility + validation -------------------------------------------
    study_dates = sorted({s["date"] for s in scheduled_sessions})
    allocated_total = round(sum(s["duration_hours"] for s in scheduled_sessions if s["kind"] != "revision"), 1)
    capacity_hours = round(capacity_minutes / 60.0, 1)
    days_available = max(1, (end_exclusive - start_date).days)
    is_feasible = unplaced == 0 and scale >= 0.999
    fits_compressed = unplaced == 0 and scale < 0.999

    study_only = [s["date"] for s in scheduled_sessions if s["kind"] != "revision"]
    if is_feasible:
        message = (f"Everything fits! {allocated_total}h of study across {len(set(study_only))} days. "
                   f"All topics are covered by {study_only[-1] if study_only else deadline_date_str}"
                   + (f", leaving time for {len(revision_sessions)} revision sessions before your deadline."
                      if revision_sessions else " - before your deadline."))
    elif fits_compressed:
        message = (f"Your syllabus needs about {total_needed_hours}h but only {capacity_hours}h are free before "
                   f"{deadline_date_str}. Every topic is still covered: study time was compressed to "
                   f"{int(round(scale * 100))}% so the whole syllabus fits your deadline.")
    else:
        needed_daily = round(total_needed_hours * MIN_COMPRESSION / days_available, 1)
        message = (f"Even compressed to {int(MIN_COMPRESSION * 100)}%, {len(unscheduled)} topic(s) do not fit before "
                   f"{deadline_date_str}. Add more study windows (about {needed_daily}h/day) or move the deadline.")

    # when the plan had to be compressed, find the date a full-length plan would finish
    recommended_deadline = deadline_date_str
    if not is_feasible and topic_items:
        long_days = build_free_time(start_date, start_date + datetime.timedelta(days=240),
                                    availability, connection, now=now, user_id=user_id)
        full_work = _make_work(topic_items, 1.0)
        full = _place(full_work, subject_order, long_days, daily_limit_minutes)
        if full and not any(w["remaining"] for lst in full_work.values() for w in lst):
            last = datetime.datetime.strptime(full[-1]["date"], "%Y-%m-%d").date()
            recommended_deadline = (last + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    validation = {
        "is_feasible": is_feasible or fits_compressed,
        "fully_feasible": is_feasible,
        "compressed": fits_compressed,
        "compression_percent": int(round(scale * 100)),
        "total_predicted_hours": total_needed_hours,
        "total_available_hours": capacity_hours,
        "days_available": days_available,
        "daily_available_hours": round(daily_cap_hours if daily_cap_hours < 24 else student_capacity, 1),
        "deficit_hours": round(max(0.0, total_needed_hours - capacity_hours), 1),
        "surplus_hours": round(max(0.0, capacity_hours - allocated_total), 1),
        "recommended_daily_hours": round(total_needed_hours / days_available, 1),
        "recommended_extended_deadline": recommended_deadline,
        "unscheduled_topics": unscheduled,
        "message": message,
    }
    checks = validate_plan(scheduled_sessions, availability)

    return {
        "validation": validation,
        "checks": checks,
        "topics_analysis": topic_items,
        "subject_summary": subject_summary,
        "scheduled_sessions": scheduled_sessions,
        "total_sessions": len(scheduled_sessions),
        "total_study_days": len(study_dates),
        "total_predicted_hours": total_needed_hours,
        "allocated_hours": allocated_total,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": study_dates[-1] if study_dates else deadline_date_str,
        "deadline": deadline_date_str,
        "daily_available_hours": daily_cap_hours if daily_cap_hours < 24 else student_capacity,
        "student_availability": {d: list(map(list, w)) for d, w in availability.items()},
        "student_capacity_hours": student_capacity,
    }
