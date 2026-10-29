import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME = Path(os.environ.get('HUILAN_DATA_DIR', BASE_DIR / 'runtime'))
RUNTIME.mkdir(parents=True, exist_ok=True)
SECRET_FILE = RUNTIME / 'secret.key'
if not SECRET_FILE.exists():
    SECRET_FILE.write_text(secrets.token_urlsafe(64), encoding='utf-8')
SECRET_KEY = SECRET_FILE.read_text(encoding='utf-8').strip()
DEBUG = False
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', 'testserver']
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'core']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.contrib.messages.middleware.MessageMiddleware']
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages', 'core.views.identity_context']}}]
WSGI_APPLICATION = 'config.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': RUNTIME / 'db.sqlite3', 'OPTIONS': {'timeout': 10}}}
AUTH_PASSWORD_VALIDATORS = [{'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 10}}]
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/assets/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = RUNTIME / 'static'
MEDIA_ROOT = RUNTIME / 'uploads'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
CRM_PROVIDER = os.environ.get('HUILAN_CRM_PROVIDER', '')
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/diagnosis/'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 8 * 60 * 60
CSRF_COOKIE_SAMESITE = 'Lax'
DATA_UPLOAD_MAX_MEMORY_SIZE = 22 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
LOGGING = {'version': 1, 'disable_existing_loggers': False, 'handlers': {'console': {'class': 'logging.StreamHandler'}}, 'loggers': {'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False}}}
