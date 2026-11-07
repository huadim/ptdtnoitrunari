from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import MonthField
from .models import Employee, MonthClose


def page(request, template, **context):
    return render(request, f"school/{template}.html", {
        "school": request.school, "member": request.member, "rights": request.rights, **context,
    })


def chosen_month(request, params=None):
    params = request.GET if params is None else params
    value = params.get("month")
    return MonthField().clean(value) if value is not None else timezone.localdate().replace(day=1)


def month_redirect(name, month):
    return redirect(f"{reverse(name)}?month={month:%Y-%m}")


def bad_month():
    return HttpResponseBadRequest("Tháng không hợp lệ. Dùng định dạng YYYY-MM.")


def post_form(request, form):
    if form.is_valid():
        return form.cleaned_data
    for errors in form.errors.values():
        for error in errors:
            messages.error(request, error)
    return None


def employees(request):
    return Employee.objects.filter(school=request.school, active=True)


def month_locked(school, month):
    return MonthClose.objects.filter(school=school, month=month, locked=True).exists()


def fail(message):
    raise ValidationError(message)
