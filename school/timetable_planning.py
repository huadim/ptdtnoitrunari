"""Teaching plans, Excel intake and reviewable scheduling proposals."""

from dataclasses import asdict, replace
from datetime import date, timedelta
from hashlib import sha256
from io import BytesIO
from json import dumps
from zipfile import BadZipFile, ZipFile

from django import forms
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.http import FileResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from .access import record, require, school_view
from .common import employees, fail, page, post_form
from .forms import date_field
from .models import Decision, Employee, Holiday, Lesson, Request, State, TeachingBreak, TeachingRequirement, TimetablePlan
from .timetable import get_week
from .timetable_solver import (BREAK_FIELDS, DAY_NAMES, MAX_PERIODS, Entry, Problem, Requirement, TeacherLimit,
                               review_slots, solve, spread_cost, teacher_gap_cost, unplaced_requirements, validate)

EXCEL_HEADERS = ("Mã giáo viên", "Lớp", "Môn", "Phòng", "Số tiết", "Số cặp tiết đôi", "Buổi")
SESSION_NAMES = {"sáng": "am", "chiều": "pm", "cả hai": "any", "": "any"}
MAX_ROWS = 500
TOKEN_LIFETIME = 1200


class PlanForm(forms.Form):
    days = forms.MultipleChoiceField(label="Ngày học", choices=[(str(i), name) for i, name in enumerate(DAY_NAMES)],
                                     widget=forms.CheckboxSelectMultiple)
    morning = forms.IntegerField(label="Số tiết buổi sáng", min_value=0, max_value=10)
    afternoon = forms.IntegerField(label="Số tiết buổi chiều", min_value=0, max_value=10)

    def clean(self):
        data = super().clean()
        if "morning" in data and "afternoon" in data and not 1 <= data["morning"] + data["afternoon"] <= 15:
            raise ValidationError("Tổng số tiết sáng và chiều phải từ 1 đến 15.")
        return data


class RequirementForm(forms.Form):
    teacher = forms.ModelChoiceField(label="Giáo viên", queryset=None)
    classroom = forms.CharField(label="Lớp", max_length=40)
    subject = forms.CharField(label="Môn", max_length=80)
    room = forms.CharField(label="Phòng", max_length=40)
    periods = forms.IntegerField(label="Số tiết trong tuần", min_value=1, max_value=30)
    doubles = forms.IntegerField(label="Số cặp tiết đôi", min_value=0, max_value=15, initial=0)
    session = forms.ChoiceField(label="Buổi được xếp", choices=TeachingRequirement.Session, initial="any")

    def __init__(self, *args, teachers, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["teacher"].queryset = teachers

    def clean(self):
        data = super().clean()
        if "periods" in data and "doubles" in data and 2 * data["doubles"] > data["periods"]:
            self.add_error("doubles", "Số tiết của các cặp tiết đôi vượt số tiết phân công.")
        for field in ("classroom", "subject", "room"):
            if field in data:
                data[field] = " ".join(data[field].split()).upper()
        return data


class PeriodRangeForm(forms.Form):
    date = date_field("Ngày")
    first = forms.IntegerField(label="Từ tiết", min_value=1, max_value=15)
    last = forms.IntegerField(label="Đến tiết", min_value=1, max_value=15)

    def __init__(self, *args, week, **kwargs):
        super().__init__(*args, **kwargs)
        self.week = week

    def clean(self):
        data = super().clean()
        if "date" in data and not 0 <= (data["date"] - self.week.monday).days < 7:
            self.add_error("date", "Ngày phải thuộc tuần đang mở.")
        if "first" in data and "last" in data and data["first"] > data["last"]:
            self.add_error("last", "Tiết kết thúc phải từ tiết bắt đầu trở đi.")
        return data


class UnavailableForm(PeriodRangeForm):
    teacher = forms.ModelChoiceField(label="Giáo viên", queryset=None)
    field_order = ("teacher", "date", "first", "last")

    def __init__(self, *args, teachers, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["teacher"].queryset = teachers


class TeachingBreakForm(PeriodRangeForm):
    target = forms.ChoiceField(label="Lớp, môn hoặc phòng")
    field_order = ("target", "date", "first", "last")

    def __init__(self, *args, plan, **kwargs):
        super().__init__(*args, **kwargs)
        self.plan = plan
        choices = []
        if plan:
            fields = tuple(BREAK_FIELDS[kind] for kind, _ in TeachingBreak.Kind.choices)
            rows = list(plan.requirements.values_list(*fields)) + list(self.week.lesson_set.values_list(*fields))
            for index, (kind, label) in enumerate(TeachingBreak.Kind.choices):
                names = {row[index] for row in rows}
                resources = {name.casefold(): name for name in sorted(names)}
                choices.append((label, [(f"{kind}:{name}", name) for name in sorted(resources.values(), key=str.casefold)]))
        self.fields["target"].choices = [("", "Chọn đối tượng"), *choices]

    def clean(self):
        data = super().clean()
        if self.plan and "last" in data and data["last"] > self.plan.morning + self.plan.afternoon:
            self.add_error("last", "Tiết kết thúc vượt quá khung học của tuần.")
        return data


class TeacherLimitForm(forms.Form):
    teacher = forms.ModelChoiceField(label="Giáo viên", queryset=None)
    days = forms.IntegerField(label="Tối đa ngày/tuần", required=False, min_value=1, max_value=7)
    daily = forms.IntegerField(label="Tối đa tiết/ngày", required=False, min_value=1, max_value=15)
    session = forms.IntegerField(label="Tối đa tiết/buổi", required=False, min_value=1, max_value=10)

    def __init__(self, *args, teachers, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["teacher"].queryset = teachers

    def clean(self):
        data = super().clean()
        if not self.errors and all(data[field] is None for field in ("days", "daily", "session")):
            raise ValidationError("Nhập ít nhất một giới hạn. Để bỏ giới hạn, dùng nút Xóa trong danh sách.")
        return data


def problem_for(week, plan):
    items = tuple(Requirement(row.teacher_id, f"{row.teacher.name} · {row.teacher.code}", row.classroom,
                              row.subject, row.room, row.periods, row.doubles, row.session)
                  for row in plan.requirements.select_related("teacher").order_by("pk"))
    existing = tuple(to_entry(week, row) for row in week.lesson_set.order_by("pk"))
    people = {row.teacher for row in items} | {row.teacher for row in existing}
    days = {week.monday + timedelta(days=index): index
            for index in range(min(6, (date.max - week.monday).days) + 1)}
    unavailable = {(block.teacher_id, days[block.date], period)
                   for block in plan.unavailable.all() if block.date in days
                   for period in range(block.first, block.last + 1)}
    leaves = Request.objects.filter(employee_id__in=people, employee__school=week.school, slot__isnull=True,
                                    status=Decision.APPROVED, start__lte=max(days), end__gte=week.monday)
    unavailable.update((leave.employee_id, index, period) for leave in leaves
                       for day, index in days.items() if leave.start <= day <= leave.end
                       for period in range(1, 16))
    holidays = Holiday.objects.filter(school=week.school, date__in=days).values_list("date", flat=True)
    active = set(Employee.objects.filter(school=week.school, active=True, pk__in=people).values_list("pk", flat=True))
    resource_breaks = frozenset((block.kind, block.resource.casefold(), days[block.date], period)
                                for block in plan.breaks.all() if block.date in days
                                for period in range(block.first, block.last + 1))
    limits = tuple(TeacherLimit(row.teacher_id, f"{row.teacher.name} · {row.teacher.code}", row.days, row.daily, row.session)
                   for row in plan.teacher_limits.filter(teacher__school=week.school).select_related("teacher").order_by("pk"))
    return Problem(tuple(day for day in map(int, plan.days) if day in days.values()), plan.morning, plan.afternoon, items, existing,
                   frozenset(unavailable), frozenset(days[day] for day in holidays), frozenset(people - active),
                   resource_breaks, limits)


def to_entry(week, lesson):
    return Entry(lesson.teacher_id, lesson.classroom, lesson.subject, lesson.room,
                 (lesson.date - week.monday).days, lesson.period, lesson.pinned)


def fingerprint(week, problem):
    data = (week.revision, problem.days, problem.morning, problem.afternoon,
            [asdict(item) for item in problem.requirements], [asdict(item) for item in problem.fixed],
            sorted(problem.unavailable), sorted(problem.holidays), sorted(problem.inactive),
            sorted(problem.resource_breaks), [asdict(limit) for limit in problem.teacher_limits])
    return sha256(dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def sign(request, week, purpose, data):
    return signing.dumps([week.pk, request.member.pk, purpose, data], salt="teaching-plan-review", compress=True)


def read_signed(request, week, purpose):
    try:
        week_id, member_id, kind, data = signing.loads(request.POST.get("token", ""), salt="teaching-plan-review",
                                                       max_age=TOKEN_LIFETIME)
    except (signing.BadSignature, ValueError, TypeError):
        fail("Bản xem trước không còn hợp lệ. Hãy tạo lại trước khi xác nhận.")
    if (week_id, member_id, kind) != (week.pk, request.member.pk, purpose):
        fail("Bản xem trước không thuộc tài khoản hoặc tuần này.")
    return data


def advance_revision(week):
    week.revision += 1
    week.save(update_fields=["revision"])


def excel_requirements(upload, teachers):
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException
    from defusedxml.common import DefusedXmlException
    from xml.etree.ElementTree import ParseError

    if not upload.name.lower().endswith(".xlsx") or upload.size > 2_000_000:
        fail("Tệp phải có định dạng .xlsx và dung lượng tối đa 2 MB.")
    teacher_map = {person.code.casefold(): person for person in teachers}
    try:
        with ZipFile(upload) as archive:
            if len(archive.infolist()) > 100 or sum(item.file_size for item in archive.infolist()) > 10_000_000:
                fail("Tệp Excel vượt kích thước giải nén cho phép.")
        upload.seek(0)
        book = load_workbook(upload, read_only=True, keep_links=False)
        try:
            sheet = book.active
            sheet.reset_dimensions()
            rows = sheet.iter_rows(min_col=1, max_col=len(EXCEL_HEADERS))
            header = next(rows, ())
            if tuple(str(cell.value or "").strip().casefold() for cell in header) != tuple(
                    title.casefold() for title in EXCEL_HEADERS):
                fail("Tiêu đề cột chưa đúng. Hãy dùng mẫu Excel phân công.")
            output = []
            issues = []
            for number, cells in enumerate(rows, 2):
                if number > MAX_ROWS + 1:
                    fail(f"Tệp vượt giới hạn {MAX_ROWS} dòng.")
                values = [cell.value for cell in cells]
                if not any(value is not None for value in values):
                    continue
                if any(cell.data_type == "f" for cell in cells):
                    issues.append(f"Dòng {number}: hãy dán giá trị thay cho công thức.")
                    continue
                code, classroom, subject, room, periods, doubles, session = values
                teacher = teacher_map.get(str(code).strip().casefold())
                if teacher is None:
                    issues.append(f"Dòng {number}: mã giáo viên {str(code)[:40]} không có trong danh sách đang hoạt động.")
                    continue
                form = RequirementForm({
                    "teacher": teacher.pk, "classroom": classroom, "subject": subject, "room": room,
                    "periods": periods, "doubles": 0 if doubles is None else doubles,
                    "session": SESSION_NAMES.get(str(session or "").strip().casefold(), "invalid"),
                }, teachers=teachers)
                if form.is_valid():
                    output.append((number, form.cleaned_data))
                else:
                    details = " ".join(f"{form.fields[field].label}: {' '.join(errors)}"
                                       for field, errors in form.errors.items())
                    issues.append(f"Dòng {number}: {details}")
            if issues:
                raise ValidationError(issues[:20])
            if not output:
                fail("Tệp chưa có dòng phân công.")
            return output
        finally:
            book.close()
    except (BadZipFile, InvalidFileException, DefusedXmlException, ParseError, OSError, ValueError, KeyError, TypeError):
        fail("Không đọc được tệp Excel. Hãy kiểm tra lại định dạng và dùng mẫu phân công.")


def normalized_key(data):
    return data["teacher"].pk, data["classroom"].casefold(), data["subject"].casefold()


def check_new_requirements(plan, proposed, editing=None):
    existing = plan.requirements.exclude(pk=editing) if editing else plan.requirements.all()
    keys = {(row.teacher_id, row.classroom.casefold(), row.subject.casefold()) for row in existing}
    total = sum(row.periods for row in existing)
    issues = []
    for number, data in proposed:
        if normalized_key(data) in keys:
            issues.append(f"Dòng {number}: trùng phân công {data['subject']} lớp {data['classroom']} và giáo viên.")
        keys.add(normalized_key(data))
        total += data["periods"]
    if total > MAX_PERIODS:
        issues.append(f"Tổng số tiết vượt giới hạn {MAX_PERIODS} tiết trong một tuần.")
    if issues:
        raise ValidationError(issues[:20])


def prepare_solve(request, week, plan, *, optimize=False):
    problem = problem_for(week, plan)
    fixed = tuple(entry for entry in problem.fixed if entry.pinned) if optimize else problem.fixed
    movable = frozenset(problem.fixed) - frozenset(fixed)
    search = replace(problem, fixed=fixed)
    result = solve(search, preferred=movable if optimize else None)
    if not optimize and result.status in {"INFEASIBLE", "UNKNOWN"}:
        partial_result = solve(search, allow_partial=True)
        elapsed = result.seconds + partial_result.seconds
        result = replace(partial_result if partial_result.entries else result, seconds=elapsed)
    if not result.entries:
        return {"result": result}
    after = (*fixed, *result.entries)
    partial = result.status == "PARTIAL"
    remaining = unplaced_requirements(problem, after)
    context = {"proposal_placed": len(after), "proposal_missing": sum(missing for _, missing, _ in remaining),
               "partial_proposal": partial}
    if optimize:
        old_gaps = teacher_gap_cost(problem, problem.fixed)
        new_gaps = teacher_gap_cost(problem, after)
        before_score = spread_cost(problem.fixed) + 2 * old_gaps
        after_score = spread_cost(after) + 2 * new_gaps
        if not validate(problem, problem.fixed, complete=True) and after_score >= before_score:
            return {"result": replace(result, entries=(), messages=("Chưa tìm được phương án tốt hơn lịch hiện tại.",))}
        context.update(optimization=True, old_gaps=old_gaps, new_gaps=new_gaps,
                       moved_count=len(movable.difference(result.entries)), pinned_count=len(fixed))
    staff = {row.teacher: row.teacher_label for row in problem.requirements}
    rows = sorted(result.entries, key=lambda row: (row.day, row.period, row.classroom))
    purpose = "optimize" if optimize else "solve"
    return {**context, "result": result,
            "proposal": [(week.monday + timedelta(days=row.day), row, staff[row.teacher]) for row in rows],
            "solve_token": sign(request, week, purpose, [fingerprint(week, problem), [asdict(row) for row in rows], partial])}


def apply_solve(request, week, plan, *, optimize=False):
    original, rows, *flags = read_signed(request, week, "optimize" if optimize else "solve")
    partial = not optimize and flags == [True]
    problem = problem_for(week, plan)
    if original != fingerprint(week, problem):
        fail("Dữ liệu tuần đã thay đổi sau khi tạo phương án. Hãy chạy lại bộ xếp lịch.")
    proposed = tuple(Entry(**row) for row in rows)
    fixed = tuple(entry for entry in problem.fixed if entry.pinned) if optimize else problem.fixed
    errors = validate(problem, (*fixed, *proposed), complete=not partial)
    if errors:
        raise ValidationError(errors[:20])
    new_entries = proposed
    removed = 0
    if optimize:
        existing = {to_entry(week, row): row.pk for row in week.lesson_set.all()}
        desired = set(proposed)
        replaced = [pk for entry, pk in existing.items() if not entry.pinned and entry not in desired]
        if replaced:
            week.lesson_set.filter(pk__in=replaced).delete()
        removed = len(replaced)
        new_entries = [row for row in proposed if row not in existing]
    Lesson.objects.bulk_create([
        Lesson(week=week, date=week.monday + timedelta(days=row.day), period=row.period,
               classroom=row.classroom, subject=row.subject, room=row.room, teacher_id=row.teacher) for row in new_entries
    ])
    advance_revision(week)
    if optimize:
        record(request, "timetable.optimize_apply", f"Tuần #{week.pk}, thay vị trí {removed} tiết.")
        messages.success(request, "Đã áp dụng phương án tối ưu vào bản nháp.")
    else:
        record(request, "timetable.solve_apply", f"Tuần #{week.pk}, bổ sung {len(new_entries)} tiết sau khi xác nhận.")
        status = f"Đã đưa {len(new_entries)} tiết vào bản nháp."
        messages.success(request, status + (" Còn mục cần xử lý trong phần Tinh chỉnh." if partial else ""))
    return redirect("week", pk=week.pk)


def placement_review(request, week, problem, assignments):
    ids = {(row.teacher_id, row.classroom.casefold(), row.subject.casefold()): row.pk for row in assignments}
    rows = [(ids[item.key], item, missing, pairs) for item, missing, pairs in unplaced_requirements(problem, problem.fixed)]
    teachers = {str(item.teacher): item.teacher_label for _, item, _, _ in rows}
    teacher = request.GET.get("review_teacher", "")
    if teacher not in teachers:
        teacher = ""
    if teacher:
        rows = [row for row in rows if str(row[1].teacher) == teacher]
    selected = next((row for row in rows if str(row[0]) == request.GET.get("review")), None)
    context = {"placement_rows": rows, "missing_periods": sum(row[2] for row in rows),
               "review_teacher": teacher, "review_teachers": sorted(teachers.items(), key=lambda item: item[1].casefold())}
    if selected is not None:
        _, item, missing, _ = selected
        slots, blockers = review_slots(problem, item) if missing else ([], [])
        context.update(review_assignment=selected, review_slot_count=len(slots), review_blockers=blockers,
                       review_choices=[(week.monday + timedelta(days=entry.day), entry.period) for entry in slots[:12]])
    return context


def handle_plan_action(request, week, plan, teachers):
    action = request.POST.get("action")
    if action == "settings":
        form = PlanForm(request.POST)
        if data := post_form(request, form):
            if plan is None:
                plan = TimetablePlan(week=week)
            plan.days = "".join(sorted(data["days"]))
            plan.morning, plan.afternoon = data["morning"], data["afternoon"]
            plan.save()
            advance_revision(week)
            record(request, "timetable.plan_settings", f"Tuần #{week.pk}")
            messages.success(request, "Đã lưu khung học của tuần.")
        return redirect("timetable_plan", pk=week.pk)

    if plan is None:
        fail("Hãy lưu khung học trước khi nhập phân công.")
    if action in ("add", "edit"):
        form = RequirementForm(request.POST, teachers=teachers)
        item = get_object_or_404(plan.requirements, pk=request.POST.get("row")) if action == "edit" else None
        if data := post_form(request, form):
            check_new_requirements(plan, [(1, data)], editing=item.pk if item else None)
            if item:
                plan.requirements.filter(pk=item.pk).update(**data)
            else:
                plan.requirements.create(**data)
            advance_revision(week)
            record(request, "timetable.plan_assignment", f"Tuần #{week.pk}")
            messages.success(request, "Đã lưu phân công.")
        return redirect("timetable_plan", pk=week.pk)
    if action == "remove":
        get_object_or_404(plan.requirements, pk=request.POST.get("row")).delete()
    elif action == "block":
        form = UnavailableForm(request.POST, teachers=teachers, week=week)
        if not (data := post_form(request, form)):
            return redirect("timetable_plan", pk=week.pk)
        plan.unavailable.get_or_create(**data)
    elif action == "unblock":
        get_object_or_404(plan.unavailable, pk=request.POST.get("row")).delete()
    elif action == "resource_break":
        form = TeachingBreakForm(request.POST, week=week, plan=plan, prefix="break")
        if not (data := post_form(request, form)):
            return redirect("timetable_plan", pk=week.pk)
        kind, resource = data.pop("target").split(":", 1)
        plan.breaks.get_or_create(kind=kind, resource=resource, **data)
    elif action == "remove_break":
        get_object_or_404(plan.breaks, pk=request.POST.get("row")).delete()
    elif action == "teacher_limit":
        form = TeacherLimitForm(request.POST, teachers=teachers, prefix="teacher-limit")
        if not (data := post_form(request, form)):
            return redirect("timetable_plan", pk=week.pk)
        plan.teacher_limits.update_or_create(teacher=data.pop("teacher"), defaults=data)
    elif action == "remove_teacher_limit":
        get_object_or_404(plan.teacher_limits.filter(teacher__school=week.school), pk=request.POST.get("row")).delete()
    elif action == "preview_import":
        upload = request.FILES.get("workbook")
        if upload is None:
            fail("Hãy chọn tệp Excel phân công.")
        incoming = excel_requirements(upload, teachers)
        check_new_requirements(plan, incoming)
        serialized = [[number, {**data, "teacher": data["teacher"].pk}] for number, data in incoming]
        return {"import_rows": incoming, "import_token": sign(request, week, "import", serialized)}
    elif action == "apply_import":
        incoming = []
        for number, values in read_signed(request, week, "import"):
            form = RequirementForm(values, teachers=teachers)
            if not form.is_valid():
                fail(f"Dòng {number} đã thay đổi tính hợp lệ. Hãy nhập lại tệp.")
            incoming.append((number, form.cleaned_data))
        check_new_requirements(plan, incoming)
        TeachingRequirement.objects.bulk_create([TeachingRequirement(plan=plan, **data) for _, data in incoming])
        messages.success(request, f"Đã nhập {len(incoming)} phân công.")
    elif action in {"solve", "optimize"}:
        return prepare_solve(request, week, plan, optimize=action == "optimize")
    elif action in {"apply_solve", "apply_optimize"}:
        return apply_solve(request, week, plan, optimize=action == "apply_optimize")
    else:
        fail("Thao tác không hợp lệ.")
    advance_revision(week)
    record(request, f"timetable.plan_{action}", f"Tuần #{week.pk}")
    return redirect("timetable_plan", pk=week.pk)


@school_view
def timetable_plan(request, pk):
    require(request, "plan")
    week = get_week(request, pk)
    plan = TimetablePlan.objects.filter(week=week).first()
    teachers = employees(request)
    context = {}
    if request.method == "POST":
        if week.state != State.DRAFT:
            return HttpResponseBadRequest("Chỉ sửa kế hoạch của tuần ở trạng thái nháp.")
        try:
            revision = request.POST.get("revision")
            if revision is not None and revision != str(week.revision):
                fail("Tuần đã được cập nhật. Hãy xem lại dữ liệu hiện tại trước khi lưu hoặc xác nhận.")
            result = handle_plan_action(request, week, plan, teachers)
        except ValidationError as error:
            context["issues"] = error.messages
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Mã dữ liệu không hợp lệ.")
        else:
            if not isinstance(result, dict):
                return result
            context.update(result)
    edit = None
    if request.GET.get("edit", "").isdigit() and plan and week.state == State.DRAFT:
        edit = plan.requirements.filter(pk=request.GET["edit"]).first()
    initial = {"days": list(plan.days if plan else "01234"), "morning": plan.morning if plan else 5,
               "afternoon": plan.afternoon if plan else 0}
    problem = problem_for(week, plan) if plan else None
    assignments = list(plan.requirements.select_related("teacher")) if plan else []
    placement = placement_review(request, week, problem, assignments) if problem else {}
    return page(request, "timetable_plan", week=week, menu_week=week, plan=plan, plan_form=PlanForm(initial=initial),
                can_edit=week.state == State.DRAFT, week_picker_url=f"{reverse('week', args=[week.pk])}?weeks=1",
                requirements=assignments,
                blocked=plan.unavailable.select_related("teacher") if plan else (),
                breaks=plan.breaks.all() if plan else (),
                teacher_limits=plan.teacher_limits.filter(teacher__school=week.school).select_related("teacher") if plan else (),
                break_form=TeachingBreakForm(week=week, plan=plan, prefix="break",
                                              initial={"date": week.monday, "first": 1, "last": 1}) if plan else None,
                teacher_limit_form=TeacherLimitForm(teachers=teachers, prefix="teacher-limit"),
                requirement_form=RequirementForm(teachers=teachers, initial={
                    **{field: getattr(edit, field) for field in ("classroom", "subject", "room", "periods", "doubles", "session")},
                    "teacher": edit.teacher_id} if edit else None),
                block_form=UnavailableForm(teachers=teachers, week=week, initial={"date": week.monday, "first": 1, "last": 15}),
                required=sum(item.periods for item in problem.requirements) if problem else 0,
                existing=len(problem.fixed) if problem else 0, edit=edit,
                diagnostics=validate(problem, problem.fixed, complete=True)[:20] if problem else (),
                **placement, **context)


@school_view
def timetable_diagnostics(request, pk):
    require(request, "plan")
    if request.method == "POST":
        return HttpResponseBadRequest()
    week = get_week(request, pk)
    plan = get_object_or_404(TimetablePlan, week=week)
    problem = problem_for(week, plan)
    return JsonResponse({"week": week.pk, "required_periods": sum(item.periods for item in problem.requirements),
                         "placed_periods": len(problem.fixed),
                         "issues": validate(problem, problem.fixed, complete=True)})


@school_view
def timetable_plan_template(request, pk):
    require(request, "plan")
    if request.method == "POST":
        return HttpResponseBadRequest()
    get_week(request, pk)
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = "Phân công"
    sheet.append(EXCEL_HEADERS)
    for letter in "ABCDEFG":
        sheet.column_dimensions[letter].width = 22
    sheet.column_dimensions["A"].number_format = "@"
    sheet.freeze_panes = "B2"
    stream = BytesIO()
    book.save(stream)
    stream.seek(0)
    return FileResponse(stream, as_attachment=True, filename="mau-phan-cong-thoi-khoa-bieu.xlsx",
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
