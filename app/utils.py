from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta

CYCLE_LABELS = {
    "day": "天",
    "week": "周",
    "quarter": "季",
    "halfyear": "半年",
    "month": "月",
    "year": "年",
    "2year": "两年",
    "3year": "三年",
    "4year": "四年",
    "5year": "五年",
}

CYCLE_OPTIONS = [
    "day",
    "week",
    "quarter",
    "halfyear",
    "month",
    "year",
    "2year",
    "3year",
    "4year",
    "5year",
]

DEFAULT_CURRENCIES = [
    "CNY",
    "USD",
    "EUR",
    "HKD",
    "JPY",
    "GBP",
    "AUD",
    "SGD",
]


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def format_date(value):
    return value.strftime("%Y-%m-%d")


def today_date():
    return date.today()


def cycle_delta(cycle):
    if cycle == "day":
        return timedelta(days=1)
    if cycle == "week":
        return timedelta(weeks=1)
    if cycle == "quarter":
        return relativedelta(months=3)
    if cycle == "halfyear":
        return relativedelta(months=6)
    if cycle == "month":
        return relativedelta(months=1)
    if cycle == "year":
        return relativedelta(years=1)
    if cycle == "2year":
        return relativedelta(years=2)
    if cycle == "3year":
        return relativedelta(years=3)
    if cycle == "4year":
        return relativedelta(years=4)
    if cycle == "5year":
        return relativedelta(years=5)
    raise ValueError(f"Unsupported cycle: {cycle}")


def add_cycle(value, cycle):
    return value + cycle_delta(cycle)


def subtract_cycle(value, cycle):
    return value - cycle_delta(cycle)


def normalize_due_date(due_date, cycle, today=None):
    if today is None:
        today = today_date()
    updated = False
    if due_date >= today:
        return due_date, updated
    if cycle in {"day", "week"}:
        cycle_days = 1 if cycle == "day" else 7
        diff_days = (today - due_date).days
        steps = diff_days // cycle_days
        if diff_days % cycle_days:
            steps += 1
        due_date = due_date + timedelta(days=steps * cycle_days)
        updated = True
        return due_date, updated
    while due_date < today:
        due_date = add_cycle(due_date, cycle)
        updated = True
    return due_date, updated


def cycle_length_days(due_date, cycle):
    start_date = subtract_cycle(due_date, cycle)
    delta_days = (due_date - start_date).days
    return max(delta_days, 1)


def remaining_days(due_date, today=None):
    if today is None:
        today = today_date()
    return max((due_date - today).days, 0)


def remaining_value(amount_cny, due_date, cycle, today=None):
    if amount_cny is None:
        return None
    if today is None:
        today = today_date()
    if due_date <= today:
        return 0

    if cycle in {"day", "week"}:
        cycle_days = 1 if cycle == "day" else 7
        left_days = (due_date - today).days
        return amount_cny * left_days / cycle_days

    # For month/year style cycles, align to the current cycle and count full cycles ahead.
    steps = 0
    cycle_end = due_date
    cycle_start = subtract_cycle(cycle_end, cycle)
    while cycle_start > today:
        cycle_end = cycle_start
        cycle_start = subtract_cycle(cycle_end, cycle)
        steps += 1

    total_days = (cycle_end - cycle_start).days
    if total_days <= 0:
        total_days = 1
    left_days = (cycle_end - today).days
    fraction = left_days / total_days
    return amount_cny * (steps + fraction)


def as_bool(value):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
