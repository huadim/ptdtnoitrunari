"""Rules shared by manual assignments, publication, replacements and the solver."""

from bisect import bisect_left
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import timedelta
from itertools import combinations, islice

from django.db.models import Q

from .models import Decision, Employee, Request, Slot


def month_dates(month):
    return [month.replace(day=day) for day in range(1, monthrange(month.year, month.month)[1] + 1)]


def dates_between(start, end):
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def weekend(day):
    return day.weekday() >= 5


def week_index(day):
    return (day.toordinal() - day.weekday()) // 7


def pairs(ids):
    return combinations(sorted(ids), 2)


def eligible_employees(kind):
    active = Employee.objects.filter(school=kind.school, active=True)
    allowed = kind.eligible_staff.all()
    return active.filter(pk__in=allowed) if allowed.exists() else active


class Rules:
    def __init__(self, roster):
        self.roster = roster
        school = roster.duty_type.school
        days = list(roster.dutyday_set.values_list("date", "not_required"))
        self.dates = [date for date, not_required in days if not not_required]
        first, last = (days[0][0], days[-1][0]) if days else (roster.month, month_dates(roster.month)[-1])
        self.weekends = [first + timedelta(days=i) for i in range(-366, (last - first).days + 367)
                         if weekend(first + timedelta(days=i))]
        self.people = dict(eligible_employees(roster.duty_type).values_list("id", "name"))
        self.leave = defaultdict(set)
        for employee, start, end in Request.objects.filter(
            employee__school=school, status=Decision.APPROVED, slot__isnull=True, start__lte=last, end__gte=first
        ).values_list("employee_id", "start", "end"):
            self.leave[employee].update(day for day in self.dates if start <= day <= end)

        self.other = defaultdict(set)
        self.history = defaultdict(list)
        self.old_pairs = defaultdict(list)
        self.totals = Counter()
        self.weekend_totals = Counter()
        external = defaultdict(list)
        history_window = max(roster.min_gap, (max(roster.pair_rest, roster.weekend_rest) + 1) * 7)
        history_start = first - timedelta(days=max(90, history_window))
        history_end = last + timedelta(days=history_window)
        rows = Slot.objects.filter(day__roster__duty_type__school=school).exclude(day__roster=roster).filter(
            Q(day__date__range=(first, last)) |
            Q(day__roster__duty_type=roster.duty_type, day__date__range=(history_start, history_end))
        ).values_list("day__date", "day__roster__duty_type_id", "assigned_id")
        for day, kind, employee in rows:
            if first <= day <= last:
                self.other[day].add(employee)
            if kind == roster.duty_type_id:
                self.history[employee].append(day)
                external[day].append(employee)
                if day < first:
                    self.totals[employee] += 1
                    if weekend(day):
                        self.weekend_totals[employee] += 1
        for day, ids in external.items():
            if weekend(day):
                for pair in pairs(ids):
                    self.old_pairs[pair].append(day)

    def reasons(self, day, ids, assignments):
        errors = []
        if len(ids) > self.roster.people:
            errors.append(f"Đã chọn {len(ids)} người, tối đa {self.roster.people} người cho ngày này.")
        if len(set(ids)) != len(ids):
            errors.append("Có nhân sự bị chọn trùng.")
        for employee in ids:
            name = self.people.get(employee)
            if not name:
                errors.append("Có nhân sự ngừng hoạt động hoặc chưa được phép trực loại này.")
                continue
            if day in self.leave[employee]:
                errors.append(f"{name} đã được duyệt nghỉ.")
            if employee in self.other[day]:
                errors.append(f"{name} đã có nhiệm vụ trực khác trong ngày.")
            if any(abs((day - other).days) < self.roster.min_gap for other in self.history[employee]):
                errors.append(f"{name} không đủ khoảng cách tối thiểu với lịch tháng khác.")
            if any(other != day and employee in members and abs((day - other).days) < self.roster.min_gap
                   for other, members in assignments.items()):
                errors.append(f"{name} không đủ khoảng cách tối thiểu với ca khác trong tháng.")
            if weekend(day) and self.roster.weekend_rest:
                nearby = self.history[employee] + [other for other, members in assignments.items()
                                                   if other != day and employee in members]
                if any(weekend(other) and abs(week_index(day) - week_index(other)) <= self.roster.weekend_rest
                       for other in nearby):
                    errors.append(f"{name} chưa qua đủ {self.roster.weekend_rest} cuối tuần nghỉ.")
        if weekend(day) and self.roster.pair_rest:
            index = bisect_left(self.weekends, day)
            for pair in pairs(ids):
                recent = self.old_pairs[pair] + [other for other, members in assignments.items()
                                                if other != day and weekend(other) and set(pair) <= set(members)]
                if any(abs(index - bisect_left(self.weekends, other)) <= self.roster.pair_rest for other in recent):
                    errors.append("Cặp trực chưa qua đủ số ngày trực cuối tuần theo quy định.")
        return list(dict.fromkeys(errors))

    def validate(self, assignments):
        errors = []
        for day in self.dates:
            ids = assignments.get(day, [])
            if len(ids) != self.roster.people:
                errors.append(f"{day:%d/%m}: Có {len(ids)}/{self.roster.people} người.")
            errors.extend(f"{day:%d/%m}: {reason}" for reason in self.reasons(day, ids, assignments))
        return errors

    def solve(self, fixed):
        """Find a feasible roster, preferring fairness and a 7–8 day gap by default."""
        assignments = {day: tuple(ids) for day, ids in fixed.items()}
        for day, ids in fixed.items():
            reasons = self.reasons(day, ids, assignments)
            if reasons or len(ids) != self.roster.people:
                return None, f"Ngày khóa {day:%d/%m}: " + (" ".join(reasons) or "Chưa đủ người.")

        open_days = [day for day in self.dates if day not in fixed]
        candidates = {day: [employee for employee in self.people
                            if not self.reasons(day, [employee], assignments)] for day in open_days}
        shortage = [f"{day:%d/%m} ({len(ids)}/{self.roster.people})" for day, ids in candidates.items()
                    if len(ids) < self.roster.people]
        if shortage:
            return None, "Thiếu nhân sự khả dụng ngày " + ", ".join(shortage) + ". Kiểm tra ngày nghỉ và khoảng cách bắt buộc."

        # Prioritize the most constrained days, and keep a strict work budget for HTTP requests.
        open_days.sort(key=lambda day: (len(candidates[day]), day))
        totals = self.totals.copy()
        weekends = self.weekend_totals.copy()
        repeat = Counter({pair: len(dates) for pair, dates in self.old_pairs.items()})
        visits = 0
        blocked = None

        def count(day, ids, delta):
            for employee in ids:
                totals[employee] += delta
                if weekend(day):
                    weekends[employee] += delta
            for pair in pairs(ids):
                repeat[pair] += delta

        for day, ids in fixed.items():
            count(day, ids, 1)

        def penalty(day, employee):
            neighbors = self.history[employee] + [other for other, ids in assignments.items() if employee in ids]
            gap = min((abs((day - other).days) for other in neighbors), default=self.roster.preferred_max)
            distance = max(self.roster.preferred_min - gap, gap - self.roster.preferred_max, 0)
            return totals[employee] * 5 + weekends[employee] * 8 * weekend(day) + min(distance, 20) * 2

        def search(depth):
            nonlocal visits, blocked
            if depth == len(open_days):
                return True
            if visits >= 2500:
                return False
            visits += 1
            day = open_days[depth]
            available = [employee for employee in candidates[day] if not self.reasons(day, [employee], assignments)]
            scores = {employee: penalty(day, employee) for employee in available}
            shortlist = sorted(available, key=lambda employee: (scores[employee], employee))[:14]
            choices = sorted(combinations(shortlist, self.roster.people),
                             key=lambda ids: sum(scores[employee] for employee in ids)
                             + 3 * sum(repeat[pair] for pair in pairs(ids)))
            for ids in islice(choices, 50):
                if visits >= 2500:
                    break
                if self.reasons(day, ids, assignments):
                    continue
                assignments[day] = ids
                count(day, ids, 1)
                if search(depth + 1):
                    return True
                count(day, ids, -1)
                del assignments[day]
            blocked = blocked or day
            return False

        if search(0):
            return assignments, ""
        date = f" ở vùng ngày {blocked:%d/%m}" if blocked else ""
        return None, f"Chưa tìm được lịch hợp lệ{date} trong giới hạn tìm kiếm. Kiểm tra ngày khóa, ngày nghỉ hoặc giảm khoảng cách bắt buộc."
