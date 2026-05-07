"""
Roof plane detection via iterative RANSAC on ODM point clouds.
Pure numpy/scipy implementation — no open3d or scikit-learn required.

Point cloud loading is handled entirely by pdal (CLI) which processes LAZ/LAS/PLY
in streaming chunks. Python only ever receives a small spatially-sampled CSV.
"""

import numpy as np


# ── Point cloud loading via pdal ──────────────────────────────────────────────

def load_pointcloud(path):
    """
    Load and spatially downsample a point cloud using pdal.
    pdal reads in streaming chunks — Python memory stays minimal regardless of file size.
    sample_radius=0.12 m yields ~70 pts/m², plenty for RANSAC on roof planes.
    Returns float32 (N,3) array.
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
            {"type": "filters.decimation", "step": 100},
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
        return np.column_stack([x, y, z]).astype(np.float32)
    finally:
        for tmp in [json_tmp, las_tmp]:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ── Voxel downsampling (safety net after pdal load) ───────────────────────────

def voxel_downsample(pts, voxel=0.20):
    """Reduce point cloud to ~1 point per voxel cell using numpy unique."""
    keys = np.floor(pts / voxel).astype(np.int32)
    _, idx = np.unique(keys, axis=0, return_index=True)
    return pts[idx]


# ── RANSAC plane fit ──────────────────────────────────────────────────────────

def fit_plane_ransac(pts, iterations=500, threshold=0.08, min_inlier_ratio=0.04):
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

def remove_ground(pts, iterations=200, threshold=0.15, max_slope_cos=0.26):
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


def detect_planes(pts, n_planes=8, iterations=300, threshold=0.10,
                  min_inlier_ratio=0.04, stop_fraction=0.05):
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


def _clip_line_to_inliers(origin, direction, pts_a, pts_b, margin=0.5):
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


def compute_edges(planes):
    edges = []
    n = len(planes)
    # Only consider inclined planes (roof faces, not flat ground/ceiling remnants)
    roof_planes = [p for p in planes if abs(p.normal[2]) < 0.90]
    n = len(roof_planes)
    for i in range(n):
        for j in range(i + 1, n):
            pa, pb = roof_planes[i], roof_planes[j]
            result = _plane_intersection_line(pa, pb)
            if result is None:
                continue
            origin, direction = result
            clipped = _clip_line_to_inliers(origin, direction, pa.points, pb.points)
            if clipped is None:
                continue
            start, end = clipped
            edge_type = _classify_edge(pa, pb, start, end)
            edges.append({'start': start, 'end': end, 'type': edge_type})
    return edges


# ── Main entry point ───────────────────────────────────────────────────────────

def run_detection(laz_path, progress_callback=None):
    def _progress(msg, pct):
        if progress_callback:
            progress_callback(msg, pct)

    _progress('Punktwolke laden (pdal)…', 5)
    pts = load_pointcloud(laz_path)
    n_loaded = len(pts)

    _progress('Downsampling…', 20)
    pts = voxel_downsample(pts, voxel=0.30)
    n_voxel = len(pts)

    _progress('Bodenpunkte entfernen…', 30)
    pts = remove_ground(pts)
    n_ground = len(pts)

    _progress('Dachebenen erkennen (RANSAC)…', 40)
    # Lenient parameters: low inlier ratio, more iterations, looser threshold
    planes = detect_planes(pts, n_planes=10, iterations=800,
                           threshold=0.15, min_inlier_ratio=0.005)

    _progress('Schnittlinien berechnen…', 80)
    edges = compute_edges(planes)

    _progress('Fertig', 100)
    return {
        'edges': edges,
        'plane_count': len(planes),
        'debug': {
            'n_loaded':  n_loaded,
            'n_voxel':   n_voxel,
            'n_after_ground': n_ground,
        },
    }
