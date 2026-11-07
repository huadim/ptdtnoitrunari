"""Independent validation and bounded CP-SAT search for one teaching week."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import groupby
from time import monotonic

DAY_NAMES = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật")
MAX_PERIODS = 500
MAX_SECONDS = 6


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

    @property
    def key(self):
        return self.teacher, self.classroom.casefold(), self.subject.casefold()

    @property
    def resources(self):
        return (("giáo viên", self.teacher), ("lớp", self.classroom.casefold()), ("phòng", self.room.casefold()))


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
                and (entry.teacher, entry.day, entry.period) not in self.unavailable)

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


def validate(problem, entries, *, complete=False):
    requirements = {item.key: item for item in problem.requirements}
    errors = []
    occupied = defaultdict(set)
    grouped = defaultdict(list)
    for entry in entries:
        when = f"{DAY_NAMES[entry.day] if 0 <= entry.day < 7 else 'Ngoài tuần'}, tiết {entry.period}"
        item = requirements.get(entry.key)
        if item is None:
            errors.append(f"H02 · {entry.subject} lớp {entry.classroom}, {when}: chưa có trong phân công.")
        else:
            grouped[entry.key].append(entry)
            if entry.room.casefold() != item.room.casefold():
                errors.append(f"H07 · {item.label}, {when}: cần phòng {item.room}.")
            if not problem.allowed(item, entry):
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
    return list(dict.fromkeys(errors))


def solve(problem, *, seconds=MAX_SECONDS):
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

    existing_counts = Counter((entry.classroom.casefold(), entry.subject.casefold(), entry.day)
                              for entry in problem.fixed)
    costs = []
    for key in used_per_day.keys() | existing_counts.keys():
        variables = used_per_day[key]
        overload = model.new_int_var(0, len(variables) + existing_counts[key], f"spread_{len(costs)}")
        model.add_max_equality(overload, [0, sum(variables) + existing_counts[key] - 2])
        costs.append(overload)
    model.minimize(sum(costs))

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
