from app.plugins import PluginBase, MountPoint
from .api import DetectView, DetectStatusView, ResultView, CancelView, CADView, CADStatusView, CADResultView, CADMeshView


class Plugin(PluginBase):
    def api_mount_points(self):
        return [
            # ── RANSAC detection ──────────────────────────────────────────────
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/detect/$',
                DetectView.as_view()
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/detect/result/$',
                ResultView.as_view()
            ),
            # cancel-Route VOR der generischen status-Route (URL-Matching wertet
            # in Reihenfolge aus; status-Pattern könnte sonst greifen wenn pkg
            # geändert wird).
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/detect/(?P<celery_task_id>[^/.]+)/cancel/$',
                CancelView.as_view()
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/detect/(?P<celery_task_id>[^/.]+)/$',
                DetectStatusView.as_view()
            ),
            # ── point2cad ─────────────────────────────────────────────────────
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/cad/$',
                CADView.as_view()
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/cad/result/$',
                CADResultView.as_view()
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/cad/mesh/$',
                CADMeshView.as_view()
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/cad/(?P<celery_task_id>[^/.]+)/cancel/$',
                CancelView.as_view()
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/cad/(?P<celery_task_id>[^/.]+)/$',
                CADStatusView.as_view()
            ),
        ]
