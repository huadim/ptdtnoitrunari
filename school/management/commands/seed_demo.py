from secrets import token_urlsafe

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from school.models import DutyType, Employee, Membership, Role, School


class Command(BaseCommand):
    help = "Tạo dữ liệu thử nghiệm hư cấu ở chế độ DEBUG."

    @transaction.atomic
    def handle(self, **options):
        if not settings.DEBUG:
            raise CommandError("Chỉ chạy dữ liệu thử nghiệm với DJANGO_DEBUG=1.")
        if School.objects.filter(slug="truong-thu-nghiem").exists():
            raise CommandError("Trường thử nghiệm đã tồn tại.")
        school = School.objects.create(name="Trường Thực hành EDUSCHOOL", slug="truong-thu-nghiem", location="Na Rì")
        DutyType.objects.create(school=school, name="Trực nội trú")
        for i in range(1, 18):
            employee = Employee.objects.create(school=school, code=f"TN{i:02}", name=f"Nhân sự thử nghiệm {i:02}",
                                               department="Tổ A" if i % 2 else "Tổ B")
            if i in (1, 2, 3):
                role = {1: Role.PRINCIPAL, 2: Role.OFFICE, 3: Role.TEACHER}[i]
                username = f"demo-{role}"
                password = token_urlsafe(18)
                user = User.objects.create_user(username=username, password=password)
                Membership.objects.create(employee=employee, user=user, role=role)
                self.stdout.write(f"{role.label}: {username} / {password}")
