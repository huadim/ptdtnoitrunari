#!/bin/sh
set -eu

python manage.py migrate --noinput
if [ "${TIMETABLE_ONLY:-}" = "1" ]; then
    exec python manage.py create_school --if-missing \
        --slug timetable --name "${TIMETABLE_SCHOOL_NAME:-Thời khóa biểu}" --location "${TIMETABLE_LOCATION:-Việt Nam}" \
        --username "${TIMETABLE_ADMIN_USERNAME:-quantri.tkb}" --principal "${TIMETABLE_ADMIN_NAME:-Quản trị thời khóa biểu}"
fi

python manage.py create_school --if-missing \
    --slug na-ri --name "Trường PTDT Nội trú Na Rì" --location "Na Rì" \
    --username "${EDUSCHOOL_PRINCIPAL_USERNAME:-hieutruong.nari}" --principal "Hiệu trưởng Na Rì"
