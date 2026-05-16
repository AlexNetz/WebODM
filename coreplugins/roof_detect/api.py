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
    # Outlier-context filter: drops plane inliers whose XY-neighbourhood is
    # dominated by points belonging to other planes. Removes "Verlängerung"-
    # lobes where a plane equation extends into another roof patch's region.
    # 0.0 = disabled; 0.5 = neighbourhood majority must be own plane.
    'min_same_plane_ratio': 0.0,
}

# Defaults for the point2cad phase (NOT consumed by run_detection — keep
# separate from SETTING_DEFAULTS so DetectView doesn't forward them).
P2CAD_PHASE_DEFAULTS = {
    # Post-trim distance (m): after point2cad finishes, drops mesh faces whose
    # centroid is farther than this from any inlier of their source plane.
    # Compensates for point2cad's clipping leaving lobes attached when they
    # don't cross another surface. 0.0 disables post-trim.
    'posttrim_distance_m': 0.30,
    # Outline alpha radius (m): max gap considered "inside" the plane footprint
    # when computing the 2D alpha-shape boundary of each plane's inliers. Used
    # to add outer edges (eaves/gables) to topo.outline_curves alongside
    # point2cad's intersection curves. 0.0 disables outline computation.
    'outline_alpha_radius_m': 0.8,
    # Outline simplify (m): Douglas-Peucker tolerance applied AFTER alpha-shape.
    # Removes wavy noise from the boundary so the result reads as architectural
    # straight lines with a few corners. 0 disables DP (keep raw alpha-shape).
    'outline_simplify_m': 0.30,
    # Outline OBB mode: replace each plane's alpha-shape boundary with a
    # minimum-area oriented bounding box (rotated rectangle, 4 corners). Only
    # sensible for rectangular planes. Overrides outline_simplify_m when True.
    'outline_obb_mode': False,
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
                        p2cad_args=None, posttrim_distance_m=0.30,
                        outline_alpha_radius_m=0.5,
                        outline_simplify_m=0.30, outline_obb_mode=False,
                        progress_callback=None):
    """
    Celery worker function: calls the point2cad microservice and stores the result.
    Self-contained (all imports inside).

    p2cad_args: optional dict, durchgereicht an den point2cad-CLI. Whitelist
    {max_parallel_surfaces, num_inr_fit_attempts, seed, surfaces_multiprocessing}
    wird Service-seitig nochmal erzwungen.

    posttrim_distance_m: after point2cad finishes, mesh faces whose centroid is
    farther than this (in metres) from any inlier of their source plane get
    dropped. 0 or negative disables the post-trim. Compensates for point2cad's
    clipping leaving lobes attached when they don't cross another surface.

    outline_alpha_radius_m: max circumradius (in metres) for the 2D alpha-shape
    that produces outer-edge polylines per RANSAC plane. Stored under
    `topo.outline_curves`. 0 or negative disables outline computation.

    On both SUCCESS and FAILURE, the latest stdout+stderr captured by the service
    are persisted to project_data so the frontend can show what point2cad logged.
    """
    import os, sys, json, time
    import urllib.request
    import urllib.error
    from datetime import datetime, timezone

    DB_LOG_LIMIT = 10000   # cap per stream when storing in JSONField
    LIVE_LOG_TAIL = 4000   # chars per stream forwarded through progress meta

    def _progress(msg, pct, live_stdout=None, live_stderr=None):
        # The shared `progress_callback` (app/plugins/worker.py) only accepts
        # `(status, perc)` and is image-baked (not bind-mounted), so we cannot
        # ship a signature change without a docker-compose down/up. Instead we
        # JSON-encode the extra payload into the `status` string and unpack it
        # on the read side (CADStatusView). Plain-string statuses still work.
        if progress_callback is None:
            return
        if live_stdout is not None or live_stderr is not None:
            payload = json.dumps({
                'msg': msg,
                'live_stdout': live_stdout or '',
                'live_stderr': live_stderr or '',
            })
            progress_callback(payload, pct)
        else:
            progress_callback(msg, pct)

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
    for i in range(1200):
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

    # ── Post-trim: drop mesh faces whose centroid is far from any inlier of
    # their source plane. Compensates for point2cad's clipping leaving lobes
    # attached when they don't cross another surface. Idempotency of
    # normalize_points means mesh and xyzc share the same coordinate frame
    # (we pre-normalised in export_xyzc).
    posttrim_log_lines = []
    if posttrim_distance_m and posttrim_distance_m > 0:
        try:
            import numpy as np
            import trimesh
            from scipy.spatial import cKDTree

            clipped_ply = os.path.join(out_path, 'clipped', 'mesh.ply')
            mesh = trimesh.load(clipped_ply, process=False)
            n_faces_before = len(mesh.faces)

            face_colors_attr = getattr(mesh.visual, 'face_colors', None)
            if face_colors_attr is None or len(face_colors_attr) != n_faces_before:
                raise RuntimeError(
                    'Post-Trim: mesh hat keine Face-Colors — Source-Plane-Zuordnung '
                    'nicht möglich. Wurde point2cad ohne save_clipped_meshes ausgeführt?'
                )
            face_colors = np.asarray(face_colors_attr)[:, :3].astype(np.int32)

            data = np.loadtxt(xyzc_path)
            inlier_points = data[:, :3].astype(np.float64)
            inlier_labels = data[:, 3].astype(np.int32)
            unique_pids = np.unique(inlier_labels)

            face_centroids = mesh.vertices[mesh.faces].mean(axis=1)

            plane_trees = {
                int(pid): cKDTree(inlier_points[inlier_labels == pid])
                for pid in unique_pids
            }

            # Threshold lives in the same normalised frame as the mesh — we
            # divide the user's metric request by the export-time `scale`.
            roof_entry = ProjectEntry.objects.filter(
                project_id=project_id, task_id=task_id, entry_type='roof_outline'
            ).first()
            scale = None
            if roof_entry:
                xyzc_stats = (roof_entry.data or {}).get('xyzc_stats') or {}
                scale = xyzc_stats.get('scale')

            if not scale or scale <= 0:
                posttrim_log_lines.append(
                    'Post-Trim übersprungen: kein `scale` in xyzc_stats. '
                    'Bitte RANSAC erneut ausführen, damit xyzc neu geschrieben wird.'
                )
            else:
                threshold_norm = posttrim_distance_m / scale

                # Group faces by RGB triple (alpha ignored).
                color_to_face_indices = {}
                for i in range(n_faces_before):
                    key = (int(face_colors[i, 0]), int(face_colors[i, 1]),
                           int(face_colors[i, 2]))
                    color_to_face_indices.setdefault(key, []).append(i)

                # Median-distance heuristic: for each colour, the source plane is
                # the one whose inliers most face-centroids of that colour cluster
                # around. Median is robust to a minority of lobe faces sitting in
                # another plane's region.
                color_to_plane = {}
                for color, idxs in color_to_face_indices.items():
                    centroids = face_centroids[idxs]
                    best_pid = None
                    best_med = float('inf')
                    for pid, tree in plane_trees.items():
                        d, _ = tree.query(centroids, k=1)
                        med = float(np.median(d))
                        if med < best_med:
                            best_med = med
                            best_pid = pid
                    color_to_plane[color] = best_pid

                keep_mask = np.ones(n_faces_before, dtype=bool)
                for color, idxs in color_to_face_indices.items():
                    pid = color_to_plane[color]
                    if pid is None:
                        continue
                    tree = plane_trees[pid]
                    centroids = face_centroids[idxs]
                    distances, _ = tree.query(centroids, k=1)
                    drop_local = distances >= threshold_norm
                    for i_local, i_global in enumerate(idxs):
                        if drop_local[i_local]:
                            keep_mask[i_global] = False

                n_dropped = int((~keep_mask).sum())
                if n_dropped > 0:
                    mesh.update_faces(keep_mask)
                    mesh.remove_unreferenced_vertices()
                    mesh.export(clipped_ply)
                posttrim_log_lines.append(
                    'Post-Trim: {} → {} Faces '
                    '(threshold={:.2f} m / {:.4f} norm, scale={:.3f}), '
                    '{} dropped'.format(
                        n_faces_before, n_faces_before - n_dropped,
                        posttrim_distance_m, threshold_norm, scale, n_dropped,
                    )
                )
        except Exception as e:
            posttrim_log_lines.append('Post-Trim fehlgeschlagen: {}'.format(e))

    # ── Outline-Curves: Außenkanten via 2D-Alpha-Shape pro Plane ──────────
    # Pro RANSAC-Plane Inlier in 2D-Plane-Local-Coords projizieren, Alpha-Shape
    # bilden, Boundary-Polylines zurück nach 3D heben. Resultat parallel zu
    # point2cad's `topo.curves` (Schnittlinien) als `topo.outline_curves`
    # (Außenkanten). Frontend rendert sie in einer anderen Farbe.
    outline_log_lines = []
    if outline_alpha_radius_m and outline_alpha_radius_m > 0:
        try:
            roof_entry = ProjectEntry.objects.filter(
                project_id=project_id, task_id=task_id, entry_type='roof_outline'
            ).first()
            scale_for_outline = None
            if roof_entry:
                stats_for_outline = (roof_entry.data or {}).get('xyzc_stats') or {}
                scale_for_outline = stats_for_outline.get('scale')

            if not scale_for_outline or scale_for_outline <= 0:
                outline_log_lines.append(
                    'Outline übersprungen: kein `scale` in xyzc_stats. '
                    'Bitte RANSAC erneut ausführen, damit xyzc neu geschrieben wird.'
                )
            else:
                # Lazy import detection — same pattern as _run_detection_task.
                if plugin_dir not in sys.path:
                    sys.path.insert(0, plugin_dir)
                if 'detection' in sys.modules:
                    del sys.modules['detection']
                from detection import compute_outline_curves

                alpha_threshold_norm = outline_alpha_radius_m / scale_for_outline
                simplify_norm = (outline_simplify_m / scale_for_outline
                                 if outline_simplify_m and outline_simplify_m > 0
                                 else 0.0)
                outline_curves = compute_outline_curves(
                    xyzc_path, alpha_threshold_norm,
                    simplify_norm=simplify_norm,
                    obb_mode=bool(outline_obb_mode),
                )
                topo['outline_curves'] = outline_curves
                mode = ('OBB' if outline_obb_mode
                        else 'DP simplify={:.2f} m / {:.4f} norm'.format(
                            outline_simplify_m, simplify_norm,
                        ))
                outline_log_lines.append(
                    'Outline: {} Polylines '
                    '(alpha_radius={:.2f} m / {:.4f} norm, {})'.format(
                        len(outline_curves), outline_alpha_radius_m,
                        alpha_threshold_norm, mode,
                    )
                )
        except Exception as e:
            outline_log_lines.append('Outline fehlgeschlagen: {}'.format(e))

    # Append post-trim + outline summary to stdout so it shows up in the UI logs panel.
    if posttrim_log_lines or outline_log_lines:
        appended = '\n'.join(posttrim_log_lines + outline_log_lines)
        logs = {
            'stdout': (logs.get('stdout', '') + '\n' + appended)[-DB_LOG_LIMIT:],
            'stderr': logs.get('stderr', ''),
        }

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


class CancelView(TaskView):
    """Revoked die Celery-Task mit dem gegebenen ID. Mit terminate=True schickt
    der Worker SIGTERM an den Task-Prozess → die laufende Berechnung wird
    abgebrochen. Generisch für detect- und cad-Tasks."""

    def post(self, request, pk=None, project_pk=None, celery_task_id=None):
        self.get_and_check_task(request, pk)
        try:
            res = TestSafeAsyncResult(celery_task_id)
            res.revoke(terminate=True, signal='SIGTERM')
            return Response({'cancelled': True}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


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

        # Post-trim distance (in metres) — applied after point2cad finishes to
        # remove mesh faces far from any inlier of their source plane. Lives in
        # P2CAD_PHASE_DEFAULTS so DetectView doesn't accidentally forward it to
        # run_detection (which doesn't take this kwarg).
        try:
            posttrim_distance_m = float(
                request.data.get('posttrim_distance_m',
                                  P2CAD_PHASE_DEFAULTS['posttrim_distance_m'])
            )
        except (TypeError, ValueError):
            posttrim_distance_m = P2CAD_PHASE_DEFAULTS['posttrim_distance_m']

        # Outline alpha radius (m) — controls the concavity of the 2D alpha-
        # shape that produces outer-edge polylines for each plane.
        try:
            outline_alpha_radius_m = float(
                request.data.get('outline_alpha_radius_m',
                                  P2CAD_PHASE_DEFAULTS['outline_alpha_radius_m'])
            )
        except (TypeError, ValueError):
            outline_alpha_radius_m = P2CAD_PHASE_DEFAULTS['outline_alpha_radius_m']

        # Outline simplify (m) — Douglas-Peucker tolerance applied after alpha
        # shape. 0 disables DP, keep raw boundary.
        try:
            outline_simplify_m = float(
                request.data.get('outline_simplify_m',
                                  P2CAD_PHASE_DEFAULTS['outline_simplify_m'])
            )
        except (TypeError, ValueError):
            outline_simplify_m = P2CAD_PHASE_DEFAULTS['outline_simplify_m']

        # Outline OBB mode — replace alpha-shape boundary with oriented BB.
        outline_obb_mode = bool(
            request.data.get('outline_obb_mode',
                              P2CAD_PHASE_DEFAULTS['outline_obb_mode'])
        )

        try:
            celery_result = run_function_async(
                _run_point2cad_task,
                str(task.project_id),
                str(task.id),
                plugin_dir,
                P2CAD_SERVICE,
                P2CAD_DATA,
                p2cad_args,
                posttrim_distance_m,
                outline_alpha_radius_m,
                outline_simplify_m,
                outline_obb_mode,
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
                raw_status = res.info.get('status', res.state)
                out['progress'] = res.info.get('progress', 0)
                # _run_point2cad_task JSON-encodes the status string when it
                # has live log tails to forward (workaround for the image-baked
                # progress_callback that only accepts (status, perc)). Decode
                # here; fall back to plain string for older payloads / other
                # task types.
                import json as _json
                parsed = None
                if isinstance(raw_status, str) and raw_status.startswith('{'):
                    try:
                        parsed = _json.loads(raw_status)
                    except (ValueError, TypeError):
                        parsed = None
                if isinstance(parsed, dict):
                    out['status'] = parsed.get('msg', '')
                    out['live_stdout'] = parsed.get('live_stdout', '')
                    out['live_stderr'] = parsed.get('live_stderr', '')
                else:
                    out['status'] = raw_status
                    out['live_stdout'] = ''
                    out['live_stderr'] = ''
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
