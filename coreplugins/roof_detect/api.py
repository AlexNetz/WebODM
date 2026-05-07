import os
from datetime import datetime, timezone

from rest_framework import status
from rest_framework.response import Response

from app.plugins.views import TaskView
from app.plugins.worker import run_function_async
from worker.tasks import TestSafeAsyncResult


SETTING_DEFAULTS = {
    'decimation_step': 100, 'voxel_size': 0.05, 'height_percentile': 40,
    'n_planes': 15, 'iterations': 1000, 'threshold': 0.15,
    'min_inlier_ratio': 0.01, 'normal_z_max': 0.9848, 'margin': 1.0,
    'parallel_cos': 0.97, 'max_gap': 5.0,
}


def _run_detection_task(laz_path, project_id, task_id, plugin_dir, settings, progress_callback=None):
    """
    Celery worker function: runs RANSAC detection and saves result to project_data.
    Must be self-contained (all imports inside) because run_function_async uses inspect.getsource().
    plugin_dir is passed as argument because __file__ is not defined in eval() context.
    """
    import os
    import sys
    from datetime import datetime, timezone

    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    # Force fresh import on every call — Celery workers are long-lived and
    # would otherwise serve stale code from sys.modules cache after deployments.
    if 'detection' in sys.modules:
        del sys.modules['detection']
    from detection import run_detection

    def _progress(msg, pct):
        if progress_callback:
            progress_callback(msg, pct)

    _progress('Starte Erkennung…', 2)

    if not os.path.isfile(laz_path):
        raise FileNotFoundError(f'Punktwolke nicht gefunden: {laz_path}')

    result = run_detection(laz_path, **settings, progress_callback=_progress)

    from coreplugins.project_data.models import ProjectEntry

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
            'debug': result.get('debug', {}),
            'computed_at': datetime.now(timezone.utc).isoformat(),
        }
    )

    return {'output': {'edges': result['edges'], 'plane_count': result['plane_count'],
                       'debug': result.get('debug', {})}}


class DetectView(TaskView):
    def post(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)

        # PLY first (pure Python reader, no LAZ backend needed), then LAS, then LAZ
        candidates = [
            task.get_asset_download_path('georeferenced_model.ply'),
            task.get_asset_download_path('georeferenced_model.las'),
            task.get_asset_download_path('georeferenced_model.laz'),
        ]
        point_cloud_path = next(
            (os.path.abspath(p) for p in candidates if os.path.isfile(os.path.abspath(p))),
            None
        )

        if point_cloud_path is None:
            checked = ', '.join(os.path.abspath(p) for p in candidates)
            return Response(
                {'error': f'Keine Punktwolke gefunden. Gesucht in: {checked}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Tell the frontend which file we're using (helps debug format issues)
        import logging
        logging.getLogger(__name__).info(f'[roof_detect] Using point cloud: {point_cloud_path}')

        # Compute plugin_dir here (where __file__ IS defined) and pass it as argument
        plugin_dir = os.path.dirname(os.path.abspath(__file__))

        settings = {k: request.data.get(k, v) for k, v in SETTING_DEFAULTS.items()}

        try:
            celery_result = run_function_async(
                _run_detection_task,
                point_cloud_path,
                str(task.project_id),
                str(task.id),
                plugin_dir,
                settings,
                with_progress=True,
            )
            return Response({'celery_task_id': celery_result.task_id}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DetectStatusView(TaskView):
    def get(self, request, pk=None, project_pk=None, celery_task_id=None):
        self.get_and_check_task(request, pk)

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
    def delete(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)

        from coreplugins.project_data.models import ProjectEntry
        deleted, _ = ProjectEntry.objects.filter(
            project_id=task.project_id,
            task_id=task.id,
            entry_type='roof_outline'
        ).delete()

        return Response({'deleted': deleted}, status=status.HTTP_200_OK)
