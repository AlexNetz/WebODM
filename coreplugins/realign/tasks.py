def _run_export_task(laz_path, glb_path, matrix, output_dir, geo_offset=None, progress_callback=None):
    """
    Celery worker function: transforms LAZ + GLB files using the alignment matrix.

    Self-contained (all imports inside) — run_function_async uses inspect.getsource()
    to ship this function's source to the worker. Do not reference module-level names.

    matrix: list of 16 floats, column-major (THREE.js Matrix4.toArray() output).
    glb_path: absolute path to GLB, or None if not available.
    """
    import os
    import json
    import subprocess
    import tempfile
    import numpy as np

    def _progress(msg, pct):
        if progress_callback:
            progress_callback(msg, pct)

    def _transform_glb(in_path, out_path, M):
        """
        Apply 4x4 matrix M to the GLB via a GLTF node-level transformation.

        ODM exports Draco-compressed GLB (KHR_draco_mesh_compression), so accessor
        bufferViews are None and vertex data cannot be modified in-place.
        Instead we embed M as the matrix of each root scene node — any compliant
        GLTF renderer then applies it at draw time, including correct normal
        transformation (inverse-transpose of the rotation component).
        """
        import pygltflib
        import numpy as np

        gltf = pygltflib.GLTF2.load(in_path)

        scene_idx = gltf.scene if gltf.scene is not None else 0
        if not gltf.scenes or scene_idx >= len(gltf.scenes):
            raise ValueError('GLB hat keine gültige Scene.')

        scene_def = gltf.scenes[scene_idx]

        def _trs_to_matrix(node):
            """Compose translation/rotation/scale into a 4×4 numpy matrix."""
            T = np.eye(4, dtype=np.float64)
            if node.translation:
                T[:3, 3] = node.translation
            R = np.eye(4, dtype=np.float64)
            if node.rotation:
                x, y, z, w = node.rotation
                R[:3, :3] = np.array([
                    [1 - 2*(y*y + z*z),     2*(x*y - z*w),     2*(x*z + y*w)],
                    [    2*(x*y + z*w), 1 - 2*(x*x + z*z),     2*(y*z - x*w)],
                    [    2*(x*z - y*w),     2*(y*z + x*w), 1 - 2*(x*x + y*y)],
                ])
            S = np.eye(4, dtype=np.float64)
            if node.scale:
                S[0, 0], S[1, 1], S[2, 2] = node.scale
            return T @ R @ S

        for node_idx in (scene_def.nodes or []):
            node = gltf.nodes[node_idx]

            if node.matrix:
                existing = np.array(node.matrix, dtype=np.float64).reshape(4, 4, order='F')
                combined = M @ existing
            else:
                combined = M @ _trs_to_matrix(node)
                node.translation = None
                node.rotation = None
                node.scale = None

            # GLTF stores matrices column-major
            node.matrix = combined.flatten(order='F').tolist()

        gltf.save(out_path)

    # THREE.js Matrix4.toArray() is column-major → reshape with order='F' to get
    # the correct mathematical 4×4 matrix where M[row, col] = element at (row, col).
    M = np.asarray(matrix, dtype=np.float64).reshape(4, 4, order='F')

    # GLB vertices use LOCAL coordinates: local_xy = UTM_xy - geo_offset, Z is absolute.
    # Our M was computed from UTM pick-coordinates, so we need a local version:
    #   M_local = T(-offset) · M · T(offset)
    # Applied to a local vertex v: M_local @ v = T(-offset) @ M @ (v + offset)
    #                                           = M(UTM_vertex) - offset  (leveled local)
    if geo_offset and (geo_offset[0] != 0.0 or geo_offset[1] != 0.0):
        ox, oy = float(geo_offset[0]), float(geo_offset[1])
        T_pos = np.eye(4, dtype=np.float64)
        T_pos[0, 3] = ox
        T_pos[1, 3] = oy
        T_neg = np.eye(4, dtype=np.float64)
        T_neg[0, 3] = -ox
        T_neg[1, 3] = -oy
        M_local = T_neg @ M @ T_pos
    else:
        M_local = M

    os.makedirs(output_dir, exist_ok=True)

    _progress('Starte LAZ-Export…', 2)

    if not os.path.isfile(laz_path):
        raise FileNotFoundError('LAZ-Datei nicht gefunden: ' + laz_path)

    out_laz = os.path.join(output_dir, 'model_realigned.laz')

    # PDAL filters.transformation expects a row-major 4×4 matrix as 16 space-separated
    # floats.  M.flatten(order='C') gives row-major from our numpy matrix.
    pipeline = {
        'pipeline': [
            laz_path,
            {
                'type': 'filters.transformation',
                'matrix': ' '.join('{:.10f}'.format(v) for v in M.flatten(order='C')),
            },
            {
                'type': 'writers.las',
                'filename': out_laz,
                'compression': 'true',
            },
        ]
    }

    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        json.dump(pipeline, f)
        pipeline_path = f.name
    try:
        subprocess.check_call(['pdal', 'pipeline', pipeline_path])
    finally:
        try:
            os.unlink(pipeline_path)
        except OSError:
            pass

    _progress('LAZ fertig, starte GLB-Export…', 50)

    result = {'laz': out_laz, 'glb': None}

    if glb_path and os.path.isfile(glb_path):
        out_glb = os.path.join(output_dir, 'model_realigned.glb')
        _transform_glb(glb_path, out_glb, M_local)
        result['glb'] = out_glb
    else:
        _progress('Kein GLB vorhanden, überspringe…', 90)

    _progress('Fertig', 100)
    return result


def _run_apply_task(project_id_str, task_id_str, assets_root, realigned_dir, progress_callback=None):
    """
    Apply realigned files persistently:
      1. Regenerate Entwine octree from realigned LAZ
      2. Backup originals (idempotent — only on first apply)
      3. Swap entwine_pointcloud + textured_model.glb in-place
      4. Mark project_data alignment_transform.applied = True

    Pre-condition: ExportView must have been run so model_realigned.laz exists
    in realigned_dir. The API layer (ApplyView) checks this and returns 400
    if missing.

    Self-contained: all imports inside (run_function_async ships source via
    inspect.getsource and eval's it in the worker).
    """
    import os
    import shutil
    import subprocess

    def _progress(msg, pct):
        if progress_callback:
            progress_callback(msg, pct)

    realigned_laz = os.path.join(realigned_dir, 'model_realigned.laz')
    realigned_glb = os.path.join(realigned_dir, 'model_realigned.glb')
    realigned_ept = os.path.join(realigned_dir, 'entwine_pointcloud')

    if not os.path.isfile(realigned_laz):
        raise FileNotFoundError(
            'Realignte Punktwolke nicht gefunden: ' + realigned_laz +
            '. Bitte zuerst den Export ausführen.'
        )

    ept_target     = os.path.join(assets_root, 'entwine_pointcloud')
    ept_backup     = os.path.join(assets_root, 'entwine_pointcloud_original')
    glb_target     = os.path.join(assets_root, 'odm_texturing', 'odm_textured_model_geo.glb')
    glb_backup     = os.path.join(assets_root, 'odm_texturing', 'odm_textured_model_geo.original.glb')

    # 1. Regenerate EPT from realigned LAZ
    _progress('Erzeuge Entwine-Octree…', 5)
    if os.path.isdir(realigned_ept):
        shutil.rmtree(realigned_ept)
    subprocess.check_call([
        'entwine', 'build',
        '-i', realigned_laz,
        '-o', realigned_ept,
    ])

    _progress('Sichere Original-Dateien…', 60)

    # 2. Backups — idempotent.
    # If ept_backup exists, the current ept_target is already a realigned version
    # from a previous apply. Discard it.
    if os.path.isdir(ept_backup):
        if os.path.isdir(ept_target):
            shutil.rmtree(ept_target)
    elif os.path.isdir(ept_target):
        shutil.move(ept_target, ept_backup)

    if os.path.isfile(glb_backup):
        if os.path.isfile(glb_target):
            os.remove(glb_target)
    elif os.path.isfile(glb_target):
        shutil.move(glb_target, glb_backup)

    _progress('Tausche Dateien…', 80)

    # 3. Swap in the realigned versions.
    # EPT: move (avoids double disk usage for potentially large octree).
    # GLB: copy2 — keep the downloadable file in realigned_dir intact.
    shutil.move(realigned_ept, ept_target)
    if os.path.isfile(realigned_glb):
        os.makedirs(os.path.dirname(glb_target), exist_ok=True)
        shutil.copy2(realigned_glb, glb_target)

    # 4. Update project_data alignment_transform.applied = True
    _progress('Markiere als angewendet…', 95)
    from coreplugins.project_data.models import ProjectEntry
    entries = ProjectEntry.objects.filter(
        project_id=project_id_str,
        task_id=task_id_str,
        entry_type='alignment_transform',
    )
    for entry in entries:
        data = dict(entry.data or {})
        data['applied'] = True
        entry.data = data
        entry.save(update_fields=['data', 'updated_at'])

    _progress('Fertig', 100)
    return {'ok': True}
