"""
Roof plane detection via iterative RANSAC on ODM point clouds.
Pure numpy/scipy implementation — no open3d or scikit-learn required.

Point cloud loading is handled entirely by pdal (CLI) which processes LAZ/LAS/PLY
in streaming chunks. Python only ever receives a small spatially-sampled CSV.
"""

import numpy as np


# ── Point cloud loading via pdal ──────────────────────────────────────────────

def get_laz_z_offset(path):
    """
    Read the Z header offset from the original LAZ/LAS file without loading all points.
    pdal strips this offset when converting, so we need to add it back to restored coords.
    """
    import laspy
    with laspy.open(path) as f:
        return float(f.header.offset[2])


def load_pointcloud(path, decimation_step=100):
    """
    Load and spatially downsample a point cloud using pdal.
    pdal reads in streaming chunks — Python memory stays minimal regardless of file size.
    Returns float32 (N,3) array with CORRECTED Z coordinates (pdal strips header Z offset).
    """
    import json, subprocess, tempfile, os

    fd_json, json_tmp = tempfile.mkstemp(suffix='.json')
    os.close(fd_json)

    # Write to LAS (binary) — laspy reads it in milliseconds vs. np.loadtxt on CSV
    fd_las,  las_tmp  = tempfile.mkstemp(suffix='.las')
    os.close(fd_las)

    pipeline = {
        "pipeline": [
            path,
            # Truly streaming: no buffering, processes one point at a time.
            # step=100 on 20M points → ~200k output, plenty for RANSAC.
            {"type": "filters.decimation", "step": decimation_step},
            las_tmp,
        ]
    }

    try:
        with open(json_tmp, 'w') as f:
            json.dump(pipeline, f)
        result = subprocess.run(
            ['pdal', 'pipeline', json_tmp],
            capture_output=True, timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(
                f'pdal pipeline failed: {result.stderr.decode(errors="replace")[:500]}'
            )
        import laspy
        las = laspy.read(las_tmp)
        x = np.array(las.x, dtype=np.float64)
        y = np.array(las.y, dtype=np.float64)
        z = np.array(las.z, dtype=np.float64)
        # pdal strips the LAS header Z offset when writing the output file.
        # Re-apply the original offset so coordinates match Potree's measurement space.
        z_offset = get_laz_z_offset(path)
        z += z_offset
        return np.column_stack([x, y, z]).astype(np.float32)
    finally:
        for tmp in [json_tmp, las_tmp]:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ── Voxel downsampling (safety net after pdal load) ───────────────────────────

def voxel_downsample(pts, voxel=0.10):
    """Reduce point cloud to ~1 point per voxel cell using numpy unique."""
    keys = np.floor(pts / voxel).astype(np.int32)
    _, idx = np.unique(keys, axis=0, return_index=True)
    return pts[idx]


# ── RANSAC plane fit ──────────────────────────────────────────────────────────

def fit_plane_ransac(pts, iterations=1000, threshold=0.10, min_inlier_ratio=0.02):
    """
    Fit a plane to pts via RANSAC.
    Returns (normal, d, inlier_mask) or None if not enough inliers.
    Plane equation: normal · p + d = 0
    """
    n = len(pts)
    if n < 10:
        return None

    best_mask = None
    best_count = 0

    rng = np.random.default_rng()   # no fixed seed — each call explores differently
    pts64 = pts.astype(np.float64)

    for _ in range(iterations):
        idx = rng.choice(n, 3, replace=False)
        p0, p1, p2 = pts64[idx]
        v1 = p1 - p0
        v2 = p2 - p0
        normal = np.cross(v1, v2)
        norm_len = np.linalg.norm(normal)
        if norm_len < 1e-9:
            continue
        normal /= norm_len
        d = -float(normal @ p0)

        dist = np.abs(pts64 @ normal + d)
        mask = dist < threshold
        count = int(mask.sum())
        if count > best_count:
            best_count = count
            best_mask = mask

    if best_mask is None or best_count < min_inlier_ratio * n:
        return None

    # Refit on inliers via SVD for a more stable normal
    inliers = pts64[best_mask]
    centroid = inliers.mean(axis=0)
    _, _, Vt = np.linalg.svd(inliers - centroid, full_matrices=False)
    normal = Vt[-1]
    if normal[2] < 0:
        normal = -normal
    d = -float(normal @ centroid)

    dist = np.abs(pts64 @ normal + d)
    final_mask = dist < threshold

    return normal, d, final_mask


# ── Ground removal ─────────────────────────────────────────────────────────────

def remove_ground(pts, iterations=400, threshold=0.15, max_slope_cos=0.1):
    """Remove the dominant near-horizontal plane (ground)."""
    result = fit_plane_ransac(pts, iterations=iterations, threshold=threshold)
    if result is None:
        return pts
    normal, _, mask = result
    if abs(normal[2]) < max_slope_cos:
        return pts
    return pts[~mask]


# ── Iterative plane detection ──────────────────────────────────────────────────

class Plane:
    def __init__(self, normal, d, points):
        self.normal = normal
        self.d = d
        self.points = points
        self.center = points.mean(axis=0).astype(np.float64)


def detect_planes(pts, n_planes=15, iterations=500, threshold=0.10,
                  min_inlier_ratio=0.02, stop_fraction=0.05):
    remaining = pts.copy()
    total = len(pts)
    planes = []

    for _ in range(n_planes):
        if len(remaining) < total * stop_fraction:
            break
        result = fit_plane_ransac(remaining, iterations=iterations,
                                  threshold=threshold, min_inlier_ratio=min_inlier_ratio)
        if result is None:
            break
        normal, d, mask = result
        planes.append(Plane(normal, d, remaining[mask]))
        remaining = remaining[~mask]

    return planes


# ── Intersection line computation ─────────────────────────────────────────────

def _plane_intersection_line(plane_a, plane_b):
    n1, d1 = plane_a.normal, plane_a.d
    n2, d2 = plane_b.normal, plane_b.d
    direction = np.cross(n1, n2)
    dir_len = np.linalg.norm(direction)
    if dir_len < 1e-6:
        return None
    direction /= dir_len
    A = np.array([n1, n2, direction])
    b = np.array([-d1, -d2, 0.0])
    try:
        point = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None
    return point, direction


def _project_onto_line(pts, origin, direction):
    return (pts.astype(np.float64) - origin) @ direction


def _clip_line_to_inliers(origin, direction, pts_a, pts_b, margin=1):
    t_a = _project_onto_line(pts_a, origin, direction)
    t_b = _project_onto_line(pts_b, origin, direction)
    t_all = np.concatenate([t_a, t_b])
    p5, p95 = np.percentile(t_all, [5, 95])
    if (p95 - p5) < 0.3:
        return None
    start = origin + direction * (p5 - margin)
    end   = origin + direction * (p95 + margin)
    return start.tolist(), end.tolist()


def _classify_edge(plane_a, plane_b, start, end):
    cos_angle = abs(float(np.dot(plane_a.normal, plane_b.normal)))
    mid_z = (start[2] + end[2]) / 2.0
    top_z = max(plane_a.center[2], plane_b.center[2])
    bot_z = min(plane_a.center[2], plane_b.center[2])
    relative_z = (mid_z - bot_z) / max(top_z - bot_z, 0.1)
    if relative_z < 0.35:
        return 'valley'
    if cos_angle < 0.35 and relative_z > 0.55:
        return 'ridge'
    return 'hip'


def _bbox_gap_2d(pts_a, pts_b):
    """XY bounding-box gap between two point clouds. 0 if they overlap."""
    dx = max(float(pts_a[:, 0].min() - pts_b[:, 0].max()),
             float(pts_b[:, 0].min() - pts_a[:, 0].max()), 0.0)
    dy = max(float(pts_a[:, 1].min() - pts_b[:, 1].max()),
             float(pts_b[:, 1].min() - pts_a[:, 1].max()), 0.0)
    return float(np.sqrt(dx * dx + dy * dy))


def compute_edges(planes, normal_z_max=0.9848, margin=1.0, parallel_cos=0.97, max_gap=5.0):
    edges = []
    n = len(planes)
    # Only consider inclined planes (roof faces, not flat ground/ceiling remnants)
    roof_planes = [p for p in planes if abs(p.normal[2]) < normal_z_max]
    n = len(roof_planes)
    for i in range(n):
        for j in range(i + 1, n):
            pa, pb = roof_planes[i], roof_planes[j]
            # Skip near-parallel planes — likely the same physical surface split by RANSAC
            if abs(float(np.dot(pa.normal, pb.normal))) > parallel_cos:
                continue
            # Skip non-adjacent planes — their intersection would float through empty space
            if _bbox_gap_2d(pa.points, pb.points) > max_gap:
                continue
            result = _plane_intersection_line(pa, pb)
            if result is None:
                continue
            origin, direction = result
            clipped = _clip_line_to_inliers(origin, direction, pa.points, pb.points, margin=margin)
            if clipped is None:
                continue
            start, end = clipped
            edge_type = _classify_edge(pa, pb, start, end)
            edges.append({'start': start, 'end': end, 'type': edge_type})
    return edges


# ── point2cad helpers ─────────────────────────────────────────────────────────

def export_xyzc(planes, path):
    """Write RANSAC planes as .xyzc (point2cad input). Returns coordinate stats."""
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pts_all = np.vstack([p.points for p in planes])
    ids_all = np.concatenate([np.full(len(p.points), i) for i, p in enumerate(planes)])
    np.savetxt(path, np.column_stack([pts_all.astype(np.float64), ids_all]), fmt='%.6f %.6f %.6f %d')
    return {
        'centroid': pts_all.mean(axis=0).tolist(),
        'scale': float((pts_all.max(0) - pts_all.min(0)).max()),
        'n_planes': len(planes),
        'n_points': int(len(pts_all)),
    }


def make_preview_points(planes, step=15):
    """Return downsampled coloured point cloud normalised to [-1,1] for Three.js."""
    pts_list, ids_list = [], []
    for i, plane in enumerate(planes):
        pts = plane.points[::step]
        pts_list.append(pts)
        ids_list.append(np.full(len(pts), i, dtype=np.int32))
    if not pts_list:
        return {'positions': [], 'plane_ids': [], 'plane_count': 0}
    pts_all  = np.vstack(pts_list).astype(np.float64)
    ids_all  = np.concatenate(ids_list)
    centroid = pts_all.mean(axis=0)
    pts_c    = pts_all - centroid
    scale    = float(np.abs(pts_c).max()) or 1.0
    pts_norm = (pts_c / scale).astype(np.float32)
    return {
        'positions':   pts_norm.tolist(),
        'plane_ids':   ids_all.tolist(),
        'plane_count': len(planes),
    }


# ── Main entry point ───────────────────────────────────────────────────────────

def run_detection(laz_path, *, decimation_step=100, voxel_size=0.05,
                  height_percentile=40, n_planes=15, iterations=1000,
                  threshold=0.15, min_inlier_ratio=0.01,
                  normal_z_max=0.9848, margin=1.0,
                  parallel_cos=0.97, max_gap=5.0,
                  xyzc_out_path=None, progress_callback=None):
    def _progress(msg, pct):
        if progress_callback:
            progress_callback(msg, pct)

    _progress('Punktwolke laden (pdal)…', 5)
    pts = load_pointcloud(laz_path, decimation_step=decimation_step)
    n_loaded = len(pts)

    _progress('Downsampling…', 20)
    pts = voxel_downsample(pts, voxel=voxel_size)
    n_voxel = len(pts)

    _progress('Bodenpunkte entfernen…', 30)
    # Remove dominant ground plane first
    pts = remove_ground(pts)

    # Keep only the top 20% of points by elevation.
    # For a house scan: ground fills 70-80% of scan area → top 20% ≈ walls + roof.
    # This is robust against sloped terrain where a fixed +Nm offset fails.
    z_cutoff = float(np.percentile(pts[:, 2], height_percentile))
    pts = pts[pts[:, 2] > z_cutoff]
    n_ground = len(pts)

    _progress('Dachebenen erkennen (RANSAC)…', 40)
    # Lenient parameters: low inlier ratio, more iterations, looser threshold
    planes = detect_planes(pts, n_planes=n_planes, iterations=iterations,
                           threshold=threshold, min_inlier_ratio=min_inlier_ratio)

    _progress('Schnittlinien berechnen…', 80)
    edges = compute_edges(planes, normal_z_max=normal_z_max, margin=margin,
                          parallel_cos=parallel_cos, max_gap=max_gap)

    # Filter spurious edges: midpoint must lie within the Z range of the input points
    # (edges outside this range are mathematical artefacts between non-adjacent planes)
    if len(pts) > 0:
        z_roof_min = float(pts[:, 2].min()) - 1.0
        z_roof_max = float(pts[:, 2].max()) + 2.0
        edges = [e for e in edges
                 if z_roof_min <= (e['start'][2] + e['end'][2]) / 2 <= z_roof_max]

    _progress('Fertig', 100)
    roof_planes = [p for p in planes if abs(p.normal[2]) < normal_z_max]

    xyzc_stats = None
    if xyzc_out_path and roof_planes:
        xyzc_stats = export_xyzc(roof_planes, xyzc_out_path)

    preview = make_preview_points(roof_planes)

    return {
        'edges': edges,
        'plane_count': len(planes),
        'preview_points': preview,
        'xyzc_stats': xyzc_stats,
        'debug': {
            'n_loaded':       n_loaded,
            'n_voxel':        n_voxel,
            'n_after_ground': n_ground,
            'n_roof_planes':  len(roof_planes),
            'normals_z':      [round(float(p.normal[2]), 3) for p in planes],
        },
    }
