"""
Eingabe-GLB für den Realign-Export (reine Stdlib, ohne Django — separat testbar).

Bei angewendeter Ausrichtung trägt das aktuelle GLB (odm_textured_model_geo.glb) die
Ausrichtung bereits als Knoten-Matrix. Ein Export daraus würde die Matrix ein zweites Mal
anwenden (doppelt gekippt). Das Original liegt dann im Backup und ist die richtige Eingabe —
genau so lange, wie die Ausrichtung angewendet ist (Apply legt es an, Revert benennt es zurück).
"""
import os

GLB_BACKUP_REL = ('odm_texturing', 'odm_textured_model_geo.original.glb')


def export_glb_source(assets_root, textured_glb_path):
    """Pfad des GLB, auf das der Export die Matrix anwendet; None ohne GLB."""
    backup = os.path.join(assets_root, *GLB_BACKUP_REL)
    if os.path.isfile(backup):
        return backup
    if textured_glb_path and os.path.isfile(textured_glb_path):
        return textured_glb_path
    return None
