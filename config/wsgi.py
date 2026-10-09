"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_wsgi_application()

# Load the station table and place indexes and open the routing connection now, in the background,
# so the first search after a restart is not the slow one. (Skipped by management commands and tests,
# which never import this file. FUEL_WARM_ON_START=0 turns it off.)
from routeplanner.services.warmup import warm_in_background  # noqa: E402

warm_in_background()
