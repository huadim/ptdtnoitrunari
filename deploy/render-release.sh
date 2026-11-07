#!/bin/sh
set -eu

python manage.py migrate --noinput
python manage.py create_school --if-missing \
    --slug na-ri --name "Trường PTDT Nội trú Na Rì" --location "Na Rì" \
    --username hieutruong.nari --principal "Hiệu trưởng Na Rì"
