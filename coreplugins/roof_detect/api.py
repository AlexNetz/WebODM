import os
from datetime import datetime, timezone

from rest_framework import status
from rest_framework.response import Response

from app.plugins.views import TaskView
from app.plugins.worker import run_function_async
from worker.tasks import TestSafeAsyncResult

P2CAD_SERVICE = os.environ.get('P2CAD_SERVICE_URL', 'http://point2cad:8765')
P2CAD_DATA    = os.environ.get('P2CAD_DATA_DIR', '/data')

SETTING_DEFAULTS = {
    'decimation_step': 100, 'voxel_size': 0.05, 'height_percentile': 40,
    'n_planes': 15, 'iterations': 1000, 'threshold': 0.15,
    'min_inlier_ratio': 0.01, 'min_normal_z': 0.10, 'margin': 1.0,
    'parallel_cos': 0.97, 'max_gap': 5.0,
    # Spatial-cluster split: separates dormers / chimneys that RANSAC merged
    # into one plane (same orientation + height but disjoint XY footprint).
    'split_gap': 1.0,           # m — points farther apart in XY = separate cluster
    'min_cluster_pts': 15,      # drop sub-clusters smaller than this (noise)
    # Morphological-erosion split: breaks thin corridors that link physically
    # separate roof patches sharing a plane equation (e.g. ridge-cap streak
    # connecting dormers on opposite roof sides). 0 = disabled.
    'bridge_width': 0.0,        # m — erode away inlier corridors thinner than this
}


# ── RANSAC detection task ──────────────────────────────────────────────────────

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
    if 'detection' in sys.modules:
        del sys.modules['detection']
    from detection import run_detection

    def _progress(msg, pct):
        if progress_callback:
            progress_callback(msg, pct)

    _progress('Starte Erkennung…', 2)

    if not os.path.isfile(laz_path):
        raise FileNotFoundError(f'Punktwolke nicht gefunden: {laz_path}')

    # Export .xyzc to shared volume for point2cad
    xyzc_dir = os.path.join('/data', task_id)
    os.makedirs(xyzc_dir, exist_ok=True)
    xyzc_path = os.path.join(xyzc_dir, 'input.xyzc')

    clip_bounds = settings.pop('clip_bounds', None)
    result = run_detection(laz_path, **settings, xyzc_out_path=xyzc_path,
                           clip_bounds=clip_bounds, progress_callback=_progress)

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
            'preview_points': result.get('preview_points', {}),
            'xyzc_stats': result.get('xyzc_stats'),
            'debug': result.get('debug', {}),
            'computed_at': datetime.now(timezone.utc).isoformat(),
        }
    )

    return {'output': {
        'edges': result['edges'],
        'plane_count': result['plane_count'],
        'preview_points': result.get('preview_points', {}),
        'debug': result.get('debug', {}),
    }}


# ── point2cad task ─────────────────────────────────────────────────────────────

def _run_point2cad_task(project_id, task_id, plugin_dir, p2cad_service, p2cad_data,
                        p2cad_args=None, progress_callback=None):
    """
    Celery worker function: calls the point2cad microservice and stores the result.
    Self-contained (all imports inside).

    p2cad_args: optional dict, durchgereicht an den point2cad-CLI. Whitelist
    {max_parallel_surfaces, num_inr_fit_attempts, seed, surfaces_multiprocessing}
    wird Service-seitig nochmal erzwungen.

    On both SUCCESS and FAILURE, the latest stdout+stderr captured by the service
    are persisted to project_data so the frontend can show what point2cad logged.
    """
    import os, sys, json, time
    import urllib.request
    import urllib.error
    from datetime import datetime, timezone

    DB_LOG_LIMIT = 10000   # cap per stream when storing in JSONField
    LIVE_LOG_TAIL = 4000   # chars per stream forwarded through progress meta

    def _progress(msg, pct, **extra):
        if progress_callback:
            progress_callback(msg, pct, **extra)

    _progress('Starte point2cad…', 2)

    xyzc_path = os.path.join(p2cad_data, task_id, 'input.xyzc')
    out_path   = os.path.join(p2cad_data, task_id, 'out')

    if not os.path.isfile(xyzc_path):
        raise FileNotFoundError(
            'Punktwolke (xyzc) nicht gefunden: ' + xyzc_path +
            '. Bitte zuerst RANSAC-Erkennung ausführen.'
        )

    # Trigger point2cad service
    body = json.dumps({
        'xyzc_path': xyzc_path,
        'out_path':  out_path,
        'p2cad_args': p2cad_args or {},
    }).encode()
    req = urllib.request.Request(
        '{}/run'.format(p2cad_service),
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            p2cad_task_id = json.loads(resp.read())['task_id']
    except urllib.error.URLError as e:
        raise RuntimeError(
            'point2cad-Service nicht erreichbar ({}): {}. '
            'Läuft der Service? docker compose up point2cad'.format(p2cad_service, e)
        )

    _progress('point2cad läuft…', 5)

    # Poll status (max 40 min). st is the most-recent payload received.
    st = None
    for i in range(480):
        time.sleep(5)
        try:
            with urllib.request.urlopen(
                '{}/status/{}'.format(p2cad_service, p2cad_task_id), timeout=10
            ) as resp:
                st = json.loads(resp.read())
        except Exception:
            continue  # transient network error — keep polling

        if st.get('done'):
            break

        # Forward live log tails to the frontend through Celery progress meta.
        # Frontend shows the latest stderr/stdout snippets so a stalled 90 %
        # bar isn't a black box.
        pct = min(90, 5 + int(i * 85 / 200))
        live_stdout = (st.get('stdout') or '')[-LIVE_LOG_TAIL:]
        live_stderr = (st.get('stderr') or '')[-LIVE_LOG_TAIL:]
        _progress(
            'Verarbeite Flächen… ({}s)'.format(i * 5), pct,
            live_stdout=live_stdout, live_stderr=live_stderr,
        )
    else:
        raise TimeoutError('point2cad timed out after 40 minutes')

    # Capture logs regardless of success/failure
    logs = {
        'stdout': (st.get('stdout') or '')[-DB_LOG_LIMIT:],
        'stderr': (st.get('stderr') or '')[-DB_LOG_LIMIT:],
    }

    from coreplugins.project_data.models import ProjectEntry

    ProjectEntry.objects.filter(
        project_id=project_id, task_id=task_id, entry_type='cad_result'
    ).delete()

    if not st.get('success'):
        # Persist the failure (logs + flag) before raising so the frontend can
        # fetch /cad/result/ and show point2cad's own diagnostics.
        ProjectEntry.objects.create(
            project_id=project_id, task_id=task_id,
            entry_type='cad_result', title='Point2CAD Ergebnis (Fehler)',
            data={
                'topo': None,
                'mesh_available': False,
                'logs': logs,
                'failed': True,
                'computed_at': datetime.now(timezone.utc).isoformat(),
            }
        )
        error_text = st.get('error') or 'unbekannter Fehler'
        raise RuntimeError('point2cad fehlgeschlagen:\n' + error_text)

    _progress('Ergebnis speichern…', 92)

    topo_path = os.path.join(out_path, 'topo', 'topo.json')
    try:
        with open(topo_path) as f:
            topo = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        # point2cad reported success but topo.json is missing/broken — surface
        # the situation through logs rather than crashing opaquely.
        ProjectEntry.objects.create(
            project_id=project_id, task_id=task_id,
            entry_type='cad_result', title='Point2CAD Ergebnis (Topo fehlt)',
            data={
                'topo': None,
                'mesh_available': os.path.isfile(os.path.join(out_path, 'clipped', 'mesh.ply')),
                'logs': logs,
                'failed': True,
                'computed_at': datetime.now(timezone.utc).isoformat(),
            }
        )
        raise RuntimeError('topo.json nicht lesbar: {}'.format(e))

    ProjectEntry.objects.create(
        project_id=project_id, task_id=task_id,
        entry_type='cad_result', title='Point2CAD Ergebnis',
        data={
            'topo': topo,
            'mesh_available': os.path.isfile(os.path.join(out_path, 'clipped', 'mesh.ply')),
            'logs': logs,
            'computed_at': datetime.now(timezone.utc).isoformat(),
        }
    )

    _progress('Fertig', 100)

    return {'output': {
        'curves': len(topo.get('curves', [])),
        'corners': len(topo.get('corners', [])),
    }}


# ── RANSAC views ───────────────────────────────────────────────────────────────

class DetectView(TaskView):
    def post(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)

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

        import logging
        logging.getLogger(__name__).info(f'[roof_detect] Using point cloud: {point_cloud_path}')

        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        settings = {k: request.data.get(k, v) for k, v in SETTING_DEFAULTS.items()}
        # clip_bounds vom Frontend: optional 4 (XY-AABB) oder 6 (XYZ-AABB) Werte.
        # Mit 6 Werten überschreibt der User-Bereich Höhenfilter + Bodenerkennung.
        clip_bounds = request.data.get('clip_bounds')  # [xmin,xmax,ymin,ymax] oder [...,zmin,zmax] oder None
        if clip_bounds:
            settings['clip_bounds'] = clip_bounds  # piggyback on settings dict to avoid Celery signature issues

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
            'preview_points': output.get('preview_points', {}),
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


# ── point2cad views ────────────────────────────────────────────────────────────

P2CAD_ARG_WHITELIST = {
    'max_parallel_surfaces',
    'num_inr_fit_attempts',
    'seed',
    'surfaces_multiprocessing',
}


class CADView(TaskView):
    def post(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)
        plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # Defense-in-depth: filter to known args before passing to the worker
        # (the point2cad service enforces the same whitelist).
        raw_args = request.data.get('p2cad_args') or {}
        if not isinstance(raw_args, dict):
            return Response(
                {'error': 'p2cad_args muss ein Objekt sein.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        p2cad_args = {k: v for k, v in raw_args.items() if k in P2CAD_ARG_WHITELIST}

        try:
            celery_result = run_function_async(
                _run_point2cad_task,
                str(task.project_id),
                str(task.id),
                plugin_dir,
                P2CAD_SERVICE,
                P2CAD_DATA,
                p2cad_args,
                with_progress=True,
            )
            return Response({'celery_task_id': celery_result.task_id}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CADStatusView(TaskView):
    def get(self, request, pk=None, project_pk=None, celery_task_id=None):
        self.get_and_check_task(request, pk)

        res = TestSafeAsyncResult(celery_task_id)

        if not res.ready():
            out = {'ready': False, 'status': res.state, 'progress': 0}
            if res.state == 'PROGRESS' and res.info:
                out['status'] = res.info.get('status', res.state)
                out['progress'] = res.info.get('progress', 0)
                # Live log tails — set by _run_point2cad_task each poll so the
                # frontend can render point2cad's current output while running.
                out['live_stdout'] = res.info.get('live_stdout', '')
                out['live_stderr'] = res.info.get('live_stderr', '')
            return Response(out)

        try:
            result = res.get()
        except Exception as e:
            return Response({'ready': True, 'status': 'FAILURE', 'error': str(e)})

        output = result.get('output', {}) if isinstance(result, dict) else {}
        return Response({
            'ready': True,
            'status': 'SUCCESS',
            'progress': 100,
            'curves': output.get('curves', 0),
            'corners': output.get('corners', 0),
        })


class CADResultView(TaskView):
    def get(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)

        from coreplugins.project_data.models import ProjectEntry
        entry = ProjectEntry.objects.filter(
            project_id=task.project_id,
            task_id=task.id,
            entry_type='cad_result',
        ).first()

        if not entry:
            return Response({'error': 'Kein CAD-Ergebnis vorhanden'}, status=status.HTTP_404_NOT_FOUND)

        return Response(entry.data)

    def delete(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)

        from coreplugins.project_data.models import ProjectEntry
        deleted, _ = ProjectEntry.objects.filter(
            project_id=task.project_id,
            task_id=task.id,
            entry_type='cad_result',
        ).delete()

        return Response({'deleted': deleted}, status=status.HTTP_200_OK)


class CADMeshView(TaskView):
    def get(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)
        ply_path = os.path.join(P2CAD_DATA, str(task.id), 'out', 'clipped', 'mesh.ply')

        if not os.path.isfile(ply_path):
            return Response({'error': 'mesh.ply nicht gefunden'}, status=status.HTTP_404_NOT_FOUND)

        from django.http import FileResponse
        return FileResponse(
            open(ply_path, 'rb'),
            content_type='application/octet-stream',
            as_attachment=True,
            filename='mesh.ply',
        )
