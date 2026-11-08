from datetime import timedelta
from hashlib import sha256
from secrets import token_urlsafe

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.crypto import salted_hmac
from django.utils.timezone import localdate, now
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .access import record, require, school_view
from .common import fail, page, post_form
from .forms import AccountForm, PasswordSetupForm, RegistrationForm, RoleForm
from .models import AccountInvitation, Audit, Employee, LoginBucket, Membership, Role, School, Slot, State
from .timetable import visible_weeks


def delegation(data):
    if data["role"] != Role.DEPUTY:
        data["approve_duty"] = data["approve_attendance"] = False
    return data


def account_action(request, employee, action):
    require(request, "settings")
    member = getattr(employee, "membership", None)
    if member and member.role == Role.PRINCIPAL:
        fail("Tài khoản hiệu trưởng được quản lý bằng công cụ vận hành.")
    result = {}
    if action == "account":
        if member:
            fail("Nhân sự đã có tài khoản.")
        if not employee.active:
            fail("Kích hoạt hồ sơ nhân sự trước khi cấp tài khoản.")
        data = post_form(request, AccountForm(request.POST))
        if not data:
            return None
        username = User.normalize_username(data.pop("username")).casefold()
        if len(username) > 150:
            fail("Tên đăng nhập tối đa 150 ký tự.")
        password = data.pop("password")
        try:
            with transaction.atomic():
                if User.objects.filter(username__iexact=username).exists():
                    fail("Tên đăng nhập đã được sử dụng.")
                user = User.objects.create_user(username=username, password=password)
                Membership.objects.create(user=user, employee=employee, **delegation(data))
                AccountInvitation.objects.filter(employee=employee, used_at__isnull=True).update(used_at=now())
        except IntegrityError:
            fail("Tên đăng nhập đã được sử dụng.")
    elif action == "role":
        if not member:
            fail("Cần tạo tài khoản trước.")
        data = post_form(request, RoleForm(request.POST))
        if not data:
            return None
        Membership.objects.filter(pk=member.pk).update(**delegation(data))
    elif action == "toggle":
        if employee.active and Slot.objects.filter(
            assigned=employee, day__date__gte=localdate(), day__roster__state__in=[State.APPROVED, State.PUBLISHED],
        ).exists():
            fail("Nhân sự còn lịch trực đã duyệt. Hãy xử lý lịch trực thay trước.")
        employee.active = not employee.active
        employee.save(update_fields=["active"])
        if member and not employee.active:
            member.session_version += 1
            member.save(update_fields=["session_version"])
    elif action in {"suspend", "restore"}:
        if not member:
            fail("Nhân sự chưa có tài khoản.")
        active = action == "restore"
        if member.user.is_active == active:
            fail("Tài khoản đã ở trạng thái này.")
        member.user.is_active = active
        member.user.save(update_fields=["is_active"])
        if not active:
            member.session_version += 1
            member.save(update_fields=["session_version"])
    elif action == "password":
        if not member:
            fail("Nhân sự chưa có tài khoản.")
        form = PasswordSetupForm(request.POST, user=member.user)
        if not post_form(request, form):
            return None
        member.user.set_password(form.cleaned_data["password1"])
        member.user.save(update_fields=["password"])
    elif action == "invite":
        if member or not employee.active:
            fail("Chỉ cấp mã mời cho nhân sự đang hoạt động và chưa có tài khoản.")
        code = token_urlsafe(32)
        invitation, _ = AccountInvitation.objects.update_or_create(employee=employee, defaults={
            "digest": sha256(code.encode()).hexdigest(), "expires_at": now() + timedelta(hours=48), "used_at": None,
        })
        result = {"invite_code": code, "expires_at": invitation.expires_at}
    elif action == "revoke":
        invitation = get_object_or_404(AccountInvitation, employee=employee)
        if invitation.used_at:
            fail("Mã mời đã được sử dụng hoặc thu hồi.")
        invitation.used_at = now()
        invitation.save(update_fields=["used_at"])
    else:
        fail("Thao tác không hợp lệ.")
    record(request, f"account.{action}", f"Nhân sự #{employee.pk}" +
           (f", vai trò {data['role']}" if action in {"account", "role"} else ""))
    return result


@school_view
def accounts(request):
    require(request, "settings")
    if request.method == "POST":
        return HttpResponseBadRequest()
    rows = Employee.objects.filter(school=request.school).select_related("membership__user", "accountinvitation")
    total = rows.count()
    with_accounts = rows.filter(membership__isnull=False).count()
    suspended = rows.filter(membership__user__is_active=False).count()
    query = request.GET.get("q", "").strip()[:120]
    if query:
        from django.db.models import Q
        rows = rows.filter(Q(name__icontains=query) | Q(code__icontains=query) | Q(membership__user__username__icontains=query))
    role = request.GET.get("role", "")
    if role in Role.values:
        rows = rows.filter(membership__role=role)
    status = request.GET.get("status", "")
    if status == "pending":
        rows = rows.filter(membership__isnull=True)
    elif status == "suspended":
        rows = rows.filter(membership__user__is_active=False)
    elif status == "active":
        rows = rows.filter(membership__user__is_active=True, active=True)
    return page(request, "accounts", employees=rows, query=query, selected_role=role, selected_status=status,
                roles=Role.choices, counts=(total, with_accounts, suspended, total - with_accounts))


@school_view
def account_detail(request, pk):
    require(request, "settings")
    employee = get_object_or_404(
        Employee.objects.select_related("membership__user", "accountinvitation"), school=request.school, pk=pk,
    )
    result = {}
    if request.method == "POST":
        try:
            result = account_action(request, employee, request.POST.get("action"))
            if result is not None:
                messages.success(request, "Đã cập nhật tài khoản.")
            if not result or not result.get("invite_code"):
                return redirect("account_detail", pk=pk)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("account_detail", pk=pk)
    member = getattr(employee, "membership", None)
    initial = {field: getattr(member, field) for field in ("role", "approve_duty", "approve_attendance")} if member else {}
    return page(request, "account_detail", employee=employee, target_member=member,
                menu_week=visible_weeks(request).order_by("-monday").first(),
                account_form=AccountForm(initial={"role": Role.TEACHER}), role_form=RoleForm(initial=initial),
                password_form=PasswordSetupForm(), registration_url=request.build_absolute_uri("/dang-ky/"), **result)


@never_cache
@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = RegistrationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        address = request.META.get("REMOTE_ADDR", "")
        # Registration uses the same trusted-proxy boundary as the sign-in endpoint.
        from django.conf import settings
        if settings.SECURE_PROXY_SSL_HEADER:
            address = request.META.get("HTTP_X_FORWARDED_FOR", address).split(",")[0].strip()
        key = salted_hmac("school.registration", address, algorithm="sha256").hexdigest()
        with transaction.atomic():
            bucket, _ = LoginBucket.objects.select_for_update().get_or_create(key=key, defaults={"window": now()})
            if bucket.window < now() - timedelta(minutes=10):
                bucket.failures, bucket.window = 0, now()
            allowed = bucket.failures < 20
            if allowed:
                bucket.failures += 1
                bucket.save(update_fields=["failures", "window"])
        if not allowed:
            form.add_error(None, "Đã gửi quá nhiều yêu cầu. Hãy thử lại sau ít phút.")
        elif form.is_valid():
            try:
                create_from_invitation(form)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, "Đăng ký thành công. Bạn có thể đăng nhập bằng tài khoản vừa tạo.")
                return redirect("login")
    return render(request, "school/register.html", {"form": form})


def create_from_invitation(form):
    digest = sha256(form.cleaned_data["invite_code"].encode()).hexdigest()
    school_id = AccountInvitation.objects.filter(digest=digest).values_list("employee__school_id", flat=True).first()
    invalid = "Mã mời đã hết hạn, đã được sử dụng hoặc không hợp lệ. Hãy liên hệ hiệu trưởng để được cấp lại."
    if not school_id:
        fail(invalid)
    try:
        with transaction.atomic():
            School.objects.select_for_update().get(pk=school_id)
            invitation = AccountInvitation.objects.select_related("employee").filter(
                digest=digest, used_at__isnull=True, expires_at__gt=now(), employee__active=True,
                employee__membership__isnull=True,
            ).first()
            if not invitation:
                fail(invalid)
            username = form.cleaned_data["username"]
            if User.objects.filter(username__iexact=username).exists():
                fail("Tên đăng nhập đã được sử dụng. Hãy chọn tên khác.")
            user = User.objects.create_user(username=username, password=form.cleaned_data["password1"])
            member = Membership.objects.create(employee=invitation.employee, user=user, role=Role.TEACHER)
            invitation.used_at = now()
            invitation.save(update_fields=["used_at"])
            Audit.objects.create(actor=member, action="account.register", detail=f"Nhân sự #{invitation.employee_id}")
    except IntegrityError:
        fail("Không thể hoàn tất đăng ký. Hãy kiểm tra lại mã mời và tên đăng nhập.")
