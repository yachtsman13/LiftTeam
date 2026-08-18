"""
WSGI config for lifteam project.
v2.39.3
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lifteam.settings')

application = get_wsgi_application()




