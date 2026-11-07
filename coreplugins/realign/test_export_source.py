"""
Tests für die Wahl des Export-Eingabe-GLB (reine Stdlib, ohne Django).

Lauf: python coreplugins/realign/test_export_source.py
Das Modul wird per Dateipfad geladen, weil das Paket coreplugins.realign beim Import
das Plugin (und damit Django) lädt.
"""
import importlib.util
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('export_source', os.path.join(_HERE, 'export_source.py'))
export_source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_source)


class ExportGlbSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.assets = self.tmp.name
        os.makedirs(os.path.join(self.assets, 'odm_texturing'))
        self.textured = os.path.join(self.assets, 'odm_texturing', 'odm_textured_model_geo.glb')
        self.backup = os.path.join(self.assets, 'odm_texturing', 'odm_textured_model_geo.original.glb')

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, path):
        with open(path, 'wb') as f:
            f.write(b'glb')

    def test_nicht_angewendet_nimmt_das_aktuelle_glb(self):
        self._touch(self.textured)
        self.assertEqual(export_source.export_glb_source(self.assets, self.textured), self.textured)

    def test_angewendet_nimmt_das_gesicherte_original(self):
        # Angewendet: das aktuelle GLB trägt schon die Ausrichtung, das Original liegt im Backup.
        self._touch(self.textured)
        self._touch(self.backup)
        self.assertEqual(export_source.export_glb_source(self.assets, self.textured), self.backup)

    def test_ohne_glb_none(self):
        self.assertIsNone(export_source.export_glb_source(self.assets, self.textured))


if __name__ == '__main__':
    unittest.main()
