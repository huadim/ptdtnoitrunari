import os
from getpass import getpass

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import UnicodeUsernameValidator, validate_slug
from django.db import IntegrityError, transaction

from school.models import DutyType, Employee, Membership, Role, School


class Command(BaseCommand):
    help = "Tạo một trường, hồ sơ và tài khoản hiệu trưởng. Nhập mật khẩu qua lời nhắc hoặc biến môi trường."

    def add_arguments(self, parser):
        parser.add_argument("--slug", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--location", required=True)
        parser.add_argument("--username", required=True)
        parser.add_argument("--principal", required=True)

    def handle(self, **options):
        username = options["username"].strip().casefold()
        slug = options["slug"].strip()
        name = options["name"].strip()
        location = options["location"].strip()
        principal = options["principal"].strip()
        for value, size in ((username, 150), (slug, 50), (name, 180), (location, 100), (principal, 120)):
            if not value or len(value) > size:
                raise CommandError("Một trường dữ liệu trống hoặc quá dài.")
        password = os.getenv("EDUSCHOOL_PRINCIPAL_PASSWORD")
        if not password:
            password = getpass("Mật khẩu hiệu trưởng: ")
            if password != getpass("Nhập lại mật khẩu: "):
                raise CommandError("Hai mật khẩu không khớp.")
        try:
            validate_slug(slug)
            UnicodeUsernameValidator()(username)
            validate_password(password, User(username=username))
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        try:
            with transaction.atomic():
                if School.objects.filter(slug=slug).exists() or User.objects.filter(username__iexact=username).exists():
                    raise CommandError("Mã trường hoặc tên đăng nhập đã tồn tại.")
                school = School.objects.create(slug=slug, name=name, location=location)
                employee = Employee.objects.create(school=school, code="HT", name=principal)
                user = User.objects.create_user(username=username, password=password)
                Membership.objects.create(user=user, employee=employee, role=Role.PRINCIPAL)
                DutyType.objects.create(school=school, name="Trực nội trú")
        except IntegrityError as exc:
            raise CommandError("Mã trường hoặc tên đăng nhập đã tồn tại.") from exc
        self.stdout.write(self.style.SUCCESS(f"Đã tạo {school.name}. Tên đăng nhập: {username}"))
