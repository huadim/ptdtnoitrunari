from collections import Counter, defaultdict
from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Exists, OuterRef
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.timezone import localdate

from .access import employee_scope, record, require, school_view
from .common import employees, fail, page
from .exports import document
from .forms import LessonForm, WeekForm
from .models import Decision, Holiday, Lesson, Notification, Request, State, TimetablePlan, TimetableWeek
from .timetable_solver import DAY_NAMES

HEADERS = ("Ngày", "Tiết", "Lớp", "Môn", "Giáo viên", "Phòng")
FILTERS = (("class", "classroom", "Lớp"), ("teacher", "teacher__code", "Giáo viên"), ("room", "room", "Phòng"))


def get_week(request, pk):
    week = get_object_or_404(TimetableWeek, school=request.school, pk=pk)
    if week.state != State.PUBLISHED and not request.rights["all_timetable"]:
        raise PermissionDenied
    return week


def visible_weeks(request):
    weeks = TimetableWeek.objects.filter(school=request.school)
    return weeks if request.rights["all_timetable"] else weeks.filter(state=State.PUBLISHED)


def filter_values(request):
    return {key: value for key, _, _ in FILTERS if (value := request.GET.get(key, "").strip())}


def filtered_lessons(rows, values):
    return rows.filter(**{f"{field}__iexact": values[key] for key, field, _ in FILTERS if key in values})


def scoped_lessons(request, week):
    return employee_scope(request, week.lesson_set.select_related("teacher"), "teacher", "all_timetable")


def schedule_board(request, rows, all_lessons, requirements, filters, choices, selected):
    """Build the resource navigator and related schedules from the user's visible scope."""
    scope_keys = {key for key, _, _ in FILTERS}
    scope = request.GET.get("scope", "")
    if scope not in scope_keys:
        scope = next(iter(filters), "")
    tabs = [("", "Toàn trường", ""), *[(key, label, urlencode({"scope": key})) for key, _, label in FILTERS]]
    resource_options = []
    if scope:
        for key, _, options in choices:
            if key == scope:
                resource_options = [(name, active, urlencode({"scope": scope, scope: value}))
                                    for value, name, active in options]

    planned = None
    remaining = []
    if requirements is not None:
        placed = Counter((entry.teacher_id, entry.classroom.casefold(), entry.subject.casefold()) for entry in rows)
        planned = 0
        for item in requirements:
            resource_values = {"class": item.classroom, "teacher": item.teacher.code, "room": item.room}
            if any(resource_values[key].casefold() != value.casefold() for key, value in filters.items()):
                continue
            planned += item.periods
            missing = item.periods - placed[item.teacher_id, item.classroom.casefold(), item.subject.casefold()]
            if missing > 0:
                remaining.append((item.classroom, item.subject, item.teacher.name, missing))

    related = []
    if selected:
        related = [
            (label, title, [lesson for lesson in all_lessons if getattr(lesson, field) == value])
            for label, title, field, value in (
                ("Lớp học", selected.classroom, "classroom", selected.classroom),
                ("Giáo viên", selected.teacher.name, "teacher_id", selected.teacher_id),
                ("Phòng học", selected.room, "room", selected.room),
            )
        ]
    return {"board_scope": scope, "board_tabs": tabs, "resource_options": resource_options,
            "planned_count": planned, "remaining_rows": remaining[:30], "missing_count": sum(item[-1] for item in remaining),
            "related": related, "selected_lesson": selected}


@school_view
def timetable(request):
    if request.method == "POST":
        require(request, "plan")
        form = WeekForm(request.POST)
        if form.is_valid():
            week, created = TimetableWeek.objects.get_or_create(
                school=request.school, monday=form.cleaned_data["monday"],
                defaults={"academic_year": form.cleaned_data["academic_year"]},
            )
            if created:
                record(request, "timetable.create", f"Tuần #{week.pk}")
            return redirect("week", pk=week.pk)
    else:
        today = localdate()
        form = WeekForm(initial={"monday": today - timedelta(days=today.weekday())})
    weeks = visible_weeks(request).order_by("-monday")
    return page(request, "timetable", weeks=weeks[:100], week_form=form)


@school_view
def week_detail(request, pk):
    week = get_week(request, pk)
    can_edit = request.rights["plan"] and week.state == State.DRAFT
    filters = filter_values(request)
    list_view = request.GET.get("view") == "list"
    scope = request.GET.get("scope", "")
    screen_query = urlencode({**filters, **({"view": "list"} if list_view else {}),
                              **({"scope": scope} if scope in {"class", "teacher", "room"} else {})})
    week_url = reverse("week", args=[pk])
    screen_url = f"{week_url}?{screen_query}" if screen_query else week_url
    form, edit_id = None, None
    if request.method == "POST":
        action = request.POST.get("action")
        if can_edit and action in {"add", "edit"}:
            form = LessonForm(request.POST, teachers=employees(request))
            if action == "edit":
                edit_id = request.POST.get("id", "0")
        try:
            change_week(request, week, form)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Mã dữ liệu không hợp lệ.")
        except ValidationError as exc:
            if form is None:
                messages.error(request, " ".join(exc.messages))
            else:
                form.add_error(None, exc)
        if form is None or not form.errors:
            return redirect(screen_url)

    scoped = scoped_lessons(request, week)
    all_lessons = list(scoped)
    rows = list(filtered_lessons(scoped, filters)) if filters else all_lessons
    days = [week.monday + timedelta(days=offset) for offset in range(min(6, (date.max - week.monday).days) + 1)]
    cells = defaultdict(list)
    for lesson in rows:
        cells[lesson.date, lesson.period].append(lesson)
    plan = TimetablePlan.objects.filter(week=week).first()
    configured_periods = plan.morning + plan.afternoon if plan else 5
    grid = [(period, [(day, cells[day, period]) for day in days])
            for period in range(1, max(configured_periods, max((lesson.period for lesson in rows), default=0)) + 1)]

    choices = []
    visible_requirements = list(employee_scope(request, plan.requirements.select_related("teacher"),
                                               "teacher", "all_timetable")) if plan else None
    resources = (*all_lessons, *(visible_requirements or ()))
    for key, _, label in FILTERS:
        selected = filters.get(key, "")
        if key == "teacher":
            values = {lesson.teacher.code: f"{lesson.teacher.name} · {lesson.teacher.code}" for lesson in resources}
        else:
            field = "classroom" if key == "class" else "room"
            values = {getattr(lesson, field): getattr(lesson, field) for lesson in resources}
        if selected and not any(value.casefold() == selected.casefold() for value in values):
            values[selected] = selected
        choices.append((key, label, [(value, name, value.casefold() == selected.casefold())
                                    for value, name in sorted(values.items(), key=lambda item: item[1].casefold())]))

    editor_open = form is not None
    move_preview, move_errors = False, []
    if can_edit and form is None:
        teachers = employees(request)
        item = next((lesson for lesson in all_lessons if str(lesson.pk) == request.GET.get("edit")), None)
        if item:
            initial = {field: getattr(item, field) for field in ("date", "period", "classroom", "subject", "room")}
            initial["teacher"] = item.teacher_id
            edit_id = item.pk
            target = next((day for day in days if day.isoformat() == request.GET.get("target_day")), None)
            target_period = request.GET.get("target_period", "")
            if target and target_period in map(str, range(1, 16)):
                initial.update(date=target, period=int(target_period))
                move_preview = True
                move_errors = lesson_conflicts(request, week, initial | {"teacher": item.teacher}, item)
        else:
            day = next((day for day in days if day.isoformat() == request.GET.get("day")), week.monday)
            period = request.GET.get("period", "1")
            initial = {"date": day, "period": period if period in map(str, range(1, 16)) else 1,
                       "classroom": filters.get("class"), "room": filters.get("room")}
            if "teacher" in filters:
                initial["teacher"] = teachers.filter(code__iexact=filters["teacher"]).values_list("pk", flat=True).first()
        form = LessonForm(teachers=teachers, initial=initial)
        editor_open = bool(item or "day" in request.GET or "add" in request.GET)

    weeks = visible_weeks(request)
    selected_lesson = next((lesson for lesson in rows if str(lesson.pk) == request.GET.get("focus")), None)
    return page(request, "week", week=week, days=[(day, DAY_NAMES[day.weekday()]) for day in days], grid=grid,
                last_day=days[-1],
                lessons=rows, can_edit=can_edit, form=form, edit_id=edit_id, editor_open=editor_open,
                previous_week=weeks.filter(monday__lt=week.monday).order_by("-monday").first(),
                next_week=weeks.filter(monday__gt=week.monday).order_by("monday").first(),
                choices=choices, list_view=list_view, export_query=urlencode(filters),
                screen_query=screen_query, screen_url=screen_url, today=localdate(), plan=plan,
                move_preview=move_preview, move_errors=move_errors,
                **schedule_board(request, rows, all_lessons, visible_requirements, filters, choices, selected_lesson))


def lesson_conflicts(request, week, data, item):
    day, period, teacher = data["date"], data["period"], data["teacher"]
    if not 0 <= (day - week.monday).days < 7:
        return ["Ngày học phải thuộc tuần đang mở."]
    if Holiday.objects.filter(school=request.school, date=day).exists():
        return ["H09 · Ngày này đã được cấu hình là ngày lễ."]
    if Request.objects.filter(employee=teacher, status=Decision.APPROVED, slot__isnull=True,
                              start__lte=day, end__gte=day).exists():
        return [f"H08 · {teacher.name} đã được duyệt nghỉ vào ngày này."]
    same = week.lesson_set.filter(date=day, period=period)
    if item:
        same = same.exclude(pk=item.pk)
    errors = []
    for field, label, code in (("teacher", "giáo viên", "H03"), ("classroom", "lớp", "H04"), ("room", "phòng", "H06")):
        if same.filter(**{field: data[field]}).exists():
            name = teacher.name if field == "teacher" else data[field]
            errors.append(f"Trùng {label} ở ngày và tiết đã chọn. {code} · {name}, {day:%d/%m/%Y}, tiết {period}.")
    plan = TimetablePlan.objects.filter(week=week).first()
    if plan:
        from .timetable_planning import problem_for, to_entry
        from .timetable_solver import Entry, validate

        problem = problem_for(week, plan)
        old_entry = to_entry(week, item) if item else None
        new_entry = Entry(teacher.pk, data["classroom"], data["subject"], data["room"],
                          (day - week.monday).days, period)
        baseline = set(validate(problem, problem.fixed))
        after = (*[entry for entry in problem.fixed if entry != old_entry], new_entry)
        errors.extend(issue for issue in validate(problem, after) if issue not in baseline)
    return errors


def change_week(request, week, form):
    action = request.POST.get("action")
    if action in {"add", "edit", "remove", "submit"}:
        require(request, "plan")
        if week.state != State.DRAFT:
            fail("Chỉ chỉnh sửa tuần ở trạng thái nháp.")
    elif action in {"return", "approve", "publish"}:
        require(request, "duty_approve")
    else:
        fail("Thao tác không hợp lệ.")
    rows = week.lesson_set
    revision = request.POST.get("revision")
    if revision is not None and revision != str(week.revision):
        fail("Tuần đã được người khác cập nhật. Mở lại tuần trước khi lưu thay đổi.")
    if action in {"add", "edit"}:
        item = get_object_or_404(rows, pk=request.POST.get("id", "0")) if action == "edit" else None
        if not form.is_valid():
            return
        data = form.cleaned_data
        if errors := lesson_conflicts(request, week, data, item):
            raise ValidationError(errors)
        if item:
            rows.filter(pk=item.pk).update(**data)
        else:
            item = Lesson.objects.create(week=week, **data)
    elif action == "remove":
        item = get_object_or_404(rows, pk=request.POST.get("id", "0"))
        item.delete()
    else:
        old, new = {
            "submit": ([State.DRAFT], State.SUBMITTED),
            "approve": ([State.SUBMITTED], State.APPROVED),
            "publish": ([State.APPROVED], State.PUBLISHED),
            "return": ([State.SUBMITTED, State.APPROVED], State.DRAFT),
        }[action]
        if week.state not in old:
            fail("Tuần không ở trạng thái phù hợp.")
        if action != "return":
            if not rows.exists():
                fail("Cần nhập ít nhất một tiết.")
            if rows.filter(teacher__active=False).exists() or rows.filter(
                date__in=Holiday.objects.filter(school=request.school).values("date")
            ).exists():
                fail("Có tiết rơi vào ngày lễ hoặc có giáo viên đã ngừng hoạt động.")
            unavailable = Request.objects.filter(employee=OuterRef("teacher"), status=Decision.APPROVED,
                                                 slot__isnull=True, start__lte=OuterRef("date"), end__gte=OuterRef("date"))
            if rows.filter(Exists(unavailable)).exists():
                fail("Có tiết học trùng ngày nghỉ đã được duyệt của giáo viên.")
            plan = TimetablePlan.objects.filter(week=week).first()
            if plan:
                from .timetable_planning import problem_for
                from .timetable_solver import validate

                problem = problem_for(week, plan)
                errors = validate(problem, problem.fixed, complete=True)
                if errors:
                    raise ValidationError(errors[:8])
        week.state = new
        week.save(update_fields=["state"])
        if action == "publish":
            Notification.objects.bulk_create([
                Notification(employee_id=teacher, text=f"Đã công bố thời khóa biểu tuần từ {week.monday:%d/%m/%Y}.")
                for teacher in set(rows.values_list("teacher_id", flat=True))
            ])
    week.revision += 1
    week.save(update_fields=["revision"])
    suffix = f", tiết #{item.pk}" if action in {"add", "edit"} else ""
    record(request, f"timetable.{action}", f"Tuần #{week.pk}{suffix}")
    messages.success(request, "Đã lưu thời khóa biểu.")


@school_view
def timetable_export(request, pk, format):
    if request.method == "POST":
        return HttpResponseBadRequest()
    week = get_week(request, pk)
    rows = [(row.date.strftime("%d/%m/%Y"), row.period, row.classroom, row.subject, row.teacher.name, row.room)
            for row in filtered_lessons(scoped_lessons(request, week), filter_values(request))]
    return document(request, "THỜI KHÓA BIỂU", f"Năm học {week.academic_year} · Tuần từ {week.monday:%d/%m/%Y}"
                    f" · {week.get_state_display()}", HEADERS, rows, f"thoi-khoa-bieu-{week.monday}", format,
                    landscape=True)
