"""
Tests für die reinen Rechenfunktionen (Matrix-Stand, Punkt-Umrechnung; Stdlib, ohne Django).

Lauf: python coreplugins/realign/test_data_transform.py
Das Modul wird per Dateipfad geladen, weil das Paket coreplugins.realign beim Import
das Plugin (und damit Django) lädt.
"""
import copy
import importlib.util
import math
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('data_transform', os.path.join(_HERE, 'data_transform.py'))
dt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dt)

IDENTITY = [1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0]


def scale_about(c, f):
    """T(c)·S(f)·T(-c), column-major: Skalierung um den Drehpunkt c."""
    m = [float(f), 0.0, 0.0, 0.0,
         0.0, float(f), 0.0, 0.0,
         0.0, 0.0, float(f), 0.0,
         c[0] * (1 - f), c[1] * (1 - f), c[2] * (1 - f), 1.0]
    return m


def rotation_z(deg):
    a = math.radians(deg)
    m = list(IDENTITY)
    m[0], m[1], m[4], m[5] = math.cos(a), math.sin(a), -math.sin(a), math.cos(a)
    m[12], m[13], m[14] = 3.0, -2.0, 1.0
    return m


C = [512852.0, 5374507.0, 430.0]  # Drehpunkt in UTM-Größe
S = scale_about(C, 1.5)
S2 = scale_about(C, 1.02)


class DataTransformTest(unittest.TestCase):
    def assertAlmostEqualList(self, a, b, tol=1e-6):
        self.assertEqual(len(a), len(b))
        for x, y in zip(a, b):
            self.assertAlmostEqual(x, y, delta=tol)

    def test_plan_frame_switch(self):
        self.assertEqual(dt.plan_frame_switch(None, None, True), (None, None))  # Revert ohne Datenstand
        self.assertEqual(dt.plan_frame_switch(None, S, True), (None, None))     # Altbestand angewendet
        self.assertEqual(dt.plan_frame_switch(None, S, False), (S, S))          # erstes Anwenden
        delta, nxt = dt.plan_frame_switch(S, None, True)                        # Revert
        self.assertAlmostEqualList(dt.multiply(delta, S), IDENTITY, 1e-6)
        self.assertIsNone(nxt)
        delta, nxt = dt.plan_frame_switch(S, S2, True)                          # erneutes Anwenden
        self.assertAlmostEqualList(delta, dt.multiply(S2, dt.invert(S)), 1e-6)
        self.assertEqual(nxt, S2)

    def test_apply_point_drehpunkt_bleibt_und_abstand_waechst(self):
        self.assertAlmostEqualList(dt.apply_point(S, C), C)
        self.assertAlmostEqualList(dt.apply_point(S, [C[0] + 10, C[1], C[2]]), [C[0] + 15, C[1], C[2]])

    def test_apply_point_hin_und_zurueck(self):
        p = [C[0] + 3.3, C[1] - 7.1, C[2] + 0.4]
        self.assertAlmostEqualList(dt.apply_point(dt.invert(S), dt.apply_point(S, p)), p)
        r = rotation_z(37)
        self.assertAlmostEqualList(dt.apply_point(dt.invert(r), dt.apply_point(r, p)), p)

    def test_apply_point_falsche_form_unveraendert(self):
        for bad in ([1, 2], None, 'abc', [1, 2, 'x'], {'x': 1}, [1, 2, 3, 4], [True, 2, 3]):
            self.assertEqual(dt.apply_point(S, bad), bad)
        self.assertEqual(dt.apply_point(None, [1, 2, 3]), [1, 2, 3])
        self.assertEqual(dt.apply_point([1, 2], [1, 2, 3]), [1, 2, 3])

    def test_multiply_identitaet(self):
        self.assertAlmostEqualList(dt.multiply(S, IDENTITY), S)
        self.assertAlmostEqualList(dt.multiply(S, dt.invert(S)), IDENTITY, 1e-6)

    def test_scale_factor(self):
        self.assertAlmostEqual(dt.scale_factor(S), 1.5, places=9)
        self.assertAlmostEqual(dt.scale_factor(rotation_z(40)), 1.0, places=9)

    def test_validate_similarity_ok(self):
        self.assertEqual(dt.validate_similarity(rotation_z(25)), (True, None))
        self.assertEqual(dt.validate_similarity(S), (True, None))

    def test_validate_similarity_lehnt_ab(self):
        ungleich = list(IDENTITY); ungleich[0] = 2.0
        scherung = list(IDENTITY); scherung[4] = 0.5
        spiegel = list(IDENTITY); spiegel[0] = -1.0
        zu_gross = scale_about(C, 200.0)
        nan = list(IDENTITY); nan[12] = float('nan')
        letzte_zeile = list(IDENTITY); letzte_zeile[3] = 0.1
        for bad in (ungleich, scherung, spiegel, zu_gross, nan, letzte_zeile,
                    IDENTITY[:15], None, 'x', [True] * 16):
            ok, err = dt.validate_similarity(bad)
            self.assertFalse(ok)
            self.assertIsInstance(err, str)
            self.assertTrue(err)

    def test_transform_scene(self):
        scene = {
            'measurements': [{'uuid': 'u1', 'name': 'Fläche', 'points': [[C[0] + 10, C[1], C[2]], [1, 2]], 'extra': 5},
                             {'uuid': 'u2'}, 'kaputt'],
            'annotations': [{'position': [C[0] + 10, C[1], C[2]], 'title': 'A'}],
            'view': {'position': [C[0] + 10, C[1], C[2]], 'target': C, 'fov': 60},
            'fremd': {'a': 1},
        }
        orig = copy.deepcopy(scene)
        out = dt.transform_scene(scene, S)
        self.assertEqual(scene, orig)  # Eingabe unverändert
        m1 = out['measurements'][0]
        self.assertEqual((m1['uuid'], m1['name'], m1['extra']), ('u1', 'Fläche', 5))
        self.assertAlmostEqualList(m1['points'][0], [C[0] + 15, C[1], C[2]])
        self.assertEqual(m1['points'][1], [1, 2])
        self.assertEqual(out['measurements'][1], {'uuid': 'u2'})
        self.assertEqual(out['measurements'][2], 'kaputt')
        self.assertAlmostEqualList(out['annotations'][0]['position'], [C[0] + 15, C[1], C[2]])
        self.assertEqual(out['annotations'][0]['title'], 'A')
        self.assertAlmostEqualList(out['view']['position'], [C[0] + 15, C[1], C[2]])
        self.assertAlmostEqualList(out['view']['target'], C)
        self.assertEqual(out['view']['fov'], 60)
        self.assertEqual(out['fremd'], {'a': 1})

    def test_transform_scene_robust(self):
        self.assertEqual(dt.transform_scene({}, S), {})
        self.assertEqual(dt.transform_scene(None, S), None)
        self.assertEqual(dt.transform_scene('x', S), 'x')
        kaputt = {'measurements': 'x', 'annotations': None, 'view': [1]}
        self.assertEqual(dt.transform_scene(kaputt, S), kaputt)

    def test_entry_user_scene_wie_szene(self):
        data = {'annotations': [{'position': [C[0] + 10, C[1], C[2]]}], 'classification': 'x'}
        out = dt.transform_entry_data('user_scene', data, S, 1.5)
        self.assertAlmostEqualList(out['annotations'][0]['position'], [C[0] + 15, C[1], C[2]])
        self.assertEqual(out['classification'], 'x')

    def test_entry_area_cutout_und_crop_region(self):
        p = [C[0] + 10, C[1], C[2]]
        out = dt.transform_entry_data('area_cutout', {'linked_measurement_uuid': 'u', 'points': [p]}, S, 1.5)
        self.assertEqual(out['linked_measurement_uuid'], 'u')
        self.assertAlmostEqualList(out['points'][0], [C[0] + 15, C[1], C[2]])
        out = dt.transform_entry_data('crop_region', {'points': [p, [1, 2]]}, S, 1.5)
        self.assertAlmostEqualList(out['points'][0], [C[0] + 15, C[1], C[2]])
        self.assertEqual(out['points'][1], [1, 2])
        self.assertEqual(dt.transform_entry_data('crop_region', {}, S, 1.5), {})

    def test_entry_roof_geometry(self):
        p = [C[0] + 10, C[1], C[2]]
        data = {'elements': [
            {'id': 'a', 'template': 't', 'points': [p], 'ridgePoints': [p],
             'attika': {'points': [p], 'heightM': 0.5, 'heightMeasurementUuid': 'h'}},
            {'id': 'b', 'points': [p]},
            'kaputt',
        ]}
        orig = copy.deepcopy(data)
        out = dt.transform_entry_data('roof_geometry', data, S, 1.5)
        self.assertEqual(data, orig)
        a = out['elements'][0]
        self.assertAlmostEqualList(a['points'][0], [C[0] + 15, C[1], C[2]])
        self.assertAlmostEqualList(a['ridgePoints'][0], [C[0] + 15, C[1], C[2]])
        self.assertAlmostEqualList(a['attika']['points'][0], [C[0] + 15, C[1], C[2]])
        self.assertAlmostEqual(a['attika']['heightM'], 0.75)
        self.assertEqual(a['attika']['heightMeasurementUuid'], 'h')
        b = out['elements'][1]
        self.assertNotIn('attika', b)
        self.assertAlmostEqualList(b['points'][0], [C[0] + 15, C[1], C[2]])
        self.assertEqual(out['elements'][2], 'kaputt')

    def test_entry_unbekannter_typ_none(self):
        self.assertIsNone(dt.transform_entry_data('image', {'points': [[1, 2, 3]]}, S, 1.5))

    def test_matrix_datei_rundweg(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'sub.json')
            dt.save_matrix_file(path, S)
            self.assertEqual(dt.load_matrix_file(path), S)
            self.assertEqual(os.listdir(d), ['sub.json'])  # keine tmp-Reste

    def test_matrix_datei_fehlt_oder_kaputt(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(dt.load_matrix_file(os.path.join(d, 'nix.json')))
            for content in ('{kaputt', '{"matrix": [1, 2]}', '{"matrix": "x"}', '[]'):
                path = os.path.join(d, 'k.json')
                with open(path, 'w') as f:
                    f.write(content)
                self.assertIsNone(dt.load_matrix_file(path))


if __name__ == '__main__':
    unittest.main()
