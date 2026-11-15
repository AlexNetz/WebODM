"""
Bezugsrahmen-Zustand der gespeicherten Punkte eines Tasks (Zustandszeile + Umrechnung).

Django-Importe passieren lazy in den Funktionen, damit das Plugin-Laden nicht von der
Ladereihenfolge abhängt.
"""
import uuid

from .data_transform import plan_frame_switch, scale_factor, transform_scene, transform_entry_data, _is_matrix

STATE_TITLE = 'realign_state'
AFFECTED_TYPES = ('user_scene', 'area_cutout', 'crop_region', 'roof_geometry')


def read_state(project_id, task_id):
    """-> {'data_matrix': list | None, 'frame_rev': str | None}"""
    from coreplugins.project_data.models import ProjectEntry

    row = ProjectEntry.objects.filter(
        project_id=project_id, task_id=task_id,
        entry_type='keyvalue', title=STATE_TITLE,
    ).first()
    data = row.data if row is not None and isinstance(row.data, dict) else {}
    matrix = data.get('data_matrix')
    rev = data.get('frame_rev')
    return {
        'data_matrix': [float(v) for v in matrix] if _is_matrix(matrix) else None,
        'frame_rev': rev if isinstance(rev, str) else None,
    }


def switch_data_frame(project_id, task_id, new_matrix, was_applied):
    """Rechnet alle gespeicherten Punkte des Tasks in den neuen Rahmen um.

    Alles in einer Transaktion; ein Fehler rollt komplett zurück und wird nicht verschluckt.
    -> 'transformed' | 'unchanged'
    """
    from django.db import transaction
    from app.models import Task
    from coreplugins.project_data.models import ProjectEntry

    with transaction.atomic():
        # Task-Zeile = Mutex für den ganzen Wechsel (die Zustandszeile existiert evtl. noch nicht).
        task = Task.objects.select_for_update().get(pk=task_id)

        state_row = ProjectEntry.objects.select_for_update().filter(
            project_id=project_id, task_id=task_id,
            entry_type='keyvalue', title=STATE_TITLE,
        ).first()
        sdata = state_row.data if state_row is not None and isinstance(state_row.data, dict) else {}
        prev = sdata.get('data_matrix')
        prev = [float(v) for v in prev] if _is_matrix(prev) else None

        delta, nxt = plan_frame_switch(prev, new_matrix, was_applied)

        if delta is not None:
            factor = scale_factor(delta)
            Task.objects.filter(pk=task_id).update(
                potree_scene=transform_scene(task.potree_scene, delta)
            )
            entries = ProjectEntry.objects.select_for_update().filter(
                project_id=project_id, task_id=task_id,
                entry_type__in=AFFECTED_TYPES,
            )
            for entry in entries:
                new_data = transform_entry_data(entry.entry_type, entry.data, delta, factor)
                if isinstance(new_data, dict):
                    entry.data = new_data
                    entry.save(update_fields=['data', 'updated_at'])

        new_state = {'data_matrix': nxt, 'frame_rev': uuid.uuid4().hex}
        if state_row is not None:
            state_row.data = new_state
            state_row.save(update_fields=['data', 'updated_at'])
        else:
            ProjectEntry.objects.create(
                project_id=project_id, task_id=task_id,
                entry_type='keyvalue', title=STATE_TITLE, data=new_state,
            )

    return 'transformed' if delta is not None else 'unchanged'
