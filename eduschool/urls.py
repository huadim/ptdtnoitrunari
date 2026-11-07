from django.contrib.auth import views as auth
from django.urls import path, reverse_lazy

from school import attendance, home, rosters, staff, timetable

urlpatterns = [
    path("", home.home, name="home"),
    path("dang-nhap/", home.SchoolLogin.as_view(), name="login"),
    path("dang-xuat/", auth.LogoutView.as_view(), name="logout"),
    path("doi-mat-khau/", auth.PasswordChangeView.as_view(template_name="school/password.html",
                                                         success_url=reverse_lazy("home")), name="password"),
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
    path("thoi-khoa-bieu/<int:pk>/xuat/<str:format>/", timetable.timetable_export, name="timetable_export"),
    path("thong-bao/", home.notifications, name="notifications"),
]
