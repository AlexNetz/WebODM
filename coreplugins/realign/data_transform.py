"""
Reine Rechenfunktionen für den Matrix-Stand und die Umrechnung gespeicherter Punkte.

Nur Standardbibliothek (kein numpy, kein Django), damit die Tests das Modul per Dateipfad laden.
Matrizen sind überall Listen aus 16 Zahlen in column-major-Reihenfolge (three.js Matrix4.toArray()):
Element (Zeile r, Spalte c) liegt an Index c*4 + r, die Verschiebung an 12, 13, 14.
Jede Eingabe mit unerwarteter Form wird unverändert zurückgegeben und löst nie eine Ausnahme aus.
"""
import copy
import json
import math
import os
import tempfile


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_matrix(m):
    return isinstance(m, (list, tuple)) and len(m) == 16 and all(_is_num(v) for v in m)


def _is_point(p):
    return isinstance(p, (list, tuple)) and len(p) == 3 and all(_is_num(v) for v in p)


def multiply(a, b):
    """a·b (column-major)."""
    out = [0.0] * 16
    for col in range(4):
        for row in range(4):
            out[col * 4 + row] = sum(a[k * 4 + row] * b[col * 4 + k] for k in range(4))
    return out


def invert(m):
    """Inverse einer affinen Matrix (letzte Zeile 0 0 0 1)."""
    a, d, g = m[0], m[1], m[2]    # Spalte 0
    b, e, h = m[4], m[5], m[6]    # Spalte 1
    c, f, i = m[8], m[9], m[10]   # Spalte 2
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if det == 0:
        raise ValueError('Matrix ist nicht invertierbar')
    # Inverse der 3x3 (Zeile r, Spalte c der Inversen)
    r00, r01, r02 = (e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det
    r10, r11, r12 = (f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det
    r20, r21, r22 = (d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det
    tx, ty, tz = m[12], m[13], m[14]
    return [
        r00, r10, r20, 0.0,
        r01, r11, r21, 0.0,
        r02, r12, r22, 0.0,
        -(r00 * tx + r01 * ty + r02 * tz),
        -(r10 * tx + r11 * ty + r12 * tz),
        -(r20 * tx + r21 * ty + r22 * tz),
        1.0,
    ]


def apply_point(m, p):
    """Wendet m auf [x, y, z] an; jede andere Form kommt unverändert zurück."""
    if not _is_matrix(m) or not _is_point(p):
        return p
    x, y, z = p
    return [
        m[0] * x + m[4] * y + m[8] * z + m[12],
        m[1] * x + m[5] * y + m[9] * z + m[13],
        m[2] * x + m[6] * y + m[10] * z + m[14],
    ]


def _det3(m):
    return (m[0] * (m[5] * m[10] - m[9] * m[6])
            - m[4] * (m[1] * m[10] - m[9] * m[2])
            + m[8] * (m[1] * m[6] - m[5] * m[2]))


def scale_factor(m):
    """Kubikwurzel aus |det(3x3)|."""
    return abs(_det3(m)) ** (1.0 / 3.0)


def _col_len(m, c):
    return math.sqrt(m[c * 4] ** 2 + m[c * 4 + 1] ** 2 + m[c * 4 + 2] ** 2)


def _col_dot(m, c1, c2):
    return sum(m[c1 * 4 + k] * m[c2 * 4 + k] for k in range(3))


def validate_similarity(m):
    """Prüft auf eine Ähnlichkeitsabbildung (Drehung + Verschiebung + gleichmäßiger Faktor).

    Gibt (ok, fehler) zurück; fehler ist None oder ein kurzer deutscher Text.
    """
    if not _is_matrix(m):
        return False, 'Die Matrix muss aus 16 endlichen Zahlen bestehen.'
    if m[3] != 0 or m[7] != 0 or m[11] != 0 or m[15] != 1:
        return False, 'Die letzte Zeile der Matrix muss 0 0 0 1 sein.'
    lens = [_col_len(m, c) for c in range(3)]
    if min(lens) <= 0:
        return False, 'Die Matrix ist entartet (Spalte der Länge 0).'
    ref = max(lens)
    if any(abs(l - ref) > 1e-6 * ref for l in lens):
        return False, 'Die Matrix skaliert ungleichmäßig (Achsen unterschiedlich lang).'
    for c1, c2 in ((0, 1), (0, 2), (1, 2)):
        if abs(_col_dot(m, c1, c2)) > 1e-6 * ref * ref:
            return False, 'Die Matrix enthält eine Scherung (Achsen nicht senkrecht).'
    if _det3(m) <= 0:
        return False, 'Die Matrix spiegelt das Modell (Determinante nicht positiv).'
    factor = scale_factor(m)
    if factor < 0.01 or factor > 100:
        return False, 'Der Skalierungsfaktor muss zwischen 0,01 und 100 liegen.'
    return True, None


def plan_frame_switch(prev, new, was_applied):
    """Plant den Bezugsrahmen-Wechsel der gespeicherten Punkte.

    prev: Matrix, in deren Rahmen die Daten gerade stehen (None = Rohrahmen / unbekannt)
    new: Matrix des neuen Zustands (None = Revert auf den Rohrahmen)
    was_applied: war vorher schon eine Ausrichtung angewendet?
    Gibt (delta, next_data_matrix) zurück; delta None = nichts umzurechnen.
    """
    if new is None:
        if prev is None:
            return None, None            # Revert ohne Datenstand
        return invert(prev), None        # zurück in den Rohrahmen
    if prev is None:
        if was_applied:
            return None, None            # Altbestand: Datenstand unbekannt, nicht anfassen
        return new, new                  # erstes Anwenden: Rohrahmen -> neu
    return multiply(new, invert(prev)), new


def _points(points, m):
    if not isinstance(points, list):
        return points
    return [apply_point(m, p) for p in points]


def _transform_view(view, m):
    if not isinstance(view, dict):
        return view
    out = dict(view)
    for key in ('position', 'target'):
        if key in out:
            out[key] = apply_point(m, out[key])
    return out


def transform_scene(scene, m):
    """Rechnet Messpunkte, Annotationen und Kamera einer Potree-Szene um; Eingabe bleibt unverändert."""
    if not isinstance(scene, dict):
        return scene
    out = copy.deepcopy(scene)
    if isinstance(out.get('measurements'), list):
        for meas in out['measurements']:
            if isinstance(meas, dict) and 'points' in meas:
                meas['points'] = _points(meas['points'], m)
    if isinstance(out.get('annotations'), list):
        for ann in out['annotations']:
            if isinstance(ann, dict) and 'position' in ann:
                ann['position'] = apply_point(m, ann['position'])
    if 'view' in out:
        out['view'] = _transform_view(out['view'], m)
    return out


def _transform_points_key(data, m):
    out = copy.deepcopy(data)
    if 'points' in out:
        out['points'] = _points(out['points'], m)
    return out


def _transform_roof_geometry(data, m, factor):
    out = copy.deepcopy(data)
    if not isinstance(out.get('elements'), list):
        return out
    for el in out['elements']:
        if not isinstance(el, dict):
            continue
        for key in ('points', 'ridgePoints'):
            if key in el:
                el[key] = _points(el[key], m)
        att = el.get('attika')
        if isinstance(att, dict):
            if 'points' in att:
                att['points'] = _points(att['points'], m)
            if _is_num(att.get('heightM')) and _is_num(factor):
                att['heightM'] = att['heightM'] * factor
    return out


def transform_entry_data(entry_type, data, m, factor):
    """Rechnet die Daten eines Projekt-Eintrags um; None = Typ nicht betroffen."""
    if entry_type == 'user_scene':
        return transform_scene(data, m)
    if entry_type in ('area_cutout', 'crop_region'):
        return _transform_points_key(data, m) if isinstance(data, dict) else copy.deepcopy(data)
    if entry_type == 'roof_geometry':
        return _transform_roof_geometry(data, m, factor) if isinstance(data, dict) else copy.deepcopy(data)
    return None


def load_matrix_file(path):
    """Liest {"matrix": [...16...]}; None bei fehlender, unlesbarer oder ungültiger Datei."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    m = data.get('matrix') if isinstance(data, dict) else None
    if not _is_matrix(m):
        return None
    return [float(v) for v in m]


def save_matrix_file(path, m):
    """Schreibt {"matrix": [...]} atomar (tmp im selben Ordner + os.replace)."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'matrix': [float(v) for v in m]}, f)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
