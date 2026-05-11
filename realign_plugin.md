# Plugin `realign` — Modell horizontal ausrichten & Files exportieren

Dokumentation und Plan für das WebODM-Plugin `coreplugins/realign`. Setzt sich
aus zwei Phasen zusammen:

- **Phase 1 (umgesetzt):** Im 3D-Viewer 3 Punkte auf einer waagerechten Fläche
  wählen, daraus eine 4×4-Korrekturmatrix berechnen, persistieren und beim
  manuellen "Anwenden" auf Pointcloud + Mesh im Viewer wirken lassen.
- **Phase 2 (geplant):** Auf Klick die transformierten LAZ- und GLB-Files
  asynchron im Task-Asset-Ordner erzeugen und als Download anbieten — Basis
  für den Roof-S-Import-Workflow.

---

## Phase 1 — Status & Architektur

### Was umgesetzt ist

- Plugin `coreplugins/realign` aktiv, Frontend baut, in Plugin-Liste sichtbar.
- 3-Punkt-Picker im 3D-Viewer (über Potree `MeasuringTool` mit `maxMarkers=2` +
  Polling auf `marker_dropped`).
- Matrix wird per `computeAlignmentMatrix` aus den 3 Punkten berechnet:
  Rotation um Centroid der gewählten Punkte (NICHT um Welt-Origin), optional
  Z-Nullpunkt-Verschiebung.
- Persistenz: neuer `entry_type: "alignment_transform"` im `project_data`-Plugin,
  Schema in [`coreplugins/project_data/DataPlugin_API.md`](coreplugins/project_data/DataPlugin_API.md) dokumentiert.
- Apply-Logik: direkte Matrix-Manipulation auf
  `viewer.scene.scenePointCloud.children` (Pointclouds via `pcoGeometry`-Filter)
  und `viewer.scene.scene.children` (Mesh + Camera-Marker, ohne Lichter).
  `matrixAutoUpdate=false`, `frustumCulled=false`, `viewer.update`-Event-Hook
  zwingt die Matrix bei jedem Frame zurück.

### Datenfluss

```text
[ModelView.jsx, Core, unverändert]
   │ initialisiert Potree.Viewer + lädt Pointcloud + Mesh
   ▼
[realign/public/main.js]
   - hookt PluginsAPI.ModelView.addActionButton (sync register, kein deps-Array)
   - lädt realign/build/app.js parallel via SystemJS.import
   ▼
[realign/public/app.jsx]
   - UI: Waage-Button + Modal mit 3-Punkt-Picker, Toggle, Save, Reset, Export
   - Mathe: 3 Punkte → 4×4-Matrix (Three.js)
   - Persistenz: GET/POST/PATCH/DELETE über project_data-API
   - Apply: direkte Matrix-Schreibung auf scenePointCloud-Children + scene-Children
```

### Mathematik

```js
const v1 = p2.clone().sub(p1);
const v2 = p3.clone().sub(p1);
const n = v1.cross(v2).normalize();
if (n.z < 0) n.negate();
const up = new THREE.Vector3(0, 0, 1);
const q  = new THREE.Quaternion().setFromUnitVectors(n, up);
const R  = new THREE.Matrix4().makeRotationFromQuaternion(q);

const centroid = p1.clone().add(p2).add(p3).divideScalar(3);

// Rotation um Centroid — kritisch bei UTM-Koordinaten (~518000, ~5370000):
// Eine Rotation um Welt-Origin würde das Modell um (I-R)·c verschieben,
// was bei kleinem R ≈ 1° bereits 100+ km Translation bedeutet.
const M = new THREE.Matrix4()
  .makeTranslation(centroid.x, centroid.y, centroid.z)
  .multiply(R)
  .multiply(new THREE.Matrix4().makeTranslation(-centroid.x, -centroid.y, -centroid.z));

if (resetZ) {
  M.premultiply(new THREE.Matrix4().makeTranslation(0, 0, -centroid.z));
}
```

### Dateien

| Datei | Rolle |
| --- | --- |
| `coreplugins/realign/manifest.json` | Plugin-Metadaten |
| `coreplugins/realign/__init__.py` | `from .plugin import *` |
| `coreplugins/realign/plugin.py` | `include_js_files()`, `build_jsx_components()` |
| `coreplugins/realign/public/main.js` | sync-Registrierung `addActionButton`, LazyLoader |
| `coreplugins/realign/public/app.jsx` | React-UI, Mathe, Apply, Save |
| `coreplugins/project_data/models.py` | `ALIGNMENT_TRANSFORM` in `ENTRY_TYPES` |
| `coreplugins/project_data/DataPlugin_API.md` | Schema + Roof-S-Konsum-Snippet |
| `docker-compose.override.yml` | Bind-Mount `./coreplugins/realign` |

---

## Phase 1 — Wichtige Erkenntnisse aus der Implementierung

Auflistung der Stolpersteine, die schmerzhaft gelernt wurden. Reihenfolge sortiert
nach "wie schnell würde ich beim nächsten Mal darüber stolpern".

### 1. `THREE` muss zur Laufzeit gelesen werden

Mein `main.js` lädt das Modul via `SystemJS.import('realign/build/app.js')` sofort
beim Page-Load (parallel, damit der `addActionButton`-Trigger nicht verpasst
wird). Zu diesem Zeitpunkt ist `window.THREE` evtl. noch nicht vom Haupt-Bundle
gesetzt. Ein `const THREE = window.THREE;` auf Top-Level des Moduls friert dann
`undefined` fest. **Lösung:** `function getTHREE() { return window.THREE; }`
und in jeder Funktion `const THREE = getTHREE();` lokal lesen.

### 2. Plugin-Build-Webpack kennt `THREE` nicht als external

Das Haupt-Webpack hat `externals: { "THREE": "THREE" }` (siehe
`webpack.config.js:103`). Das Plugin-Template
(`app/plugins/templates/webpack.config.js.tmpl`) listet `THREE` NICHT als
external. Konsequenz: `import * as THREE from 'THREE'` knallt im Plugin-Build mit
"Module not found". → `window.THREE` direkt nutzen, kein Import.

### 3. Race-Condition im `triggerAddActionButton`-Response-Listener

[`app/static/app/js/classes/plugins/ApiFactory.js:69-79`](app/static/app/js/classes/plugins/ApiFactory.js#L69-L79):
der Listener entfernt sich nach `setTimeout(0)` selbst. Plugins, die ihre
Dependencies via `addActionButton(['deps'], cb)` async laden, kommen mit ihrem
Response evtl. zu spät — Listener ist dann schon weg, ihr Button verschwindet.

**Lösung im main.js:** `addActionButton` **synchron ohne `deps`-Array**
registrieren. Das Modul wird im main.js parallel via `SystemJS.import` geladen,
und im Sync-Callback wird entweder direkt das geladene Modul verwendet oder ein
LazyLoader-Wrapper geliefert.

### 4. Korrekte Property-Namen in Potree

- `viewer.scene.scenePointCloud` — **mit großem `C`, ohne `s` am Ende**.
  `scenePointclouds` (mit `s` oder `c`) existiert nicht.
- Pointcloud-Identifikation: `obj.pcoGeometry !== undefined` (PointCloudOctree
  hat das Property, andere Children der `scenePointCloud`-Scene wie
  `referenceFrame` oder Lichter haben es nicht).
- Mess-Marker liegen in `viewer.scene.measuringTool.scene` (separate Three.js
  Scene), nicht in `viewer.scene.scene`.

### 5. Potree `MeasuringTool`-Picking braucht `maxMarkers >= 2`

`viewer.measuringTool.startInsertion({maxMarkers: 1})` hängt **keinen
`mouseup`-Listener an** (siehe `potree.js:68655`: `if (measure.maxMarkers > 1)`).
Der initiale Marker bleibt als schwebender Cursor bei (0,0,0) hängen — kein
Klick wird erfasst.

**Lösung:** `maxMarkers: 2`. Erster Klick fixiert `points[0]`, `points[1]` ist
der neue Vorschau-Marker, den wir nach dem `marker_dropped`-Event mit
`removeMarker(1)` selbst entfernen.

Das richtige Event ist **`marker_dropped`** (feuert beim Mouseup nach Drag),
nicht `marker_added` (feuert auch für den initialen 0,0,0-Marker).
Implementiert mit 50 ms Delay + Polling-Fallback.

### 6. Rotation um Centroid, nicht um Welt-Origin

Bei UTM-Koordinaten (~518000, ~5370000) ergäbe eine Rotation um (0,0,0) bei
nur 1° Korrektur eine Translation von >90 km. Modell wäre außerhalb jedes
Frustums. Lösung: `M = T(c)·R·T(-c)` mit `c = centroid` der drei gewählten
Punkte.

### 7. Auto-Apply ist deaktiviert

Beim Page-Load durchläuft Potree mehrere Phasen, in denen `pointcloud.position`
mehrfach manipuliert wird (Constructor mit `geometry.offset`, später
`Potree.loadProject` Hook auf `viewer.update`). Jeder Apply-Versuch innerhalb
dieser Phase wird überschrieben.

**Sichtbares Symptom:** Pointcloud verschwindet — `quaternion` bleibt
unsere Rotation, `position` wird auf (0,0,0) zurückgesetzt. Rotation um Origin
katapultiert die Pointcloud km-weit weg.

Versuchte Workarounds (alle nicht zuverlässig):

- `matrixAutoUpdate = false` mit `obj.matrix.copy(M)` — Potree resetet trotzdem.
- `viewer.update`-Event-Hook mit Force-Pose pro Frame — Race bleibt.
- Wrapper-Group, die die Pointcloud in eine eigene Group packt — wurde aus
  unklarem Grund nicht angewendet (`hasWrapper: false` im DevTools-Test).
- 15 s setTimeout nach `pointcloud_added`-Event — Modell verschwindet trotzdem
  nach genau 15 s.

**Aktueller Stand:** `RealignController.loadFromBackend` lädt nur die Daten,
ruft `applyMatrix` **nicht** auf. State im Panel wird mit
`enabled: ctrl.applied` hydratiert (effektiv immer `false`). User klickt
einmal pro Session manuell "Anwenden".

Saubere Lösung für später: Patch in
[`app/static/app/js/ModelView.jsx:464`](app/static/app/js/ModelView.jsx#L464)
direkt nach dem `Potree.loadProject(viewer, sceneData)`-Aufruf — dort ist
Potrees Init garantiert durch. Aber Core-Patch, muss bei Upstream-Merges
mitgeführt werden.

### 8. Cloudflare-Cache vor `build/app.js`

Cloudflare cached `.js`-Files standardmäßig. Nach jedem Plugin-Build:
**Cache für `build/app.js` und `main.js` purgen** oder Cache-Buster `?v=N`
zum Verifizieren benutzen. Sonst lädt der Browser den alten Build und
Diagnose-Output ist irreführend.

### 9. WebODM rebuilt nur, wenn `build/` fehlt

[`app/plugins/functions.py:132`](app/plugins/functions.py#L132):
`elif not plugin.path_exists("public/build")` — Plugin-Webpack-Build läuft
nur, wenn das `build/`-Verzeichnis nicht existiert. Bei Source-Änderungen am
`app.jsx` also vor dem Container-Restart:

```bash
docker exec webapp rm -rf /webodm/coreplugins/realign/public/build
docker compose restart webapp
```

### 10. Mount NICHT `:ro` für Plugins mit jsx-Build

WebODM schreibt eine generierte `webpack.config.js` in `public/`. Mit
`:ro`-Mount → `OSError: Read-only file system`. Override-Mount für `realign`
muss **schreibbar** sein:

```yaml
- ./coreplugins/realign:/webodm/coreplugins/realign
```

`build/`, `node_modules/` und `webpack.config.js` werden ohnehin gitignored
(globale `.gitignore` regelt das).

---

## Phase 2 — Export der ausgerichteten Files (TODO)

### Kontext

Vermarktungs-Workflow für Roof-S:

1. Drohnenaufnahmen (Kunde oder selbst)
2. Modell rechnen — lokal auf großem Rechner oder via WebODM Lightning
3. In WebODM säubern + ausrichten (Phase 1)
4. **Ausgerichtete LAZ + GLB exportieren (Phase 2)**
5. In Roof-S als neuen Task importieren (Roof-S baut Octree selbst beim Import)
6. Kunde erhält Zugang über Roof-S

Roof-S bekommt damit fertig ausgerichtete Files, kein Live-Matrix-Apply nötig.
Das ist insbesondere deshalb wichtig, weil Live-Apply in Phase 1 fragil ist
(siehe Erkenntnis #7).

### Architektur

```text
[Modal "Modell exportieren"-Button]
   │ POST /api/plugins/realign/projects/{pid}/tasks/{tid}/export/
   │ body: {matrix: [16 floats]}
   ▼
[realign/api.py: ExportView]
   │ run_function_async(_run_export_task, ..., with_progress=True)
   ▼
[realign/tasks.py: _run_export_task]
   1. PDAL: filters.transformation auf georeferenced_model.laz
   2. pygltflib + numpy: GLB-Vertices transformieren (CESIUM_RTC einbacken!)
   3. Output nach task.assets_path('realigned/')
   ▼
[Frontend pollt GET .../export/{celery_task_id}/]
   │ bei done: Download-Links anzeigen
   ▼
[Download-View liefert FileResponse aus task.assets_path('realigned/...')]
```

Pattern direkt von `coreplugins/roof_detect/` gespiegelt (Detect/Status/Result).

### Backend

#### `coreplugins/realign/api.py` (neu)

Drei `TaskView`-basierte Views:

- **`ExportView` (POST)** — Body: `{matrix: number[16]}`. Validiert Matrix,
  ermittelt LAZ + GLB-Pfade über `task.assets_path()` mit den `ASSETS_MAP`-Keys
  (`georeferenced_model.laz`, `textured_model.glb`), startet
  `run_function_async(_run_export_task, laz_path, glb_path, matrix, output_dir,
  with_progress=True)`. Antwort: `{celery_task_id}`.
- **`ExportStatusView` (GET)** mit `celery_task_id` — pollt
  `TestSafeAsyncResult(celery_task_id)`. Antwort:
  `{ready: bool, status: 'progress'|'done'|'failed', progress: 0..100,
  output: {laz_url, glb_url}}`. URLs zeigen auf die Download-View.
- **`ExportDownloadView` (GET)** mit `kind ∈ {laz, glb}` — `FileResponse` mit
  `as_attachment=True` aus `task.assets_path('realigned/...')`. Vorlage:
  `coreplugins/roof_detect/api.py: CADMeshView`.

URL-Routen in `coreplugins/realign/plugin.py`:

```python
'projects/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/export/$'
'projects/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/export/(?P<celery_task_id>[^/.]+)/$'
'projects/(?P<project_pk>[^/.]+)/tasks/(?P<pk>[^/.]+)/export/download/(?P<kind>laz|glb)/$'
```

#### `coreplugins/realign/tasks.py` (neu)

```python
def _run_export_task(laz_path, glb_path, matrix, output_dir, progress_callback=None):
    import os, json, subprocess, tempfile
    import numpy as np, pygltflib

    # Matrix ist column-major (THREE.Matrix4.toArray); für PDAL/numpy in row-major
    M = np.asarray(matrix, dtype=np.float64).reshape(4, 4, order='F')
    os.makedirs(output_dir, exist_ok=True)

    # 1. LAZ via PDAL — Pipeline-JSON
    if progress_callback: progress_callback('LAZ', 5)
    out_laz = os.path.join(output_dir, 'model_realigned.laz')
    pipeline = {
        "pipeline": [
            laz_path,
            {"type": "filters.transformation",
             "matrix": " ".join(f"{v:.10f}" for v in M.flatten(order='C'))},
            {"type": "writers.las", "filename": out_laz, "compression": "true"}
        ]
    }
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        json.dump(pipeline, f); pipeline_path = f.name
    try:
        subprocess.check_call(['pdal', 'pipeline', pipeline_path])
    finally:
        os.unlink(pipeline_path)
    if progress_callback: progress_callback('LAZ done', 50)

    # 2. GLB via pygltflib — CESIUM_RTC einbacken, dann M anwenden
    out_glb = os.path.join(output_dir, 'model_realigned.glb')
    _transform_glb(glb_path, out_glb, M)
    if progress_callback: progress_callback('GLB done', 100)

    return {'laz': out_laz, 'glb': out_glb}
```

`_transform_glb` Details:

- `pygltflib.GLTF2.load(glb_path)`.
- Wenn Extension `CESIUM_RTC` mit `center` vorhanden: Center in Vertex-Koords
  einbacken (jede `POSITION` `+= center`), Extension entfernen. Sonst wirkt M
  nur auf den mesh-lokalen Anteil.
- Für jedes Mesh / jede Primitive: `POSITION`-Accessor lesen, Vertices als
  `(N, 4)`-homogen erweitern, `(M @ verts.T).T[:, :3]`, zurück in den Buffer.
- `NORMAL` / `TANGENT` (falls vorhanden) mit dem oberen 3×3-Block von M
  (normalisiert) transformieren.
- `gltf.save(out_glb)`.

Dependency: `pygltflib` in `coreplugins/realign/requirements.txt` (analog
`coreplugins/roof_detect/requirements.txt`, das `laspy>=2.0` listet). PDAL ist
laut `Dockerfile:44,57` schon installiert, kein Container-Rebuild nötig.
`numpy` ist im Image vorhanden.

#### `coreplugins/realign/plugin.py` (erweitern)

`api_mount_points()` für die drei Routen oben.

### Frontend

#### `coreplugins/realign/public/app.jsx` (erweitern)

In `RealignPanel`:

- Neuer Button **"Modell exportieren"** unter Speichern/Reset.
  Disabled, solange kein gespeicherter Eintrag existiert (`!entryId`) — der
  Export braucht eine persistierte Matrix.
- State: `exportTaskId`, `exportProgress` (0..100), `exportStatus`, `exportUrls`.
- Click-Handler: POST mit der aktuell gespeicherten Matrix
  (`controller.lastData.matrix`) → bekommt `celery_task_id`, startet 2-s-Poll
  auf `ExportStatusView`. Setzt `exportProgress` und – bei done –
  `exportUrls`.
- UI: Progress-Bar während Polling; bei done zwei Download-Buttons
  ("LAZ herunterladen", "GLB herunterladen") mit `href` auf die Download-URLs.

Authentifizierung: `$.ajax` mit Session-Cookie + CSRF-Setup (`csrf.js` global).
Downloads als `<a href>` — die Browser-Standard-Auth (Cookie) reicht für
FileResponse.

### CESIUM_RTC-Hinweis

Aus [`ModelView.jsx:794-797`](app/static/app/js/ModelView.jsx#L794-L797) wissen
wir, dass das GLB optional einen `CESIUM_RTC.center`-Offset trägt. Bei
direkter Vertex-Transformation muss dieser Offset **vor** dem Apply der Matrix
in die Vertices eingebacken werden — sonst wirkt M auf
`vertex - RTC.center` statt auf `vertex` selbst. Reihenfolge:

1. `RTC.center` aus Extension auslesen
2. Jede Vertex-Position: `pos += RTC.center` (in-place auf den Buffer schreiben)
3. Extension entfernen
4. M anwenden

### Critical Files (Phase 2)

| Datei | Verwendung |
| --- | --- |
| [coreplugins/roof_detect/api.py](coreplugins/roof_detect/api.py) | LESEN: Vorlage Detect/Status/Result-Trio, FileResponse-Pattern |
| [coreplugins/roof_detect/detection.py](coreplugins/roof_detect/detection.py) | LESEN: PDAL-Pipeline-Aufruf via `subprocess` |
| [app/plugins/worker.py](app/plugins/worker.py) | LESEN: `run_function_async` + `with_progress=True` |
| [app/models/task.py](app/models/task.py) | LESEN: `assets_path()`, `ASSETS_MAP` (Z. 175–221) |
| `coreplugins/realign/plugin.py` | EDIT: `api_mount_points()` ergänzen |
| `coreplugins/realign/api.py` | NEU: ExportView, ExportStatusView, ExportDownloadView |
| `coreplugins/realign/tasks.py` | NEU: `_run_export_task` + `_transform_glb` |
| `coreplugins/realign/requirements.txt` | NEU: `pygltflib` |
| `coreplugins/realign/public/app.jsx` | EDIT: Export-Button + Polling + Download-Links |

---

## Deployment

### Erstes Deployment / nach Plugin-Änderungen

```bash
cd ~/WebODM
git fetch myfork && git reset --hard myfork/master
docker exec webapp rm -rf /webodm/coreplugins/realign/public/build
docker compose restart webapp worker
```

`webpack-Build` läuft beim Boot automatisch. Server-Routing:

```bash
docker restart roof-s-roofinspector_nginx-1  # falls externe 502 hängen
sudo systemctl reload snap.nextcloud.apache.service  # nur falls Nextcloud-Pfad
```

(Siehe Memory `WebODM Server Routing` für Pfad: Cloudflare Tunnel →
roof-s-nginx → WebODM.)

### Cache nach Plugin-Updates

Cloudflare cached `.js` aggressiv. Nach jedem Build:

- Cloudflare-Dashboard → Purge für `https://odm.netz-montageserver.de/plugins/realign/build/app.js`
- ODER Browser-DevTools öffnen + Strg+Shift+R (umgeht Cache nur, wenn
  Cloudflare den File nicht edge-cached)
- ODER langfristig: Cache-Rule für `*/plugins/*` auf "Bypass"

---

## Verifikation

### Phase 1

1. Task öffnen, Waage-Icon im Viewer-Toolbar klicken.
2. 3 Punkte auf einer waagerechten Fläche wählen (Pflasterboden, Dachfläche).
3. "Anwenden" → Pointcloud + Mesh rotieren sichtbar gemeinsam.
4. Höhenmessung zwischen zwei Punkten auf dieser Ebene ≈ 0.
5. Höhenmessung Boden ↔ Dachfirst → plausibler Wert.
6. "Speichern" → API-Eintrag in `project_data` mit `entry_type:
   "alignment_transform"`.
7. Reload + manuelles "Anwenden" → wieder ausgerichtet (Auto-Apply bleibt aus).

### Phase 2 (nach Umsetzung)

1. Nach Phase-1-Save: "Modell exportieren" klicken.
2. Progress-Bar 0 → 50 (LAZ done) → 100 (GLB done).
3. Zwei Download-Buttons erscheinen.
4. LAZ in CloudCompare öffnen → horizontal ausgerichtet, gleiche Ebene wie
   Viewer-Apply.
5. GLB in MeshLab/Blender öffnen → deckungsgleich mit LAZ, korrekt rotiert.
6. Beide Files in Roof-S importieren → neuer Task, Modell steht horizontal.
