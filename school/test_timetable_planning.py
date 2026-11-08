from dataclasses import replace
from datetime import date, timedelta
from io import BytesIO
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import Workbook

from .models import (Employee, Holiday, Lesson, Membership, Role, School, State, TeachingRequirement,
                     TeachingTeacherLimit, TimetablePlan, TimetableWeek)
from .timetable_planning import EXCEL_HEADERS
from .timetable_solver import Entry, Problem, Requirement, TeacherLimit, solve, validate


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

    def test_partial_solver_respects_double_periods_and_schedules_other_assignments(self):
        items = (Requirement(1, "A", "8A", "Văn", "P01", 2, 1),
                 Requirement(2, "B", "8B", "Toán", "P02", 1))
        problem = self.problem(items, days=(0,), morning=1, afternoon=1)
        result = solve(problem, allow_partial=True)
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual([entry.subject for entry in result.entries], ["Toán"])
        self.assertFalse(validate(problem, result.entries))

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

    def test_fixed_breaks_and_teacher_limits_apply_to_existing_and_new_entries(self):
        item = Requirement(1, "Giáo viên A", "6A", "Toán", "P1", 2)
        fixed = Entry(1, "6A", "Toán", "P1", 0, 1, pinned=True)
        problem = self.problem((item,), fixed=(fixed,), morning=3, afternoon=1,
                               teacher_limits=(TeacherLimit(1, "Giáo viên A", 1, 2, 1),),
                               resource_breaks=frozenset({("room", "p1", 0, 2)}))
        result = solve(problem)
        self.assertEqual(result.entries, (Entry(1, "6A", "Toán", "P1", 0, 4),))
        self.assertEqual(validate(problem, (*problem.fixed, *result.entries), complete=True), [])
        invalid = (*problem.fixed, Entry(1, "6A", "Toán", "P1", 0, 2))
        self.assertIn("H12", " ".join(validate(problem, invalid)))
        self.assertIn("H13", " ".join(validate(problem, invalid)))


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

    def test_timetable_entry_opens_full_screen_and_picker_keeps_week_permissions(self):
        previous = TimetableWeek.objects.create(school=self.school, monday=self.monday - timedelta(days=7),
                                                academic_year="2026–2027", state=State.PUBLISHED)
        published_url = reverse("week", args=[previous.pk])
        entry_url = reverse("timetable")
        with patch("school.timetable.localdate", return_value=self.monday):
            opened = self.client.get(entry_url, follow=True)
            self.assertRedirects(opened, self.week_url)
            self.assertContains(opened, "timetable-workbench-page")
            self.assertContains(opened, "data-timetable-workbench")
            self.assertNotContains(opened, 'class="sidebar"')
            self.assertNotContains(opened, 'id="chon-tuan"')
            self.assertContains(opened, f"{self.week_url}?weeks=1#chon-tuan")
            picker = self.client.get(f"{self.week_url}?weeks=1")
            self.assertContains(picker, 'id="chon-tuan"')
            self.assertContains(picker, f'href="{published_url}"')
            self.assertContains(picker, 'name="action" value="create_week"')
            invalid = self.client.post(self.week_url, {"action": "create_week", "monday": "2026-11-10",
                                                       "academic_year": "2026–2027"})
            self.assertContains(invalid, "Chọn ngày thứ Hai.")
            self.assertContains(invalid, "data-timetable-workbench")
            new = self.client.post(self.week_url, {"action": "create_week", "monday": "2026-11-16",
                                                   "academic_year": "2026–2027"})
            created = TimetableWeek.objects.get(school=self.school, monday=date(2026, 11, 16))
            self.assertRedirects(new, reverse("week", args=[created.pk]))
            self.login(self.teacher)
            self.assertRedirects(self.client.get(entry_url), published_url)
            picker = self.client.get(f"{published_url}?weeks=1")
            self.assertNotContains(picker, f'href="{self.week_url}"')
            self.assertNotContains(picker, 'name="action" value="create_week"')
            self.assertEqual(self.client.post(published_url, {"action": "create_week"}).status_code, 403)

    def test_partial_proposal_and_refinement_workflow(self):
        plan = TimetablePlan.objects.create(week=self.week, days="0", morning=2, afternoon=0)
        TeachingRequirement.objects.create(plan=plan, teacher=self.teacher, classroom="7A", subject="Toán",
                                           room="P01", periods=2)
        blocked = TeachingRequirement.objects.create(plan=plan, teacher=self.office, classroom="7B", subject="Tin",
                                                     room="P02", periods=1)
        pinned = Lesson.objects.create(week=self.week, teacher=self.teacher, classroom="7A", subject="Toán",
                                       room="P01", date=self.monday, period=1, pinned=True)
        unavailable = plan.unavailable.create(teacher=self.office, date=self.monday, first=1, last=2)
        preview = self.client.post(self.plan_url, {"action": "solve"})
        self.assertContains(preview, "Xếp được một phần")
        self.assertContains(preview, "Còn thiếu 1 tiết")
        self.assertEqual(self.week.lesson_set.count(), 1)
        applied = self.client.post(self.plan_url, {"action": "apply_solve", "token": preview.context["solve_token"]})
        self.assertRedirects(applied, self.week_url)
        self.assertEqual(self.week.lesson_set.count(), 2)
        self.assertTrue(self.week.lesson_set.filter(pk=pinned.pk, pinned=True, period=1).exists())
        self.client.post(self.week_url, {"action": "submit"})
        self.week.refresh_from_db()
        self.assertEqual(self.week.state, State.DRAFT)
        review = self.client.get(self.plan_url, {"review": blocked.pk, "review_teacher": self.office.pk})
        self.assertContains(review, "Giáo viên bận hoặc nghỉ")
        self.assertNotContains(review, 'class="sidebar"')
        self.assertEqual(review.context["review_slot_count"], 0)
        unavailable.delete()
        review = self.client.get(self.plan_url, {"review": blocked.pk})
        self.assertEqual(review.context["review_slot_count"], 2)
        editor = self.client.get(self.week_url, {"requirement": blocked.pk, "day": self.monday.isoformat(), "period": 1})
        self.assertContains(editor, "Đã điền lớp, môn, giáo viên và phòng")
        self.assertEqual(editor.context["form"].initial["teacher"], self.office.pk)
        self.assertEqual(editor.context["form"].initial["subject"], "Tin")
        saved = self.client.post(self.week_url, {"action": "add", "teacher": self.office.pk, "classroom": "7B",
                                                 "subject": "Tin", "room": "P02", "date": self.monday, "period": 1})
        self.assertRedirects(saved, self.week_url)
        self.assertContains(self.client.get(self.plan_url), "Đã xếp đủ số tiết và cặp tiết đôi")

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
        self.assertContains(self.client.get(self.week_url), "Bảng phân công giảng dạy")
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

    def test_shared_menu_and_reports_respect_week_and_teacher_scope(self):
        self.setup_plan()
        Lesson.objects.create(week=self.week, teacher=self.teacher, date=self.monday, period=1,
                              classroom="7A", subject="Toán", room="P1")
        Lesson.objects.create(week=self.week, teacher=self.office, date=self.monday, period=1,
                              classroom="7B", subject="Tin học", room="P2")
        Lesson.objects.create(week=self.week, teacher=self.teacher, date=self.monday, period=2,
                              classroom="7A", subject="Toán", room="P1")
        report_url = reverse("timetable_report", args=[self.week.pk, "class-session"])
        report = self.client.get(report_url)
        self.assertEqual(report.context["rows"], [("09/11/2026", "Sáng", 2)])
        self.assertEqual(self.client.post(report_url).status_code, 405)
        self.assertEqual(self.client.get(reverse("timetable_report", args=[self.other_week.pk, "overview"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("timetable_report", args=[self.week.pk, "unknown"])).status_code, 404)

        self.login(self.principal)
        account = self.client.get(reverse("account_detail", args=[self.teacher.pk]))
        self.assertContains(account, 'data-tkb-menu')
        self.assertContains(account, report_url)
        self.assertContains(account, "Phân quyền và ủy quyền")

        self.login(self.teacher)
        self.assertEqual(self.client.get(report_url).status_code, 403)
        self.week.state = State.PUBLISHED
        self.week.save(update_fields=["state"])
        self.assertEqual(self.client.get(report_url).context["rows"], [("09/11/2026", "Sáng", 1)])
        self.assertEqual(self.client.get(reverse("timetable_report", args=[self.week.pk, "assignments"])).status_code, 403)

    def test_optimization_requires_confirmation_and_keeps_pinned_lesson(self):
        self.setup_plan()
        plan = self.week.plan
        plan.morning = 4
        plan.save(update_fields=["morning"])
        TeachingRequirement.objects.create(plan=plan, teacher=self.teacher, classroom="6A",
                                           subject="Toán", room="P1", periods=2, session="am")
        fields = {"week": self.week, "teacher": self.teacher, "date": self.monday,
                  "classroom": "6A", "subject": "Toán", "room": "P1"}
        first = Lesson.objects.create(period=1, **fields)
        last = Lesson.objects.create(period=4, **fields)
        self.assertRedirects(self.client.post(self.week_url, {"action": "pin", "id": first.pk}), self.week_url)
        self.assertContains(self.client.get(self.week_url, {"edit": first.pk}), "Bỏ cố định")
        self.client.post(self.week_url, {"action": "remove", "id": first.pk})
        self.assertTrue(Lesson.objects.filter(pk=first.pk, pinned=True).exists())

        self.assertRedirects(self.client.post(self.plan_url, {
            "action": "teacher_limit", "teacher-limit-teacher": self.teacher.pk,
            "teacher-limit-days": 1, "teacher-limit-daily": 2,
        }), self.plan_url)
        self.assertEqual(TeachingTeacherLimit.objects.get().daily, 2)
        self.assertRedirects(self.client.post(self.plan_url, {
            "action": "resource_break", "break-target": "class:6A", "break-date": self.monday.isoformat(),
            "break-first": 4, "break-last": 4,
        }), self.plan_url)
        self.assertContains(self.client.get(self.plan_url), "Lớp học 6A")
        preview = self.client.post(self.plan_url, {"action": "optimize"})
        self.assertContains(preview, "Xác nhận áp dụng phương án tối ưu")
        self.assertEqual((preview.context["old_gaps"], preview.context["new_gaps"]), (2, 0))
        self.assertEqual(set(self.week.lesson_set.values_list("pk", flat=True)), {first.pk, last.pk})
        token = preview.context["solve_token"]
        self.assertContains(self.client.post(self.plan_url, {"action": "apply_solve", "token": token}),
                            "Bản xem trước không thuộc tài khoản hoặc tuần này.")
        self.assertRedirects(self.client.post(self.plan_url, {"action": "apply_optimize", "token": token}), self.week_url)
        self.assertEqual(list(self.week.lesson_set.values_list("period", "pinned")), [(1, True), (2, False)])
        self.assertTrue(Lesson.objects.filter(pk=first.pk, pinned=True).exists())

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

    def test_grid_plus_lists_compatible_assignments_and_saves_selected_one(self):
        plan = TimetablePlan.objects.create(week=self.week, days="0", morning=3, afternoon=1)
        available = TeachingRequirement.objects.create(plan=plan, teacher=self.teacher, classroom="7A",
                                                       subject="Toán", room="P1", periods=2, doubles=1)
        blocked = TeachingRequirement.objects.create(plan=plan, teacher=self.office, classroom="7B",
                                                     subject="Tin", room="P2", periods=1, session="pm")
        Lesson.objects.create(week=self.week, teacher=self.teacher, classroom="7A", subject="Toán",
                              room="P1", date=self.monday, period=1)
        start = self.client.get(self.week_url)
        self.assertContains(start, 'data-can-edit')
        self.assertContains(start, f'{self.week_url}?day={self.monday}&amp;period=2#lesson-editor')
        picker = self.client.get(self.week_url, {"day": self.monday, "period": 2, "class": "7A"})
        self.assertContains(picker, "Chọn phân công cho ô lịch")
        self.assertEqual(picker.context["slot_options"], [(available, 1)])
        no_slot = self.client.get(self.week_url, {"day": self.monday, "period": 3, "class": "7A"})
        self.assertContains(no_slot, "Vị trí này chưa hoàn thành đủ cặp tiết đôi")
        self.assertNotContains(no_slot, 'value="add_assignment"')
        payload = {"action": "add_assignment", "assignment": available.pk, "date": self.monday, "period": 3}
        rejected = self.client.post(self.week_url, payload)
        self.assertContains(rejected, "H11")
        self.assertContains(rejected, "Chọn phân công cho ô lịch")
        rejected = self.client.post(self.week_url, {**payload, "assignment": blocked.pk, "period": 2})
        self.assertContains(rejected, "H08/H09")
        self.assertEqual(self.week.lesson_set.count(), 1)
        saved = self.client.post(self.week_url, {**payload, "period": 2, "classroom": "Sai", "room": "Sai"})
        self.assertRedirects(saved, self.week_url)
        self.assertTrue(self.week.lesson_set.filter(period=2, teacher=self.teacher, classroom="7A", room="P1").exists())
        self.assertNotContains(self.client.get(self.week_url, {"day": self.monday, "period": 3, "class": "7A"}),
                               'value="add_assignment"')

    def test_pin_and_remove_menu_open_distinct_confirmation_actions(self):
        lesson = Lesson.objects.create(week=self.week, teacher=self.teacher, classroom="7A", subject="Toán",
                                       room="P1", date=self.monday, period=1)
        for manage, title, label, action in (
            ("pin", "Chọn tiết cần cố định", "Cố định hoặc bỏ cố định tiết học", "pin"),
            ("remove", "Chọn tiết cần xóa", "Xác nhận xóa tiết học", "remove"),
        ):
            menu_path = f"{self.week_url}?view=list&amp;manage={manage}#board-title"
            self.assertContains(self.client.get(self.week_url), menu_path)
            board = self.client.get(self.week_url, {"view": "list", "manage": manage})
            self.assertContains(board, title)
            response = self.client.get(self.week_url, {"view": "list", "manage": manage, "edit": lesson.pk})
            self.assertContains(response, label)
            self.assertContains(response, f'name="action" value="{action}"')
            self.assertTrue(self.week.lesson_set.filter(pk=lesson.pk, pinned=False).exists())

    @skipUnless(connection.vendor == "postgresql", "Kiểm tra trigger trên PostgreSQL")
    def test_database_rejects_a_teacher_from_another_school(self):
        plan = TimetablePlan.objects.create(week=self.week)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TeachingRequirement.objects.create(plan=plan, teacher=self.other_teacher, classroom="7A",
                                               subject="TOÁN", room="P1", periods=2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TeachingTeacherLimit.objects.create(plan=plan, teacher=self.other_teacher, days=4)
