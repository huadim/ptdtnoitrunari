from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.utils.timezone import localdate

from .access import employee_scope, record, require, school_view
from .common import bad_month, chosen_month, fail, month_locked, month_redirect, page, post_form
from .exports import document
from .forms import ConfirmForm, MonthField, ReasonForm
from .models import Attendance, DayKind, Decision, Holiday, MonthClose, Rate, Request, Result, Roster, Slot, State

COMPLETED = {Result.DONE, Result.SUBSTITUTE}


def published(request, month, end=None):
    return Slot.objects.filter(day__roster__duty_type__school=request.school, day__roster__month__range=(month, end or month),
                               day__roster__state=State.PUBLISHED).select_related(
        "day__roster__duty_type", "assigned", "original", "attendance__actual").order_by("day__date", "assigned__name")


def visible(request, month, end=None):
    # A teacher retains their own original assignment after an approved replacement.
    from django.db.models import Q
    rows = published(request, month, end)
    if request.rights["all_attendance"]:
        return rows
    if request.rights["department"]:
        department = request.member.employee.department
        return rows.filter(Q(original__department=department) | Q(assigned__department=department))
    return rows.filter(Q(original=request.member.employee) | Q(assigned=request.member.employee))


def confirm(request, month):
    require(request, "plan")
    if month_locked(request.school, month):
        fail("Bảng chấm công đã khóa.")
    slot = get_object_or_404(published(request, month), pk=request.POST.get("slot", "0"))
    data = post_form(request, ConfirmForm(request.POST))
    if not data:
        return
    date = slot.day.date
    if date > localdate():
        fail("Chỉ xác nhận ca từ ngày thực hiện trở về trước.")
    result = data["result"]
    if result == Result.DONE and slot.original_id != slot.assigned_id:
        result = Result.SUBSTITUTE
    previous = getattr(slot, "attendance", None)
    kind = DayKind.WEEKDAY
    if previous:
        kind = previous.kind
    elif Holiday.objects.filter(school=request.school, date=date).exists():
        kind = DayKind.HOLIDAY
    elif date.weekday() == 5:
        kind = DayKind.SATURDAY
    elif date.weekday() == 6:
        kind = DayKind.SUNDAY
    rate = None
    if result in COMPLETED:
        rate = previous.rate if previous and previous.rate_id else Rate.objects.filter(
            duty_type=slot.day.roster.duty_type, kind=kind, effective__lte=date).order_by("-effective").first()
        if not rate:
            fail(f"Chưa có hệ số {DayKind(kind).label} hiệu lực ngày {date:%d/%m/%Y} cho loại trực này.")
    units = rate.units if rate else Decimal("0")
    before = f"{previous.result}, {previous.units}" if previous else Result.PENDING
    Attendance.objects.update_or_create(slot=slot, defaults={
        "actual": slot.assigned, "result": result, "kind": kind, "rate": rate, "units": units,
        "note": data["note"], "confirmed_by": request.member,
    })
    record(request, "attendance.confirm", f"Ca #{slot.pk}: {before} → {result}, {units}. {data['note']}")
    messages.success(request, "Đã xác nhận ca trực.")


def month_action(request, month, action):
    require(request, "attendance_approve")
    if action == "lock":
        slots = published(request, month)
        if (not slots.exists() or slots.filter(attendance__isnull=True).exists()
                or Roster.objects.filter(duty_type__school=request.school, month=month).exclude(state=State.PUBLISHED).exists()):
            fail("Cần công bố mọi lịch trực đã tạo trong tháng và xác nhận tất cả ca trước khi khóa.")
        if Request.objects.filter(slot__in=slots, status=Decision.PENDING).exists():
            fail("Cần xử lý các đề nghị trực thay đang chờ trước khi khóa.")
        if month_locked(request.school, month):
            fail("Bảng chấm công đã khóa.")
        MonthClose.objects.update_or_create(school=request.school, month=month, defaults={"locked": True})
        record(request, "attendance.lock", f"Tháng {month:%m/%Y}")
    else:
        data = post_form(request, ReasonForm(request.POST))
        if not data:
            return
        if not MonthClose.objects.filter(school=request.school, month=month, locked=True).update(locked=False):
            fail("Bảng chấm công hiện đang mở.")
        record(request, "attendance.unlock", f"Tháng {month:%m/%Y}: {data['reason']}")
    messages.success(request, "Đã cập nhật trạng thái chấm công.")


def report(request, slots):
    def item():
        return [0, 0, 0, 0, 0, 0, 0, 0, 0, Decimal("0")]

    def can_show(employee):
        if request.rights["all_attendance"]:
            return True
        if request.rights["department"]:
            return employee.department == request.member.employee.department
        return employee.pk == request.member.employee_id

    values = defaultdict(item)
    people = {}
    categories = [DayKind.WEEKDAY, DayKind.SATURDAY, DayKind.SUNDAY, DayKind.HOLIDAY]
    for slot in slots:
        if can_show(slot.original):
            people[slot.original_id] = slot.original
            values[slot.original_id][0] += 1
        if not can_show(slot.assigned):
            continue
        people[slot.assigned_id] = slot.assigned
        row = values[slot.assigned_id]
        actual = getattr(slot, "attendance", None)
        if not actual:
            row[8] += 1
        elif actual.result in COMPLETED:
            row[1] += 1
            row[2 + categories.index(actual.kind)] += 1
            row[6] += actual.result == Result.SUBSTITUTE
            row[9] += actual.units
        else:
            row[7] += 1
    return [(employee.name, employee.department or "—", *values[employee.pk])
            for employee in sorted(people.values(), key=lambda employee: (employee.name, employee.code))]


def department_summary(rows):
    totals = defaultdict(lambda: [0, 0, 0, Decimal("0")])
    for row in rows:
        group = totals[row[1]]
        group[0] += 1
        group[1] += row[3]
        group[2] += row[10]
        group[3] += row[11]
    return [(name, *totals[name]) for name in sorted(totals)]


HEADERS = ("Họ và tên", "Tổ", "Phân công", "Hoàn thành", "Thường", "T. Bảy", "CN", "Lễ", "Trực thay",
           "Nghỉ / bù", "Chờ xác nhận", "Công")


def range_end(request, start):
    value = request.GET.get("end")
    if not value:
        return start
    end = MonthField().clean(value)
    if not 0 <= month_index(end) - month_index(start) < 12:
        fail("Chọn khoảng báo cáo từ 1 đến 12 tháng.")
    return end


def month_index(month):
    return month.year * 12 + month.month


def range_closed(school, start, end):
    count = MonthClose.objects.filter(school=school, month__range=(start, end), locked=True).count()
    return count == month_index(end) - month_index(start) + 1


@school_view
def attendance(request):
    try:
        month = chosen_month(request, request.POST if request.method == "POST" else None)
    except ValidationError:
        return bad_month()
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "confirm":
                confirm(request, month)
            elif action in ("lock", "unlock"):
                month_action(request, month, action)
            else:
                return HttpResponseBadRequest("Thao tác không hợp lệ.")
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Mã dữ liệu không hợp lệ.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return month_redirect("attendance", month)
    try:
        end = range_end(request, month)
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    slots = list(visible(request, month, end))
    # Original-only visibility is reflected in aggregate reports, without exposing the replacement's attendance.
    details = employee_scope(request, published(request, month), "assigned", "all_attendance")
    rows = report(request, slots)
    return page(request, "attendance", month=month, end=end, multiple=end != month, details=details, rows=rows,
                headers=HEADERS, locked=range_closed(request.school, month, end),
                results=ConfirmForm.base_fields["result"].choices,
                departments=department_summary(rows) if request.rights["all_attendance"] else [],
                totals=(len(rows), sum(row[3] for row in rows), sum(row[10] for row in rows), sum(row[11] for row in rows)))


@school_view
def attendance_export(request, format):
    if request.method == "POST":
        return HttpResponseBadRequest()
    try:
        month = chosen_month(request)
        end = range_end(request, month)
    except ValidationError:
        return bad_month()
    state = "ĐÃ KHÓA" if range_closed(request.school, month, end) else "CÓ THÁNG CHƯA KHÓA"
    period = f"{month:%m/%Y}" if end == month else f"{month:%m/%Y} – {end:%m/%Y}"
    scope = "TOÀN TRƯỜNG" if request.rights["all_attendance"] else "THEO PHẠM VI TÀI KHOẢN"
    return document(request, f"BÁO CÁO CHẤM CÔNG {scope}", f"Tháng {period} · {state}", HEADERS,
                    report(request, visible(request, month, end)), f"cham-cong-{month:%Y-%m}", format, landscape=True)
