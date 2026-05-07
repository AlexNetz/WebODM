from app.plugins import PluginBase, MountPoint
from .api import DetectView, DetectStatusView, ResultView


class Plugin(PluginBase):
    def api_mount_points(self):
        return [
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/detect/$',
                DetectView.as_view()
            ),
            # result/ must come before the celery_task_id catch-all to avoid false match
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/detect/result/$',
                ResultView.as_view()
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/detect/(?P<celery_task_id>[^/.]+)/$',
                DetectStatusView.as_view()
            ),
        ]
