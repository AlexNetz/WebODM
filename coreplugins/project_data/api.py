import mimetypes
import os

from django.http import FileResponse
from rest_framework import status
from rest_framework.parsers import MultiPartParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from app.api.common import get_and_check_project
from .models import ProjectEntry, ProjectEntryAttachment
from .serializers import ProjectEntrySerializer, ProjectEntryWriteSerializer, ProjectEntryAttachmentSerializer


class EntryListView(APIView):
    """
    GET  /api/plugins/project_data/project/{project_pk}/entries/
    POST /api/plugins/project_data/project/{project_pk}/entries/
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, JSONParser]

    def get(self, request, project_pk=None):
        project = get_and_check_project(request, project_pk)
        qs = ProjectEntry.objects.filter(project=project).select_related('created_by').prefetch_related('attachments')

        entry_type = request.query_params.get('type')
        if entry_type:
            qs = qs.filter(entry_type=entry_type)

        task_id = request.query_params.get('task')
        if task_id:
            qs = qs.filter(task_id=task_id)

        serializer = ProjectEntrySerializer(qs, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request, project_pk=None):
        # Roof-S Per-User-Modell: jeder authenticated User darf in einem für ihn
        # sichtbaren Projekt eigene Entries anlegen. `created_by` wird hier
        # serverseitig auf request.user gepinnt, ein User kann also nicht für
        # andere User schreiben. Damit ist `view_project` ausreichend statt
        # `change_project` (was nur den Projekt-Owner ranlässt).
        project = get_and_check_project(request, project_pk, perms=('view_project',))

        # Support multipart (with optional file) and JSON
        data = request.data

        # task_settings (admin-only): nur is_superuser darf Tasks global flippen.
        # Sonst könnte jeder authentifizierte User per Direkt-POST den
        # Global-Mode für einen Task forcen und damit shared State erzwingen.
        entry_type = data.get('entry_type') if hasattr(data, 'get') else None
        if entry_type == 'task_settings' and not request.user.is_superuser:
            from rest_framework import exceptions
            raise exceptions.PermissionDenied(detail="Admin permission required for task_settings.")

        write_serializer = ProjectEntryWriteSerializer(data=data)
        if not write_serializer.is_valid():
            return Response(write_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        entry = write_serializer.save(project=project, created_by=request.user)

        # If a file was included in the same request, create an attachment immediately
        if 'file' in request.FILES:
            f = request.FILES['file']
            mime_type, _ = mimetypes.guess_type(f.name)
            ProjectEntryAttachment.objects.create(
                entry=entry,
                file=f,
                filename=f.name,
                mime_type=mime_type or '',
            )

        serializer = ProjectEntrySerializer(
            ProjectEntry.objects.prefetch_related('attachments').get(pk=entry.pk),
            context={'request': request}
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class EntryDetailView(APIView):
    """
    GET    /api/plugins/project_data/project/{project_pk}/entries/{entry_pk}/
    PUT    /api/plugins/project_data/project/{project_pk}/entries/{entry_pk}/
    PATCH  /api/plugins/project_data/project/{project_pk}/entries/{entry_pk}/
    DELETE /api/plugins/project_data/project/{project_pk}/entries/{entry_pk}/
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, JSONParser]

    def _get_entry(self, request, project_pk, entry_pk, write=False):
        # Roof-S Per-User-Modell: `view_project` reicht für alle Operations.
        # Owner-Check (write nur durch created_by-Eigentümer) erfolgt in
        # `_assert_owner` weiter unten — das verhindert dass User mit
        # view_project-Permission fremde Entries via Detail-View manipulieren.
        project = get_and_check_project(request, project_pk, perms=('view_project',))
        try:
            entry = ProjectEntry.objects.select_related('created_by').prefetch_related('attachments').get(
                pk=entry_pk, project=project
            )
        except ProjectEntry.DoesNotExist:
            from rest_framework import exceptions
            raise exceptions.NotFound()
        if write:
            self._assert_owner(request, entry)
        return entry

    @staticmethod
    def _assert_owner(request, entry):
        """Schreibzugriff (PUT/PATCH/DELETE) nur durch den Owner.
        Admins (is_superuser) dürfen alles — gewollt für die Wartung.
        Legacy-Entries ohne created_by (created_by is NULL) sind frei
        editierbar — passiert nur bei alten Daten vor dem Per-User-Modell.
        """
        from rest_framework import exceptions
        if request.user.is_superuser:
            return
        if entry.created_by_id is None:
            return
        if entry.created_by_id != request.user.id:
            raise exceptions.NotFound()   # Konsistent mit check_project_perms (404 statt 403)

    def get(self, request, project_pk=None, entry_pk=None):
        entry = self._get_entry(request, project_pk, entry_pk)
        serializer = ProjectEntrySerializer(entry, context={'request': request})
        return Response(serializer.data)

    def put(self, request, project_pk=None, entry_pk=None):
        entry = self._get_entry(request, project_pk, entry_pk, write=True)
        serializer = ProjectEntryWriteSerializer(entry, data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        read_serializer = ProjectEntrySerializer(
            ProjectEntry.objects.prefetch_related('attachments').get(pk=entry.pk),
            context={'request': request}
        )
        return Response(read_serializer.data)

    def patch(self, request, project_pk=None, entry_pk=None):
        entry = self._get_entry(request, project_pk, entry_pk, write=True)
        serializer = ProjectEntryWriteSerializer(entry, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        read_serializer = ProjectEntrySerializer(
            ProjectEntry.objects.prefetch_related('attachments').get(pk=entry.pk),
            context={'request': request}
        )
        return Response(read_serializer.data)

    def delete(self, request, project_pk=None, entry_pk=None):
        entry = self._get_entry(request, project_pk, entry_pk, write=True)
        entry.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AttachView(APIView):
    """
    POST /api/plugins/project_data/project/{project_pk}/entries/{entry_pk}/attach/
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request, project_pk=None, entry_pk=None):
        # Roof-S Per-User-Modell — view_project genügt; Owner-Check via _assert_owner.
        get_and_check_project(request, project_pk, perms=('view_project',))
        try:
            entry = ProjectEntry.objects.get(pk=entry_pk, project_id=project_pk)
        except ProjectEntry.DoesNotExist:
            from rest_framework import exceptions
            raise exceptions.NotFound()
        EntryDetailView._assert_owner(request, entry)

        if 'file' not in request.FILES:
            return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

        f = request.FILES['file']
        mime_type, _ = mimetypes.guess_type(f.name)
        attachment = ProjectEntryAttachment.objects.create(
            entry=entry,
            file=f,
            filename=f.name,
            mime_type=mime_type or '',
        )

        serializer = ProjectEntryAttachmentSerializer(attachment, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class AttachmentDownloadView(APIView):
    """
    GET /api/plugins/project_data/project/{project_pk}/entries/{entry_pk}/attachments/{filename}/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_pk=None, entry_pk=None, filename=None):
        get_and_check_project(request, project_pk)
        try:
            entry = ProjectEntry.objects.only('id', 'project_id').get(pk=entry_pk, project_id=project_pk)
            attachment = ProjectEntryAttachment.objects.get(entry=entry, filename=filename)
        except (ProjectEntry.DoesNotExist, ProjectEntryAttachment.DoesNotExist):
            from rest_framework import exceptions
            raise exceptions.NotFound()

        file_path = attachment.file.path
        if not os.path.exists(file_path):
            from rest_framework import exceptions
            raise exceptions.NotFound()

        mime_type = attachment.mime_type or 'application/octet-stream'
        response = FileResponse(open(file_path, 'rb'), content_type=mime_type)
        response['Content-Disposition'] = 'inline; filename="{}"'.format(attachment.filename)
        return response
