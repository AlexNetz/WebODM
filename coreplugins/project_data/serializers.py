from rest_framework import serializers
from .models import ProjectEntry, ProjectEntryAttachment


class ProjectEntryAttachmentSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = ProjectEntryAttachment
        fields = ['id', 'filename', 'mime_type', 'url', 'created_at']
        read_only_fields = fields

    def get_url(self, obj):
        request = self.context.get('request')
        # URL to the download endpoint: /api/plugins/project_data/project/{pid}/entries/{eid}/attachments/{filename}/
        entry = obj.entry
        path = 'project/{}/entries/{}/attachments/{}/'.format(
            entry.project_id, entry.id, obj.filename
        )
        if request:
            return request.build_absolute_uri(
                '/api/plugins/project_data/' + path
            )
        return '/api/plugins/project_data/' + path


class ProjectEntrySerializer(serializers.ModelSerializer):
    attachments = ProjectEntryAttachmentSerializer(many=True, read_only=True)
    created_by = serializers.SlugRelatedField(slug_field='username', read_only=True)

    class Meta:
        model = ProjectEntry
        fields = [
            'id', 'project', 'task', 'created_by',
            'entry_type', 'title', 'content', 'data',
            'created_at', 'updated_at', 'attachments',
        ]
        read_only_fields = ['id', 'project', 'created_by', 'created_at', 'updated_at', 'attachments']


class ProjectEntryWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectEntry
        fields = ['entry_type', 'title', 'content', 'data', 'task']

    def validate_entry_type(self, value):
        valid = [t[0] for t in ProjectEntry.ENTRY_TYPES]
        if value not in valid:
            raise serializers.ValidationError(
                'Invalid entry_type. Must be one of: {}'.format(', '.join(valid))
            )
        return value
