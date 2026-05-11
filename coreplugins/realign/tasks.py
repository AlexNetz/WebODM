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
        Transform GLB mesh vertices/normals/tangents using 4x4 matrix M.

        Two-pass strategy for POSITION vertices:
          Pass 1 — bake CESIUM_RTC (x,y only), apply M → world-space coords (float64)
          After pass 1 — compute new centroid from all transformed POSITION vertices
          Pass 2 — subtract centroid (x,y) to get local coords, write float32
          Then set new CESIUM_RTC extension with updated center.

        This keeps the output compatible with WebODM's viewer and Cesium-based viewers:
        both expect large UTM coordinates to be handled via CESIUM_RTC, not stored raw
        in float32 vertices (which would lose precision and confuse camera auto-placement).
        """
        import pygltflib
        import numpy as np

        gltf = pygltflib.GLTF2.load(in_path)

        # Extract and remove old CESIUM_RTC (only x,y used by WebODM viewer)
        rtc_xy = np.zeros(2, dtype=np.float64)
        if gltf.extensionsUsed and 'CESIUM_RTC' in gltf.extensionsUsed:
            ext_data = (gltf.extensions or {}).get('CESIUM_RTC', {})
            center   = ext_data.get('center', [0.0, 0.0, 0.0])
            rtc_xy   = np.array([center[0], center[1]], dtype=np.float64)
            gltf.extensionsUsed = [e for e in gltf.extensionsUsed if e != 'CESIUM_RTC']
            if gltf.extensionsRequired:
                gltf.extensionsRequired = [e for e in gltf.extensionsRequired
                                           if e != 'CESIUM_RTC']
            if gltf.extensions and 'CESIUM_RTC' in gltf.extensions:
                del gltf.extensions['CESIUM_RTC']

        R3  = M[:3, :3]  # rotation part — applied to normals/tangents
        raw = gltf.binary_blob()
        if raw is None:
            raise ValueError('GLB hat keinen Binary-Chunk.')
        blob = bytearray(raw)

        def _read_verts(byte_offset, count, comp, item_bytes, byte_stride):
            if byte_stride and byte_stride != item_bytes:
                return np.array([
                    np.frombuffer(
                        blob[byte_offset + i * byte_stride:
                             byte_offset + i * byte_stride + item_bytes],
                        dtype=np.float32,
                    )
                    for i in range(count)
                ], dtype=np.float32)
            return np.frombuffer(
                blob[byte_offset:byte_offset + count * item_bytes],
                dtype=np.float32,
            ).reshape(count, comp).copy()

        def _write_verts(result_f32, byte_offset, count, item_bytes, byte_stride):
            new_bytes = result_f32.tobytes()
            if byte_stride and byte_stride != item_bytes:
                for i in range(count):
                    s = byte_offset + i * byte_stride
                    blob[s:s + item_bytes] = new_bytes[i * item_bytes:(i + 1) * item_bytes]
            else:
                blob[byte_offset:byte_offset + len(new_bytes)] = new_bytes

        # Pass 1: transform all attributes; collect world-space POSITION arrays
        pos_records = []  # (acc, byte_offset, count, item_bytes, byte_stride, world_verts_f64)

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
                    if acc.bufferView is None:
                        continue  # sparse accessor — skip
                    bv = gltf.bufferViews[acc.bufferView]

                    byte_offset = (bv.byteOffset or 0) + (acc.byteOffset or 0)
                    count       = acc.count
                    byte_stride = bv.byteStride
                    comp        = 3 if attr_name in ('POSITION', 'NORMAL') else 4
                    item_bytes  = comp * 4

                    verts = _read_verts(byte_offset, count, comp, item_bytes, byte_stride).astype(np.float64)

                    if attr_name == 'POSITION':
                        verts[:, 0] += rtc_xy[0]   # bake RTC x
                        verts[:, 1] += rtc_xy[1]   # bake RTC y (z stays absolute)
                        ones   = np.ones((count, 1), dtype=np.float64)
                        result = (M @ np.concatenate([verts, ones], axis=1).T).T[:, :3]
                        pos_records.append((acc, byte_offset, count, item_bytes, byte_stride, result))
                        # (written in pass 2 after centroid is known)

                    elif attr_name == 'NORMAL':
                        result = (R3 @ verts.T).T
                        lens   = np.linalg.norm(result, axis=1, keepdims=True)
                        lens[lens < 1e-10] = 1.0
                        result /= lens
                        _write_verts(result.astype(np.float32), byte_offset, count, item_bytes, byte_stride)

                    else:  # TANGENT
                        r_xyz = (R3 @ verts[:, :3].T).T
                        lens  = np.linalg.norm(r_xyz, axis=1, keepdims=True)
                        lens[lens < 1e-10] = 1.0
                        result = np.concatenate([r_xyz / lens, verts[:, 3:4]], axis=1)
                        _write_verts(result.astype(np.float32), byte_offset, count, item_bytes, byte_stride)

        # Compute new centroid from all transformed POSITION vertices
        if pos_records:
            all_x = np.concatenate([r[5][:, 0] for r in pos_records])
            all_y = np.concatenate([r[5][:, 1] for r in pos_records])
            new_cx = float(np.mean(all_x))
            new_cy = float(np.mean(all_y))

            # Pass 2: subtract centroid (x,y) → local coords, write float32
            for (acc, byte_offset, count, item_bytes, byte_stride, world_verts) in pos_records:
                world_verts[:, 0] -= new_cx
                world_verts[:, 1] -= new_cy
                acc.min = world_verts.min(axis=0).tolist()
                acc.max = world_verts.max(axis=0).tolist()
                _write_verts(world_verts.astype(np.float32), byte_offset, count, item_bytes, byte_stride)

            # Reconstruct CESIUM_RTC with new center (z=0: vertices carry absolute Z)
            if not gltf.extensionsUsed:
                gltf.extensionsUsed = []
            if 'CESIUM_RTC' not in gltf.extensionsUsed:
                gltf.extensionsUsed.append('CESIUM_RTC')
            if not gltf.extensions:
                gltf.extensions = {}
            gltf.extensions['CESIUM_RTC'] = {'center': [new_cx, new_cy, 0.0]}

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
