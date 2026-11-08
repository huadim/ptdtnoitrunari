from django.conf import settings
from django.db import models
from django.db.models import Q


class School(models.Model):
    name = models.CharField(max_length=180)
    slug = models.SlugField(unique=True)
    location = models.CharField(max_length=100, default="Na Rì")

    def __str__(self):
        return self.name


class Employee(models.Model):
    school = models.ForeignKey(School, on_delete=models.PROTECT)
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    department = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name", "code"]
        constraints = [models.UniqueConstraint(fields=["school", "code"], name="employee_school_code")]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Role(models.TextChoices):
    PRINCIPAL = "principal", "Hiệu trưởng"
    DEPUTY = "deputy", "Phó hiệu trưởng"
    OFFICE = "office", "Giáo vụ"
    LEAD = "lead", "Tổ trưởng"
    TEACHER = "teacher", "Giáo viên"


class Membership(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    employee = models.OneToOneField(Employee, on_delete=models.PROTECT)
    role = models.CharField(max_length=12, choices=Role)
    session_version = models.PositiveIntegerField(default=0)
    approve_duty = models.BooleanField(default=False)
    approve_attendance = models.BooleanField(default=False)

    @property
    def school(self):
        return self.employee.school


class DutyType(models.Model):
    school = models.ForeignKey(School, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    people = models.PositiveSmallIntegerField(default=2)
    min_gap = models.PositiveSmallIntegerField(default=1)
    preferred_min = models.PositiveSmallIntegerField(default=7)
    preferred_max = models.PositiveSmallIntegerField(default=8)
    pair_rest = models.PositiveSmallIntegerField(default=0)
    weekend_rest = models.PositiveSmallIntegerField(default=0)
    eligible_staff = models.ManyToManyField(Employee, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="duty_type_school_name"),
            models.CheckConstraint(condition=Q(people__gte=1) & Q(people__lte=4), name="duty_type_people"),
        ]

    def __str__(self):
        return self.name


class State(models.TextChoices):
    DRAFT = "draft", "Bản nháp"
    SUBMITTED = "submitted", "Chờ duyệt"
    APPROVED = "approved", "Đã duyệt"
    PUBLISHED = "published", "Đã công bố"


class Roster(models.Model):
    duty_type = models.ForeignKey(DutyType, on_delete=models.PROTECT)
    month = models.DateField()
    round_number = models.PositiveSmallIntegerField(null=True, blank=True)
    state = models.CharField(max_length=12, choices=State, default=State.DRAFT)
    # Keep the approved rules attached to a particular roster.
    people = models.PositiveSmallIntegerField()
    min_gap = models.PositiveSmallIntegerField()
    preferred_min = models.PositiveSmallIntegerField()
    preferred_max = models.PositiveSmallIntegerField()
    pair_rest = models.PositiveSmallIntegerField()
    weekend_rest = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["duty_type", "month"], condition=Q(round_number__isnull=True),
                                    name="roster_type_month_only"),
        ]

    @property
    def label(self):
        if self.round_number is not None:
            return f"Lượt {self.round_number:02d}"
        return f"Tháng {self.month:%m/%Y}"


class DutyDay(models.Model):
    roster = models.ForeignKey(Roster, on_delete=models.PROTECT)
    date = models.DateField()
    locked = models.BooleanField(default=False)
    not_required = models.BooleanField(default=False)

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["roster", "date"], name="roster_duty_date")]


class Slot(models.Model):
    day = models.ForeignKey(DutyDay, on_delete=models.PROTECT, related_name="slots")
    original = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="original_slots")
    assigned = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="current_slots")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["day", "original"], name="slot_day_original"),
            models.UniqueConstraint(fields=["day", "assigned"], name="slot_day_assigned"),
        ]


class Decision(models.TextChoices):
    PENDING = "pending", "Chờ xử lý"
    APPROVED = "approved", "Đã duyệt"
    REJECTED = "rejected", "Từ chối"
    CANCELLED = "cancelled", "Đã hủy"


class Request(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT)
    start = models.DateField()
    end = models.DateField()
    reason = models.CharField(max_length=500)
    slot = models.ForeignKey(Slot, null=True, blank=True, on_delete=models.PROTECT)
    replacement = models.ForeignKey(Employee, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=12, choices=Decision, default=Decision.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(start__lte=models.F("end")), name="request_dates_order")]


class DayKind(models.TextChoices):
    WEEKDAY = "weekday", "Ngày thường"
    SATURDAY = "saturday", "Thứ Bảy"
    SUNDAY = "sunday", "Chủ nhật"
    HOLIDAY = "holiday", "Ngày lễ"


class Holiday(models.Model):
    school = models.ForeignKey(School, on_delete=models.PROTECT)
    date = models.DateField()
    label = models.CharField(max_length=100)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["school", "date"], name="holiday_school_date")]


class Rate(models.Model):
    duty_type = models.ForeignKey(DutyType, on_delete=models.PROTECT)
    effective = models.DateField()
    kind = models.CharField(max_length=10, choices=DayKind)
    units = models.DecimalField(max_digits=6, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["duty_type", "effective", "kind"], name="rate_type_date_kind"),
            models.CheckConstraint(condition=Q(units__gte=0), name="rate_nonnegative"),
        ]


class PayRate(models.Model):
    duty_type = models.ForeignKey(DutyType, on_delete=models.PROTECT)
    effective = models.DateField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=0)
    reference = models.CharField(max_length=160)

    class Meta:
        ordering = ["-effective"]
        constraints = [
            models.UniqueConstraint(fields=["duty_type", "effective"], name="pay_rate_type_effective"),
            models.CheckConstraint(condition=Q(unit_price__gte=0), name="pay_rate_nonnegative"),
        ]


class Result(models.TextChoices):
    PENDING = "pending", "Chưa xác nhận"
    DONE = "done", "Hoàn thành"
    PARTIAL = "partial", "Hoàn thành một phần"
    EXCUSED = "excused", "Nghỉ có phép"
    ABSENT = "absent", "Nghỉ không phép"
    SUBSTITUTE = "substitute", "Trực thay"
    MAKEUP = "makeup", "Đã bố trí trực bù"


class Attendance(models.Model):
    slot = models.OneToOneField(Slot, on_delete=models.PROTECT)
    actual = models.ForeignKey(Employee, on_delete=models.PROTECT)
    result = models.CharField(max_length=12, choices=Result)
    kind = models.CharField(max_length=10, choices=DayKind)
    rate = models.ForeignKey(Rate, null=True, on_delete=models.PROTECT)
    units = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=0, null=True, blank=True)
    price_reference = models.CharField(max_length=160, blank=True)
    note = models.CharField(max_length=500, blank=True)
    confirmed_by = models.ForeignKey(Membership, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(auto_now=True)


class MonthClose(models.Model):
    school = models.ForeignKey(School, on_delete=models.PROTECT)
    month = models.DateField()
    locked = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["school", "month"], name="close_school_month")]


class Audit(models.Model):
    actor = models.ForeignKey(Membership, on_delete=models.PROTECT)
    action = models.CharField(max_length=50)
    detail = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)


class Notification(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT)
    text = models.CharField(max_length=300)
    seen = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class TimetableWeek(models.Model):
    school = models.ForeignKey(School, on_delete=models.PROTECT)
    monday = models.DateField()
    academic_year = models.CharField(max_length=20)
    semester = models.PositiveSmallIntegerField(choices=((1, "1"), (2, "2")), default=1)
    effective_date = models.DateField(null=True, blank=True)
    schedule_number = models.PositiveSmallIntegerField(default=1)
    state = models.CharField(max_length=12, choices=State, default=State.DRAFT)
    revision = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["school", "monday"], name="timetable_school_week")]


class TimetableProfile(models.Model):
    school = models.OneToOneField(School, on_delete=models.PROTECT)
    name = models.CharField(max_length=180)
    address = models.CharField(max_length=180, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    website = models.URLField(blank=True)


class TeachingGrade(models.Model):
    school = models.ForeignKey(School, on_delete=models.PROTECT)
    name = models.CharField(max_length=80)
    code = models.CharField(max_length=24)
    number = models.PositiveSmallIntegerField()
    notes = models.CharField(max_length=200, blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("position", "pk")
        constraints = [models.UniqueConstraint(fields=("school", "code"), name="teaching_grade_school_code")]


class TeachingTeacherCategory(models.Model):
    class Kind(models.TextChoices):
        GROUP = "nhom", "Danh sách nhóm giáo viên"
        HOMEROOM = "chu-nhiem", "Danh sách giáo viên chủ nhiệm"
        DUTY = "kiem-nhiem", "Danh sách giáo viên kiêm nhiệm"
        LEAD = "to-truong", "Danh sách tổ trưởng chuyên môn"

    teacher = models.ForeignKey(Employee, on_delete=models.PROTECT)
    kind = models.CharField(max_length=10, choices=Kind)
    label = models.CharField(max_length=100)

    class Meta:
        ordering = ("label", "teacher__name", "pk")
        constraints = [models.UniqueConstraint(fields=("teacher", "kind", "label"), name="teaching_teacher_category_once")]


class Lesson(models.Model):
    week = models.ForeignKey(TimetableWeek, on_delete=models.PROTECT)
    date = models.DateField()
    period = models.PositiveSmallIntegerField()
    classroom = models.CharField(max_length=40)
    subject = models.CharField(max_length=80)
    teacher = models.ForeignKey(Employee, on_delete=models.PROTECT)
    room = models.CharField(max_length=40)
    pinned = models.BooleanField(default=False)

    class Meta:
        ordering = ["date", "period", "classroom"]
        constraints = [
            models.UniqueConstraint(fields=["week", "date", "period", "classroom"], name="lesson_class_time"),
            models.UniqueConstraint(fields=["week", "date", "period", "teacher"], name="lesson_teacher_time"),
            models.UniqueConstraint(fields=["week", "date", "period", "room"], name="lesson_room_time"),
            models.CheckConstraint(condition=Q(period__gte=1) & Q(period__lte=15), name="lesson_period"),
        ]


class TimetablePlan(models.Model):
    week = models.OneToOneField(TimetableWeek, on_delete=models.PROTECT, related_name="plan")
    days = models.CharField(max_length=7, default="01234")
    morning = models.PositiveSmallIntegerField(default=5)
    afternoon = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(morning__lte=10, afternoon__lte=10) & Q(morning__lte=15 - models.F("afternoon"))
                & (Q(morning__gt=0) | Q(afternoon__gt=0)),
                name="teaching_plan_session_lengths",
            ),
        ]


class TeachingRequirement(models.Model):
    class Session(models.TextChoices):
        ANY = "any", "Cả hai buổi"
        AM = "am", "Buổi sáng"
        PM = "pm", "Buổi chiều"

    plan = models.ForeignKey(TimetablePlan, on_delete=models.PROTECT, related_name="requirements")
    teacher = models.ForeignKey(Employee, on_delete=models.PROTECT)
    classroom = models.CharField(max_length=40)
    subject = models.CharField(max_length=80)
    room = models.CharField(max_length=40)
    periods = models.PositiveSmallIntegerField()
    doubles = models.PositiveSmallIntegerField(default=0)
    session = models.CharField(max_length=3, choices=Session, default=Session.ANY)

    class Meta:
        ordering = ("classroom", "subject", "teacher_id")
        constraints = [
            models.UniqueConstraint(fields=["plan", "teacher", "classroom", "subject"],
                                    name="teaching_requirement_once"),
            models.CheckConstraint(condition=Q(periods__gte=1, periods__lte=30) & Q(periods__gte=2 * models.F("doubles")),
                                   name="teaching_requirement_periods"),
        ]


class TeachingUnavailable(models.Model):
    plan = models.ForeignKey(TimetablePlan, on_delete=models.PROTECT, related_name="unavailable")
    teacher = models.ForeignKey(Employee, on_delete=models.PROTECT)
    date = models.DateField()
    first = models.PositiveSmallIntegerField()
    last = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ("date", "first", "teacher_id")
        constraints = [
            models.UniqueConstraint(fields=["plan", "teacher", "date", "first", "last"], name="teaching_unavailable_once"),
            models.CheckConstraint(condition=Q(first__gte=1, last__lte=15, last__gte=models.F("first")),
                                   name="teaching_unavailable_periods"),
        ]


class TeachingBreak(models.Model):
    class Kind(models.TextChoices):
        CLASSROOM = "class", "Lớp học"
        SUBJECT = "subject", "Môn học"
        ROOM = "room", "Phòng học"

    plan = models.ForeignKey(TimetablePlan, on_delete=models.PROTECT, related_name="breaks")
    kind = models.CharField(max_length=7, choices=Kind)
    resource = models.CharField(max_length=80)
    date = models.DateField()
    first = models.PositiveSmallIntegerField()
    last = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ("date", "first", "kind", "resource")
        constraints = [
            models.UniqueConstraint(fields=["plan", "kind", "resource", "date", "first", "last"], name="teaching_break_once"),
            models.CheckConstraint(condition=Q(first__gte=1, last__gte=models.F("first"), last__lte=15),
                                   name="teaching_break_periods"),
        ]


class TeachingTeacherLimit(models.Model):
    plan = models.ForeignKey(TimetablePlan, on_delete=models.PROTECT, related_name="teacher_limits")
    teacher = models.ForeignKey(Employee, on_delete=models.PROTECT)
    days = models.PositiveSmallIntegerField(null=True, blank=True)
    daily = models.PositiveSmallIntegerField(null=True, blank=True)
    session = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ("teacher__name", "teacher_id")
        constraints = [
            models.UniqueConstraint(fields=["plan", "teacher"], name="teaching_teacher_limit_once"),
            models.CheckConstraint(
                condition=(Q(days__isnull=True) | Q(days__range=(1, 7)))
                & (Q(daily__isnull=True) | Q(daily__range=(1, 15)))
                & (Q(session__isnull=True) | Q(session__range=(1, 10)))
                & ~Q(days__isnull=True, daily__isnull=True, session__isnull=True),
                name="teaching_teacher_limit_ranges",
            ),
        ]


class LoginBucket(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    window = models.DateTimeField(db_index=True)
    failures = models.PositiveSmallIntegerField(default=0)


class AccountInvitation(models.Model):
    employee = models.OneToOneField(Employee, on_delete=models.PROTECT)
    digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True)
