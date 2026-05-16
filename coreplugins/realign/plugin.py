from app.plugins import PluginBase, MountPoint
from .api import (
    ExportView, ExportStatusView, ExportDownloadView, ExportFilesStatusView,
    ApplyView, ApplyStatusView, RevertView,
)


class Plugin(PluginBase):
    def include_js_files(self):
        return ['main.js']

    def build_jsx_components(self):
        return ['app.jsx']

    def api_mount_points(self):
        return [
            # ── Export (Phase 2) ──────────────────────────────────────────
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/export/$',
                ExportView.as_view(),
            ),
            # files/ route VOR der generischen status-Route — sonst greift
            # die <celery_task_id>-Regex und "files" wird als Task-ID gelesen.
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/export/files/$',
                ExportFilesStatusView.as_view(),
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/export/download/(?P<kind>laz|glb)/$',
                ExportDownloadView.as_view(),
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/export/(?P<celery_task_id>[^/.]+)/$',
                ExportStatusView.as_view(),
            ),

            # ── Apply / Revert (Roof-S Phase A) ──────────────────────────
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/apply/$',
                ApplyView.as_view(),
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/apply-status/(?P<celery_task_id>[^/.]+)/$',
                ApplyStatusView.as_view(),
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/revert/$',
                RevertView.as_view(),
            ),
        ]
