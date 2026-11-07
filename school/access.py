from functools import wraps

from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import Audit, Membership, Role, School


def permissions(member):
    principal = member.role == Role.PRINCIPAL
    deputy = member.role == Role.DEPUTY
    office = member.role == Role.OFFICE
    duty = principal or (deputy and member.approve_duty)
    attendance = principal or (deputy and member.approve_attendance)
    return {
        "settings": principal,
        "plan": principal or office,
        "duty_approve": duty,
        "attendance_approve": attendance,
        "all_duty": principal or office or duty or attendance,
        "all_attendance": principal or office or attendance,
        "all_timetable": principal or office or duty,
        "department": member.role == Role.LEAD and bool(member.employee.department),
    }


def school_view(view):
    @wraps(view)
    @require_http_methods(["GET", "POST", "HEAD"])
    @never_cache
    @login_required
    def wrapper(request, *args, **kwargs):
        if request.method == "POST":
            # Serialize writes within one school, including cross-roster checks.
            with transaction.atomic():
                initial = get_object_or_404(Membership.objects.select_related("employee"), user=request.user)
                School.objects.select_for_update().get(pk=initial.employee.school_id)
                return scoped(request, *args, **kwargs)
        return scoped(request, *args, **kwargs)

    def scoped(request, *args, **kwargs):
        request.member = get_object_or_404(Membership.objects.select_related("employee__school"),
                                          user=request.user, employee__active=True)
        if request.session.get("school_session_version", 0) != request.member.session_version:
            logout(request)
            return redirect("login")
        request.school = request.member.school
        request.rights = permissions(request.member)
        return view(request, *args, **kwargs)

    return wrapper


def require(request, permission):
    if not request.rights[permission]:
        raise PermissionDenied


def record(request, action, detail):
    Audit.objects.create(actor=request.member, action=action, detail=detail)


def employee_scope(request, query, field, permission):
    if request.rights[permission]:
        return query
    if request.rights["department"]:
        return query.filter(**{f"{field}__department": request.member.employee.department})
    return query.filter(**{f"{field}_id": request.member.employee_id})
