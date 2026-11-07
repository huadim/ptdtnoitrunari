from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Exists, OuterRef
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect

from .access import employee_scope, record, require, school_view
from .common import employees, fail, page, post_form
from .exports import document
from .forms import LessonForm, WeekForm
from .models import Decision, Holiday, Lesson, Request, State, TimetableWeek

HEADERS = ("Ngày", "Tiết", "Lớp", "Môn", "Giáo viên", "Phòng")


def get_week(request, pk):
    week = get_object_or_404(TimetableWeek, school=request.school, pk=pk)
    if week.state != State.PUBLISHED and not request.rights["all_timetable"]:
        raise PermissionDenied
    return week


def lessons(request, week):
    rows = employee_scope(request, week.lesson_set.select_related("teacher"), "teacher", "all_timetable")
    for parameter, field in (("class", "classroom"), ("teacher", "teacher__code"), ("room", "room")):
        if request.GET.get(parameter):
            rows = rows.filter(**{f"{field}__iexact": request.GET[parameter].strip()})
    return rows


@school_view
def timetable(request):
    weeks = TimetableWeek.objects.filter(school=request.school).order_by("-monday")
    if request.method == "POST":
        require(request, "plan")
        data = post_form(request, WeekForm(request.POST))
        if not data:
            return redirect("timetable")
        week, created = weeks.get_or_create(monday=data["monday"], defaults={"school": request.school,
                                                                              "academic_year": data["academic_year"]})
        if created:
            record(request, "timetable.create", f"Tuần #{week.pk}")
        return redirect("week", pk=week.pk)
    if not request.rights["all_timetable"]:
        weeks = weeks.filter(state=State.PUBLISHED)
    return page(request, "timetable", weeks=weeks[:100], week_form=WeekForm())


@school_view
def week_detail(request, pk):
    week = get_week(request, pk)
    if request.method == "POST":
        try:
            change_week(request, week)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Mã dữ liệu không hợp lệ.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("week", pk=week.pk)
    return page(request, "week", week=week, lessons=lessons(request, week), form=LessonForm(),
                options=employees(request), can_edit=request.rights["plan"] and week.state == State.DRAFT)


def change_week(request, week):
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
    if action in {"add", "edit"}:
        data = post_form(request, LessonForm(request.POST))
        if not data:
            return
        date = data["date"]
        if not week.monday <= date < week.monday + timedelta(days=7):
            fail("Ngày học phải thuộc tuần đang mở.")
        if Holiday.objects.filter(school=request.school, date=date).exists():
            fail("Ngày này đã được cấu hình là ngày lễ.")
        teacher = get_object_or_404(employees(request), pk=request.POST.get("teacher", "0"))
        if Request.objects.filter(employee=teacher, status=Decision.APPROVED, slot__isnull=True,
                                  start__lte=date, end__gte=date).exists():
            fail("Giáo viên đã được duyệt nghỉ vào ngày này.")
        data["teacher"] = teacher
        same = rows.filter(date=date, period=data["period"])
        item = get_object_or_404(rows, pk=request.POST.get("id", "0")) if action == "edit" else None
        if item:
            same = same.exclude(pk=item.pk)
        for field, label in (("teacher", "giáo viên"), ("classroom", "lớp"), ("room", "phòng")):
            if same.filter(**{field: data[field]}).exists():
                fail(f"Trùng {label} ở ngày và tiết đã chọn.")
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
        week.state = new
        week.save(update_fields=["state"])
    suffix = f", tiết #{item.pk}" if action in {"add", "edit"} else ""
    record(request, f"timetable.{action}", f"Tuần #{week.pk}{suffix}")
    messages.success(request, "Đã lưu thời khóa biểu.")


@school_view
def timetable_export(request, pk, format):
    if request.method == "POST":
        return HttpResponseBadRequest()
    week = get_week(request, pk)
    rows = [(row.date.strftime("%d/%m/%Y"), row.period, row.classroom, row.subject, row.teacher.name, row.room)
            for row in lessons(request, week)]
    return document(request, "THỜI KHÓA BIỂU", f"Năm học {week.academic_year} · Tuần từ {week.monday:%d/%m/%Y}"
                    f" · {week.get_state_display()}", HEADERS, rows, f"thoi-khoa-bieu-{week.monday}", format,
                    landscape=True)
