from dataclasses import replace
from datetime import date, timedelta
from io import BytesIO
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import Workbook

from .models import Employee, Holiday, Lesson, Membership, Role, School, State, TeachingRequirement, TimetablePlan, TimetableWeek
from .timetable_planning import EXCEL_HEADERS
from .timetable_solver import Entry, Problem, Requirement, solve, validate


class SolverTests(SimpleTestCase):
    def problem(self, items, **kwargs):
        defaults = dict(days=(0, 1), morning=2, afternoon=2, requirements=tuple(items),
                        fixed=(), unavailable=frozenset(), holidays=frozenset(), inactive=frozenset())
        return Problem(**(defaults | kwargs))

    def test_solution_keeps_fixed_entries_and_uses_legal_same_session_pairs(self):
        items = (
            Requirement(1, "Giáo viên A", "7A", "Toán", "P01", 3, 1, "am"),
            Requirement(2, "Giáo viên B", "7B", "Văn", "P01", 2, 1, "pm"),
            Requirement(1, "Giáo viên A", "7B", "Tin", "P02", 1),
        )
        fixed = (Entry(1, "7A", "Toán", "P01", 0, 1),)
        problem = self.problem(items, fixed=fixed, unavailable=frozenset({(1, 1, 1)}))
        result = solve(problem)
        self.assertEqual(result.status, "OPTIMAL")
        self.assertEqual(result.day_spread_cost, 0)
        self.assertEqual(len(result.entries), 5)
        self.assertEqual(validate(problem, (*fixed, *result.entries), complete=True), [])
        self.assertIn(Entry(1, "7A", "Toán", "P01", 0, 2), result.entries)

    def test_double_cannot_cross_lunch_even_when_two_individual_slots_exist(self):
        problem = self.problem((Requirement(1, "A", "8A", "Văn", "P01", 2, 1),),
                               days=(0,), morning=1, afternoon=1)
        result = solve(problem)
        self.assertEqual(result.status, "INFEASIBLE")
        self.assertIn("H11", " ".join(result.messages))

    def test_resource_capacity_and_technical_timeout_are_distinct(self):
        items = (Requirement(1, "A", "7A", "Toán", "P01", 2),
                 Requirement(2, "B", "7B", "Văn", "P01", 2))
        problem = self.problem(items, days=(0,), afternoon=0)
        result = solve(problem)
        self.assertEqual(result.status, "INFEASIBLE")
        self.assertIn("Phòng p01", " ".join(result.messages))
        deadline = solve(replace(problem, days=(0, 1, 2)), seconds=0)
        self.assertEqual(deadline.status, "UNKNOWN")

    def test_independent_validation_detects_duplicate_resources(self):
        first = Requirement(1, "A", "7A", "Toán", "P01", 1)
        second = Requirement(1, "A", "7B", "Văn", "P01", 1)
        problem = self.problem((first, second))
        errors = validate(problem, (Entry(1, "7A", "Toán", "P01", 0, 1),
                                    Entry(1, "7B", "Văn", "P01", 0, 1)), complete=True)
        self.assertIn("trùng giáo viên", " ".join(errors))
        self.assertIn("trùng phòng", " ".join(errors))


class TeachingPlanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="Trường kiểm thử", slug="test-timetable")
        cls.foreign = School.objects.create(name="Trường khác", slug="test-other")
        cls.monday = date(2026, 11, 9)
        cls.week = TimetableWeek.objects.create(school=cls.school, monday=cls.monday, academic_year="2026–2027")
        cls.other_week = TimetableWeek.objects.create(school=cls.foreign, monday=cls.monday, academic_year="2026–2027")
        cls.office = cls.make_user(cls.school, "OFFICE", Role.OFFICE)
        cls.principal = cls.make_user(cls.school, "PRINCIPAL", Role.PRINCIPAL)
        cls.teacher = cls.make_user(cls.school, "GV01", Role.TEACHER)
        cls.other_teacher = cls.make_user(cls.foreign, "GV02", Role.TEACHER)
        Employee.objects.create(school=cls.school, code="GV03", name=cls.teacher.name)

    @classmethod
    def make_user(cls, school, code, role):
        employee = Employee.objects.create(school=school, code=code, name=f"Nhân sự {code}")
        user = User.objects.create_user(username=code)
        Membership.objects.create(user=user, employee=employee, role=role)
        return employee

    def login(self, employee):
        self.client.force_login(employee.membership.user)

    def setUp(self):
        self.login(self.office)
        self.plan_url = reverse("timetable_plan", args=[self.week.pk])
        self.week_url = reverse("week", args=[self.week.pk])

    def workbook(self, *rows):
        book = Workbook()
        book.active.append(EXCEL_HEADERS)
        for row in rows:
            book.active.append(row)
        output = BytesIO()
        book.save(output)
        return SimpleUploadedFile("phan-cong.xlsx", output.getvalue())

    def setup_plan(self):
        response = self.client.post(self.plan_url, {"action": "settings", "days": ["0", "1", "2"],
                                                   "morning": 2, "afternoon": 1})
        self.assertRedirects(response, self.plan_url)

    def test_excel_review_solve_publish_and_teacher_scope(self):
        self.assertContains(self.client.get(self.week_url), "Phân công và tự động xếp lịch")
        self.setup_plan()
        preview = self.client.post(self.plan_url, {"action": "preview_import", "workbook": self.workbook(
            ("GV01", "7A", "Toán", "P1", 2, 1, "Sáng"),
        )})
        self.assertContains(preview, "Xác nhận nhập 1 dòng")
        self.assertFalse(TeachingRequirement.objects.exists())
        response = self.client.post(self.plan_url, {"action": "apply_import", "token": preview.context["import_token"]})
        self.assertRedirects(response, self.plan_url)
        self.assertEqual(TeachingRequirement.objects.get().teacher, self.teacher)
        self.client.post(self.plan_url, {"action": "block", "teacher": self.teacher.pk, "date": self.monday.isoformat(),
                                         "first": 1, "last": 15})
        Holiday.objects.create(school=self.school, date=self.monday + timedelta(days=2), label="Nghỉ")
        proposed = self.client.post(self.plan_url, {"action": "solve"})
        self.assertContains(proposed, "OPTIMAL")
        self.assertContains(proposed, "Xác nhận đưa vào bản nháp")
        self.assertFalse(Lesson.objects.exists())
        self.assertRedirects(self.client.post(self.plan_url, {"action": "apply_solve",
                                                             "token": proposed.context["solve_token"]}), self.week_url)
        self.assertEqual(list(Lesson.objects.values_list("date", "period")),
                         [(self.monday + timedelta(days=1), 1), (self.monday + timedelta(days=1), 2)])
        diagnostics = self.client.get(reverse("timetable_diagnostics", args=[self.week.pk])).json()
        self.assertEqual((diagnostics["placed_periods"], diagnostics["issues"]), (2, []))
        self.client.post(self.week_url, {"action": "submit"})
        self.login(self.principal)
        self.client.post(self.week_url, {"action": "approve"})
        self.client.post(self.week_url, {"action": "publish"})
        self.week.refresh_from_db()
        self.assertEqual(self.week.state, State.PUBLISHED)
        self.assertTrue(self.teacher.notification_set.filter(text__contains="thời khóa biểu").exists())
        self.login(self.teacher)
        self.assertContains(self.client.get(self.week_url), "TOÁN")
        self.assertEqual(self.client.get(self.plan_url).status_code, 403)

    def test_rejects_wrong_school_codes_duplicate_rows_and_stale_proposals(self):
        self.setup_plan()
        bad = self.client.post(self.plan_url, {"action": "preview_import", "workbook": self.workbook(
            ("GV02", "7A", "Toán", "P1", 2, 1, "Sáng"),
        )})
        self.assertContains(bad, "Dòng 2: mã giáo viên GV02")
        row = ("GV01", "7A", "Toán", "P1", 2, 1, "Sáng")
        duplicate = self.client.post(self.plan_url, {"action": "preview_import", "workbook": self.workbook(row, row)})
        self.assertContains(duplicate, "Dòng 3: trùng phân công")
        self.assertFalse(TeachingRequirement.objects.exists())
        self.assertEqual(self.client.get(reverse("timetable_plan", args=[self.other_week.pk])).status_code, 404)
        self.client.post(self.plan_url, {"action": "add", "teacher": self.teacher.pk, "classroom": "7A", "subject": "Toán",
                                         "room": "P1", "periods": 2, "doubles": 1, "session": "am"})
        proposed = self.client.post(self.plan_url, {"action": "solve"})
        Holiday.objects.create(school=self.school, date=self.monday, label="Mới cập nhật")
        stale = self.client.post(self.plan_url, {"action": "apply_solve", "token": proposed.context["solve_token"]})
        self.assertContains(stale, "Dữ liệu tuần đã thay đổi")
        self.assertFalse(Lesson.objects.exists())

    def test_managed_week_rejects_missing_periods_and_stale_manual_form(self):
        self.setup_plan()
        self.client.post(self.plan_url, {"action": "add", "teacher": self.teacher.pk, "classroom": "7A", "subject": "Toán",
                                         "room": "P1", "periods": 2, "doubles": 1, "session": "am"})
        data = {"action": "add", "teacher": self.teacher.pk, "classroom": "7A", "subject": "Toán",
                "room": "P1", "date": self.monday, "period": 1}
        self.assertContains(self.client.post(self.week_url, {**data, "revision": 0}), "người khác cập nhật")
        self.client.post(self.week_url, data)
        result = self.client.post(self.week_url, {"action": "submit"}, follow=True)
        self.assertContains(result, "thiếu 1 tiết")
        self.week.refresh_from_db()
        self.assertEqual(self.week.state, State.DRAFT)

    def test_board_resource_scope_and_move_preview_without_writing(self):
        self.setup_plan()
        self.client.post(self.plan_url, {"action": "add", "teacher": self.teacher.pk, "classroom": "8A", "subject": "Toán",
                                         "room": "P1", "periods": 2, "doubles": 0, "session": "am"})
        self.client.post(self.plan_url, {"action": "add", "teacher": self.teacher.pk, "classroom": "8B", "subject": "Văn",
                                         "room": "P2", "periods": 1, "doubles": 0, "session": "am"})
        item = Lesson.objects.create(week=self.week, teacher=self.teacher, classroom="8A", subject="TOÁN",
                                     room="P1", date=self.monday, period=1)
        other = Lesson.objects.create(week=self.week, teacher=self.teacher, classroom="8B", subject="VĂN",
                                      room="P2", date=self.monday, period=2)
        board = self.client.get(self.week_url, {"scope": "class", "class": "8A", "focus": item.pk})
        self.assertEqual(board.context["planned_count"], 2)
        self.assertEqual(board.context["missing_count"], 1)
        self.assertEqual([entry for entry in board.context["related"][1][2]], [item, other])
        self.assertContains(board, "Lịch liên quan đến tiết đang chọn")

        preview = self.client.get(self.week_url, {"edit": item.pk, "target_day": self.monday, "target_period": 2})
        self.assertContains(preview, "Vị trí đề xuất có xung đột")
        self.assertContains(preview, "H03")
        item.refresh_from_db()
        self.assertEqual(item.period, 1)
        self.assertEqual(preview.context["form"].initial["period"], 2)

        self.week.state = State.PUBLISHED
        self.week.save(update_fields=["state"])
        unseen = Lesson.objects.create(week=self.week, teacher=self.office, classroom="8A", subject="Nội dung riêng",
                                       room="P1", date=self.monday, period=3)
        self.login(self.teacher)
        public = self.client.get(self.week_url, {"scope": "class", "class": "8A", "focus": item.pk})
        self.assertNotContains(public, unseen.subject)
        self.assertNotContains(public, "data-drag-lesson")
        self.assertIsNone(self.client.get(self.week_url, {"focus": unseen.pk}).context["selected_lesson"])

    @skipUnless(connection.vendor == "postgresql", "Kiểm tra trigger trên PostgreSQL")
    def test_database_rejects_a_teacher_from_another_school(self):
        plan = TimetablePlan.objects.create(week=self.week)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TeachingRequirement.objects.create(plan=plan, teacher=self.other_teacher, classroom="7A",
                                               subject="TOÁN", room="P1", periods=2)
