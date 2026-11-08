from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.models import User
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import DayKind, Employee, Result, Role


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


def date_field(label, **kwargs):
    return forms.DateField(label=label, widget=DateInput, **kwargs)


class MonthField(forms.RegexField):
    def __init__(self, **kwargs):
        super().__init__(r"^20\d{2}-(0[1-9]|1[0-2])\Z", max_length=7, label="Tháng", **kwargs)
        self.widget.input_type = "month"

    def clean(self, value):
        from datetime import date
        return date.fromisoformat(super().clean(value) + "-01")


class EmployeeForm(forms.Form):
    code = forms.CharField(label="Mã nhân sự", max_length=40)
    name = forms.CharField(label="Họ và tên", max_length=120)
    department = forms.CharField(label="Tổ chuyên môn", max_length=100, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if settings.TIMETABLE_ONLY:
            self.fields["code"].label = "Mã giáo viên"


class ImportForm(forms.Form):
    workbook = forms.FileField(label="Tệp Excel (.xlsx)", widget=forms.FileInput(attrs={"accept": ".xlsx"}))


class AccountForm(forms.Form):
    username = forms.RegexField(r"^[\w.@+-]+\Z", label="Tên đăng nhập (dùng chung hệ thống)", max_length=150)
    password = forms.CharField(label="Mật khẩu ban đầu", widget=forms.PasswordInput, max_length=128, strip=False)
    role = forms.ChoiceField(label="Vai trò", choices=[item for item in Role.choices if item[0] != Role.PRINCIPAL])
    approve_duty = forms.BooleanField(label="Ủy quyền duyệt và công bố lịch cho phó hiệu trưởng", required=False)
    approve_attendance = forms.BooleanField(label="Ủy quyền khóa chấm công cho phó hiệu trưởng", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if settings.TIMETABLE_ONLY:
            self.fields["username"].label = "Tên đăng nhập"
            self.fields["approve_duty"].label = "Ủy quyền duyệt và công bố thời khóa biểu"
            del self.fields["approve_attendance"]

    def clean_password(self):
        password = self.cleaned_data["password"]
        from django.contrib.auth.models import User
        password_validation.validate_password(password, User(username=self.cleaned_data.get("username", "")))
        return password


class RoleForm(forms.Form):
    role = forms.ChoiceField(label="Vai trò", choices=[item for item in Role.choices if item[0] != Role.PRINCIPAL])
    approve_duty = forms.BooleanField(label="Duyệt và công bố lịch", required=False)
    approve_attendance = forms.BooleanField(label="Khóa và mở lại chấm công", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if settings.TIMETABLE_ONLY:
            self.fields["approve_duty"].label = "Duyệt và công bố thời khóa biểu"
            del self.fields["approve_attendance"]


class DutyTypeForm(forms.Form):
    name = forms.CharField(label="Loại trực", max_length=100)
    people = forms.IntegerField(label="Số người mỗi ngày", min_value=1, max_value=4, initial=2)
    min_gap = forms.IntegerField(label="Khoảng cách bắt buộc (ngày)", min_value=1, max_value=30, initial=1)
    preferred_min = forms.IntegerField(label="Khoảng cách ưu tiên từ", min_value=1, max_value=30, initial=7)
    preferred_max = forms.IntegerField(label="Khoảng cách ưu tiên đến", min_value=1, max_value=30, initial=8)
    pair_rest = forms.IntegerField(label="Số ngày trực cuối tuần cặp phải nghỉ", min_value=0, max_value=20, initial=0)
    weekend_rest = forms.IntegerField(label="Số cuối tuần nghỉ theo từng người", min_value=0, max_value=12,
                                      initial=0, required=False)

    def clean(self):
        data = super().clean()
        data["weekend_rest"] = data.get("weekend_rest") or 0
        if {"preferred_min", "preferred_max", "min_gap"} <= data.keys():
            if not data["min_gap"] <= data["preferred_min"] <= data["preferred_max"]:
                raise ValidationError("Kiểm tra lại thứ tự các mốc khoảng cách.")
        return data


class DutyRoundForm(forms.Form):
    round_number = forms.IntegerField(label="Lượt", min_value=1, max_value=999)
    start = date_field("Từ ngày")
    end = date_field("Đến ngày")

    def clean(self):
        data = super().clean()
        if "start" in data and "end" in data:
            start, end = data["start"], data["end"]
            if not (2000 <= start.year <= end.year <= 2099 and 0 <= (end - start).days < 31):
                raise ValidationError("Chọn khoảng từ 1 đến 31 ngày, trong các năm từ 2000 đến 2099.")
        return data


class RateForm(forms.Form):
    effective = date_field("Hiệu lực từ", initial=timezone.localdate)
    kind = forms.ChoiceField(label="Nhóm ngày", choices=DayKind)
    units = forms.DecimalField(label="Công quy đổi / người", max_digits=6, decimal_places=2, min_value=0)


class PayRateForm(forms.Form):
    effective = date_field("Hiệu lực từ", initial=timezone.localdate)
    unit_price = forms.DecimalField(label="Đơn giá (đồng/công)", max_digits=12, decimal_places=0, min_value=0)
    reference = forms.CharField(label="Căn cứ đơn giá", max_length=160)


class EligibleStaffForm(forms.Form):
    employees = forms.ModelMultipleChoiceField(label="Nhân sự được phân công", queryset=Employee.objects.none(),
                                               required=False)

    def __init__(self, *args, staff, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employees"].queryset = staff


class HolidayForm(forms.Form):
    date = date_field("Ngày", initial=timezone.localdate)
    label = forms.CharField(label="Tên ngày lễ", max_length=100)


class LeaveForm(forms.Form):
    start = date_field("Từ ngày", initial=timezone.localdate)
    end = date_field("Đến ngày", initial=timezone.localdate)
    reason = forms.CharField(label="Lý do", max_length=500, widget=forms.Textarea(attrs={"rows": 2}))

    def clean(self):
        data = super().clean()
        if {"start", "end"} <= data.keys():
            if not 0 <= (data["end"] - data["start"]).days <= 365:
                raise ValidationError("Ngày kết thúc phải từ ngày bắt đầu đến tối đa 365 ngày sau.")
        return data


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Lý do", max_length=500)


class ConfirmForm(forms.Form):
    result = forms.ChoiceField(label="Kết quả", choices=[("", "Chọn kết quả")] + [item for item in Result.choices if item[0] not in
                                                                                    (Result.PENDING, Result.SUBSTITUTE)])
    note = forms.CharField(label="Ghi chú", max_length=500, required=False)
    units = forms.DecimalField(label="Công thực tế cho ca một phần", max_digits=6, decimal_places=2,
                               min_value=Decimal("0.01"), required=False)

    def clean(self):
        data = super().clean()
        if data.get("result") == Result.PARTIAL:
            if data.get("units") is None and not self.has_error("units"):
                self.add_error("units", "Nhập số công thực tế cho ca một phần.")
            if not data.get("note"):
                self.add_error("note", "Ghi lý do xác nhận ca một phần.")
        return data


class WeekForm(forms.Form):
    monday = date_field("Thứ Hai đầu tuần")
    academic_year = forms.CharField(label="Năm học", max_length=20,
                                    widget=forms.TextInput(attrs={"placeholder": "VD 2026–2027"}))

    def clean_monday(self):
        monday = self.cleaned_data["monday"]
        if monday.weekday():
            raise ValidationError("Chọn ngày thứ Hai.")
        return monday


class LessonForm(forms.Form):
    date = date_field("Ngày học")
    period = forms.IntegerField(label="Tiết", min_value=1, max_value=15)
    classroom = forms.CharField(label="Lớp", max_length=40)
    subject = forms.CharField(label="Môn học", max_length=80)
    teacher = forms.ModelChoiceField(label="Giáo viên", queryset=Employee.objects.none(), empty_label="Chọn giáo viên")
    room = forms.CharField(label="Phòng", max_length=40)

    def __init__(self, *args, teachers, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["teacher"].queryset = teachers

    def clean_classroom(self):
        return " ".join(self.cleaned_data["classroom"].upper().split())

    def clean_room(self):
        return " ".join(self.cleaned_data["room"].upper().split())


class PasswordSetupForm(forms.Form):
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    password1 = forms.CharField(
        label="Mật khẩu mới", strip=False, max_length=128,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "minlength": "12"}),
    )
    password2 = forms.CharField(
        label="Nhập lại mật khẩu", strip=False, max_length=128,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "minlength": "12"}),
    )

    def clean(self):
        data = super().clean()
        password = data.get("password1")
        if password and data.get("password2"):
            if password != data["password2"]:
                self.add_error("password2", "Hai mật khẩu chưa khớp.")
            try:
                password_validation.validate_password(password, self.user or User(username=data.get("username", "")))
            except ValidationError as exc:
                self.add_error("password1", exc)
        return data


class RegistrationForm(PasswordSetupForm):
    invite_code = forms.RegexField(
        r"^[A-Za-z0-9_-]{43}$", label="Mã mời của nhà trường", max_length=43,
        error_messages={"invalid": "Mã mời gồm 43 ký tự. Hãy sao chép nguyên mã do hiệu trưởng cấp."},
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
    )
    username = forms.CharField(
        label="Tên đăng nhập", max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "username", "spellcheck": "false"}),
    )
    field_order = ["invite_code", "username", "password1", "password2"]

    def clean_username(self):
        username = User.normalize_username(self.cleaned_data["username"]).casefold()
        if len(username) > 150:
            raise ValidationError("Tên đăng nhập tối đa 150 ký tự.")
        UnicodeUsernameValidator()(username)
        return username
