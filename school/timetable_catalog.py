from io import BytesIO
from itertools import islice
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Max
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from openpyxl import Workbook, load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .access import record, require, school_view
from .common import employees, fail, page
from .forms import ImportForm, date_field
from .models import State, TeachingGrade, TeachingTeacherCategory, TimetableProfile
from .timetable import get_week, render_week, visible_weeks, week_picker

GRADE_HEADERS = ("Tên khối học", "Mã khối học", "Số khối", "Ghi chú")
GRADE_SORTS = {"name": ("name", "number", "code"), "number": ("number", "name", "code")}
CATEGORY_LABELS = dict(zip(TeachingTeacherCategory.Kind.values, ("Tên nhóm", "Lớp chủ nhiệm", "Nhiệm vụ", "Tổ chuyên môn")))


class ProfileForm(forms.ModelForm):
    class Meta:
        model = TimetableProfile
        fields = ("name", "address", "phone", "email", "website")
        labels = dict(zip(fields, ("Tên trường", "Địa chỉ", "Điện thoại", "Email", "Website")))
        widgets = {"phone": forms.TextInput(attrs={"type": "tel"})}


class WeekInfoForm(forms.Form):
    academic_year = forms.CharField(label="Năm học", max_length=20)
    semester = forms.TypedChoiceField(label="Học kỳ", choices=((1, "1"), (2, "2")), coerce=int)
    effective_date = date_field("Ngày áp dụng", required=False)
    schedule_number = forms.IntegerField(label="TKB số", min_value=1, max_value=9999)


@school_view
def timetable_info(request, pk=None):
    require(request, "plan")
    week = get_week(request, pk) if pk is not None else None
    profile = TimetableProfile.objects.filter(school=request.school).first()
    data = request.POST if request.method == "POST" else None
    profile_form = ProfileForm(data, instance=profile, initial={"name": request.school.name} if profile is None else None)
    week_form = None
    if week:
        initial = {name: getattr(week, name) for name in WeekInfoForm.base_fields}
        initial["effective_date"] = week.effective_date or week.monday
        week_form = WeekInfoForm(data, initial=initial)
        if week.state != State.DRAFT:
            for field in week_form.fields.values():
                field.disabled = True
    if data is not None:
        profile_valid = profile_form.is_valid()
        week_valid = week_form is None or week_form.is_valid()
        if week and week.state == State.DRAFT and data.get("revision") != str(week.revision):
            week_form.add_error(None, "Tuần học vừa được cập nhật. Hãy mở lại Thông tin chung.")
            week_valid = False
        if profile_valid and week_valid:
            saved = profile_form.save(commit=False)
            saved.school = request.school
            saved.save()
            if week_form and week.state == State.DRAFT and week_form.changed_data:
                for field, value in week_form.cleaned_data.items():
                    setattr(week, field, value)
                week.revision += 1
                week.save(update_fields=[*week_form.fields, "revision"])
            record(request, "timetable.info", f"Thông tin chung · tuần #{week.pk}" if week else "Thông tin trường học")
            messages.success(request, "Đã lưu thông tin chung.")
            return redirect("week", pk=week.pk) if week else redirect("timetable")
    modal = {"profile_form": profile_form, "info_week_form": week_form,
             "info_action": reverse("timetable_week_info", args=[week.pk]) if week else reverse("timetable_info"),
             "info_back": reverse("week", args=[week.pk]) if week else reverse("timetable")}
    if week:
        return render_week(request, week, **modal)
    return page(request, "week", **week_picker(request, None), **modal)


class GradeForm(forms.ModelForm):
    number = forms.IntegerField(label="Số khối", min_value=1, max_value=99)

    class Meta:
        model = TeachingGrade
        fields = ("name", "code", "number", "notes")
        labels = dict(zip(fields, GRADE_HEADERS))

    def clean_code(self):
        code = self.cleaned_data["code"].upper()
        if len(code) > 24:
            raise ValidationError("Mã khối tối đa 24 ký tự.")
        return code


def grade_workbook(upload):
    try:
        if not upload.name.lower().endswith(".xlsx") or upload.size > 2_000_000:
            fail("Chọn tệp .xlsx tối đa 2 MB.")
        with ZipFile(upload) as archive:
            if len(archive.infolist()) > 100 or sum(entry.file_size for entry in archive.infolist()) > 10_000_000:
                fail("Tệp Excel vượt giới hạn giải nén.")
        upload.seek(0)
        book = load_workbook(upload, read_only=True, keep_links=False)
        try:
            sheet = book.active
            sheet.reset_dimensions()
            rows = islice(sheet.iter_rows(min_row=1, max_col=4), 502)
            headers = tuple(str(cell.value or "").strip().casefold() for cell in next(rows))
            if headers != tuple(label.casefold() for label in GRADE_HEADERS):
                fail("Các cột phải theo mẫu Excel của danh sách khối học.")
            results = {}
            for line, cells in enumerate(rows, 2):
                if line > 501:
                    fail("Chỉ nhập tối đa 500 dòng mỗi lần.")
                if all(cell.value is None for cell in cells):
                    continue
                values = dict(zip(GradeForm.base_fields, (cell.value for cell in cells)))
                form = GradeForm(values)
                if any(cell.data_type == "f" for cell in cells) or not form.is_valid():
                    fail(f"Dòng {line}: kiểm tra tên, mã, số khối và ghi chú. Dùng giá trị thay cho công thức.")
                code = form.cleaned_data["code"]
                if code in results:
                    fail(f"Dòng {line}: mã {code} trùng trong tệp Excel.")
                results[code] = form.cleaned_data
            if not results:
                fail("Tệp chưa có dòng dữ liệu.")
            return results
        finally:
            book.close()
    except (BadZipFile, InvalidFileException, ParseError, DefusedXmlException, KeyError, ValueError, OSError, StopIteration):
        fail("Không đọc được tệp Excel. Hãy tải lại mẫu khối học.")


def change_grades(request, rows, action, target):
    if action == "import":
        form = ImportForm(request.POST, request.FILES)
        if not form.is_valid():
            fail("Hãy chọn tệp Excel khối học.")
        incoming = grade_workbook(form.cleaned_data["workbook"])
        existing = {grade.code: grade for grade in rows.filter(code__in=incoming)}
        added, updated = [], []
        position = rows.aggregate(value=Max("position", default=0))["value"]
        for code, values in incoming.items():
            if grade := existing.get(code):
                for field, value in values.items():
                    setattr(grade, field, value)
                updated.append(grade)
            else:
                position += 1
                added.append(TeachingGrade(school=request.school, position=position, **values))
        TeachingGrade.objects.bulk_update(updated, GradeForm.Meta.fields)
        TeachingGrade.objects.bulk_create(added)
    elif action == "delete" and target:
        target.delete()
    elif action in GRADE_SORTS or action in {"up", "down"} and target:
        ordered = list(rows.order_by(*GRADE_SORTS[action])) if action in GRADE_SORTS else list(rows)
        if action in {"up", "down"}:
            index = ordered.index(target)
            neighbor = index + (-1 if action == "up" else 1)
            if 0 <= neighbor < len(ordered):
                ordered[index], ordered[neighbor] = ordered[neighbor], ordered[index]
        for position, grade in enumerate(ordered, 1):
            grade.position = position
        TeachingGrade.objects.bulk_update(ordered, ["position"])
    else:
        fail("Hãy chọn một khối học và thao tác phù hợp.")


@school_view
def timetable_grades(request):
    require(request, "plan")
    rows = TeachingGrade.objects.filter(school=request.school)
    data = request.POST if request.method == "POST" else request.GET
    action = data.get("action")
    identifier = data.get("grade")
    target = None
    if identifier:
        if not (identifier.isascii() and identifier.isdecimal() and len(identifier) < 19):
            raise Http404
        target = get_object_or_404(rows, pk=identifier)
    form = GradeForm(instance=target)
    mode = data.get("mode")
    if request.method == "POST" and action in {"edit", "confirm_delete"}:
        if target:
            mode = "edit" if action == "edit" else "delete"
        else:
            messages.error(request, "Hãy chọn một khối học trong bảng.")
    elif request.method == "POST":
        try:
            if action == "save":
                form = GradeForm(data, instance=target)
                if form.is_valid():
                    if rows.exclude(pk=target.pk if target else None).filter(code=form.cleaned_data["code"]).exists():
                        form.add_error("code", "Mã khối học đã tồn tại.")
                    else:
                        grade = form.save(commit=False)
                        grade.school = request.school
                        if not target:
                            grade.position = rows.aggregate(value=Max("position", default=0))["value"] + 1
                        grade.save()
                        record(request, "timetable.grade.save", f"Khối #{grade.pk}")
                        messages.success(request, "Đã lưu khối học.")
                        return redirect(f"{reverse('timetable_grades')}?grade={grade.pk}")
            else:
                change_grades(request, rows, action, target)
                record(request, "timetable.grade.change", action)
                messages.success(request, "Đã cập nhật danh sách khối học.")
                return redirect("timetable_grades")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    if request.method == "POST" and action == "save":
        mode = "edit" if target else "add"
    return page(request, "timetable_grades", catalog_title="Dữ liệu khối học", grades=rows, grade=target,
                grade_form=form, import_form=ImportForm(), mode=mode,
                menu_week=visible_weeks(request).order_by("-monday").first())


@school_view
def timetable_grade_template(request):
    require(request, "plan")
    if request.method == "POST":
        raise Http404
    book = Workbook()
    sheet = book.active
    sheet.title = "Khối học"
    sheet.append(GRADE_HEADERS)
    for key in "ABCD":
        sheet.column_dimensions[key].width = 24
    sheet.column_dimensions["B"].number_format = "@"
    stream = BytesIO()
    book.save(stream)
    stream.seek(0)
    return FileResponse(stream, as_attachment=True, filename="mau-khoi-hoc.xlsx",
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class TeacherCategoryForm(forms.ModelForm):
    class Meta:
        model = TeachingTeacherCategory
        fields = ("teacher", "label")
        labels = {"teacher": "Giáo viên"}

    def __init__(self, *args, teachers, label, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["teacher"].queryset = teachers
        self.fields["label"].label = label


@school_view
def timetable_teacher_categories(request, kind):
    require(request, "plan")
    if kind not in TeachingTeacherCategory.Kind.values:
        raise Http404
    label = CATEGORY_LABELS[kind]
    rows = TeachingTeacherCategory.objects.filter(kind=kind, teacher__school=request.school).select_related("teacher")
    data = request.POST if request.method == "POST" else request.GET
    identifier = data.get("item", "")
    selected = None
    if identifier:
        if not (identifier.isascii() and identifier.isdecimal() and len(identifier) < 19):
            raise Http404
        selected = get_object_or_404(rows, pk=identifier)
    form = TeacherCategoryForm(teachers=employees(request), label=label, instance=selected)
    if request.method == "POST":
        action = data.get("action")
        url = reverse("timetable_teacher_categories", args=[kind])
        if action == "delete" and selected:
            selected.delete()
            record(request, "timetable.teacher_category.delete", kind)
            messages.success(request, "Đã xóa mục khỏi danh sách.")
            return redirect(url)
        if action != "save":
            raise Http404
        form = TeacherCategoryForm(data, teachers=employees(request), label=label, instance=selected)
        if form.is_valid():
            duplicates = rows.filter(label__iexact=form.cleaned_data["label"]).exclude(pk=selected.pk if selected else None)
            if kind != TeachingTeacherCategory.Kind.HOMEROOM:
                duplicates = duplicates.filter(teacher=form.cleaned_data["teacher"])
            if duplicates.exists():
                form.add_error("label", "Mục này đã có trong danh sách.")
            else:
                entry = form.save(commit=False)
                entry.kind = kind
                entry.save()
                record(request, "timetable.teacher_category.save", f"{kind} #{entry.pk}")
                messages.success(request, "Đã cập nhật danh sách.")
                return redirect(url)
    return page(request, "timetable_teacher_categories", catalog_title=TeachingTeacherCategory.Kind(kind).label,
                category=kind, rows=rows, selected=selected, label=label, form=form,
                menu_week=visible_weeks(request).order_by("-monday").first())
