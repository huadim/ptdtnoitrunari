from collections import Counter, defaultdict

from django.http import Http404
from django.views.decorators.http import require_safe

from .access import employee_scope, require, school_view
from .common import page
from .models import TimetablePlan
from .timetable import get_week, scoped_lessons

RESOURCES = {
    "class": ("lớp học", "classroom"),
    "teacher": ("giáo viên", "teacher_id"),
    "room": ("phòng học", "room"),
    "lesson": ("tiết", None),
}
GROUPS = {"slot": "thời điểm", "session": "buổi học"}


def grouped_lessons(lessons, group, field, morning):
    grouped = defaultdict(set)
    for lesson in lessons:
        slot = lesson.period if group == "slot" else int(lesson.period > morning)
        value = getattr(lesson, field) if field else lesson.pk
        grouped[lesson.date, slot].add(value.casefold() if isinstance(value, str) else value)
    return [
        (f"{day:%d/%m/%Y}", f"Tiết {slot}" if group == "slot" else ("Chiều" if slot else "Sáng"),
         len(values))
        for (day, slot), values in sorted(grouped.items())
    ]


def compare_periods(planned, lessons, field, has_plan):
    def label(row):
        return f"{row.teacher.name} · {row.teacher.code}" if field == "teacher" else row.subject

    target = Counter()
    for row in planned:
        target[label(row)] += row.periods
    placed = Counter(label(row) for row in lessons)
    return [(name, target[name] if has_plan else "—", placed[name])
            for name in sorted(target.keys() | placed.keys(), key=str.casefold)]


@require_safe
@school_view
def timetable_report(request, pk, report):
    week = get_week(request, pk)
    if report == "assignments":
        require(request, "plan")
    resource, _, group = report.partition("-")
    is_grouped = resource in RESOURCES and group in GROUPS
    if not is_grouped and report not in {"overview", "assignments", "subjects", "teacher-periods"}:
        raise Http404

    plan = TimetablePlan.objects.filter(week=week).first()
    lessons = list(scoped_lessons(request, week))
    planned = (
        list(employee_scope(request, plan.requirements.select_related("teacher"), "teacher", "all_timetable"))
        if plan and not is_grouped else []
    )
    if is_grouped:
        label, field = RESOURCES[resource]
        title = f"Thống kê số {label} theo {GROUPS[group]}"
        headers = ("Ngày", "Tiết" if group == "slot" else "Buổi", f"Số {label}")
        rows = grouped_lessons(lessons, group, field, plan.morning if plan else 5)
    elif report == "assignments":
        title = "Bảng phân công giảng dạy"
        headers = ("Giáo viên", "Lớp", "Môn", "Phòng", "Số tiết", "Tiết đôi", "Buổi")
        rows = [(row.teacher, row.classroom, row.subject, row.room, row.periods, row.doubles,
                 row.get_session_display()) for row in planned]
    elif report in {"subjects", "teacher-periods"}:
        is_subjects = report == "subjects"
        title = "Dữ liệu môn học" if is_subjects else "Thống kê số tiết của giáo viên"
        headers = ("Môn học" if is_subjects else "Giáo viên", "Số tiết theo phân công", "Số tiết đã xếp")
        rows = compare_periods(planned, lessons, "subject" if is_subjects else "teacher", bool(plan))
    else:
        title = "Thống kê thời khóa biểu"
        headers = ("Chỉ tiêu", "Số lượng")
        rows = [
            ("Lớp có tiết", len({row.classroom.casefold() for row in lessons})),
            ("Giáo viên có tiết", len({row.teacher_id for row in lessons})),
            ("Phòng có tiết", len({row.room.casefold() for row in lessons})),
            ("Tiết đã xếp", len(lessons)),
            ("Số dòng phân công", len(planned) if plan else "—"),
            ("Tiết theo phân công", sum(row.periods for row in planned) if plan else "—"),
        ]
    return page(request, "timetable_report", menu_week=week, title=title, headers=headers, rows=rows,
                grouped=is_grouped, session_note=is_grouped and group == "session" and not plan)
