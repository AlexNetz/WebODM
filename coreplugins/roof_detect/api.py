import os
from datetime import datetime, timezone

from rest_framework import status
from rest_framework.response import Response

from app.plugins.views import TaskView
from app.plugins.worker import run_function_async
from worker.tasks import TestSafeAsyncResult


def _run_detection_task(laz_path, project_id, task_id, progress_callback=None):
    """
    Celery worker function: runs RANSAC detection and saves result to project_data.
    Must be self-contained (all imports inside) because run_function_async uses inspect.getsource().
    """
    import os
    from datetime import datetime, timezone

    # Local import of detection pipeline
    import sys
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    from detection import run_detection

    def _progress(msg, pct):
        if progress_callback:
            progress_callback(msg, pct)

    _progress('Starte Erkennung…', 2)

    if not os.path.isfile(laz_path):
        raise FileNotFoundError(f'LAZ-Datei nicht gefunden: {laz_path}')

    result = run_detection(laz_path, progress_callback=_progress)

    # Persist to project_data
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'webodm.settings')

    from coreplugins.project_data.models import ProjectEntry

    # Remove previous result for this task
    ProjectEntry.objects.filter(
        project_id=project_id,
        task_id=task_id,
        entry_type='roof_outline'
    ).delete()

    ProjectEntry.objects.create(
        project_id=project_id,
        task_id=task_id,
        entry_type='roof_outline',
        title='Dachkanten-Erkennung',
        data={
            'edges': result['edges'],
            'plane_count': result['plane_count'],
            'computed_at': datetime.now(timezone.utc).isoformat(),
        }
    )

    return {'output': {'edges': result['edges'], 'plane_count': result['plane_count']}}


class DetectView(TaskView):
    """
    POST  — start async detection, returns celery_task_id
    """

    def post(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)

        # Prefer uncompressed LAS (no lazrs backend needed); fall back to LAZ
        laz_path = os.path.abspath(task.get_asset_download_path('georeferenced_model.las'))
        if not os.path.isfile(laz_path):
            laz_path = os.path.abspath(task.get_asset_download_path('georeferenced_model.laz'))
        if not os.path.isfile(laz_path):
            return Response(
                {'error': 'Keine Punktwolke gefunden (LAS/LAZ). Bitte zuerst eine ODM-Verarbeitung mit Punktwolke durchführen.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            celery_result = run_function_async(
                _run_detection_task,
                laz_path,
                str(task.project_id),
                str(task.id),
                with_progress=True,
            )
            return Response({'celery_task_id': celery_result.task_id}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DetectStatusView(TaskView):
    """
    GET  — poll Celery task status
    Response: {ready, status, progress, error?, edges?, plane_count?}
    """

    def get(self, request, pk=None, project_pk=None, celery_task_id=None):
        self.get_and_check_task(request, pk)  # permission check

        res = TestSafeAsyncResult(celery_task_id)

        if not res.ready():
            out = {'ready': False, 'status': res.state, 'progress': 0}
            if res.state == 'PROGRESS' and res.info:
                out['status'] = res.info.get('status', res.state)
                out['progress'] = res.info.get('progress', 0)
            return Response(out)

        try:
            result = res.get()
        except Exception as e:
            return Response({'ready': True, 'status': 'FAILURE', 'error': str(e)})

        if isinstance(result, dict) and result.get('error'):
            return Response({'ready': True, 'status': 'FAILURE', 'error': result['error']})

        output = result.get('output', {}) if isinstance(result, dict) else {}
        return Response({
            'ready': True,
            'status': 'SUCCESS',
            'progress': 100,
            'edges': output.get('edges', []),
            'plane_count': output.get('plane_count', 0),
        })


class ResultView(TaskView):
    """
    DELETE  — remove stored roof_outline entry for this task
    """

    def delete(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)

        from coreplugins.project_data.models import ProjectEntry
        deleted, _ = ProjectEntry.objects.filter(
            project_id=task.project_id,
            task_id=task.id,
            entry_type='roof_outline'
        ).delete()

        return Response({'deleted': deleted}, status=status.HTTP_200_OK)
