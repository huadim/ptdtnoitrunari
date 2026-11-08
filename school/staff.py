from itertools import islice
from zipfile import BadZipFile, ZipFile

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.utils.timezone import localdate

from .access import record, require, school_view
from .common import fail, page, post_form
from .forms import DutyTypeForm, EligibleStaffForm, EmployeeForm, HolidayForm, ImportForm, PayRateForm, RateForm
from .models import Audit, DutyType, Employee, Holiday, PayRate, Rate

EMPLOYEE_HEADERS = ("mã nhân sự", "họ và tên", "tổ chuyên môn")
TEACHER_HEADERS = ("mã giáo viên", *EMPLOYEE_HEADERS[1:])
SMARTSCHEDULER_HEADERS = ("tt", "họ đệm", "tên", "mã gv", "mã gv 2",
                          "điện thoại di động", "email", "zalo uid", "ghi chú")


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
            data = islice(sheet.iter_rows(min_row=1, min_col=1, max_col=9), 502)
            headers = tuple(str(cell.value or "").strip().casefold() for cell in next(data))
            smart = headers == SMARTSCHEDULER_HEADERS
            if not smart and headers[:3] not in (EMPLOYEE_HEADERS, TEACHER_HEADERS):
                fail("Hãy dùng mẫu nhập hồ sơ hoặc mẫu danh sách giáo viên SmartScheduler 9 cột.")
            seen = set()
            result = []
            for index, cells in enumerate(data, 2):
                if index > 501:
                    fail("Chỉ nhập tối đa 500 dòng mỗi lần.")
                cells = cells[1:4] if smart else cells[:3]
                if all(cell.value is None for cell in cells):
                    continue
                if any(cell.data_type == "f" for cell in cells):
                    fail(f"Dòng {index}: chỉ nhập giá trị, không dùng công thức.")
                fields = [str(cell.value) if cell.value is not None else "" for cell in cells]
                if smart:
                    surname, name, code = fields
                    fields = [code, " ".join((surname + " " + name).split()), ""]
                form = EmployeeForm(dict(zip(("code", "name", "department"), fields)))
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
    sheet.title = "Giáo viên" if settings.TIMETABLE_ONLY else "Nhân sự"
    sheet.append(["Mã giáo viên" if settings.TIMETABLE_ONLY else "Mã nhân sự", "Họ và tên", "Tổ chuyên môn"])
    for column in sheet[1]:
        column.font = Font(bold=True)
    for col in "ABC":
        sheet.column_dimensions[col].width = 30
    sheet.column_dimensions["A"].number_format = "@"
    stream = BytesIO()
    book.save(stream)
    stream.seek(0)
    return FileResponse(stream, as_attachment=True,
                        filename="mau-nhap-giao-vien.xlsx" if settings.TIMETABLE_ONLY else "mau-nhap-nhan-su.xlsx",
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
                from .accounts import account_action
                employee = get_object_or_404(rows, pk=request.POST.get("id", "0"))
                if account_action(request, employee, action) is None:
                    return redirect("staff")
            else:
                return HttpResponseBadRequest("Thao tác không hợp lệ.")
            messages.success(request, "Đã lưu hồ sơ nhân sự.")
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Mã dữ liệu không hợp lệ.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("staff")
    return page(request, "staff", staff=rows, employee_form=EmployeeForm(), import_form=ImportForm())


@school_view
def configuration(request):
    require(request, "settings")
    types = DutyType.objects.filter(school=request.school).order_by("name")
    active_staff = Employee.objects.filter(school=request.school, active=True)
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
            elif action == "pay_rate":
                kind = get_object_or_404(types, pk=request.POST.get("id", "0"))
                data = post_form(request, PayRateForm(request.POST))
                if data:
                    rate, new = PayRate.objects.get_or_create(
                        duty_type=kind, effective=data["effective"],
                        defaults={"unit_price": data["unit_price"], "reference": data["reference"]},
                    )
                    if not new:
                        fail("Đã có đơn giá cho loại trực và ngày hiệu lực này.")
                    record(request, "pay_rate.create", f"Đơn giá #{rate.pk}: {rate.unit_price} đồng/công.")
            elif action == "eligible_staff":
                kind = get_object_or_404(types, pk=request.POST.get("id", "0"))
                data = post_form(request, EligibleStaffForm(request.POST, staff=active_staff))
                if data:
                    kind.eligible_staff.set(data["employees"])
                    record(request, "duty_type.eligible_staff", f"Đã cập nhật nhân sự loại trực #{kind.pk}.")
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
    groups = [(kind, EligibleStaffForm(staff=active_staff, initial={"employees": kind.eligible_staff.all()},
                                      auto_id=f"kind-{kind.pk}-%s")) for kind in types]
    return page(request, "configuration", groups=groups, type_form=DutyTypeForm(), rate_form=RateForm(),
                pay_form=PayRateForm(), pay_rates=PayRate.objects.filter(duty_type__school=request.school)
                .select_related("duty_type")[:100],
                holiday_form=HolidayForm(), holidays=Holiday.objects.filter(school=request.school).order_by("-date")[:60],
                rates=Rate.objects.filter(duty_type__school=request.school).select_related("duty_type").order_by("-effective")[:100])


@school_view
def audits(request):
    require(request, "settings")
    rows = Audit.objects.filter(actor__employee__school=request.school).select_related("actor__employee")
    return page(request, "audits", entries=rows.order_by("-created_at")[:200])
