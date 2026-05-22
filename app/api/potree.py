"""WebODM Scene-API für den eingebauten 3D-Viewer (Potree).

Roof-S Multi-Tenant-Patch (2026-05):

Der originale Code schrieb `task.potree_scene` als globalen, geteilten Blob —
alle User auf einem Task haben dieselbe Scene gesehen. Roof-S braucht aber
**per-User-Trennung** für Annotations/Messungen.

Lösung: das `project_data`-Plugin (`coreplugins.project_data.models.ProjectEntry`)
hat den Entry-Type `user_scene`. Wir lesen/schreiben Scene-Daten dort pro
(task, request.user). Optional kann ein Admin via `task_settings`-Entry
`{global: true}` den Task auf den alten geteilten Modus zurückstellen.

Wichtig: `view`, `pointclouds`, `settings` werden — wie vorher — nicht gespeichert
(view ist kosmetisch + per-Session; pointclouds/settings sind Client-State).

Permissions: `view_project` reicht jetzt für Schreibzugriff. Der per-User-Scope
ist serverseitig durchgesetzt — `request.user` bestimmt welcher user_scene-Entry
betroffen ist. Damit kein Cross-User-Datenleck.
"""
from .tasks import TaskNestedView
from rest_framework.response import Response
from rest_framework import exceptions


# Plugin-Constants (Strings statt Import um Plugin-Reihenfolge zu entkoppeln)
USER_SCENE = "user_scene"
TASK_SETTINGS = "task_settings"


def _projectentry_model():
    """Lazy-Import damit `app` ohne das Plugin nicht crashed."""
    from coreplugins.project_data.models import ProjectEntry
    return ProjectEntry


def _is_global_task(task) -> bool:
    """True wenn ein Admin diesen Task explizit als 'global' markiert hat."""
    ProjectEntry = _projectentry_model()
    entry = (ProjectEntry.objects
             .filter(task=task, entry_type=TASK_SETTINGS)
             .order_by('-created_at').first())
    if not entry:
        return False
    return bool((entry.data or {}).get('global', False))


def _user_scene_entry(task, user):
    """Findet den user_scene-Entry für (task, user). Nie auto-create — der
    Caller entscheidet ob ein neuer Entry angelegt werden soll."""
    ProjectEntry = _projectentry_model()
    return (ProjectEntry.objects
            .filter(task=task, entry_type=USER_SCENE, created_by=user)
            .first())


class Scene(TaskNestedView):
    def get(self, request, pk=None, project_pk=None):
        """Liefert die effektive Scene für den anfragenden User.

        - **Global-Task**: Bestand-Scene (`task.potree_scene`), shared.
        - **Eigener user_scene vorhanden**: merge — annotations/measurements/view
          kommen aus dem user_scene-Entry, restliche Felder (classification,
          volumes, profiles, orientedImages, geopackages) aus task.potree_scene.
        - **Andere User haben user_scenes, ich nicht**: Isolation — leere
          annotations + measurements, sonstige Felder aus task.potree_scene.
          Damit kein alex'-Bestand-Datenleak an daro nach der Migration.
        - **Niemand hat user_scene** (pre-migration / fresh task): Legacy
          shared-Fallback auf task.potree_scene.
        """
        task = self.get_and_check_task(request, pk)
        base = dict(task.potree_scene or {})

        if _is_global_task(task):
            return Response(base)

        if not request.user or not request.user.is_authenticated:
            return Response(base)

        ProjectEntry = _projectentry_model()
        own = ProjectEntry.objects.filter(
            task=task, entry_type=USER_SCENE, created_by=request.user,
        ).first()
        if own is not None:
            merged = base
            for k, v in (own.data or {}).items():
                merged[k] = v
            return Response(merged)

        others_exist = ProjectEntry.objects.filter(
            task=task, entry_type=USER_SCENE,
        ).exclude(created_by=request.user).exists()
        if others_exist:
            # Isolation: leere annotations/measurements, behalte Base-Daten
            merged = base
            merged['annotations'] = []
            merged['measurements'] = []
            return Response(merged)

        # Legacy-Fallback bis zur Migration
        return Response(base)

    def post(self, request, pk=None, project_pk=None):
        """Speichert Scene-Daten.

        - Global-Task: schreibt nach `task.potree_scene` (alter Pfad).
        - Per-User: schreibt in den user_scene-Entry des request.user
          (create wenn nicht vorhanden, sonst update).

        Permission: `view_project` reicht, weil pro User isoliert geschrieben
        wird. Cross-User-Edits sind nicht möglich (Server bestimmt created_by).
        """
        task = self.get_and_check_task(request, pk)
        scene = request.data

        # Quick type check (wie im Original-Code)
        if scene.get('type') != 'Potree':
            raise exceptions.ValidationError(detail="Invalid potree scene")

        # Welche Keys werden persistiert? `view`, `pointclouds`, `settings`
        # bleiben weg — die sind Client-State (per-Session-View, etc.).
        skip = {"view", "pointclouds", "settings"}

        if _is_global_task(task):
            for k in scene:
                if k not in skip:
                    task.potree_scene[k] = scene[k]
            task.save()
            return Response({'success': True})

        # Per-User-Pfad: User muss authentifiziert sein (Plugin-Endpoints haben
        # IsAuthenticated; TaskNestedView ist AllowAny mit own permission check).
        if not request.user or not request.user.is_authenticated:
            raise exceptions.NotAuthenticated()

        ProjectEntry = _projectentry_model()
        entry = _user_scene_entry(task, request.user)
        if entry is None:
            entry = ProjectEntry.objects.create(
                project=task.project, task=task,
                entry_type=USER_SCENE, created_by=request.user,
                data={},
            )

        if entry.data is None:
            entry.data = {}
        for k in scene:
            if k not in skip:
                entry.data[k] = scene[k]
        entry.save()
        return Response({'success': True})


class CameraView(TaskNestedView):
    def post(self, request, pk=None, project_pk=None):
        """Speichert die Kamera-View.

        Roof-S: Kamera-View ist per-User (jeder hat seine eigene Sicht).
        Im Global-Modus wird's wie früher in task.potree_scene['view']
        gespeichert.
        """
        task = self.get_and_check_task(request, pk)
        view = request.data
        if not view:
            raise exceptions.ValidationError(detail="view parameter missing")

        if _is_global_task(task):
            if not task.potree_scene:
                task.potree_scene = {'type': 'Potree', 'version': 1.7}
            task.potree_scene['view'] = view
            task.save()
            return Response({'success': True})

        if not request.user or not request.user.is_authenticated:
            raise exceptions.NotAuthenticated()

        ProjectEntry = _projectentry_model()
        entry = _user_scene_entry(task, request.user)
        if entry is None:
            entry = ProjectEntry.objects.create(
                project=task.project, task=task,
                entry_type=USER_SCENE, created_by=request.user,
                data={},
            )
        if entry.data is None:
            entry.data = {}
        entry.data['view'] = view
        entry.save()

        return Response({'success': True})
