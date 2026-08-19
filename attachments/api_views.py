import os

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Attachment


@api_view(['GET'])
def queue(request):
    attachments = Attachment.objects.filter(ready_to_print=True, fetched_at__isnull=True)
    data = [
        {
            'id': attachment.id,
            'name': attachment.name,
            'user': attachment.user.username,
            'uploaded_at': attachment.uploaded_at,
            'download_url': request.build_absolute_uri(
                f'/api/attachments/{attachment.id}/download/'
            ),
        }
        for attachment in attachments
    ]
    return Response(data)


@api_view(['GET'])
def download(request, pk):
    attachment = get_object_or_404(Attachment, pk=pk)
    if attachment.user_id != request.user.id and not attachment.ready_to_print:
        return Response(status=403)
    return FileResponse(
        attachment.file.open('rb'),
        as_attachment=True,
        filename=os.path.basename(attachment.file.name),
    )


@api_view(['POST'])
def mark_fetched(request, pk):
    attachment = get_object_or_404(Attachment, pk=pk)
    if not attachment.fetched_at:
        attachment.fetched_at = timezone.now()
        attachment.save(update_fields=['fetched_at'])
    return Response({'status': 'ok'})
