from app.plugins import PluginBase, MountPoint
from .api import ExportView, ExportStatusView, ExportDownloadView


class Plugin(PluginBase):
    def include_js_files(self):
        return ['main.js']

    def build_jsx_components(self):
        return ['app.jsx']

    def api_mount_points(self):
        return [
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/export/$',
                ExportView.as_view(),
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/export/download/(?P<kind>laz|glb)/$',
                ExportDownloadView.as_view(),
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/export/(?P<celery_task_id>[^/.]+)/$',
                ExportStatusView.as_view(),
            ),
        ]
