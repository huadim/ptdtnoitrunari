from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch, Q
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.utils.timezone import localdate

from .access import employee_scope, record, require, school_view
from .common import bad_month, chosen_month, employees, fail, month_locked, page, post_form
from .forms import DutyRoundForm, MonthField
from .models import DutyDay, DutyType, MonthClose, Notification, Roster, Slot, State
from .scheduling import Rules, dates_between, eligible_employees, month_dates


RULE_FIELDS = ("people", "min_gap", "preferred_min", "preferred_max", "pair_rest", "weekend_rest")


def rosters_in_month(school, month):
    return Roster.objects.filter(duty_type__school=school).filter(
        Q(round_number__isnull=True, month=month) | Q(dutyday__date__range=(month, month_dates(month)[-1])),
    ).distinct()


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
            kind = get_object_or_404(types, pk=request.POST.get("duty_type", "0"))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Loại trực không hợp lệ.")
        try:
            return create_roster(request, kind)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("rosters")

    rows = rosters_in_month(request.school, month).select_related("duty_type").order_by("month", "round_number", "pk")
    if not request.rights["all_duty"]:
        visible = employee_scope(request, Slot.objects.filter(day__roster__in=rows), "assigned", "all_duty")
        rows = rows.filter(state=State.PUBLISHED, pk__in=visible.values("day__roster_id"))
    return page(request, "rosters", rosters=rows, month=month, types=types, round_form=DutyRoundForm())


def create_roster(request, kind):
    round_number = None
    if request.POST.get("action") == "round":
        data = post_form(request, DutyRoundForm(request.POST))
        if data is None:
            return redirect("rosters")
        round_number = data["round_number"]
        dates = dates_between(data["start"], data["end"])
    else:
        try:
            dates = month_dates(MonthField().clean(request.POST.get("month")))
        except ValidationError:
            return bad_month()
    month = dates[0].replace(day=1)
    if MonthClose.objects.filter(school=request.school, locked=True,
                                 month__range=(month, dates[-1].replace(day=1))).exists():
        fail("Khoảng ngày có tháng đã khóa chấm công.")

    overlapping = DutyDay.objects.filter(roster__duty_type=kind, date__range=(dates[0], dates[-1]))
    existing = overlapping.order_by().values_list("roster_id", flat=True).distinct()
    if round_number is None:
        roster = Roster.objects.filter(duty_type=kind, month=month, round_number__isnull=True).first()
        if roster:
            return redirect("roster", pk=roster.pk)
    elif len(existing) == 1:
        roster = Roster.objects.get(pk=existing[0])
        if roster.round_number == round_number and list(roster.dutyday_set.values_list("date", flat=True)) == dates:
            return redirect("roster", pk=roster.pk)
    if overlapping.exists():
        fail("Khoảng ngày trùng với một lịch đã có của loại trực này.")
    roster = Roster.objects.create(duty_type=kind, month=month, round_number=round_number,
                                   **{field: getattr(kind, field) for field in RULE_FIELDS})
    DutyDay.objects.bulk_create([DutyDay(roster=roster, date=day) for day in dates])
    record(request, "roster.create", f"#{roster.pk}, {roster.label}, {kind.name}")
    return redirect("roster", pk=roster.pk)


def change_roster(request, roster):
    action = request.POST.get("action")
    days = list(roster.dutyday_set.prefetch_related("slots"))
    current = {day.date: [slot.assigned_id for slot in day.slots.all()] for day in days}
    locked = MonthClose.objects.filter(school=request.school, locked=True,
                                       month__in={day.date.replace(day=1) for day in days} | {roster.month}).exists()
    if action in {"assign", "generate", "toggle_day", "toggle_required", "refresh_rules", "submit"}:
        require(request, "plan")
        if roster.state != State.DRAFT or locked:
            fail("Chỉ chỉnh sửa lịch nháp của tháng chưa khóa.")
    elif action in {"approve", "publish", "return"}:
        require(request, "duty_approve")
        if locked:
            fail("Tháng đã khóa chấm công.")
    else:
        fail("Thao tác không hợp lệ.")

    if action == "refresh_rules":
        for field in RULE_FIELDS:
            setattr(roster, field, getattr(roster.duty_type, field))
        roster.save()
    elif action == "generate":
        result, explanation = Rules(roster).solve({day.date: current[day.date] for day in days if day.locked and not day.not_required})
        if result is None:
            fail(explanation)
        unlocked = [day for day in days if not day.locked and not day.not_required]
        Slot.objects.filter(day__in=unlocked).delete()
        Slot.objects.bulk_create([Slot(day=day, original_id=employee, assigned_id=employee)
                                  for day in unlocked for employee in result[day.date]])
    elif action in {"assign", "toggle_day", "toggle_required"}:
        day = get_object_or_404(roster.dutyday_set, pk=request.POST.get("day", "0"))
        if action == "toggle_day":
            day.locked = not day.locked
            day.save(update_fields=["locked"])
        elif action == "toggle_required":
            if day.locked or current[day.date]:
                fail("Mở khóa ngày và bỏ phân công trước khi đổi trạng thái cần trực.")
            day.not_required = not day.not_required
            day.save(update_fields=["not_required"])
        else:
            if day.locked:
                fail("Ngày này đang khóa.")
            if day.not_required:
                fail("Ngày này không có nhiệm vụ cho loại trực này.")
            submitted = request.POST.getlist("employees")
            ids = list(employees(request).filter(pk__in=submitted).values_list("pk", flat=True))
            if len(ids) != len(submitted):
                fail(f"Ngày {day.date:%d/%m/%Y}: có nhân sự không hợp lệ hoặc bị chọn trùng. Kiểm tra lại danh sách.")
            issues = Rules(roster).reasons(day.date, ids, current)
            if issues:
                fail(f"Ngày {day.date:%d/%m/%Y}: " + " ".join(issues) + " Hãy đổi người hoặc chọn ngày khác.")
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
                Notification(employee_id=employee, text=f"Đã công bố {roster.duty_type.name} · {roster.label}.")
                for employee in {member for ids in current.values() for member in ids}
            ])
    suffix = f", ngày {day.date:%d/%m/%Y}" if action in {"assign", "toggle_day", "toggle_required"} else ""
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
    can_edit = request.rights["plan"] and roster.state == State.DRAFT
    if request.rights["all_duty"]:
        current = {day.date: [slot.assigned_id for slot in day.visible_slots] for day in days}
        rules = Rules(roster)
        if roster.state != State.PUBLISHED:
            issues = rules.validate(current)
        if can_edit:
            options = list(eligible_employees(roster.duty_type))
            for day in days:
                if day.locked or day.not_required:
                    continue
                day.options = [
                    (person, " ".join(rules.reasons(day.date, [person.pk], current)), person.pk in current[day.date])
                    for person in options
                ]
        for employee, name in rules.people.items():
            dates = [day for day, ids in current.items() if employee in ids]
            stats.append((name, len(dates), sum(day.weekday() == 5 for day in dates), sum(day.weekday() == 6 for day in dates)))
            timeline = sorted(set(rules.history[employee]) | set(dates))
            close.extend(f"{name}: {start:%d/%m} – {end:%d/%m} ({(end - start).days} ngày)"
                         for start, end in zip(timeline, timeline[1:])
                         if end in dates and not roster.preferred_min <= (end - start).days <= roster.preferred_max)
    months = sorted({day.date.replace(day=1) for day in days})
    return page(request, "roster", roster=roster, days=days, issues=issues, stats=stats, close=close, months=months,
                can_edit=can_edit)


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
                    if (day < localdate() or month_locked(request.school, day.replace(day=1))
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
            if (slot.assigned_id != item.employee_id or month_locked(request.school, slot.day.date.replace(day=1))
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
                    f"{roster.label} · {roster.get_state_display()}",
                    ("Ngày trực", "Người trực", "Phân công ban đầu"), rows,
                    f"lich-truc-{roster.pk}-{roster.month:%Y-%m}", format)
