import os
from hashlib import sha512
from pathlib import Path
from urllib.parse import unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DJANGO_DEBUG") == "1"
TIMETABLE_ONLY = os.getenv("TIMETABLE_ONLY") == "1"
secret_prefix = "TIMETABLE" if TIMETABLE_ONLY else "DJANGO"
SECRET_KEY = os.getenv(f"{secret_prefix}_SECRET_KEY", "")
secret_seed = os.getenv(f"{secret_prefix}_SECRET_SEED", "")
if not SECRET_KEY and len(secret_seed) >= 43:
    SECRET_KEY = sha512(secret_seed.encode()).hexdigest()
if DEBUG and not SECRET_KEY:
    SECRET_KEY = "local-development-only"
if not DEBUG and len(SECRET_KEY) < 50:
    raise ImproperlyConfigured(f"{secret_prefix}_SECRET_KEY phải có ít nhất 50 ký tự ngẫu nhiên.")
default_hosts = os.getenv("RENDER_EXTERNAL_HOSTNAME") or "localhost,127.0.0.1"
ALLOWED_HOSTS = [host.strip() for host in os.getenv("DJANGO_ALLOWED_HOSTS", default_hosts).split(",") if host.strip()]
default_origin = os.getenv("RENDER_EXTERNAL_URL", "")
CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in os.getenv("DJANGO_TRUSTED_ORIGINS", default_origin).split(",") if origin.strip()
]
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles", "school",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "eduschool.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages", "school.common.timetable_context",
    ]},
}]
WSGI_APPLICATION = "eduschool.wsgi.application"


def postgres_settings():
    variable = "TIMETABLE_DATABASE_URL" if TIMETABLE_ONLY else "DATABASE_URL"
    url = os.getenv(variable)
    if url:
        try:
            database = urlsplit(url)
            if database.scheme not in {"postgres", "postgresql"} or not database.hostname or not database.path.strip("/"):
                raise ValueError
            return {
                "NAME": unquote(database.path[1:]),
                "USER": unquote(database.username or ""),
                "PASSWORD": unquote(database.password or ""),
                "HOST": database.hostname,
                "PORT": str(database.port or 5432),
            }
        except ValueError:
            raise ImproperlyConfigured(f"{variable} phải là địa chỉ PostgreSQL hợp lệ.") from None
    if TIMETABLE_ONLY:
        return {}
    if os.getenv("POSTGRES_HOST"):
        return {
            "NAME": os.getenv("POSTGRES_DB", "eduschool"),
            "USER": os.getenv("POSTGRES_USER", "eduschool"),
            "PASSWORD": os.environ["POSTGRES_PASSWORD"],
            "HOST": os.environ["POSTGRES_HOST"],
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    return {}


postgres = postgres_settings()
if postgres:
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        **postgres,
        "OPTIONS": {"sslmode": os.getenv("POSTGRES_SSLMODE", "prefer")},
        "CONN_MAX_AGE": 60,
    }}
elif DEBUG:
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / ("timetable.sqlite3" if TIMETABLE_ONLY else "db.sqlite3"),
    }}
elif TIMETABLE_ONLY:
    raise ImproperlyConfigured("Ứng dụng Thời khóa biểu cần TIMETABLE_DATABASE_URL trỏ đến PostgreSQL riêng.")
else:
    raise ImproperlyConfigured("Môi trường vận hành yêu cầu PostgreSQL.")
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "vi"
TIME_ZONE = "Asia/Ho_Chi_Minh"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
if not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "timetable" if TIMETABLE_ONLY else "home"
LOGOUT_REDIRECT_URL = "login"
if TIMETABLE_ONLY:
    SESSION_COOKIE_NAME = "timetable_sessionid"
    CSRF_COOKIE_NAME = "timetable_csrftoken"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_AGE = 12 * 60 * 60
SECURE_SSL_REDIRECT = not DEBUG
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_REFERRER_POLICY = "same-origin"
if os.getenv("DJANGO_TRUST_PROXY") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
DATA_UPLOAD_MAX_MEMORY_SIZE = 1_048_576
DATA_UPLOAD_MAX_NUMBER_FIELDS = 600
PDF_FONT_DIR = os.getenv("PDF_FONT_DIR", "/usr/share/fonts/truetype/dejavu")
