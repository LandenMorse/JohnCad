from django.db import connection
from django.http import JsonResponse


def healthz(request):
    """Is the site up and can it reach the database?

    Used by scripts/update.sh after a restart, and suitable for an uptime
    monitor. Returns no details beyond ok/not ok.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception:
        return JsonResponse({'status': 'error', 'database': 'unreachable'}, status=503)
    return JsonResponse({'status': 'ok'})
