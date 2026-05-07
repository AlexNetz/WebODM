"""
Roof plane detection via iterative RANSAC on ODM point clouds.
Pure numpy/scipy implementation — no open3d or scikit-learn required.
"""

import numpy as np


# ── Point cloud loading ────────────────────────────────────────────────────────

def load_pointcloud(path):
    """
    Load XYZ from PLY or LAS/LAZ file. Returns float32 (N,3) array.
    PLY is preferred — no compression backend required.
    """
    if path.lower().endswith('.ply'):
        return _load_ply(path)
    return _load_las(path)


def _load_ply(path):
    """Read XYZ from a PLY point cloud via plyfile."""
    from plyfile import PlyData
    ply = PlyData.read(path)
    v = ply['vertex']
    x = np.array(v['x'], dtype=np.float64)
    y = np.array(v['y'], dtype=np.float64)
    z = np.array(v['z'], dtype=np.float64)
    return np.column_stack([x, y, z]).astype(np.float32)


def _load_las(path):
    """Read XYZ from LAS/LAZ file via laspy (requires lazrs backend for .laz)."""
    import laspy
    las = laspy.read(path)
    x = np.array(las.x, dtype=np.float64)
    y = np.array(las.y, dtype=np.float64)
    z = np.array(las.z, dtype=np.float64)
    return np.column_stack([x, y, z]).astype(np.float32)


# ── Voxel downsampling ─────────────────────────────────────────────────────────

def voxel_downsample(pts, voxel=0.15):
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

    rng = np.random.default_rng(42)

    for _ in range(iterations):
        idx = rng.choice(n, 3, replace=False)
        p0, p1, p2 = pts[idx]
        v1 = p1 - p0
        v2 = p2 - p0
        normal = np.cross(v1.astype(np.float64), v2.astype(np.float64))
        norm_len = np.linalg.norm(normal)
        if norm_len < 1e-9:
            continue
        normal /= norm_len
        d = -float(normal @ p0.astype(np.float64))

        dist = np.abs((pts.astype(np.float64) @ normal) + d)
        mask = dist < threshold
        count = mask.sum()
        if count > best_count:
            best_count = count
            best_mask = mask

    if best_mask is None or best_count < min_inlier_ratio * n:
        return None

    # Refit on inliers via SVD for a more stable normal
    inliers = pts[best_mask].astype(np.float64)
    centroid = inliers.mean(axis=0)
    _, _, Vt = np.linalg.svd(inliers - centroid)
    normal = Vt[-1]
    if normal[2] < 0:
        normal = -normal
    d = -float(normal @ centroid)

    dist = np.abs((pts.astype(np.float64) @ normal) + d)
    final_mask = dist < threshold

    return normal, d, final_mask


# ── Ground removal ─────────────────────────────────────────────────────────────

def remove_ground(pts, iterations=300, threshold=0.15, max_slope_cos=0.26):
    """
    Remove the dominant near-horizontal plane (ground/roof-base).
    max_slope_cos = cos(75°) ≈ 0.26 — only remove planes with normal nearly vertical.
    """
    result = fit_plane_ransac(pts, iterations=iterations, threshold=threshold)
    if result is None:
        return pts
    normal, _, mask = result
    # Only remove if the plane is roughly horizontal (normal close to Z-axis)
    if abs(normal[2]) < max_slope_cos:
        return pts
    return pts[~mask]


# ── Iterative plane detection ──────────────────────────────────────────────────

class Plane:
    def __init__(self, normal, d, points):
        self.normal = normal          # unit normal (float64)
        self.d = d                    # offset
        self.points = points          # inlier points (float32)
        self.center = points.mean(axis=0).astype(np.float64)


def detect_planes(pts, n_planes=12, iterations=500, threshold=0.08, min_inlier_ratio=0.04, stop_fraction=0.05):
    """
    Iteratively fit and remove planes until fewer than stop_fraction of original points remain.
    Returns list of Plane objects.
    """
    remaining = pts.copy()
    total = len(pts)
    planes = []

    for _ in range(n_planes):
        if len(remaining) < total * stop_fraction:
            break
        result = fit_plane_ransac(remaining, iterations=iterations, threshold=threshold,
                                  min_inlier_ratio=min_inlier_ratio)
        if result is None:
            break
        normal, d, mask = result
        # Skip near-horizontal planes (likely remaining ground)
        if abs(normal[2]) > 0.94:  # cos(20°)
            remaining = remaining[~mask]
            continue
        planes.append(Plane(normal, d, remaining[mask]))
        remaining = remaining[~mask]

    return planes


# ── Intersection line computation ─────────────────────────────────────────────

def _plane_intersection_line(plane_a, plane_b):
    """
    Compute the 3D intersection line of two planes.
    Returns (point_on_line, direction) or None if planes are parallel.
    """
    n1, d1 = plane_a.normal, plane_a.d
    n2, d2 = plane_b.normal, plane_b.d

    direction = np.cross(n1, n2)
    dir_len = np.linalg.norm(direction)
    if dir_len < 1e-6:
        return None
    direction /= dir_len

    # Find a point on the line: solve n1·p = -d1, n2·p = -d2, direction·p = 0
    A = np.array([n1, n2, direction])
    b = np.array([-d1, -d2, 0.0])
    try:
        point = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None

    return point, direction


def _project_onto_line(pts, origin, direction):
    """Project 3D points onto a line, return scalar parameters t."""
    vecs = pts.astype(np.float64) - origin
    return vecs @ direction


def _clip_line_to_inliers(origin, direction, pts_a, pts_b, margin=0.5):
    """
    Clip intersection line to the combined range of inlier projections.
    Returns (start_3d, end_3d) or None if range is too small.
    """
    t_a = _project_onto_line(pts_a, origin, direction)
    t_b = _project_onto_line(pts_b, origin, direction)
    t_all = np.concatenate([t_a, t_b])

    p5, p95 = np.percentile(t_all, [5, 95])
    if (p95 - p5) < 0.3:
        return None

    start = origin + direction * (p5 - margin)
    end   = origin + direction * (p95 + margin)
    return start.tolist(), end.tolist()


# ── Edge classification ────────────────────────────────────────────────────────

def _classify_edge(plane_a, plane_b, start, end):
    """
    Classify a roof edge based on the angle between plane normals and line height.
    ridge/First:  top line, symmetric planes
    hip/Grat:     top line, asymmetric planes
    valley/Kehle: low-lying line
    eave/Traufe:  handled separately (bottom edges)
    """
    cos_angle = abs(float(np.dot(plane_a.normal, plane_b.normal)))
    mid_z = (start[2] + end[2]) / 2.0
    top_z = max(plane_a.center[2], plane_b.center[2])
    bot_z = min(plane_a.center[2], plane_b.center[2])
    relative_z = (mid_z - bot_z) / max(top_z - bot_z, 0.1)

    if relative_z < 0.35:
        return 'valley'
    # Ridge: normals point outward symmetrically (dot near 0), line near apex
    if cos_angle < 0.35 and relative_z > 0.55:
        return 'ridge'
    return 'hip'


def compute_edges(planes):
    """
    For each pair of planes: compute intersection line, clip to inliers, classify.
    Returns list of dicts with start, end, type.
    """
    edges = []
    n = len(planes)
    for i in range(n):
        for j in range(i + 1, n):
            pa, pb = planes[i], planes[j]

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
    """
    Full detection pipeline. Returns dict with edges and plane_count.
    progress_callback(status_str, percent) is optional.
    """
    def _progress(msg, pct):
        if progress_callback:
            progress_callback(msg, pct)

    _progress('Punktwolke laden…', 5)
    pts = load_pointcloud(laz_path)

    _progress('Downsampling…', 15)
    pts = voxel_downsample(pts, voxel=0.15)

    _progress('Bodenpunkte entfernen…', 25)
    pts = remove_ground(pts)

    _progress('Dachebenen erkennen (RANSAC)…', 35)
    planes = detect_planes(pts, n_planes=12, iterations=600, threshold=0.08)

    _progress('Schnittlinien berechnen…', 75)
    edges = compute_edges(planes)

    _progress('Fertig', 100)
    return {
        'edges': edges,
        'plane_count': len(planes),
    }
