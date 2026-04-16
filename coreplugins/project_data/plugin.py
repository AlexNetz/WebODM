from app.plugins import PluginBase, MountPoint
from .api import EntryListView, EntryDetailView, AttachView, AttachmentDownloadView


class Plugin(PluginBase):
    def api_mount_points(self):
        return [
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/entries/$',
                EntryListView.as_view()
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/entries/(?P<entry_pk>[^/.]+)/$',
                EntryDetailView.as_view()
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/entries/(?P<entry_pk>[^/.]+)/attach/$',
                AttachView.as_view()
            ),
            MountPoint(
                'project/(?P<project_pk>[^/.]+)/entries/(?P<entry_pk>[^/.]+)/attachments/(?P<filename>[^/]+)/$',
                AttachmentDownloadView.as_view()
            ),
        ]
