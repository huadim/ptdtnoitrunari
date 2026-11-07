#!/bin/sh
set -eu

if [ "${1:-}" = "--bootstrap" ]; then
    sh deploy/render-release.sh
fi

python manage.py collectstatic --noinput
exec gunicorn --bind "0.0.0.0:${PORT:-8000}" --workers "${WEB_CONCURRENCY:-2}" \
    --timeout 60 eduschool.wsgi:application
