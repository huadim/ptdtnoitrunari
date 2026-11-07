from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.timezone import localdate, now
from docx import Document
from openpyxl import Workbook, load_workbook

from .models import (Attendance, Audit, DayKind, Decision, DutyDay, DutyType, Employee, Holiday, Lesson,
                     LoginBucket, Membership, MonthClose, Rate, Request, Result, Role, Roster, School, Slot, State, TimetableWeek)
from .scheduling import Rules

PASSWORD = "A-very-strong-password-for-tests-only"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SchoolTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="Trường Nội trú Thử nghiệm", slug="a")
        cls.other = School.objects.create(name="Trường B", slug="b")
        cls.kind = DutyType.objects.create(name="Trực nội trú", school=cls.school)
        cls.principal = cls.account(cls.school, "p", Role.PRINCIPAL)
        cls.office = cls.account(cls.school, "o", Role.OFFICE)
        cls.teacher = cls.account(cls.school, "t", Role.TEACHER)
        cls.lead = cls.account(cls.school, "l", Role.LEAD)
        cls.deputy = cls.account(cls.school, "d", Role.DEPUTY)
        cls.foreign = cls.account(cls.other, "f", Role.PRINCIPAL)
        cls.others = Employee.objects.bulk_create([
            Employee(school=cls.school, code=f"GV{i:02}", name=f"Nhân sự {i:02}", department="Tổ B")
            for i in range(14)
        ])

    @classmethod
    def account(cls, school, username, role):
        employee = Employee.objects.create(school=school, code=username.upper(), name=f"Họ tên {username}",
                                           department="Tổ A")
        user = User.objects.create_user(username=username, password=PASSWORD)
        return Membership.objects.create(employee=employee, user=user, role=role)

    def sign(self, membership):
        self.client.force_login(membership.user)

    def roster(self, month, state=State.DRAFT, **overrides):
        params = {"people": 2, "min_gap": 1, "preferred_min": 7, "preferred_max": 8, "pair_rest": 0, **overrides}
        return Roster.objects.create(duty_type=self.kind, month=month, state=state, **params)

    def slot(self, roster, day, member, assigned=None):
        duty_day, _ = DutyDay.objects.get_or_create(roster=roster, date=day)
        return Slot.objects.create(day=duty_day, original=member, assigned=assigned or member)

    def follow(self, name, data, *args):
        return self.client.post(reverse(name, args=args), data, follow=True)

    def test_login_csrf_and_scoped_page_rendering(self):
        client = Client(enforce_csrf_checks=True)
        self.assertRedirects(client.get(reverse("home")), f"{reverse('login')}?next=/")
        self.assertEqual(client.post(reverse("login"), {"username": "p", "password": PASSWORD}).status_code, 403)
        self.assertContains(client.get(reverse("login")), "Đăng nhập")
        self.sign(self.principal)
        for name in ("home", "staff", "configuration", "audits", "timetable", "rosters", "requests",
                     "attendance", "notifications", "password"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.sign(self.teacher)
        self.assertEqual(self.client.get(reverse("staff")).status_code, 403)
        self.assertEqual(self.client.get(reverse("configuration")).status_code, 403)
        self.assertEqual(self.client.post(reverse("rosters"), {"month": "2026-05", "duty_type": self.kind.pk}).status_code, 403)

    def test_login_throttle_survives_new_client(self):
        url = reverse("login")
        for _ in range(12):
            self.client.post(url, {"username": "P", "password": "incorrect"})
        blocked = Client().post(url, {"username": "p", "password": PASSWORD})
        self.assertContains(blocked, "Đã thử quá nhiều lần.")
        LoginBucket.objects.update(window=now() - timedelta(minutes=11))
        self.assertRedirects(Client().post(url, {"username": "P", "password": PASSWORD}), reverse("home"))

    def test_foreign_objects_hidden_from_pages_and_exports(self):
        today = localdate()
        month = today.replace(day=1)
        roster = self.roster(month, state=State.PUBLISHED)
        slot = self.slot(roster, today, self.teacher.employee)
        week = TimetableWeek.objects.create(school=self.school, monday=today - timedelta(days=today.weekday()),
                                            academic_year="2026–2027", state=State.PUBLISHED)
        self.sign(self.foreign)
        for name, args in (("roster", [roster.pk]), ("roster_export", [roster.pk, "pdf"]),
                           ("week", [week.pk]), ("timetable_export", [week.pk, "xlsx"])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 404)
        response = self.client.post(reverse("attendance"), {"month": month.strftime("%Y-%m"), "action": "confirm",
                                                            "slot": slot.pk, "result": Result.ABSENT})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Attendance.objects.exists())

    def test_excel_import_validates_before_creating_any_employee(self):
        self.sign(self.office)

        def import_book(codes):
            book = Workbook()
            book.active.append(["Mã nhân sự", "Họ và tên", "Tổ chuyên môn"])
            for code in codes:
                book.active.append([code, "Người thử nghiệm", "Tổ C"])
            buffer = BytesIO()
            book.save(buffer)
            return self.follow("staff", {"action": "import",
                                         "workbook": SimpleUploadedFile("mau.xlsx", buffer.getvalue())})

        self.assertContains(import_book(["NEW", "new"]), "trùng trong tệp")
        self.assertFalse(Employee.objects.filter(school=self.school, code="NEW").exists())
        self.assertContains(import_book(["NEW", "NEW2"]), "Đã lưu hồ sơ nhân sự.")
        self.assertEqual(Employee.objects.filter(school=self.school, code__startswith="NEW").count(), 2)
        self.assertEqual(self.client.get(reverse("staff_template")).status_code, 200)

    def test_generation_respects_leave_hard_gap_locked_day_and_approval(self):
        month = date(2026, 6, 1)
        self.kind.min_gap = 5
        self.kind.pair_rest = 3
        self.kind.save()
        Request.objects.create(employee=self.teacher.employee, start=month, end=month + timedelta(days=10),
                               reason="Thử nghiệm", status=Decision.APPROVED)
        self.sign(self.office)
        self.follow("rosters", {"month": "2026-06", "duty_type": self.kind.pk})
        roster = Roster.objects.get(month=month)
        locked = DutyDay.objects.get(roster=roster, date=month)
        self.follow("roster", {"action": "assign", "day": locked.pk, "employees": [person.pk for person in self.others[:2]]},
                    roster.pk)
        self.follow("roster", {"action": "toggle_day", "day": locked.pk}, roster.pk)
        self.assertContains(self.follow("roster", {"action": "generate"}, roster.pk), "Đã lưu lịch trực.")
        self.assertEqual(Slot.objects.filter(day__roster=roster).count(), 2 * monthrange(2026, 6)[1])
        self.assertEqual(set(locked.slots.values_list("assigned_id", flat=True)), {person.pk for person in self.others[:2]})
        self.assertFalse(Slot.objects.filter(day__roster=roster, assigned=self.teacher.employee,
                                             day__date__lte=month + timedelta(days=10)).exists())
        result = {day.date: list(day.slots.values_list("assigned_id", flat=True)) for day in roster.dutyday_set.all()}
        self.assertEqual(Rules(roster).validate(result), [])
        self.follow("roster", {"action": "submit"}, roster.pk)
        self.assertEqual(self.client.post(reverse("roster", args=[roster.pk]), {"action": "approve"}).status_code, 403)
        self.sign(self.deputy)
        self.assertEqual(self.client.post(reverse("roster", args=[roster.pk]), {"action": "approve"}).status_code, 403)
        Membership.objects.filter(pk=self.deputy.pk).update(approve_duty=True)
        self.follow("roster", {"action": "approve"}, roster.pk)
        self.follow("roster", {"action": "publish"}, roster.pk)
        roster.refresh_from_db()
        self.assertEqual(roster.state, State.PUBLISHED)

    def test_impossible_generation_leaves_draft_intact(self):
        month = date(2026, 9, 1)
        roster = self.roster(month)
        DutyDay.objects.bulk_create([DutyDay(roster=roster, date=month + timedelta(days=i)) for i in range(30)])
        slot = self.slot(roster, month, self.teacher.employee)
        Employee.objects.filter(school=self.school).exclude(pk__in=[self.teacher.employee_id, self.office.employee_id]).update(active=False)
        Request.objects.create(employee=self.office.employee, start=month, end=month + timedelta(days=29),
                               reason="Thử nghiệm", status=Decision.APPROVED)
        self.sign(self.teacher)
        self.assertEqual(self.client.get(reverse("roster", args=[roster.pk])).status_code, 403)
        self.sign(self.office)
        self.assertContains(self.follow("roster", {"action": "generate"}, roster.pk), "Thiếu nhân sự khả dụng")
        self.assertTrue(Slot.objects.filter(pk=slot.pk).exists())

    def test_substitution_attendance_snapshots_and_month_lock(self):
        today = localdate()
        month = today.replace(day=1)
        month_str = month.strftime("%Y-%m")
        roster = self.roster(month, state=State.PUBLISHED)
        slot = self.slot(roster, today, self.teacher.employee)
        self.sign(self.teacher)
        self.assertContains(self.follow("requests", {"action": "swap", "slot": slot.pk, "reason": "Báo bận"}), "Đã gửi")
        swap = Request.objects.get(slot=slot)
        self.sign(self.principal)
        response = self.follow("requests", {"action": "accept", "id": swap.pk, "replacement": self.foreign.employee_id})
        self.assertEqual(response.status_code, 404)
        self.follow("requests", {"action": "accept", "id": swap.pk, "replacement": self.others[0].pk})
        slot.refresh_from_db()
        self.assertEqual(slot.original_id, self.teacher.employee_id)
        self.assertEqual(slot.assigned_id, self.others[0].pk)
        self.assertContains(self.follow("attendance", {"month": month_str, "action": "lock"}), "xác nhận tất cả ca")
        data = {"month": month_str, "action": "confirm", "slot": slot.pk, "result": Result.DONE}
        self.assertContains(self.follow("attendance", data), "Chưa có hệ số")
        Holiday.objects.create(school=self.school, date=today, label="Lễ kiểm thử")
        rate = Rate.objects.create(duty_type=self.kind, kind=DayKind.HOLIDAY, effective=today - timedelta(days=1),
                                   units=Decimal("1.75"))
        self.follow("attendance", data)
        attendance = Attendance.objects.get(slot=slot)
        self.assertEqual((attendance.actual_id, attendance.result, attendance.rate_id, attendance.units),
                         (self.others[0].pk, Result.SUBSTITUTE, rate.pk, Decimal("1.75")))
        Rate.objects.create(duty_type=self.kind, kind=DayKind.HOLIDAY, effective=today, units=Decimal("2.25"))
        self.follow("attendance", data)
        attendance.refresh_from_db()
        self.assertEqual(attendance.units, Decimal("1.75"))
        self.follow("attendance", {"month": month_str, "action": "lock"})
        self.assertTrue(MonthClose.objects.get(month=month).locked)
        self.sign(self.office)
        self.assertContains(self.follow("attendance", {**data, "result": Result.ABSENT}), "đã khóa")
        self.sign(self.principal)
        self.follow("attendance", {"month": month_str, "action": "unlock", "reason": "Đối chiếu sổ trực"})
        self.assertFalse(MonthClose.objects.get(month=month).locked)
        self.assertTrue(Audit.objects.filter(action="attendance.unlock", detail__contains="Đối chiếu").exists())
        self.sign(self.teacher)
        personal = self.client.get(reverse("attendance"), {"month": month_str})
        self.assertNotContains(personal, self.others[0].name)
        self.assertEqual(self.client.post(reverse("attendance"), data).status_code, 403)

    def test_report_formats_have_correct_scope_and_real_values(self):
        today = localdate()
        month = today.replace(day=1)
        roster = self.roster(month, state=State.PUBLISHED)
        slot = self.slot(roster, today, self.teacher.employee)
        Attendance.objects.create(slot=slot, actual=self.teacher.employee, result=Result.DONE, kind=DayKind.WEEKDAY,
                                  units=Decimal("1.5"), confirmed_by=self.principal)
        self.slot(roster, today, self.others[0])
        self.sign(self.teacher)

        def export(fmt):
            response = self.client.get(reverse("attendance_export", args=[fmt]), {"month": month.strftime("%Y-%m")})
            self.assertEqual(response.status_code, 200)
            return b"".join(response.streaming_content)

        pdf = export("pdf")
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertRegex(pdf, rb"/MediaBox \[ 0 0 841\.88\d+ 595\.27\d+ \]")
        book = load_workbook(BytesIO(export("xlsx")))
        self.assertEqual(book.active["A5"].value, self.teacher.employee.name)
        self.assertEqual(book.active["L5"].value, 1.5)
        self.assertNotIn(self.others[0].name, [cell.value for cell in book.active["A"]])
        doc = Document(BytesIO(export("docx")))
        self.assertEqual(len(doc.tables[0].rows), 2)
        self.assertEqual(doc.tables[0].cell(1, 0).text, self.teacher.employee.name)
        self.assertEqual(doc.tables[1].cell(0, 2).text.strip(), "Hiệu trưởng")

    def test_cross_month_report_sums_attendance_and_rejects_long_ranges(self):
        for month in (date(2026, 1, 1), date(2026, 2, 1)):
            roster = self.roster(month, state=State.PUBLISHED)
            slot = self.slot(roster, month, self.teacher.employee)
            Attendance.objects.create(slot=slot, actual=self.teacher.employee, result=Result.DONE, kind=DayKind.WEEKDAY,
                                      units=Decimal("1.5"), confirmed_by=self.principal)
        self.sign(self.principal)
        params = {"month": "2026-01", "end": "2026-02"}
        result = self.client.get(reverse("attendance"), params)
        self.assertEqual(result.context["totals"], (1, 2, 0, Decimal("3")))
        self.assertTrue(result.context["multiple"])
        self.assertNotContains(result, "Duyệt và khóa tháng")
        self.assertEqual(self.client.get(reverse("attendance"), {"month": "2026-01", "end": "2027-01"}).status_code, 400)
        response = self.client.get(reverse("attendance_export", args=["xlsx"]), params)
        workbook = load_workbook(BytesIO(b"".join(response.streaming_content)))
        self.assertEqual(workbook.active["L5"].value, 3)

    def test_timetable_checks_teacher_room_class_and_private_views(self):
        monday = date(2026, 11, 2)
        self.sign(self.office)
        self.follow("timetable", {"monday": monday.isoformat(), "academic_year": "2026–2027"})
        week = TimetableWeek.objects.get(monday=monday)
        values = {"action": "add", "date": monday.isoformat(), "period": 1, "classroom": "6a",
                  "teacher": self.teacher.employee_id, "subject": "Toán", "room": "P1"}
        self.assertContains(self.follow("week", values, week.pk), "Đã lưu thời khóa biểu.")
        self.assertContains(self.follow("week", {**values, "classroom": "6b", "room": "P2"}, week.pk), "Trùng giáo viên")
        self.assertContains(self.follow("week", {**values, "teacher": self.others[0].pk, "room": "P2"}, week.pk), "Trùng lớp")
        self.assertContains(self.follow("week", {**values, "teacher": self.others[0].pk, "classroom": "6b"}, week.pk), "Trùng phòng")
        self.follow("week", {**values, "teacher": self.others[0].pk, "classroom": "6b", "room": "P2", "subject": "Vật lý"}, week.pk)
        self.follow("week", {"action": "submit"}, week.pk)
        self.sign(self.principal)
        self.follow("week", {"action": "approve"}, week.pk)
        self.follow("week", {"action": "publish"}, week.pk)
        self.sign(self.teacher)
        self.assertContains(self.client.get(reverse("week", args=[week.pk])), "Toán")
        self.assertNotContains(self.client.get(reverse("week", args=[week.pk])), "Vật lý")
        self.assertEqual(Lesson.objects.filter(week=week).count(), 2)

    @skipUnless(connection.vendor == "postgresql", "Kiểm tra trigger trên PostgreSQL")
    def test_postgres_rejects_cross_tenant_assignment(self):
        today = localdate()
        roster = self.roster(today.replace(day=1))
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.slot(roster, today, self.teacher.employee, self.foreign.employee)
