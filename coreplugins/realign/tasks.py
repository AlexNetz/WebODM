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
        """Transform GLB mesh vertices/normals/tangents in-place using 4x4 matrix M."""
        import pygltflib
        import numpy as np

        gltf = pygltflib.GLTF2.load(in_path)

        # CESIUM_RTC: bake the center offset into vertex coordinates before applying M.
        # Without this step M acts only on the local-space coords, which are near-zero —
        # the RTC center carries the world-space position, so the result would be wrong.
        rtc_center = None
        if gltf.extensionsUsed and 'CESIUM_RTC' in gltf.extensionsUsed:
            ext_data = (gltf.extensions or {}).get('CESIUM_RTC', {})
            center = ext_data.get('center', [0.0, 0.0, 0.0])
            rtc_center = np.array(center, dtype=np.float64)
            gltf.extensionsUsed = [e for e in gltf.extensionsUsed if e != 'CESIUM_RTC']
            if gltf.extensionsRequired:
                gltf.extensionsRequired = [e for e in gltf.extensionsRequired
                                           if e != 'CESIUM_RTC']
            if gltf.extensions and 'CESIUM_RTC' in gltf.extensions:
                del gltf.extensions['CESIUM_RTC']

        R3 = M[:3, :3]  # rotation part — applied to normals/tangents (no translation)

        raw = gltf.binary_blob()
        if raw is None:
            raise ValueError('GLB hat keinen Binary-Chunk.')
        blob = bytearray(raw)

        for mesh in (gltf.meshes or []):
            for primitive in (mesh.primitives or []):
                attrs = primitive.attributes
                if attrs is None:
                    continue

                for attr_name, accessor_idx in [
                    ('POSITION', getattr(attrs, 'POSITION', None)),
                    ('NORMAL',   getattr(attrs, 'NORMAL',   None)),
                    ('TANGENT',  getattr(attrs, 'TANGENT',  None)),
                ]:
                    if accessor_idx is None:
                        continue

                    acc = gltf.accessors[accessor_idx]
                    bv  = gltf.bufferViews[acc.bufferView]

                    byte_offset = (bv.byteOffset or 0) + (acc.byteOffset or 0)
                    count       = acc.count
                    byte_stride = bv.byteStride  # None = tightly packed

                    if attr_name in ('POSITION', 'NORMAL'):
                        comp       = 3
                        item_bytes = 12   # 3 × float32
                    else:
                        comp       = 4    # TANGENT = vec4 (xyz + handedness w)
                        item_bytes = 16

                    if byte_stride and byte_stride != item_bytes:
                        # Interleaved buffer — read each element individually
                        verts = np.array([
                            np.frombuffer(
                                blob[byte_offset + i * byte_stride:
                                     byte_offset + i * byte_stride + item_bytes],
                                dtype=np.float32,
                            )
                            for i in range(count)
                        ], dtype=np.float32)
                    else:
                        verts = np.frombuffer(
                            blob[byte_offset:byte_offset + count * item_bytes],
                            dtype=np.float32,
                        ).reshape(count, comp).copy()

                    verts = verts.astype(np.float64)

                    if attr_name == 'POSITION':
                        if rtc_center is not None:
                            verts += rtc_center
                        ones   = np.ones((count, 1), dtype=np.float64)
                        result = (M @ np.concatenate([verts, ones], axis=1).T).T[:, :3]
                        # Update bounding box stored in the accessor
                        acc.min = result.min(axis=0).tolist()
                        acc.max = result.max(axis=0).tolist()

                    elif attr_name == 'NORMAL':
                        result = (R3 @ verts.T).T
                        lens   = np.linalg.norm(result, axis=1, keepdims=True)
                        lens[lens < 1e-10] = 1.0
                        result = result / lens

                    else:  # TANGENT
                        r_xyz  = (R3 @ verts[:, :3].T).T
                        lens   = np.linalg.norm(r_xyz, axis=1, keepdims=True)
                        lens[lens < 1e-10] = 1.0
                        result = np.concatenate([r_xyz / lens, verts[:, 3:4]], axis=1)

                    new_bytes = result.astype(np.float32).tobytes()

                    if byte_stride and byte_stride != item_bytes:
                        for i in range(count):
                            s = byte_offset + i * byte_stride
                            blob[s:s + item_bytes] = new_bytes[i * item_bytes:(i + 1) * item_bytes]
                    else:
                        blob[byte_offset:byte_offset + len(new_bytes)] = new_bytes

        gltf.set_binary_blob(bytes(blob))
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
