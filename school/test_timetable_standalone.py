from datetime import date
from io import BytesIO, StringIO
from secrets import token_urlsafe
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from openpyxl import Workbook

from .models import (DutyType, Employee, Membership, Notification, Role, School, State, TeachingGrade,
                     TeachingTeacherCategory, TimetableProfile, TimetableWeek)
from .timetable_catalog import GRADE_HEADERS
from .timetable_planning import EXCEL_HEADERS


@skipUnless(settings.TIMETABLE_ONLY, "Run with TIMETABLE_ONLY=1.")
class StandaloneTimetableTests(TestCase):
    def test_workspace_from_first_login_to_publication(self):
        password = token_urlsafe(24)
        setup = dict(slug="timetable", name="Trường thử nghiệm", location="Na Rì",
                     username="admin.tkb", principal="Quản trị viên TKB", if_missing=True)
        with patch.dict("os.environ", {"TIMETABLE_ADMIN_PASSWORD": password}):
            call_command("create_school", stdout=StringIO(), **setup)
        with patch.dict("os.environ", {"TIMETABLE_ADMIN_PASSWORD": ""}):
            call_command("create_school", stdout=StringIO(), **setup)
        self.assertFalse(DutyType.objects.exists())
        self.assertRedirects(self.client.get("/"), "/dang-nhap/?next=/")
        self.assertContains(self.client.get(reverse("login")), "Đăng nhập Thời khóa biểu")

        login = self.client.post(reverse("login"), {"username": setup["username"], "password": password})
        self.assertRedirects(login, reverse("timetable"))
        self.assertIn("timetable_sessionid", login.cookies)
        self.assertNotIn("sessionid", login.cookies)
        start = self.client.get("/")
        self.assertContains(start, "Chưa có tuần học")
        self.assertNotContains(start, "EDUSCHOOL")
        for path in ("/lich-truc/", "/cham-cong/", "/de-nghi/", "/thong-bao/", "/nhan-su/"):
            self.assertEqual(self.client.get(path).status_code, 404)

        staff_url = reverse("staff")
        self.assertEqual(staff_url, "/giao-vien/")
        staff = self.client.get(staff_url)
        self.assertContains(staff, "Danh sách giáo viên")
        self.assertNotContains(staff, "Chấm công")
        self.client.post(staff_url, {"action": "create", "code": "GV01", "name": "Giáo viên kiểm thử"})
        teacher = Employee.objects.get(code="GV01")
        monday = date(2026, 11, 9)
        self.client.post(reverse("configuration"), {"date": "2026-11-10", "label": "Nghỉ toàn trường"})
        self.client.post(reverse("timetable"), {"action": "create_week", "monday": monday.isoformat(),
                                               "academic_year": "2026–2027"})
        week = TimetableWeek.objects.get()
        board = reverse("week", args=[week.pk])
        plan = reverse("timetable_plan", args=[week.pk])
        self.client.post(plan, {"action": "settings", "days": ["0", "1"], "morning": 3, "afternoon": 0})
        book = Workbook()
        book.active.append(EXCEL_HEADERS)
        book.active.append((teacher.code, "6A", "Toán", "P1", 2, 0, "Sáng"))
        stream = BytesIO()
        book.save(stream)
        preview = self.client.post(plan, {"action": "preview_import",
                                         "workbook": SimpleUploadedFile("phan-cong.xlsx", stream.getvalue())})
        self.assertContains(preview, "Xác nhận nhập 1 dòng")
        self.assertRedirects(self.client.post(plan, {"action": "apply_import", "token": preview.context["import_token"]}),
                             plan)
        assignment = week.plan.requirements.get()

        with CaptureQueriesContext(connection) as queries:
            picker = self.client.get(board, {"day": monday.isoformat(), "period": 1})
            self.assertContains(picker, "Giáo viên kiểm thử")
            saved = self.client.post(board, {"action": "add_assignment", "assignment": assignment.pk,
                                            "date": monday.isoformat(), "period": 1})
            self.assertRedirects(saved, board)
            proposal = self.client.post(plan, {"action": "solve"})
            self.assertContains(proposal, "Xác nhận đưa vào bản nháp")
            self.assertRedirects(self.client.post(plan, {"action": "apply_solve", "token": proposal.context["solve_token"]}),
                                 board)
            for action in ("submit", "approve", "publish"):
                self.assertRedirects(self.client.post(board, {"action": action}), board)

        week.refresh_from_db()
        self.assertEqual(week.state, State.PUBLISHED)
        self.assertEqual(list(week.lesson_set.values_list("date", flat=True)), [monday, monday])
        self.assertFalse(Notification.objects.exists())
        for query in queries.captured_queries:
            self.assertNotIn("school_request", query["sql"])
        response = self.client.get(reverse("timetable_export", args=[week.pk, "pdf"]))
        self.assertEqual(b"".join(response.streaming_content)[:5], b"%PDF-")

    def test_general_info_grade_tools_and_teacher_submenu(self):
        school = School.objects.create(slug="timetable", name="Tên khởi tạo")
        admin = Employee.objects.create(school=school, code="QT", name="Giáo vụ")
        teacher = Employee.objects.create(school=school, code="GV01", name="Giáo viên A")
        user = User.objects.create_user(username="admin")
        Membership.objects.create(user=user, employee=admin, role=Role.PRINCIPAL)
        self.client.force_login(user)
        week = TimetableWeek.objects.create(school=school, monday=date(2026, 11, 9), academic_year="2026–2027")
        board = reverse("week", args=[week.pk])
        info = reverse("timetable_week_info", args=[week.pk])
        grades = reverse("timetable_grades")
        response = self.client.get(board)
        self.assertContains(response, f'href="{info}">Thông tin chung')
        self.assertContains(response, f'href="{grades}">Dữ liệu khối học')
        self.assertContains(response, 'data-tkb-flyout><summary>Dữ liệu giáo viên')
        self.assertContains(self.client.get(info), 'data-tkb-info')
        payload = {"name": "PTDTNT Na Rì", "address": "Na Rì", "phone": "0209",
                   "academic_year": "2026–2027", "semester": 2, "effective_date": "2026-11-10",
                   "schedule_number": 3, "revision": week.revision}
        self.assertRedirects(self.client.post(info, payload), board)
        self.assertContains(self.client.get(info), 'value="PTDTNT Na Rì"')
        week.refresh_from_db()
        self.assertEqual((week.semester, week.schedule_number), (2, 3))
        self.assertContains(self.client.post(info, payload | {"name": "Ghi cũ"}), "Tuần học vừa được cập nhật")
        self.assertEqual(TimetableProfile.objects.get().name, "PTDTNT Na Rì")

        self.assertContains(self.client.get(grades, {"mode": "add"}), "Lưu khối học")
        self.client.post(grades, {"action": "save", "name": "Khối 6", "code": "k6", "number": 6})
        grade = TeachingGrade.objects.get(code="K6")
        self.assertContains(self.client.post(grades, {"action": "edit", "grade": grade.pk}), "Lưu khối học")
        self.assertContains(self.client.post(grades, {"action": "save", "name": "Trùng", "code": "K6", "number": 6}),
                            "Mã khối học đã tồn tại")
        book = Workbook()
        book.active.append(GRADE_HEADERS)
        book.active.append(("Khối 6 cập nhật", "k6", 6, "THCS"))
        book.active.append(("Khối 7", "K7", 7, ""))
        stream = BytesIO()
        book.save(stream)
        self.assertRedirects(self.client.post(grades, {"action": "import",
                                                      "workbook": SimpleUploadedFile("khoi.xlsx", stream.getvalue())}), grades)
        grade.refresh_from_db()
        self.assertEqual(grade.name, "Khối 6 cập nhật")
        self.client.post(grades, {"action": "down", "grade": grade.pk})
        self.assertEqual(list(TeachingGrade.objects.values_list("code", flat=True)), ["K7", "K6"])
        self.client.post(grades, {"action": "number"})
        self.assertEqual(list(TeachingGrade.objects.values_list("code", flat=True)), ["K6", "K7"])
        self.assertContains(self.client.post(grades, {"action": "confirm_delete", "grade": grade.pk}), "Xác nhận xóa")
        self.assertEqual(TeachingGrade.objects.count(), 2)
        self.assertRedirects(self.client.post(grades, {"action": "delete", "grade": grade.pk}), grades)
        self.assertFalse(TeachingGrade.objects.filter(pk=grade.pk).exists())

        for kind, title in TeachingTeacherCategory.Kind.choices:
            category = reverse("timetable_teacher_categories", args=[kind])
            self.assertContains(response, f'href="{category}">{title}')
            self.assertContains(self.client.get(category), title)
            entry = {"action": "save", "teacher": teacher.pk, "label": "6A"}
            self.assertRedirects(self.client.post(category, entry), category)
            self.assertContains(self.client.post(category, entry), "Mục này đã có trong danh sách.")
        self.assertEqual(TeachingTeacherCategory.objects.count(), 4)
        foreign = School.objects.create(slug="foreign", name="Trường khác")
        outside = TeachingGrade.objects.create(school=foreign, name="Khối khác", code="K6", number=6)
        self.assertEqual(self.client.post(grades, {"action": "delete", "grade": outside.pk}).status_code, 404)
