import uuid
from django.contrib.postgres.fields import JSONField
from django.db import models
from django.conf import settings


class ProjectEntry(models.Model):
    ANNOTATION = 'annotation'
    MEASUREMENT = 'measurement'
    IMAGE = 'image'
    TEXT = 'text'
    REPORT = 'report'
    KEYVALUE = 'keyvalue'
    WAYPOINT = 'waypoint'

    ENTRY_TYPES = [
        (ANNOTATION, 'Annotation'),
        (MEASUREMENT, 'Measurement'),
        (IMAGE, 'Image'),
        (TEXT, 'Text'),
        (REPORT, 'Report'),
        (KEYVALUE, 'Key-Value'),
        (WAYPOINT, 'Waypoint'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        'app.Project',
        on_delete=models.CASCADE,
        related_name='plugin_entries'
    )
    task = models.ForeignKey(
        'app.Task',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='plugin_entries'
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    entry_type = models.CharField(max_length=50, choices=ENTRY_TYPES)
    title = models.CharField(max_length=255, blank=True, default='')
    content = models.TextField(blank=True, default='')
    data = JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'app'
        ordering = ['-created_at']

    def __str__(self):
        return '{} – {} ({})'.format(self.entry_type, self.title or str(self.id), self.project_id)


class ProjectEntryAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry = models.ForeignKey(
        ProjectEntry,
        on_delete=models.CASCADE,
        related_name='attachments'
    )
    file = models.FileField(upload_to='plugin_data/attachments/')
    filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'app'
        ordering = ['created_at']

    def __str__(self):
        return self.filename
