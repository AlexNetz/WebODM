def _run_export_task(laz_path, glb_path, matrix, output_dir, progress_callback=None):
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
        _transform_glb(glb_path, out_glb, M)
        result['glb'] = out_glb
    else:
        _progress('Kein GLB vorhanden, überspringe…', 90)

    _progress('Fertig', 100)
    return result
