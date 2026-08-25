"""
WSGI config for lifteam project.
v2.76.0
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lifteam.settings')

application = get_wsgi_application()




