"""Independent validation and bounded CP-SAT search for one teaching week."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import groupby
from time import monotonic

DAY_NAMES = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật")
MAX_PERIODS = 500
MAX_SECONDS = 6
BREAK_FIELDS = {"class": "classroom", "subject": "subject", "room": "room"}


@dataclass(frozen=True)
class Requirement:
    teacher: int
    teacher_label: str
    classroom: str
    subject: str
    room: str
    periods: int
    doubles: int = 0
    session: str = "any"

    @property
    def key(self):
        return self.teacher, self.classroom.casefold(), self.subject.casefold()

    @property
    def label(self):
        return f"{self.subject} lớp {self.classroom} ({self.teacher_label})"


@dataclass(frozen=True)
class Entry:
    teacher: int
    classroom: str
    subject: str
    room: str
    day: int
    period: int
    pinned: bool = False

    @property
    def key(self):
        return self.teacher, self.classroom.casefold(), self.subject.casefold()

    @property
    def resources(self):
        return (("giáo viên", self.teacher), ("lớp", self.classroom.casefold()), ("phòng", self.room.casefold()))


@dataclass(frozen=True)
class TeacherLimit:
    teacher: int
    label: str
    days: int | None
    daily: int | None
    session: int | None


@dataclass(frozen=True)
class Problem:
    days: tuple
    morning: int
    afternoon: int
    requirements: tuple
    fixed: tuple
    unavailable: frozenset
    holidays: frozenset
    inactive: frozenset
    resource_breaks: frozenset = frozenset()
    teacher_limits: tuple = ()

    def session_at(self, period):
        if 1 <= period <= self.morning:
            return "am"
        if self.morning < period <= self.morning + self.afternoon:
            return "pm"
        return ""

    def allowed(self, requirement, entry):
        return (entry.day in self.days and entry.day not in self.holidays
                and bool(session := self.session_at(entry.period))
                and requirement.session in ("any", session)
                and entry.teacher not in self.inactive
                and (entry.teacher, entry.day, entry.period) not in self.unavailable
                and not self.is_break(entry))

    def is_break(self, entry):
        return bool(self.resource_breaks) and any(
            (kind, getattr(entry, field).casefold(), entry.day, entry.period) in self.resource_breaks
            for kind, field in BREAK_FIELDS.items()
        )

    def double_count(self, entries):
        by_session = defaultdict(list)
        for entry in entries:
            by_session[entry.day, self.session_at(entry.period)].append(entry.period)
        count = 0
        for periods in by_session.values():
            for _, group in groupby(enumerate(sorted(set(periods))), key=lambda pair: pair[1] - pair[0]):
                count += len(list(group)) // 2
        return count


@dataclass(frozen=True)
class Result:
    status: str
    entries: tuple = ()
    messages: tuple = ()
    seconds: float = 0
    day_spread_cost: int | None = None


def validate(problem, entries, *, complete=False, previous=()):
    entries = tuple(entries)
    requirements = {item.key: item for item in problem.requirements}
    errors = []
    occupied = defaultdict(set)
    grouped = defaultdict(list)
    for entry in entries:
        when = f"{DAY_NAMES[entry.day] if 0 <= entry.day < 7 else 'Ngoài tuần'}, tiết {entry.period}"
        if problem.is_break(entry):
            errors.append(f"H12 · {entry.subject} lớp {entry.classroom}, phòng {entry.room}, {when}: trùng tiết nghỉ cố định.")
        item = requirements.get(entry.key)
        if item is None:
            errors.append(f"H02 · {entry.subject} lớp {entry.classroom}, {when}: chưa có trong phân công.")
        else:
            grouped[entry.key].append(entry)
            if entry.room.casefold() != item.room.casefold():
                errors.append(f"H07 · {item.label}, {when}: cần phòng {item.room}.")
            if not problem.allowed(item, entry) and not problem.is_break(entry):
                errors.append(f"H08/H09 · {item.label}, {when}: ngoài buổi cho phép, ngày nghỉ hoặc giáo viên không khả dụng.")
        for kind, resource in entry.resources:
            slot = (entry.day, entry.period)
            if slot in occupied[kind, resource]:
                errors.append(f"H03–H06 · {entry.subject} lớp {entry.classroom}, {when}: trùng {kind}.")
            occupied[kind, resource].add(slot)

    for item in problem.requirements:
        assigned = grouped[item.key]
        remaining = item.periods - len(assigned)
        missing_pairs = max(0, item.doubles - problem.double_count(assigned))
        if remaining < 0:
            errors.append(f"H02 · {item.label}: vượt {abs(remaining)} tiết so với phân công.")
        elif complete:
            if remaining:
                errors.append(f"H02 · {item.label}: thiếu {remaining} tiết.")
            if missing_pairs:
                errors.append(f"H11 · {item.label}: thiếu {missing_pairs} cặp tiết liên tiếp.")
    if problem.teacher_limits:
        errors.extend(teacher_limit_errors(problem, entries, previous))
    return list(dict.fromkeys(errors))


def teacher_load(problem, entries):
    sessions, days = defaultdict(Counter), defaultdict(Counter)
    for entry in entries:
        if 0 <= entry.day < 7 and (session := problem.session_at(entry.period)):
            sessions[entry.teacher][entry.day, session] += 1
            days[entry.teacher][entry.day] += 1
    return sessions, days


def teacher_limit_errors(problem, entries, previous):
    sessions, days = teacher_load(problem, entries)
    old_sessions, old_days = teacher_load(problem, previous)
    errors = []
    for limit in problem.teacher_limits:
        teacher = limit.teacher
        for (day, session), count in sessions[teacher].items():
            if limit.session is not None and count > max(limit.session, old_sessions[teacher][day, session]):
                label = "sáng" if session == "am" else "chiều"
                errors.append(f"H13 · {limit.label}: {DAY_NAMES[day]} buổi {label} có {count} tiết, tối đa {limit.session}.")
        for day, count in days[teacher].items():
            if limit.daily is not None and count > max(limit.daily, old_days[teacher][day]):
                errors.append(f"H13 · {limit.label}: {DAY_NAMES[day]} có {count} tiết, tối đa {limit.daily}/ngày.")
        count = len(days[teacher])
        if limit.days is not None and count > max(limit.days, len(old_days[teacher])):
            errors.append(f"H13 · {limit.label}: dạy {count} ngày, tối đa {limit.days} ngày/tuần.")
    return errors


def add_teacher_limits(model, problem, choices):
    ids = {limit.teacher for limit in problem.teacher_limits}
    fixed = Counter((entry.teacher, entry.day, problem.session_at(entry.period))
                    for entry in problem.fixed if entry.teacher in ids)
    options = defaultdict(list)
    for selected, entry in choices:
        if entry.teacher in ids:
            options[entry.teacher, entry.day, problem.session_at(entry.period)].append(selected)
    for limit in problem.teacher_limits:
        used_days = []
        for day in problem.days:
            total_fixed = 0
            total_options = []
            for session in ("am", "pm"):
                key = limit.teacher, day, session
                total_fixed += fixed[key]
                total_options.extend(options[key])
                if limit.session is not None and options[key]:
                    model.add(sum(options[key]) + fixed[key] <= limit.session)
            if limit.daily is not None and total_options:
                model.add(sum(total_options) + total_fixed <= limit.daily)
            if limit.days is not None:
                if total_fixed:
                    used_days.append(1)
                elif total_options:
                    used = model.new_bool_var(f"teach_{limit.teacher}_{day}")
                    model.add(sum(total_options) >= 1).only_enforce_if(used)
                    model.add(sum(total_options) == 0).only_enforce_if(used.Not())
                    used_days.append(used)
        if limit.days is not None:
            model.add(sum(used_days) <= limit.days)


def teacher_gap_terms(model, problem, choices):
    slots = defaultdict(lambda: defaultdict(list))
    for entry in problem.fixed:
        slots[entry.teacher, entry.day, problem.session_at(entry.period)][entry.period].append(1)
    for selected, entry in choices:
        slots[entry.teacher, entry.day, problem.session_at(entry.period)][entry.period].append(selected)
    gaps = []
    for (teacher, day, _), periods in slots.items():
        occupancy = [sum(periods[period]) for period in range(min(periods), max(periods) + 1)]
        for index in range(1, len(occupancy) - 1):
            before = model.new_bool_var(f"before_{teacher}_{day}_{len(gaps)}")
            after = model.new_bool_var(f"after_{teacher}_{day}_{len(gaps)}")
            gap = model.new_bool_var(f"gap_{teacher}_{day}_{len(gaps)}")
            model.add_max_equality(before, occupancy[:index])
            model.add_max_equality(after, occupancy[index + 1:])
            model.add_min_equality(gap, [before, after, 1 - occupancy[index]])
            gaps.append(gap)
    return gaps


def solve(problem, *, seconds=MAX_SECONDS, preferred=None):
    from ortools.sat.python import cp_model

    start = monotonic()

    def outcome(status, entries=(), messages=(), cost=None):
        return Result(status, tuple(entries), tuple(messages), monotonic() - start, cost)

    total = sum(item.periods for item in problem.requirements)
    if not 1 <= total <= MAX_PERIODS:
        return outcome("INVALID_DATA", messages=(f"Cần khai báo từ 1 đến {MAX_PERIODS} tiết cho tuần.",))
    errors = validate(problem, problem.fixed)
    if errors:
        return outcome("INVALID_DATA", messages=errors)
    if seconds <= 0:
        return outcome("UNKNOWN", messages=("Đã hết thời gian tìm kiếm.",))

    fixed_by_key = defaultdict(list)
    busy = set()
    for entry in problem.fixed:
        fixed_by_key[entry.key].append(entry)
        busy.update((resource, entry.day, entry.period) for resource in entry.resources)

    model = cp_model.CpModel()
    candidates = defaultdict(list)
    choices = []
    used_per_day = defaultdict(list)
    capacity_needed = Counter()
    capacity_slots = defaultdict(set)
    for item in problem.requirements:
        existing = fixed_by_key[item.key]
        missing = item.periods - len(existing)
        missing_pairs = max(0, item.doubles - problem.double_count(existing))
        if missing == 0:
            if missing_pairs:
                errors.append(f"H11 · {item.label}: đã đủ tiết nhưng thiếu {missing_pairs} cặp cùng buổi. "
                              "Hãy sửa vị trí tiết trên lưới.")
            continue
        options = []
        fixed_slots = {(entry.day, entry.period) for entry in existing}
        for day in problem.days:
            for period in range(1, problem.morning + problem.afternoon + 1):
                entry = Entry(item.teacher, item.classroom, item.subject, item.room, day, period)
                if problem.allowed(item, entry) and all((resource, day, period) not in busy for resource in entry.resources):
                    options.append(entry)
                    for resource in entry.resources:
                        capacity_slots[resource].add((day, period))
        if len(options) < missing:
            errors.append(f"H02 · {item.label}: cần thêm {missing} tiết nhưng chỉ còn {len(options)} vị trí hợp lệ.")
        for resource in Entry(item.teacher, item.classroom, item.subject, item.room, 0, 1).resources:
            capacity_needed[resource] += missing

        entry_vars = {}
        for entry in options:
            selected = model.new_bool_var(f"e_{len(choices)}")
            choices.append((selected, entry))
            entry_vars[entry.day, entry.period] = selected
            used_per_day[item.classroom.casefold(), item.subject.casefold(), entry.day].append(selected)
            for resource in entry.resources:
                candidates[resource, entry.day, entry.period].append(selected)
        model.add(sum(entry_vars.values()) == missing)

        if missing_pairs:
            pairs = []
            pair_usage = defaultdict(list)
            all_slots = fixed_slots | entry_vars.keys()
            for day, period in sorted(all_slots):
                first, second = (day, period), (day, period + 1)
                if second not in all_slots or problem.session_at(period) != problem.session_at(period + 1):
                    continue
                pair = model.new_bool_var(f"pair_{len(pairs)}")
                pairs.append(pair)
                for slot in (first, second):
                    pair_usage[slot].append(pair)
                    if slot in entry_vars:
                        model.add(pair <= entry_vars[slot])
            if len(pairs) < item.doubles:
                errors.append(f"H11 · {item.label}: không đủ vị trí cho {item.doubles} cặp tiết cùng buổi.")
            model.add(sum(pairs) >= item.doubles)
            for variables in pair_usage.values():
                model.add_at_most_one(variables)

    for (kind, value), needed in capacity_needed.items():
        available = len(capacity_slots[kind, value])
        if needed > available:
            label = next((item.teacher_label for item in problem.requirements if item.teacher == value), value) \
                if kind == "giáo viên" else value
            errors.append(f"H03–H06 · {kind.capitalize()} {label}: cần thêm {needed} tiết nhưng chỉ có {available} vị trí.")
    if errors:
        return outcome("INFEASIBLE", messages=errors)
    if not any(item.periods > len(fixed_by_key[item.key]) for item in problem.requirements):
        errors = validate(problem, problem.fixed, complete=True)
        if errors:
            return outcome("INFEASIBLE", messages=errors)
        return outcome("OPTIMAL", messages=("Lịch hiện tại đã đủ tiết.",), cost=spread_cost(problem.fixed))

    for variables in candidates.values():
        model.add_at_most_one(variables)
    if problem.teacher_limits:
        add_teacher_limits(model, problem, choices)

    existing_counts = Counter((entry.classroom.casefold(), entry.subject.casefold(), entry.day)
                              for entry in problem.fixed)
    costs = []
    for key in used_per_day.keys() | existing_counts.keys():
        variables = used_per_day[key]
        overload = model.new_int_var(0, len(variables) + existing_counts[key], f"spread_{len(costs)}")
        model.add_max_equality(overload, [0, sum(variables) + existing_counts[key] - 2])
        costs.append(overload)
    quality = sum(costs)
    if preferred is not None:
        quality += 2 * sum(teacher_gap_terms(model, problem, choices))
        moved = []
        for selected, entry in choices:
            model.add_hint(selected, int(entry in preferred))
            if entry not in preferred:
                moved.append(selected)
        quality = quality * (MAX_PERIODS + 1) + sum(moved)
    model.minimize(quality)

    remaining_time = seconds - (monotonic() - start)
    if remaining_time <= 0:
        return outcome("UNKNOWN", messages=("Đã hết thời gian dựng mô hình xếp lịch.",))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = remaining_time
    solver.parameters.num_search_workers = 1
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        messages = {
            cp_model.INFEASIBLE: "Các ràng buộc đồng thời không có nghiệm. Kiểm tra phòng dùng chung, tiết đôi và giờ bận.",
            cp_model.UNKNOWN: "Đã hết thời gian tìm kiếm. Có thể thử với ít phân công hơn hoặc thêm khung giờ hợp lệ.",
            cp_model.MODEL_INVALID: "Bộ giải báo mô hình không hợp lệ.",
        }
        return outcome(solver.status_name(status), messages=(messages.get(status, "Bộ giải gặp lỗi kỹ thuật."),))

    proposed = tuple(entry for selected, entry in choices if solver.value(selected))
    all_entries = (*problem.fixed, *proposed)
    errors = validate(problem, all_entries, complete=True)
    if errors:
        return outcome("VALIDATION_ERROR", messages=errors)
    return outcome(solver.status_name(status), proposed, cost=spread_cost(all_entries))


def spread_cost(entries):
    counts = Counter((entry.classroom.casefold(), entry.subject.casefold(), entry.day) for entry in entries)
    return sum(max(0, count - 2) for count in counts.values())


def teacher_gap_cost(problem, entries):
    periods = defaultdict(set)
    for entry in entries:
        periods[entry.teacher, entry.day, problem.session_at(entry.period)].add(entry.period)
    return sum(max(values) - min(values) + 1 - len(values) for values in periods.values())
