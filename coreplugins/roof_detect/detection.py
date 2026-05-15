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


def load_pointcloud(path, decimation_step=100, clip_bounds=None):
    """
    Load and spatially downsample a point cloud using pdal.
    pdal reads in streaming chunks — Python memory stays minimal regardless of file size.
    Returns float64 (N,3) array with CORRECTED Z coordinates (pdal strips header Z offset).

    float64 is required: float32 quantises UTM-Y (~5.4 million) to 0.5 m steps,
    which collapses wall points onto discrete Y slices and creates RANSAC sub-planes.

    clip_bounds accepts 4 or 6 elements:
      - 4: [xmin, xmax, ymin, ymax] — axis-aligned 2D crop (XY only)
      - 6: [xmin, xmax, ymin, ymax, zmin, zmax] — axis-aligned 3D crop (XYZ)
    Coordinates are expected in the Potree/measurement coordinate system. For Z the
    LAZ header offset is automatically subtracted before passing to pdal, since pdal
    operates in the LAZ native Z (without offset).
    """
    import json, subprocess, tempfile, os

    fd_json, json_tmp = tempfile.mkstemp(suffix='.json')
    os.close(fd_json)

    # Write to LAS (binary) — laspy reads it in milliseconds vs. np.loadtxt on CSV
    fd_las,  las_tmp  = tempfile.mkstemp(suffix='.las')
    os.close(fd_las)

    steps = [path]
    if clip_bounds is not None:
        if len(clip_bounds) == 6:
            xmin, xmax, ymin, ymax, zmin_pot, zmax_pot = clip_bounds
            # pdal arbeitet im LAZ-nativen Z (ohne Header-Offset) — Potree-Z hat den
            # Offset addiert. Vor dem pdal-Crop also abziehen.
            z_offset = get_laz_z_offset(path)
            zmin = zmin_pot - z_offset
            zmax = zmax_pot - z_offset
            steps.append({
                "type":   "filters.crop",
                "bounds": f"([{xmin},{xmax}],[{ymin},{ymax}],[{zmin},{zmax}])",
            })
        elif len(clip_bounds) == 4:
            xmin, xmax, ymin, ymax = clip_bounds
            steps.append({
                "type":   "filters.crop",
                "bounds": f"([{xmin},{xmax}],[{ymin},{ymax}])",
            })
        else:
            raise ValueError(
                f'clip_bounds muss 4 oder 6 Elemente haben, hat {len(clip_bounds)}'
            )
    steps.append({"type": "filters.decimation", "step": decimation_step})
    steps.append(las_tmp)
    pipeline = {"pipeline": steps}

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
        return np.column_stack([x, y, z])  # keep float64 — UTM precision matters
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

def remove_ground(pts, iterations=400, threshold=0.15, max_slope_cos=0.95,
                  max_z_fraction=0.20):
    """Remove the dominant near-horizontal plane only if it lies in the lowest
    portion of the Z range (actual ground, not a low-pitched roof).

    max_slope_cos: only remove planes with |normal_z| >= this value (~18° from horizontal)
    max_z_fraction: only remove if plane centroid is in the bottom X fraction of Z range
    """
    result = fit_plane_ransac(pts, iterations=iterations, threshold=threshold)
    if result is None:
        return pts
    normal, _, mask = result
    if abs(normal[2]) < max_slope_cos:
        return pts  # plane too steep to be ground
    z_min, z_max = float(pts[:, 2].min()), float(pts[:, 2].max())
    z_range = z_max - z_min
    if z_range < 0.5:
        return pts  # degenerate case
    plane_z = float(pts[mask, 2].mean())
    if (plane_z - z_min) / z_range > max_z_fraction:
        return pts  # plane centroid too high — likely a flat roof, not ground
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


def _dbscan_xy(points_xy, eps, min_samples):
    """
    Minimal DBSCAN on 2D points. Returns int labels; -1 = noise.

    Standard DBSCAN: a point is a CORE point if it has ≥ min_samples neighbours
    within eps (incl. itself). Core points propagate their cluster id to all
    points reachable within eps. Border points (reach a core but are not core
    themselves) join that cluster. Isolated points are labelled -1 (noise).

    Density-aware: a single "bridge" point between two dense blobs has too few
    neighbours to be core, so the two blobs stay separate. This is the key
    difference from naive connected-components on r-distance pairs, which would
    glue the blobs together through that one bridge point.
    """
    from scipy.spatial import cKDTree
    n = len(points_xy)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    tree = cKDTree(points_xy)
    neighbors = tree.query_ball_point(points_xy, r=eps)

    is_core = np.fromiter(
        (len(nb) >= min_samples for nb in neighbors),
        dtype=bool, count=n,
    )
    labels = np.full(n, -1, dtype=np.int64)

    cluster_id = 0
    for i in range(n):
        if labels[i] != -1 or not is_core[i]:
            continue
        # BFS from this seed core point — assign cluster_id to everything reachable.
        labels[i] = cluster_id
        queue = list(neighbors[i])
        while queue:
            j = queue.pop()
            if labels[j] != -1:
                continue
            labels[j] = cluster_id
            if is_core[j]:
                queue.extend(neighbors[j])
        cluster_id += 1

    return labels


def split_thin_bridges(planes, bridge_width=0.0, min_component_pts=15):
    """
    Break each plane along thin spatial "bridges" via morphological erosion.

    RANSAC selects inliers purely by mathematical plane distance. A flat-dormer
    plane equation is also satisfied by main-roof points that happen to lie at
    the dormer's Z-level near the ridge top — those bridge points form a thin
    linear streak that connects dormers on opposite roof sides into one plane id.
    DBSCAN can't separate them because the streak is dense enough along its
    length to satisfy min_samples.

    Erosion is the right tool here: rasterize inliers to a 2D occupancy grid,
    erode by bridge_width/2, then label connected components. Any inlier corridor
    narrower than bridge_width disappears; dense blobs survive. After labelling,
    boundary points are reassigned to their nearest surviving component via a
    distance transform so the cluster footprint doesn't shrink.

    bridge_width <= 0 disables this pass entirely (early-return).
    """
    if bridge_width <= 0 or not planes:
        return list(planes)

    from scipy.ndimage import binary_erosion, label, distance_transform_edt

    cell_size = max(0.05, bridge_width / 4.0)
    erode_cells = max(1, int(round(bridge_width / 2.0 / cell_size)))
    structure = np.ones((2 * erode_cells + 1, 2 * erode_cells + 1), dtype=bool)

    out = []
    for plane in planes:
        pts = plane.points
        if len(pts) < min_component_pts:
            # Too few points to bother splitting — keep as-is.
            out.append(plane)
            continue

        xy = pts[:, :2].astype(np.float64)
        xmin, ymin = xy.min(axis=0)
        xmax, ymax = xy.max(axis=0)

        nx = int(np.ceil((xmax - xmin) / cell_size)) + 1
        ny = int(np.ceil((ymax - ymin) / cell_size)) + 1

        if nx <= 2 * erode_cells + 1 or ny <= 2 * erode_cells + 1:
            # Grid smaller than the structuring element → erosion would wipe
            # everything. Bail out cleanly, keep original plane.
            out.append(plane)
            continue

        gx = ((xy[:, 0] - xmin) / cell_size).astype(np.int64)
        gy = ((xy[:, 1] - ymin) / cell_size).astype(np.int64)
        gx = np.clip(gx, 0, nx - 1)
        gy = np.clip(gy, 0, ny - 1)

        grid = np.zeros((nx, ny), dtype=bool)
        grid[gx, gy] = True

        eroded = binary_erosion(grid, structure=structure)
        if not eroded.any():
            # Plane is entirely thin (all features narrower than bridge_width).
            # Don't fragment it — likely a legitimate slender structure.
            out.append(plane)
            continue

        labels_grid, n_labels = label(eroded)
        if n_labels <= 1:
            # One component → no thin bridge detected → no split needed.
            out.append(plane)
            continue

        # Dilate-back: every original grid cell gets the label of the nearest
        # surviving (labelled) cell so boundary points don't get dropped.
        _, (idx_x, idx_y) = distance_transform_edt(
            labels_grid == 0, return_indices=True
        )
        pt_labels = labels_grid[idx_x[gx, gy], idx_y[gx, gy]]

        for cluster_id in range(1, n_labels + 1):
            mask = pt_labels == cluster_id
            if mask.sum() < min_component_pts:
                continue
            out.append(Plane(plane.normal, plane.d, pts[mask]))

    return out


def filter_outlier_context(planes, radius=0.20, min_same_plane_ratio=0.0,
                           min_kept_pts=15):
    """
    Drop inliers whose XY neighbourhood is dominated by points from *other*
    planes — RANSAC selects inliers by plane-equation distance only, so a flat
    dormer plane equation is often satisfied by points belonging physically to
    a completely different roof patch on the far side of the house. Those
    "Verlängerung" points stay in the inlier set of the wrong plane and confuse
    point2cad's INR fitting.

    Algorithm:
      1. Concatenate every plane's points → universe with plane-id labels.
      2. One KDTree query per point (XY) returns its neighbour set.
      3. For each plane Pi inlier: own-share = (# Pi neighbours) / (# neighbours).
         If own-share < min_same_plane_ratio the inlier is a "boundary
         trespasser" and gets removed.

    Universe = union of *current* plane inliers only (NOT the raw point cloud).
    Wall / vegetation / ground points were already discarded upstream; including
    them would punish legitimate plane edges (a roof rim next to a wall has
    many wall neighbours but is otherwise a clean plane point).

    min_same_plane_ratio <= 0 disables this pass entirely (early-return).
    """
    if min_same_plane_ratio <= 0 or len(planes) < 2:
        return list(planes)

    from scipy.spatial import cKDTree

    pts_list = [p.points for p in planes]
    ids_list = [np.full(len(p.points), pid, dtype=np.int32)
                for pid, p in enumerate(planes)]
    universe_pts = np.vstack(pts_list)
    universe_ids = np.concatenate(ids_list)

    tree = cKDTree(universe_pts[:, :2])
    # Batched neighbour lookup — one scipy call for all points.
    all_neighbors = tree.query_ball_point(universe_pts[:, :2], r=radius)

    out = []
    offset = 0
    for pid, plane in enumerate(planes):
        n = len(plane.points)
        keep_mask = np.zeros(n, dtype=bool)

        for local_i in range(n):
            neighbors = all_neighbors[offset + local_i]
            total = len(neighbors)
            if total <= 1:
                # Self only — no context to judge, keep.
                keep_mask[local_i] = True
                continue
            same = int(np.sum(universe_ids[neighbors] == pid))
            if same / total >= min_same_plane_ratio:
                keep_mask[local_i] = True

        offset += n
        kept = int(keep_mask.sum())
        if kept >= min_kept_pts:
            out.append(Plane(plane.normal, plane.d, plane.points[keep_mask]))
        # Else: plane is discarded entirely (too few points survived).

    return out


def split_disjoint_planes(planes, split_gap=1.0, min_cluster_pts=15, min_samples=5):
    """
    Split each Plane into spatially-disjoint sub-planes via 2D-XY DBSCAN.

    RANSAC selects inliers purely by mathematical plane distance, so multiple
    physically-separate roof patches with the same orientation + height end up
    sharing one plane id (e.g. dormers on different sides of a hip roof).
    point2cad accepts those ids as ground truth and fits a single INR surface
    over the disjoint blobs — the result is a degenerate surface that confuses
    the topological clipping.

    DBSCAN over the naive r-neighbour graph: a sparse "bridge" of misclassified
    RANSAC-inlier points between two dense roof patches has <min_samples nearby
    neighbours → it is treated as noise instead of merging the patches. The
    dense patches stay separate. Sub-clusters with <min_cluster_pts points are
    dropped after labelling.

    eps = split_gap. min_samples=5 by default — matches DBSCAN's typical 2D
    setting. Increase if RANSAC produces lots of noise pairs in dense regions.
    """
    out = []
    for plane in planes:
        n = len(plane.points)
        if n < min_cluster_pts:
            continue

        xy = plane.points[:, :2].astype(np.float64)
        labels = _dbscan_xy(xy, eps=split_gap, min_samples=min_samples)

        for cluster_id in np.unique(labels):
            if cluster_id == -1:
                continue  # noise points — discard
            idxs = np.where(labels == cluster_id)[0]
            if len(idxs) < min_cluster_pts:
                continue
            sub_points = plane.points[idxs]
            # Same mathematical plane (normal, d) — only the inlier set differs.
            out.append(Plane(plane.normal, plane.d, sub_points))

    return out


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


def compute_edges(planes, min_normal_z=0.10, margin=1.0, parallel_cos=0.97, max_gap=5.0):
    edges = []
    n = len(planes)
    # Wand-Filter: nur Flächen mit ausreichend großer Vertikalkomponente betrachten.
    # |n.z| > min_normal_z → schließt nur senkrechte Wände aus, Schräg- UND Flachdächer
    # bleiben drin (Default 0.10 ≈ Flächen flacher als 84° passieren).
    roof_planes = [p for p in planes if abs(p.normal[2]) > min_normal_z]
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
    """Write RANSAC planes as .xyzc (point2cad input). Returns coordinate stats.

    Coordinates are centred at the point-cloud centroid before writing so that
    point2cad (and its output PLY / topo.json) works in a local frame near the
    origin.  The original centroid is returned so callers can georeference back.
    """
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pts_all = np.vstack([p.points for p in planes])
    ids_all = np.concatenate([np.full(len(p.points), i) for i, p in enumerate(planes)])
    centroid = pts_all.mean(axis=0)
    pts_centered = pts_all - centroid          # local coords, ~0-centred
    np.savetxt(path, np.column_stack([pts_centered.astype(np.float64), ids_all]), fmt='%.6f %.6f %.6f %d')
    return {
        'centroid': centroid.tolist(),         # UTM origin for back-projection
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
                  min_normal_z=0.10, margin=1.0,
                  parallel_cos=0.97, max_gap=5.0,
                  split_gap=1.0, min_cluster_pts=15,
                  bridge_width=0.0,
                  min_same_plane_ratio=0.0,
                  xyzc_out_path=None, clip_bounds=None, progress_callback=None):
    def _progress(msg, pct):
        if progress_callback:
            progress_callback(msg, pct)

    _progress('Punktwolke laden (pdal)…', 5)
    pts = load_pointcloud(laz_path, decimation_step=decimation_step, clip_bounds=clip_bounds)
    n_loaded = len(pts)
    # 6-Element-Bounds bedeuten: User hat per Volume-Box explizit eine 3D-AABB
    # festgelegt → Höhenfilter und Boden-Erkennung werden übersprungen, da der
    # User die Z-Range bewusst gewählt hat.
    has_z_clip = clip_bounds is not None and len(clip_bounds) == 6
    if clip_bounds:
        import logging
        logging.getLogger(__name__).info(
            f'[roof_detect] Clip bounds ({len(clip_bounds)}D): '
            f'{[round(v,1) for v in clip_bounds]}, '
            f'points after crop+decimation: {n_loaded}'
        )

    _progress('Downsampling…', 20)
    pts = voxel_downsample(pts, voxel=voxel_size)
    n_voxel = len(pts)

    if has_z_clip:
        # User hat Z-Range explizit per Volume-Box gesetzt → kein automatisches
        # Bodenpunkt-Entfernen und kein Perzentil-Höhenfilter mehr.
        n_ground = len(pts)
    else:
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
    edges = compute_edges(planes, min_normal_z=min_normal_z, margin=margin,
                          parallel_cos=parallel_cos, max_gap=max_gap)

    # Filter spurious edges: midpoint must lie within the Z range of the input points
    # (edges outside this range are mathematical artefacts between non-adjacent planes)
    if len(pts) > 0:
        z_roof_min = float(pts[:, 2].min()) - 1.0
        z_roof_max = float(pts[:, 2].max()) + 2.0
        edges = [e for e in edges
                 if z_roof_min <= (e['start'][2] + e['end'][2]) / 2 <= z_roof_max]

    _progress('Fertig', 100)
    # Wand-Filter (siehe compute_edges): nur Flächen mit |n.z| > min_normal_z behalten.
    roof_planes = [p for p in planes if abs(p.normal[2]) > min_normal_z]

    # 1. Drop planes with very few points (wall fragments, vegetation artefacts).
    if roof_planes:
        max_pts = max(len(p.points) for p in roof_planes)
        min_pts = max(50, int(max_pts * 0.01))
        roof_planes = [p for p in roof_planes if len(p.points) >= min_pts]

    # 1b. Split spatially-disjoint sub-clusters (e.g. dormers on different sides
    #     of a hip roof that RANSAC lumped into one plane). Each sub-cluster
    #     becomes its own Plane object with its own id so point2cad fits a
    #     separate INR surface per physical roof patch.
    if roof_planes:
        roof_planes = split_disjoint_planes(
            roof_planes, split_gap=split_gap, min_cluster_pts=min_cluster_pts,
        )

    # 1c. Break thin-bridge artefacts where RANSAC plane equations are satisfied
    #     by a narrow streak of points crossing the main roof (e.g. ridge cap
    #     points sharing the flat-dormer plane's Z-level). Morphological erosion
    #     removes corridors narrower than bridge_width; disabled at 0.0.
    if roof_planes and bridge_width > 0:
        roof_planes = split_thin_bridges(
            roof_planes, bridge_width=bridge_width, min_component_pts=min_cluster_pts,
        )

    # 2. Keep only the spatially connected component that contains the largest plane.
    #    Two planes are "connected" if their XY bounding boxes are within max_gap of
    #    each other — this handles L-shaped and multi-section roofs correctly.
    if len(roof_planes) > 1:
        n = len(roof_planes)
        adj = [[False] * n for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                gap = _bbox_gap_2d(roof_planes[i].points, roof_planes[j].points)
                adj[i][j] = adj[j][i] = gap <= max_gap
        start = max(range(n), key=lambda i: len(roof_planes[i].points))
        visited = [False] * n
        visited[start] = True
        queue = [start]
        while queue:
            curr = queue.pop(0)
            for nb in range(n):
                if not visited[nb] and adj[curr][nb]:
                    visited[nb] = True
                    queue.append(nb)
        roof_planes = [roof_planes[i] for i in range(n) if visited[i]]

    # 3. Outlier-context cleanup: drop inliers whose XY neighbourhood is
    #    dominated by points from OTHER planes. Removes "Verlängerungs"-lobes
    #    where a flat plane equation happens to be satisfied in a region that
    #    physically belongs to a different roof patch. Disabled at 0.0.
    if roof_planes and min_same_plane_ratio > 0:
        roof_planes = filter_outlier_context(
            roof_planes,
            radius=0.20,
            min_same_plane_ratio=min_same_plane_ratio,
            min_kept_pts=min_cluster_pts,
        )

    # For point2cad: only export sloped surfaces (exclude near-vertical walls).
    # Walls (|normal_z| < 0.05, within ~3° of vertical) bloat the surface count
    # and worsen GPU memory fragmentation in point2cad.
    cad_planes = [p for p in roof_planes if abs(p.normal[2]) > 0.05]

    xyzc_stats = None
    if xyzc_out_path and cad_planes:
        xyzc_stats = export_xyzc(cad_planes, xyzc_out_path)

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
