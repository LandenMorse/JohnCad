import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import bambuddy
from .dispatch import APPLIED, CONFLICT, apply_event, find_job_for_webhook


@csrf_exempt
@require_POST
def bambuddy_webhook(request):
    """Print status from Bambuddy's outgoing webhook.

    Meant to be reached only from Bambuddy on the same host: the public nginx
    config doesn't proxy /api/. Authenticated by BAMBUDDY_WEBHOOK_SECRET.
    """
    if not bambuddy.webhook_secret_ok(request):
        return JsonResponse({'error': 'Bad or missing webhook secret.'}, status=403)
    try:
        payload = json.loads(request.body)
    except ValueError:
        return JsonResponse({'error': 'Body is not JSON.'}, status=400)

    parsed = bambuddy.parse_webhook(payload)
    # 200 for events JohnCad doesn't track (progress, offline, prints started
    # outside JohnCad) so Bambuddy doesn't retry them.
    if parsed is None:
        return JsonResponse({'status': 'ignored'})
    job = find_job_for_webhook(parsed['job_id'], parsed['archive_id'])
    if job is None:
        return JsonResponse({'status': 'ignored'})

    result = apply_event(job, parsed['event'], parsed['detail'])
    if result == CONFLICT:
        return JsonResponse(
            {'error': f'Job is "{job.status}", can\'t apply "{parsed["event"]}".'},
            status=409,
        )
    return JsonResponse({'status': 'ok' if result == APPLIED else 'unchanged', 'job': job.id})
