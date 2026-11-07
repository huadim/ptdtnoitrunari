from datetime import timedelta
from secrets import randbelow

from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils.crypto import salted_hmac
from django.utils.timezone import localdate, now

from .access import school_view
from .common import page
from .models import Employee, LoginBucket, Notification, Roster, Slot, State


class SchoolAuthentication(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"placeholder": "Tên đăng nhập của bạn", "spellcheck": "false"})
        self.fields["password"].widget.attrs["placeholder"] = "Nhập mật khẩu"

    def clean(self):
        username = self.cleaned_data.get("username")
        if not username or not self.cleaned_data.get("password"):
            return super().clean()
        self.cleaned_data["username"] = username.casefold()
        address = self.request.META.get("REMOTE_ADDR", "")
        if settings.SECURE_PROXY_SSL_HEADER:
            address = self.request.META.get("HTTP_X_FORWARDED_FOR", address).split(",")[0].strip()
        key = salted_hmac("school.login", f"{address}:{username.casefold()}", algorithm="sha256").hexdigest()
        current = now()
        error = None
        data = None
        with transaction.atomic():
            bucket, _ = LoginBucket.objects.select_for_update().get_or_create(key=key, defaults={"window": current})
            if bucket.window < current - timedelta(minutes=10):
                bucket.window, bucket.failures = current, 0
            if bucket.failures >= 12:
                error = ValidationError("Đã thử quá nhiều lần. Hãy thử lại sau ít phút.")
            else:
                try:
                    data = super().clean()
                except ValidationError as exc:
                    error = exc
                    bucket.failures += 1
                    bucket.save(update_fields=["failures", "window"])
                else:
                    bucket.delete()
        if randbelow(128) == 0:
            LoginBucket.objects.filter(window__lt=current - timedelta(days=1)).delete()
        if error is not None:
            raise error
        return data

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not getattr(user, "membership", None) or not user.membership.employee.active:
            raise ValidationError(self.error_messages["invalid_login"], code="invalid_login")


class SchoolLogin(LoginView):
    template_name = "school/login.html"
    authentication_form = SchoolAuthentication
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        self.request.session["school_session_version"] = form.get_user().membership.session_version
        if not self.request.POST.get("remember"):
            self.request.session.set_expiry(0)
        return response


@school_view
def home(request):
    if request.method == "POST":
        return HttpResponseBadRequest()
    today = localdate()
    month = today.replace(day=1)
    own = Slot.objects.filter(day__roster__duty_type__school=request.school, day__roster__state=State.PUBLISHED,
                              assigned=request.member.employee, day__date__gte=today)
    summary = None
    if request.rights["all_duty"]:
        rosters = Roster.objects.filter(duty_type__school=request.school, month=month)
        summary = (Employee.objects.filter(school=request.school, active=True).count(), rosters.count(),
                   rosters.filter(state=State.PUBLISHED).count())
    notes = Notification.objects.filter(employee=request.member.employee, seen=False)
    return page(request, "home", today=today, upcoming=own.select_related("day__roster__duty_type").order_by("day__date")[:7],
                summary=summary, unread=notes.count(), notes=notes.order_by("-created_at")[:5])


@school_view
def notifications(request):
    notes = Notification.objects.filter(employee=request.member.employee)
    if request.method == "POST":
        notes.filter(seen=False).update(seen=True)
        return redirect("notifications")
    return page(request, "notifications", notes=notes.order_by("-created_at")[:100])
