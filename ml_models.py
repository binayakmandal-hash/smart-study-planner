import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.tree import DecisionTreeClassifier

# ==============================================================================
# PHASE 6 — MULTIPLE LINEAR REGRESSION (PREDICT STUDY HOURS)
# ==============================================================================

# Cold-start training dataset for Multiple Linear Regression
# Features: [topic_length, num_subtopics, topic_difficulty (1-5), student_experience (0.5-2.0), historical_avg_hours]
# Target: study_hours (realistic hours needed for mastery)
COLD_START_REGRESSION_X = np.array([
    # [topic_len, subtopics, difficulty, experience, hist_avg]
    [15, 0, 1.0, 1.0, 0.75],   # Introduction / Syntax basics
    [25, 1, 1.2, 1.0, 1.0],    # Variables & Data Types
    [35, 2, 1.5, 1.0, 1.25],   # Conditional Statements & Loops
    [40, 3, 2.0, 1.0, 1.5],    # Arrays & String Handling
    [50, 3, 2.5, 1.0, 1.75],   # Functions & Parameter Passing
    [65, 4, 3.0, 1.0, 2.0],    # OOP: Classes & Objects
    [75, 4, 3.5, 1.0, 2.5],    # OOP: Inheritance & Polymorphism
    [85, 5, 3.8, 1.0, 2.75],   # Exception Handling & File I/O
    [90, 5, 4.0, 1.0, 3.0],    # SQL: Joins, Subqueries & Aggregations
    [110, 6, 4.2, 1.0, 3.25],  # Database Normalization & Indexing
    [120, 6, 4.5, 1.0, 3.5],   # Concurrency & Multithreading
    [130, 7, 4.8, 1.0, 4.0],   # Dynamic Programming & Graph Algorithms
    [140, 8, 5.0, 1.0, 4.5],   # System Architecture & Cryptography
    # Fast learner variations (experience = 1.5)
    [35, 2, 1.5, 1.5, 0.9],
    [75, 4, 3.5, 1.5, 1.8],
    [120, 6, 4.5, 1.5, 2.6],
    # Slower / thorough learner variations (experience = 0.8)
    [35, 2, 1.5, 0.8, 1.6],
    [75, 4, 3.5, 0.8, 3.1],
    [120, 6, 4.5, 0.8, 4.2],
])

COLD_START_REGRESSION_Y = np.array([
    0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 2.75, 3.0, 3.25, 3.5, 4.0, 4.5,
    0.9, 1.8, 2.6,
    1.6, 3.1, 4.2
])


class StudyTimeRegressor:
    """
    Predicts required study hours using Multiple Linear Regression.
    Supports incremental retraining as real student study history is accumulated.
    """
    def __init__(self):
        self.model = LinearRegression()
        self.is_fitted = False
        self._fit_initial_model()

    def _fit_initial_model(self):
        self.model.fit(COLD_START_REGRESSION_X, COLD_START_REGRESSION_Y)
        self.is_fitted = True

    def train_with_history(self, history_records):
        """
        Retrains the model incorporating real student study history.
        history_records: list of dicts with keys:
          [topic_length, num_subtopics, difficulty, experience, actual_hours]
        """
        if not history_records:
            self._fit_initial_model()
            return

        X_hist = []
        y_hist = []

        for row in history_records:
            try:
                t_len = float(row.get("topic_length", 40) or 40)
                n_sub = float(row.get("num_subtopics", 2) or 2)
                diff = float(row.get("difficulty", 2.5) or 2.5)
                exp = float(row.get("experience", 1.0) or 1.0)
                planned = float(row.get("planned_hours", 2.0) or 2.0)
                actual = float(row.get("actual_hours", 2.0) or 2.0)

                X_hist.append([t_len, n_sub, diff, exp, planned])
                y_hist.append(actual)
            except (ValueError, TypeError):
                continue

        if X_hist:
            # Combine cold start with student history (weighting actual history)
            X_combined = np.vstack([COLD_START_REGRESSION_X, np.array(X_hist)])
            y_combined = np.concatenate([COLD_START_REGRESSION_Y, np.array(y_hist)])
            self.model.fit(X_combined, y_combined)
            self.is_fitted = True

    def predict_hours(self, topic_length, num_subtopics, difficulty, experience=1.0, hist_avg=None):
        """
        Predicts study hours for a specific topic.
        Returns a rounded float between 0.5 and 5.0 hours.
        """
        if hist_avg is None:
            hist_avg = max(0.5, difficulty * 0.75)

        features = np.array([[
            float(topic_length),
            float(num_subtopics),
            float(difficulty),
            float(experience),
            float(hist_avg)
        ]])

        predicted = float(self.model.predict(features)[0])
        # Bound realistic bounds (minimum 0.5h, max 5.0h per topic)
        bounded = max(0.5, min(5.0, predicted))
        # Round to nearest 0.5 or 0.25
        return round(bounded * 2) / 2


# ==============================================================================
# PHASE 7 — DECISION TREE (PREDICT PREFERRED STUDY PERIOD)
# ==============================================================================

PERIOD_CLASSES = ["Morning", "Afternoon", "Evening", "Night"]

# Cold-start training dataset for Decision Tree Classifier
# Features: [difficulty (1-5), duration_hours (0.5-5.0), day_of_week (0=Mon, 6=Sun), urgency (0-1)]
# Target index: 0=Morning, 1=Afternoon, 2=Evening, 3=Night
COLD_START_TREE_X = np.array([
    # High difficulty, long duration -> Morning (fresh mind)
    [4.5, 3.0, 0, 0.2],  # Class 0 (Morning)
    [4.8, 3.5, 1, 0.4],  # Class 0 (Morning)
    [4.0, 2.5, 5, 0.1],  # Class 0 (Morning)
    [5.0, 4.0, 2, 0.6],  # Class 0 (Morning)
    [3.8, 2.5, 6, 0.1],  # Class 0 (Morning)

    # Moderate difficulty, medium duration -> Evening (peak focus after classes)
    [2.8, 2.0, 0, 0.3],  # Class 2 (Evening)
    [3.0, 1.5, 1, 0.5],  # Class 2 (Evening)
    [3.2, 2.0, 2, 0.4],  # Class 2 (Evening)
    [2.5, 1.5, 3, 0.2],  # Class 2 (Evening)
    [3.5, 2.0, 4, 0.6],  # Class 2 (Evening)

    # Light difficulty, short duration -> Afternoon (quick review / labs)
    [1.5, 1.0, 0, 0.2],  # Class 1 (Afternoon)
    [1.8, 1.0, 2, 0.1],  # Class 1 (Afternoon)
    [2.0, 1.5, 5, 0.3],  # Class 1 (Afternoon)
    [1.2, 0.75, 6, 0.1], # Class 1 (Afternoon)

    # Conceptual reading, medium difficulty -> Night (quiet hours)
    [2.2, 1.5, 1, 0.7],  # Class 3 (Night)
    [2.5, 1.5, 3, 0.8],  # Class 3 (Night)
    [3.0, 2.0, 4, 0.9],  # Class 3 (Night)
    [2.0, 1.0, 0, 0.8],  # Class 3 (Night)
])

COLD_START_TREE_Y = np.array([
    0, 0, 0, 0, 0,
    2, 2, 2, 2, 2,
    1, 1, 1, 1,
    3, 3, 3, 3
])


class StudyPeriodClassifier:
    """
    Predicts optimal study period (Morning, Afternoon, Evening, Night)
    using a DecisionTreeClassifier.
    """
    def __init__(self):
        self.model = DecisionTreeClassifier(max_depth=4, random_state=42)
        self.is_fitted = False
        self._fit_initial_model()

    def _fit_initial_model(self):
        self.model.fit(COLD_START_TREE_X, COLD_START_TREE_Y)
        self.is_fitted = True

    def train_with_history(self, history_records):
        """
        Fine-tunes the decision tree with real completion periods from student history.
        """
        if not history_records:
            self._fit_initial_model()
            return

        X_hist = []
        y_hist = []

        period_map = {name: idx for idx, name in enumerate(PERIOD_CLASSES)}

        for row in history_records:
            try:
                period_str = row.get("study_period", "Evening") or "Evening"
                if period_str not in period_map:
                    continue

                diff = float(row.get("difficulty", 2.5) or 2.5)
                duration = float(row.get("actual_hours", 1.5) or 1.5)
                day = float(row.get("day_of_week", 0) or 0)
                urgency = float(row.get("urgency", 0.3) or 0.3)

                X_hist.append([diff, duration, day, urgency])
                y_hist.append(period_map[period_str])
            except (ValueError, TypeError):
                continue

        if X_hist:
            X_combined = np.vstack([COLD_START_TREE_X, np.array(X_hist)])
            y_combined = np.concatenate([COLD_START_TREE_Y, np.array(y_hist)])
            self.model.fit(X_combined, y_combined)
            self.is_fitted = True

    def predict_period(self, difficulty, duration_hours, day_of_week=0, urgency=0.3):
        """
        Predicts preferred period ('Morning', 'Afternoon', 'Evening', 'Night').
        """
        features = np.array([[
            float(difficulty),
            float(duration_hours),
            float(day_of_week),
            float(urgency)
        ]])

        pred_idx = int(self.model.predict(features)[0])
        return PERIOD_CLASSES[pred_idx]


# Global instances
study_time_regressor = StudyTimeRegressor()
study_period_classifier = StudyPeriodClassifier()


def predict_topic_hours(topic_length, num_subtopics, difficulty, experience=1.0):
    return study_time_regressor.predict_hours(topic_length, num_subtopics, difficulty, experience)


def predict_study_period(difficulty, duration_hours, day_of_week=0, urgency=0.3):
    return study_period_classifier.predict_period(difficulty, duration_hours, day_of_week, urgency)
