import os

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Job
from .services import finish_job


@api_view(['GET'])
def queue(request):
    jobs = Job.objects.filter(status=Job.Status.QUEUED).order_by('approved_at')
    data = [
        {
            'id': job.id,
            'attachment_name': job.attachment.name,
            'username': job.user.username,
            'gcode_download_url': request.build_absolute_uri(
                f'/api/jobs/{job.id}/gcode/'
            ),
        }
        for job in jobs
    ]
    return Response(data)


@api_view(['GET'])
def download_gcode(request, pk):
    job = get_object_or_404(Job, pk=pk)
    if job.status not in (Job.Status.QUEUED, Job.Status.WORKING):
        return Response({'error': f'Job is "{job.status}", G-code only served while queued/working.'}, status=403)
    if not job.gcode:
        return Response({'error': 'Job has no G-code.'}, status=404)
    return FileResponse(
        job.gcode.open('rb'),
        as_attachment=True,
        filename=os.path.basename(job.gcode.name),
    )


@api_view(['POST'])
def mark_working(request, pk):
    job = get_object_or_404(Job, pk=pk)
    if job.status != Job.Status.QUEUED:
        return Response({'error': f'Job is "{job.status}", expected "queued".'}, status=409)
    job.status = Job.Status.WORKING
    job.working_at = timezone.now()
    job.save(update_fields=['status', 'working_at'])
    return Response({'status': 'ok'})


@api_view(['POST'])
def mark_finished(request, pk):
    job = get_object_or_404(Job, pk=pk)
    if not finish_job(job):
        job.refresh_from_db()
        return Response({'error': f'Job is "{job.status}", expected "working".'}, status=409)
    return Response({'status': 'ok'})
