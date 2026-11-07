import os

from rest_framework import status
from rest_framework.response import Response

from app.plugins.views import TaskView
from app.plugins.worker import run_function_async
from worker.tasks import TestSafeAsyncResult

from .tasks import _run_export_task, _run_apply_task
from .export_source import export_glb_source


class ExportView(TaskView):
    def post(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)

        matrix = request.data.get('matrix')
        if not isinstance(matrix, list) or len(matrix) != 16:
            return Response(
                {'error': 'matrix muss eine Liste von 16 Zahlen sein.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            matrix = [float(v) for v in matrix]
        except (TypeError, ValueError):
            return Response(
                {'error': 'matrix enthält ungültige Werte.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve source file paths (get_asset_download_path returns an absolute path
        # for string-valued ASSETS_MAP entries without checking existence)
        laz_path = task.get_asset_download_path('georeferenced_model.laz')
        if not os.path.isfile(laz_path):
            las_path = task.get_asset_download_path('georeferenced_model.las')
            if os.path.isfile(las_path):
                laz_path = las_path
            else:
                return Response(
                    {'error': 'Keine Punktwolke (LAZ/LAS) für diesen Task gefunden.'},
                    status=status.HTTP_404_NOT_FOUND,
                )

        # Bei angewendeter Ausrichtung das gesicherte Original nehmen — das aktuelle GLB ist
        # schon ausgerichtet, der Export würde die Matrix sonst doppelt anwenden.
        glb_path = export_glb_source(task.assets_path(), task.get_asset_download_path('textured_model.glb'))

        output_dir = task.assets_path('realigned')

        # Read the georeferencing offset from coords.txt (line 2: "X_UTM Y_UTM").
        # The GLB model uses local coordinates: local_xy = UTM_xy - geo_offset.
        # We pass this so the worker can compute M in local space for the GLB.
        geo_offset = [0.0, 0.0]
        coords_path = task.assets_path('odm_georeferencing', 'coords.txt')
        if os.path.isfile(coords_path):
            try:
                with open(coords_path) as f:
                    lines = f.read().strip().split('\n')
                if len(lines) >= 2:
                    parts = lines[1].strip().split()
                    if len(parts) >= 2:
                        geo_offset = [float(parts[0]), float(parts[1])]
            except Exception:
                pass

        try:
            celery_result = run_function_async(
                _run_export_task,
                laz_path,
                glb_path,
                matrix,
                output_dir,
                geo_offset,
                with_progress=True,
            )
            return Response({'celery_task_id': celery_result.task_id}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ExportStatusView(TaskView):
    def get(self, request, pk=None, project_pk=None, celery_task_id=None):
        self.get_and_check_task(request, pk)

        res = TestSafeAsyncResult(celery_task_id)

        if not res.ready():
            out = {'ready': False, 'status': res.state, 'progress': 0}
            if res.state == 'PROGRESS' and res.info:
                out['status']   = res.info.get('status', res.state)
                out['progress'] = res.info.get('progress', 0)
            return Response(out)

        try:
            result = res.get()
        except Exception as e:
            return Response({'ready': True, 'status': 'FAILURE', 'error': str(e)})

        base = '/api/plugins/realign/project/{}/tasks/{}/export/download'.format(
            project_pk, pk
        )
        output = {}
        if isinstance(result, dict):
            if result.get('laz'):
                output['laz_url'] = base + '/laz/'
            if result.get('glb'):
                output['glb_url'] = base + '/glb/'

        return Response({
            'ready':    True,
            'status':   'SUCCESS',
            'progress': 100,
            'output':   output,
        })


class ExportDownloadView(TaskView):
    def get(self, request, pk=None, project_pk=None, kind=None):
        task = self.get_and_check_task(request, pk)

        filename_map = {
            'laz': 'model_realigned.laz',
            'glb': 'model_realigned.glb',
        }
        if kind not in filename_map:
            return Response({'error': 'Ungültiger Typ.'}, status=status.HTTP_400_BAD_REQUEST)

        file_path = task.assets_path('realigned', filename_map[kind])
        if not os.path.isfile(file_path):
            return Response(
                {'error': '{}-Datei nicht gefunden. Bitte zuerst exportieren.'.format(kind.upper())},
                status=status.HTTP_404_NOT_FOUND,
            )

        from django.http import FileResponse
        return FileResponse(
            open(file_path, 'rb'),
            content_type='application/octet-stream',
            as_attachment=True,
            filename=filename_map[kind],
        )


class ExportFilesStatusView(TaskView):
    """Returns which realigned export files exist + the AUTHORITATIVE applied-state.

    Used by AlignmentModal on mount to restore the LAZ/GLB download buttons after a
    page refresh, and — crucially — to read the applied-state from disk: `applied` is
    DERIVED from the presence of the original backups, not from the stored
    `alignment_transform.applied` flag. The backups exist exactly while the alignment
    is applied (apply creates them, revert renames them away), so the disk IS the truth
    and can never drift from a separately-stored boolean.
    """

    def get(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)
        realigned_dir = task.assets_path('realigned')
        assets_root = task.assets_path()
        ept_backup = os.path.join(assets_root, 'entwine_pointcloud_original')
        glb_backup = os.path.join(assets_root, 'odm_texturing', 'odm_textured_model_geo.original.glb')
        return Response({
            'laz': os.path.isfile(os.path.join(realigned_dir, 'model_realigned.laz')),
            'glb': os.path.isfile(os.path.join(realigned_dir, 'model_realigned.glb')),
            'applied': os.path.isdir(ept_backup) or os.path.isfile(glb_backup),
        })


class ApplyView(TaskView):
    """Triggers the apply Celery task: regenerates EPT and swaps files."""

    def post(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)

        realigned_dir = task.assets_path('realigned')
        realigned_laz = os.path.join(realigned_dir, 'model_realigned.laz')

        if not os.path.isfile(realigned_laz):
            return Response(
                {'error': 'Keine realignte Punktwolke gefunden. '
                          'Bitte zuerst den Export ausführen.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        assets_root = task.assets_path()

        try:
            celery_result = run_function_async(
                _run_apply_task,
                str(task.project_id),
                str(task.id),
                assets_root,
                realigned_dir,
                with_progress=True,
            )
            return Response({'celery_task_id': celery_result.task_id}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ApplyStatusView(TaskView):
    """Polls the apply Celery task state. Mirrors ExportStatusView shape."""

    def get(self, request, pk=None, project_pk=None, celery_task_id=None):
        self.get_and_check_task(request, pk)

        res = TestSafeAsyncResult(celery_task_id)

        if not res.ready():
            out = {'ready': False, 'status': res.state, 'progress': 0}
            if res.state == 'PROGRESS' and res.info:
                out['status']   = res.info.get('status', res.state)
                out['progress'] = res.info.get('progress', 0)
            return Response(out)

        try:
            res.get()
        except Exception as e:
            return Response({'ready': True, 'status': 'FAILURE', 'error': str(e)})

        return Response({
            'ready':    True,
            'status':   'SUCCESS',
            'progress': 100,
        })


class RevertView(TaskView):
    """Restores the original EPT and GLB from backups — atomically via rename.

    The state-changing step (backup → live) is two instant, same-filesystem renames;
    the only potentially slow operation (deleting the discarded realigned octree) is
    deferred to AFTER the live state is already correct, so a request timeout/kill can
    no longer leave the files and the `applied` flag out of sync. `applied` itself is
    derived from backup presence (see ExportFilesStatusView), so the disk IS the truth.
    """

    def post(self, request, pk=None, project_pk=None):
        task = self.get_and_check_task(request, pk)
        assets_root = task.assets_path()

        ept_target = os.path.join(assets_root, 'entwine_pointcloud')
        ept_backup = os.path.join(assets_root, 'entwine_pointcloud_original')
        ept_trash  = ept_target + '.replacing'
        glb_target = os.path.join(assets_root, 'odm_texturing', 'odm_textured_model_geo.glb')
        glb_backup = os.path.join(assets_root, 'odm_texturing', 'odm_textured_model_geo.original.glb')

        if not os.path.isdir(ept_backup) and not os.path.isfile(glb_backup):
            return Response(
                {'error': 'Kein Original-Backup vorhanden. Apply wurde nie ausgeführt.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        import shutil

        try:
            if os.path.isdir(ept_backup):
                # Aktuellen (realignten) Live-Octree per rename beiseite schieben (instant),
                # dann das Original an seine Stelle benennen (instant, atomar).
                if os.path.isdir(ept_trash):
                    shutil.rmtree(ept_trash, ignore_errors=True)
                if os.path.isdir(ept_target):
                    os.rename(ept_target, ept_trash)
                os.rename(ept_backup, ept_target)

            if os.path.isfile(glb_backup):
                if os.path.isfile(glb_target):
                    os.remove(glb_target)
                os.rename(glb_backup, glb_target)

            # Invariante verifizieren: nach dem Restore dürfen KEINE Backups mehr existieren
            # (= Disk-Wahrheit 'nicht angewendet'). Sonst 500, Flag bleibt unangetastet.
            if os.path.isdir(ept_backup) or os.path.isfile(glb_backup):
                raise RuntimeError('Revert unvollständig: Backup nach dem Tausch noch vorhanden.')
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # project_data: alignment_transform.applied = False (nur Cache — autoritativ ist die
        # Disk-Wahrheit). Frame-abhängige Sims (Heatmap/PV) löschen: sie wurden im jetzt
        # verworfenen ausgerichteten Frame berechnet und sind nach dem Frame-Wechsel ungültig.
        try:
            from coreplugins.project_data.models import ProjectEntry
            for entry in ProjectEntry.objects.filter(
                project_id=task.project_id, task_id=task.id,
                entry_type='alignment_transform',
            ):
                data = dict(entry.data or {})
                data['applied'] = False
                entry.data = data
                entry.save(update_fields=['data', 'updated_at'])

            stale = ProjectEntry.objects.filter(
                project_id=task.project_id, task_id=task.id,
                entry_type__in=('heatmap', 'pvsim'),
            ).prefetch_related('attachments')
            for entry in stale:
                for att in entry.attachments.all():
                    try:
                        att.file.delete(save=False)  # Attachment-Datei entfernen (Django löscht sie sonst nicht)
                    except Exception:
                        pass
                entry.delete()  # cascade löscht die Attachment-DB-Zeilen
        except Exception:
            # File-swap already succeeded — don't surface a project_data error.
            pass

        # Verworfenen Octree nachgelagert löschen — der Live-Zustand ist hier bereits korrekt.
        if os.path.isdir(ept_trash):
            shutil.rmtree(ept_trash, ignore_errors=True)

        return Response({'ok': True}, status=status.HTTP_200_OK)
