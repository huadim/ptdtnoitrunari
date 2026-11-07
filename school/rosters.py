from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.utils.timezone import localdate

from .access import employee_scope, record, require, school_view
from .common import bad_month, chosen_month, employees, fail, month_locked, page, post_form
from .forms import MonthField
from .models import DutyDay, DutyType, Notification, Roster, Slot, State
from .scheduling import Rules, month_dates


@school_view
def rosters(request):
    try:
        month = chosen_month(request)
    except ValidationError:
        return bad_month()
    types = DutyType.objects.filter(school=request.school)
    if request.method == "POST":
        require(request, "plan")
        try:
            target = MonthField().clean(request.POST.get("month"))
        except ValidationError:
            return bad_month()
        try:
            kind = get_object_or_404(types, pk=request.POST.get("duty_type", "0"))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Loại trực không hợp lệ.")
        if month_locked(request.school, target):
            messages.error(request, "Tháng đã khóa chấm công.")
            return redirect("rosters")
        roster, created = Roster.objects.get_or_create(
            duty_type=kind, month=target, defaults={field: getattr(kind, field) for field in
                                                    ("people", "min_gap", "preferred_min", "preferred_max", "pair_rest")},
        )
        if created:
            DutyDay.objects.bulk_create([DutyDay(roster=roster, date=day) for day in month_dates(target)])
            record(request, "roster.create", f"#{roster.pk}, {target:%m/%Y}, {kind.name}")
        return redirect("roster", pk=roster.pk)

    rows = Roster.objects.filter(duty_type__school=request.school, month=month).select_related("duty_type")
    if not request.rights["all_duty"]:
        visible = employee_scope(request, Slot.objects.filter(day__roster__in=rows), "assigned", "all_duty")
        rows = rows.filter(state=State.PUBLISHED, pk__in=visible.values("day__roster_id"))
    return page(request, "rosters", rosters=rows, month=month, types=types)


def change_roster(request, roster):
    action = request.POST.get("action")
    days = list(roster.dutyday_set.prefetch_related("slots"))
    current = {day.date: [slot.assigned_id for slot in day.slots.all()] for day in days}
    if action in {"assign", "generate", "toggle_day", "refresh_rules", "submit"}:
        require(request, "plan")
        if roster.state != State.DRAFT or month_locked(request.school, roster.month):
            fail("Chỉ chỉnh sửa lịch nháp của tháng chưa khóa.")
    elif action in {"approve", "publish", "return"}:
        require(request, "duty_approve")
        if month_locked(request.school, roster.month):
            fail("Tháng đã khóa chấm công.")
    else:
        fail("Thao tác không hợp lệ.")

    if action == "refresh_rules":
        for field in ("people", "min_gap", "preferred_min", "preferred_max", "pair_rest"):
            setattr(roster, field, getattr(roster.duty_type, field))
        roster.save()
    elif action == "generate":
        result, explanation = Rules(roster).solve({day.date: current[day.date] for day in days if day.locked})
        if result is None:
            fail(explanation)
        unlocked = [day for day in days if not day.locked]
        Slot.objects.filter(day__in=unlocked).delete()
        Slot.objects.bulk_create([Slot(day=day, original_id=employee, assigned_id=employee)
                                  for day in unlocked for employee in result[day.date]])
    elif action in {"assign", "toggle_day"}:
        day = get_object_or_404(roster.dutyday_set, pk=request.POST.get("day", "0"))
        if action == "toggle_day":
            day.locked = not day.locked
            day.save(update_fields=["locked"])
        else:
            if day.locked:
                fail("Ngày này đang khóa.")
            submitted = request.POST.getlist("employees")
            ids = list(employees(request).filter(pk__in=submitted).values_list("pk", flat=True))
            if len(ids) != len(submitted):
                fail("Có nhân sự không hợp lệ hoặc bị trùng.")
            issues = Rules(roster).reasons(day.date, ids, current)
            if issues:
                fail(" ".join(issues))
            day.slots.all().delete()
            Slot.objects.bulk_create([Slot(day=day, original_id=employee, assigned_id=employee) for employee in ids])
    else:
        transitions = {
            "submit": ({State.DRAFT}, State.SUBMITTED),
            "approve": ({State.SUBMITTED}, State.APPROVED),
            "publish": ({State.APPROVED}, State.PUBLISHED),
            "return": ({State.SUBMITTED, State.APPROVED}, State.DRAFT),
        }
        expected, target = transitions[action]
        if roster.state not in expected:
            fail("Lịch trực không ở trạng thái phù hợp.")
        if action != "return":
            issues = Rules(roster).validate(current)
            if issues:
                fail(" ".join(issues[:5]))
        roster.state = target
        roster.save(update_fields=["state"])
        if action == "publish":
            Notification.objects.bulk_create([
                Notification(employee_id=employee, text=f"Đã công bố {roster.duty_type.name} tháng {roster.month:%m/%Y}.")
                for employee in {member for ids in current.values() for member in ids}
            ])
    suffix = f", ngày {day.date:%d/%m/%Y}" if action in {"assign", "toggle_day"} else ""
    record(request, f"roster.{action}", f"Lịch #{roster.pk}{suffix}")
    messages.success(request, "Đã lưu lịch trực.")


def roster_for(request, pk):
    roster = get_object_or_404(Roster.objects.select_related("duty_type__school"), duty_type__school=request.school, pk=pk)
    if not request.rights["all_duty"]:
        visible = employee_scope(request, Slot.objects.filter(day__roster=roster), "assigned", "all_duty")
        if roster.state != State.PUBLISHED or not visible.exists():
            raise PermissionDenied
    return roster


def visible_slots(request, roster):
    return employee_scope(request, Slot.objects.filter(day__roster=roster).select_related("assigned", "original"),
                          "assigned", "all_duty")


@school_view
def roster_detail(request, pk):
    roster = roster_for(request, pk)
    if request.method == "POST":
        try:
            change_roster(request, roster)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Mã dữ liệu không hợp lệ.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("roster", pk=pk)

    slots = visible_slots(request, roster)
    days = roster.dutyday_set.prefetch_related(Prefetch("slots", queryset=slots, to_attr="visible_slots"))
    if not request.rights["all_duty"]:
        days = days.filter(pk__in=slots.values("day_id"))
    days = list(days)
    issues, stats, close = [], [], []
    if request.rights["all_duty"]:
        current = {day.date: [slot.assigned_id for slot in day.visible_slots] for day in days}
        rules = Rules(roster)
        if roster.state != State.PUBLISHED:
            issues = rules.validate(current)
        for employee, name in rules.people.items():
            dates = [day for day, ids in current.items() if employee in ids]
            stats.append((name, len(dates), sum(day.weekday() == 5 for day in dates), sum(day.weekday() == 6 for day in dates)))
            close.extend(f"{name}: {start:%d/%m} – {end:%d/%m}"
                         for start, end in zip(dates, dates[1:]) if (end - start).days < roster.preferred_min)
    return page(request, "roster", roster=roster, days=days, options=employees(request),
                issues=issues, stats=stats, close=close, can_edit=request.rights["plan"] and roster.state == State.DRAFT)


@school_view
def request_list(request):
    school_requests = Slot.objects.filter(day__roster__duty_type__school=request.school,
                                         day__roster__state=State.PUBLISHED)
    return request_page(request, school_requests)


def request_page(request, school_slots):
    from .forms import LeaveForm, ReasonForm
    from .models import Decision, Request
    rows = Request.objects.filter(employee__school=request.school).select_related("employee", "slot__day", "replacement")
    if not (request.rights["plan"] or request.rights["duty_approve"]):
        rows = rows.filter(employee=request.member.employee)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "leave":
                data = post_form(request, LeaveForm(request.POST))
                if data:
                    item = Request.objects.create(employee=request.member.employee, **data)
                    record(request, "request.leave", f"Đề nghị #{item.pk}")
                    messages.success(request, "Đã gửi đề nghị nghỉ.")
            elif action == "swap":
                slot = get_object_or_404(school_slots, pk=request.POST.get("slot", "0"), assigned=request.member.employee)
                day = slot.day.date
                data = post_form(request, ReasonForm(request.POST))
                if data:
                    if (day < localdate() or month_locked(request.school, slot.day.roster.month)
                            or Request.objects.filter(slot=slot, status=Decision.PENDING).exists()
                            or hasattr(slot, "attendance")):
                        fail("Ca đã qua, đã được xác nhận, đã khóa hoặc đang có đề nghị thay ca.")
                    item = Request.objects.create(employee=request.member.employee, start=day, end=day, slot=slot, **data)
                    record(request, "request.swap", f"Đề nghị #{item.pk}")
                    messages.success(request, "Đã gửi đề nghị trực thay.")
            elif action in {"accept", "reject", "cancel"}:
                if action != "cancel":
                    require(request, "duty_approve")
                else:
                    rows = rows.filter(employee=request.member.employee)
                item = get_object_or_404(rows, pk=request.POST.get("id", "0"))
                decide_request(request, item, action, school_slots)
            else:
                return HttpResponseBadRequest("Thao tác không hợp lệ.")
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Mã dữ liệu không hợp lệ.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("requests")
    upcoming = school_slots.filter(assigned=request.member.employee, day__date__gte=localdate()
                                   ).select_related("day__roster__duty_type").order_by("day__date")[:60]
    return page(request, "requests", items=rows.order_by("-created_at")[:150], upcoming=upcoming,
                leave_form=LeaveForm(), options=employees(request))


def decide_request(request, item, action, school_slots):
    from .models import Decision
    if item.status != Decision.PENDING:
        fail("Đề nghị đã được xử lý.")
    if action == "accept":
        if item.slot_id:
            slot = item.slot
            roster = slot.day.roster
            if (slot.assigned_id != item.employee_id or month_locked(request.school, roster.month)
                    or hasattr(slot, "attendance") or item.start < localdate()):
                fail("Ca đã thay đổi, đã qua hoặc đã xác nhận chấm công.")
            replacement = get_object_or_404(employees(request), pk=request.POST.get("replacement", "0"))
            if replacement.pk in (slot.assigned_id, slot.original_id):
                fail("Chọn người trực thay khác với người đang trực và người được phân công ban đầu.")
            others = list(slot.day.slots.exclude(pk=slot.pk).values_list("assigned_id", flat=True))
            current = {}
            for day, employee in Slot.objects.filter(day__roster=roster).values_list("day__date", "assigned_id"):
                current.setdefault(day, []).append(employee)
            issues = Rules(roster).reasons(slot.day.date, others + [replacement.pk], current)
            if issues:
                fail(" ".join(issues))
            slot.assigned = replacement
            slot.save(update_fields=["assigned"])
            item.replacement = replacement
            Notification.objects.create(employee=replacement, text=f"Bạn được phân công trực thay ngày {item.start:%d/%m/%Y}.")
        elif Slot.objects.filter(day__roster__duty_type__school=request.school, assigned=item.employee,
                                 day__roster__state__in=[State.APPROVED, State.PUBLISHED],
                                 day__date__range=(item.start, item.end)).exists():
            fail("Có ca đã duyệt trong thời gian xin nghỉ. Xử lý trực thay trước khi duyệt nghỉ.")
    item.status = {"accept": Decision.APPROVED, "reject": Decision.REJECTED, "cancel": Decision.CANCELLED}[action]
    item.save(update_fields=["status", "replacement"])
    if action != "cancel":
        Notification.objects.create(employee=item.employee, text=f"Đề nghị #{item.pk}: {item.get_status_display()}.")
    record(request, f"request.{action}", f"Đề nghị #{item.pk}, người thay #{item.replacement_id or '–'}")
    messages.success(request, "Đã cập nhật đề nghị.")


@school_view
def roster_export(request, pk, format):
    from .exports import document
    if request.method == "POST":
        return HttpResponseBadRequest()
    roster = roster_for(request, pk)
    rows = [(slot.day.date.strftime("%d/%m/%Y"), slot.assigned.name,
             slot.original.name if request.rights["all_duty"] else "—")
            for slot in visible_slots(request, roster).select_related("day").order_by("day__date", "assigned__name")]
    return document(request, f"LỊCH PHÂN CÔNG {roster.duty_type.name.upper()}",
                    f"Tháng {roster.month:%m/%Y} · {roster.get_state_display()}",
                    ("Ngày trực", "Người trực", "Phân công ban đầu"), rows, f"lich-truc-{roster.month:%Y-%m}", format)
