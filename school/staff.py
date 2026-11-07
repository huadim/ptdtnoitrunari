from itertools import islice
from zipfile import BadZipFile, ZipFile

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.utils.timezone import localdate

from .access import record, require, school_view
from .common import fail, page, post_form
from .forms import AccountForm, DutyTypeForm, EmployeeForm, HolidayForm, ImportForm, RateForm, RoleForm
from .models import Audit, DutyType, Employee, Holiday, Membership, Rate, Role, Slot, State


def employee_rows(upload):
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException
    from defusedxml.common import DefusedXmlException
    from xml.etree.ElementTree import ParseError

    if not upload.name.lower().endswith(".xlsx") or upload.size > 2_000_000:
        fail("Tệp phải là .xlsx và tối đa 2 MB.")
    try:
        with ZipFile(upload) as archive:
            if len(archive.infolist()) > 100 or sum(entry.file_size for entry in archive.infolist()) > 10_000_000:
                fail("Tệp Excel vượt kích thước giải nén cho phép.")
        upload.seek(0)
        book = load_workbook(upload, read_only=True, keep_links=False)
        try:
            sheet = book.active
            sheet.reset_dimensions()
            data = islice(sheet.iter_rows(min_row=1, min_col=1, max_col=3), 502)
            headers = tuple(str(cell.value or "").strip().casefold() for cell in next(data))
            if headers != ("mã nhân sự", "họ và tên", "tổ chuyên môn"):
                fail("Ba cột đầu cần có tiêu đề Mã nhân sự, Họ và tên, Tổ chuyên môn.")
            seen = set()
            result = []
            for index, cells in enumerate(data, 2):
                if index > 501:
                    fail("Chỉ nhập tối đa 500 dòng mỗi lần.")
                if all(cell.value is None for cell in cells):
                    continue
                if any(cell.data_type == "f" for cell in cells):
                    fail(f"Dòng {index}: chỉ nhập giá trị, không dùng công thức.")
                form = EmployeeForm(dict(zip(("code", "name", "department"),
                                             (str(cell.value) if cell.value is not None else "" for cell in cells))))
                if not form.is_valid():
                    fail(f"Dòng {index}: kiểm tra mã, tên và độ dài các trường.")
                code = form.cleaned_data["code"].upper()
                if code in seen:
                    fail(f"Dòng {index}: mã {code} trùng trong tệp.")
                seen.add(code)
                result.append({**form.cleaned_data, "code": code})
            if not result:
                fail("Tệp không có nhân sự.")
            return result
        finally:
            book.close()
    except (BadZipFile, InvalidFileException, ParseError, DefusedXmlException, KeyError, ValueError, OSError, StopIteration):
        fail("Không đọc được tệp Excel. Hãy dùng lại mẫu nhập nhân sự.")


@school_view
def staff_template(request):
    require(request, "plan")
    from io import BytesIO

    from django.http import FileResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    sheet = book.active
    sheet.title = "Nhân sự"
    sheet.append(["Mã nhân sự", "Họ và tên", "Tổ chuyên môn"])
    for column in sheet[1]:
        column.font = Font(bold=True)
    for col in "ABC":
        sheet.column_dimensions[col].width = 30
    sheet.column_dimensions["A"].number_format = "@"
    stream = BytesIO()
    book.save(stream)
    stream.seek(0)
    return FileResponse(stream, as_attachment=True, filename="mau-nhap-nhan-su.xlsx",
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@school_view
def staff(request):
    require(request, "plan")
    rows = Employee.objects.filter(school=request.school).select_related("membership__user")
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "import":
                data = post_form(request, ImportForm(request.POST, request.FILES))
                if not data:
                    return redirect("staff")
                values = employee_rows(data["workbook"])
                existing = list(rows.filter(code__in=[value["code"] for value in values]).values_list("code", flat=True))
                if existing:
                    fail("Tệp có mã đã tồn tại: " + ", ".join(existing[:10]))
                Employee.objects.bulk_create([Employee(school=request.school, **value) for value in values])
                record(request, "employee.import", f"{len(values)} nhân sự.")
            elif action in {"create", "edit"}:
                data = post_form(request, EmployeeForm(request.POST))
                if not data:
                    return redirect("staff")
                data["code"] = data["code"].upper()
                if action == "create":
                    if rows.filter(code=data["code"]).exists():
                        fail("Mã nhân sự đã tồn tại.")
                    employee = Employee.objects.create(school=request.school, **data)
                else:
                    employee = get_object_or_404(rows, pk=request.POST.get("id", "0"))
                    if rows.exclude(pk=employee.pk).filter(code=data["code"]).exists():
                        fail("Mã nhân sự đã tồn tại.")
                    rows.filter(pk=employee.pk).update(**data)
                record(request, f"employee.{action}", f"Nhân sự #{employee.pk}")
            elif action in {"account", "role", "toggle"}:
                require(request, "settings")
                employee = get_object_or_404(rows, pk=request.POST.get("id", "0"))
                member = getattr(employee, "membership", None)
                if member and member.role == Role.PRINCIPAL:
                    fail("Tài khoản hiệu trưởng được quản lý bằng công cụ vận hành.")
                if action == "toggle":
                    if employee.active and Slot.objects.filter(assigned=employee, day__date__gte=localdate(),
                                                               day__roster__state__in=[State.APPROVED, State.PUBLISHED]).exists():
                        fail("Nhân sự còn lịch trực đã duyệt. Hãy xử lý lịch trực thay trước.")
                    employee.active = not employee.active
                    employee.save(update_fields=["active"])
                elif action == "account":
                    if member:
                        fail("Nhân sự đã có tài khoản.")
                    data = post_form(request, AccountForm(request.POST))
                    if not data:
                        return redirect("staff")
                    username = data.pop("username").casefold()
                    password = data.pop("password")
                    try:
                        with transaction.atomic():
                            if User.objects.filter(username__iexact=username).exists():
                                fail("Tên đăng nhập đã được sử dụng.")
                            user = User.objects.create_user(username=username, password=password)
                            Membership.objects.create(user=user, employee=employee, **delegation(data))
                    except IntegrityError:
                        fail("Tên đăng nhập đã được sử dụng.")
                else:
                    if not member:
                        fail("Cần tạo tài khoản trước.")
                    data = post_form(request, RoleForm(request.POST))
                    if not data:
                        return redirect("staff")
                    Membership.objects.filter(pk=member.pk).update(**delegation(data))
                record(request, f"employee.{action}", f"Nhân sự #{employee.pk}")
            else:
                return HttpResponseBadRequest("Thao tác không hợp lệ.")
            messages.success(request, "Đã lưu hồ sơ nhân sự.")
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Mã dữ liệu không hợp lệ.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("staff")
    return page(request, "staff", staff=rows, employee_form=EmployeeForm(), import_form=ImportForm(),
                account_form=AccountForm(), roles=[role for role in Role.choices if role[0] != Role.PRINCIPAL])


def delegation(data):
    if data["role"] != Role.DEPUTY:
        data["approve_duty"] = data["approve_attendance"] = False
    return data


@school_view
def configuration(request):
    require(request, "settings")
    types = DutyType.objects.filter(school=request.school).order_by("name")
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "type":
                data = post_form(request, DutyTypeForm(request.POST))
                if data:
                    if request.POST.get("id"):
                        target = get_object_or_404(types, pk=request.POST["id"])
                        if types.exclude(pk=target.pk).filter(name=data["name"]).exists():
                            fail("Tên loại trực đã tồn tại.")
                        types.filter(pk=target.pk).update(**data)
                    else:
                        target, new = DutyType.objects.get_or_create(school=request.school, name=data.pop("name"),
                                                                      defaults=data)
                        if not new:
                            fail("Tên loại trực đã tồn tại.")
                    record(request, "duty_type.save", f"Loại trực #{target.pk}")
            elif action == "rate":
                kind = get_object_or_404(types, pk=request.POST.get("id", "0"))
                data = post_form(request, RateForm(request.POST))
                if data:
                    units = data.pop("units")
                    rate, new = Rate.objects.get_or_create(duty_type=kind, **data, defaults={"units": units})
                    if not new:
                        fail("Đã có hệ số cho nhóm ngày và ngày hiệu lực này. Thêm ngày hiệu lực khác.")
                    record(request, "rate.create", f"Hệ số #{rate.pk}: {units}")
            elif action == "holiday":
                data = post_form(request, HolidayForm(request.POST))
                if data:
                    holiday, new = Holiday.objects.get_or_create(school=request.school, date=data["date"],
                                                                  defaults={"label": data["label"]})
                    if not new:
                        fail("Ngày lễ này đã có trong danh mục.")
                    record(request, "holiday.create", f"Ngày lễ #{holiday.pk}: {holiday.date}")
            else:
                return HttpResponseBadRequest("Thao tác không hợp lệ.")
            if data:
                messages.success(request, "Đã lưu cấu hình.")
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Mã dữ liệu không hợp lệ.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("configuration")
    return page(request, "configuration", types=types, type_form=DutyTypeForm(), rate_form=RateForm(),
                holiday_form=HolidayForm(), holidays=Holiday.objects.filter(school=request.school).order_by("-date")[:60],
                rates=Rate.objects.filter(duty_type__school=request.school).select_related("duty_type").order_by("-effective")[:100])


@school_view
def audits(request):
    require(request, "settings")
    rows = Audit.objects.filter(actor__employee__school=request.school).select_related("actor__employee")
    return page(request, "audits", entries=rows.order_by("-created_at")[:200])
