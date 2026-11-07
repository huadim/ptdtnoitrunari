from django.contrib.auth import views as auth
from django.db import OperationalError, connection
from django.http import HttpResponse
from django.urls import path, reverse_lazy

from django.views.decorators.http import require_safe

from school import accounts, attendance, home, rosters, staff, timetable, timetable_planning
from school.access import school_view

@require_safe
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except OperationalError:
        return HttpResponse("unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("healthz/", health, name="health"),
    path("", home.home, name="home"),
    path("dang-ky/", accounts.register, name="register"),
    path("tai-khoan/", accounts.accounts, name="accounts"),
    path("tai-khoan/<int:pk>/", accounts.account_detail, name="account_detail"),
    path("dang-nhap/", home.SchoolLogin.as_view(), name="login"),
    path("dang-xuat/", auth.LogoutView.as_view(), name="logout"),
    path("doi-mat-khau/", school_view(auth.PasswordChangeView.as_view(template_name="school/password.html",
                                                         success_url=reverse_lazy("home"))), name="password"),
    path("nhan-su/", staff.staff, name="staff"),
    path("nhan-su/mau/", staff.staff_template, name="staff_template"),
    path("cau-hinh/", staff.configuration, name="configuration"),
    path("nhat-ky/", staff.audits, name="audits"),
    path("lich-truc/", rosters.rosters, name="rosters"),
    path("lich-truc/<int:pk>/", rosters.roster_detail, name="roster"),
    path("lich-truc/<int:pk>/xuat/<str:format>/", rosters.roster_export, name="roster_export"),
    path("de-nghi/", rosters.request_list, name="requests"),
    path("cham-cong/", attendance.attendance, name="attendance"),
    path("cham-cong/xuat/<str:format>/", attendance.attendance_export, name="attendance_export"),
    path("thoi-khoa-bieu/", timetable.timetable, name="timetable"),
    path("thoi-khoa-bieu/<int:pk>/", timetable.week_detail, name="week"),
    path("thoi-khoa-bieu/<int:pk>/ke-hoach/", timetable_planning.timetable_plan, name="timetable_plan"),
    path("thoi-khoa-bieu/<int:pk>/ke-hoach/mau/", timetable_planning.timetable_plan_template, name="timetable_plan_template"),
    path("thoi-khoa-bieu/<int:pk>/kiem-tra.json", timetable_planning.timetable_diagnostics, name="timetable_diagnostics"),
    path("thoi-khoa-bieu/<int:pk>/xuat/<str:format>/", timetable.timetable_export, name="timetable_export"),
    path("thong-bao/", home.notifications, name="notifications"),
]
