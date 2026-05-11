# Plugin `realign` — Modell horizontal ausrichten & Files exportieren

WebODM-Plugin `coreplugins/realign`. Zwei Phasen, **beide produktiv**:

- **Phase 1:** 3-Punkt-Picker im 3D-Viewer berechnet eine 4×4-Korrekturmatrix,
  speichert sie als `alignment_transform`-Eintrag im `project_data`-Plugin und
  wendet sie auf manuellen Klick auf Pointcloud + Mesh im Viewer an.
- **Phase 2:** Auf Klick erzeugt der Celery-Worker per PDAL und pygltflib
  die ausgerichteten LAZ + GLB-Files im Task-Asset-Ordner. Diese können
  einzeln heruntergeladen oder per HTTP-API von Roof-S abgerufen werden.

Stand: Mai 2026 — End-to-End mit Roof-S-Integration verifiziert.

---

## Inhalt

1. [API-Übersicht (für Roof-S)](#api-übersicht-für-roof-s)
2. [Workflow: Alignment-Matrix anlegen & exportieren](#workflow-alignment-matrix-anlegen--exportieren)
3. [API-Referenz](#api-referenz)
   - [Realign-Plugin-Endpunkte](#realign-plugin-endpunkte)
   - [project_data-Plugin-Endpunkte (für Roof-S)](#project_data-plugin-endpunkte-für-roof-s)
4. [TypeScript-Typen](#typescript-typen)
5. [Roof-S Integration — Beispiel-Code](#roof-s-integration--beispiel-code)
6. [Architektur & Mathematik](#architektur--mathematik)
7. [Erkenntnisse aus der Implementierung](#erkenntnisse-aus-der-implementierung)
8. [Deployment & Verifikation](#deployment--verifikation)

---

## API-Übersicht (für Roof-S)

Roof-S nutzt vier API-Gruppen:

| Aufgabe | Endpunkte | Plugin |
| --- | --- | --- |
| Alignment-Matrix lesen / speichern | `GET/POST/PATCH/DELETE /api/plugins/project_data/project/{pid}/entries/` (mit `type=alignment_transform`) | `project_data` |
| Export starten | `POST /api/plugins/realign/project/{pid}/tasks/{tid}/export/` | `realign` |
| Export-Status pollen | `GET /api/plugins/realign/project/{pid}/tasks/{tid}/export/{celery_task_id}/` | `realign` |
| Exportierte Files herunterladen | `GET /api/plugins/realign/project/{pid}/tasks/{tid}/export/download/{laz\|glb}/` | `realign` |
| Dauerhaft anwenden (EPT regen + Swap) | `POST /api/plugins/realign/project/{pid}/tasks/{tid}/apply/` | `realign` |
| Apply-Status pollen | `GET /api/plugins/realign/project/{pid}/tasks/{tid}/apply-status/{celery_task_id}/` | `realign` |
| Original wiederherstellen | `POST /api/plugins/realign/project/{pid}/tasks/{tid}/revert/` | `realign` |

Authentifizierung: wie überall in der WebODM-API entweder
`Authorization: JWT <token>` (Cross-Origin) oder Session-Cookie mit `csrf.js`
(Same-Origin im WebODM-UI).

Berechtigung: jeder Endpunkt prüft Task-Zugriff über `TaskView.get_and_check_task`.
Wer den Task nicht sehen darf, bekommt **404** (WebODM-Konvention, nicht 403).

---

## Workflow: Alignment-Matrix anlegen & exportieren

```text
[1] User öffnet Task in WebODM, klickt im Viewer auf das Waage-Icon
    → Realign-Panel öffnet sich

[2] User wählt 3 Punkte auf einer waagerechten Fläche
    → Frontend berechnet 4×4-Matrix (Rotation um Centroid)

[3] User klickt "Speichern"
    → POST /api/plugins/project_data/project/{pid}/entries/
      body: { entry_type: "alignment_transform", task, data: {points, matrix, options, enabled} }
    → Eintrag persistiert in project_data

[4] User klickt "Modell exportieren"
    → POST /api/plugins/realign/project/{pid}/tasks/{tid}/export/
      body: { matrix: number[16] }
    → Antwort: { celery_task_id: "..." }

[5] Frontend pollt alle 2 s:
    → GET /api/plugins/realign/project/{pid}/tasks/{tid}/export/{celery_task_id}/
    → Antwort: { ready, status, progress, output? }
    → Bei progress=100: Download-Links sichtbar

[6] Download:
    → GET .../export/download/laz/  →  FileResponse model_realigned.laz
    → GET .../export/download/glb/  →  FileResponse model_realigned.glb
```

**Für Roof-S verkürzt:** Schritt 4–6 (3-Punkt-Picker passiert in WebODM). Wenn
Roof-S nur einen bereits ausgerichteten Task konsumiert, reicht ein direkter
Aufruf an die Download-Endpoints — vorausgesetzt der Export wurde mindestens
einmal gestartet.

---

## API-Referenz

### Realign-Plugin-Endpunkte

Alle Pfade unter Plugin-Namespace `/api/plugins/realign/`. URL-Pattern aus
[`coreplugins/realign/plugin.py`](coreplugins/realign/plugin.py).

#### `POST .../project/{project_pk}/tasks/{pk}/export/`

Startet den Export-Celery-Task. Synchroner Aufruf, kehrt sofort mit
`celery_task_id` zurück.

**Request Body** (`application/json`):

```json
{ "matrix": [m00, m10, m20, m30, m01, m11, m21, m31, m02, m12, m22, m32, m03, m13, m23, m33] }
```

Genau 16 Zahlen, **column-major** (Three.js `Matrix4.toArray()`-Format).
Die Matrix wird gegen die Original-LAZ angewendet (PDAL `filters.transformation`)
und gegen das Original-GLB (Node-Level-Matrix im GLTF-Scene-Graph).

**Erfolg (200):**

```json
{ "celery_task_id": "abc-1234-..." }
```

**Fehler:**

| Status | Bedingung |
| --- | --- |
| `400` | `matrix` fehlt, ist keine Liste, hat nicht 16 Einträge oder enthält keine validen Zahlen |
| `404` | Task nicht gefunden ODER keine Punktwolke (LAZ/LAS) für Task vorhanden |
| `500` | Celery konnte den Task nicht queuen |

Hinweise:

- Der `geo_offset` (UTM-Verschiebung des Modells) wird serverseitig aus
  `coords.txt` gelesen — Roof-S muss nichts dafür tun.
- Wenn das Task kein GLB hat, läuft der Export trotzdem (nur LAZ).

#### `GET .../project/{project_pk}/tasks/{pk}/export/{celery_task_id}/`

Pollt den Status des Celery-Tasks. Lange laufende Exporte können einige
Sekunden bis ein paar Minuten dauern (je nach Pointcloud-Größe).

**Antwort während Lauf (200):**

```json
{ "ready": false, "status": "PROGRESS", "progress": 50 }
```

`status` ist entweder ein Celery-State (`PENDING`, `STARTED`, `PROGRESS`) oder
eine Plugin-Statusmeldung aus dem Worker (`"Starte LAZ-Export…"`,
`"LAZ fertig, starte GLB-Export…"`, `"Fertig"`). `progress` ist `0..100`.

**Antwort bei Erfolg (200):**

```json
{
  "ready": true,
  "status": "SUCCESS",
  "progress": 100,
  "output": {
    "laz_url": "/api/plugins/realign/project/{pid}/tasks/{tid}/export/download/laz/",
    "glb_url": "/api/plugins/realign/project/{pid}/tasks/{tid}/export/download/glb/"
  }
}
```

`output.glb_url` fehlt, wenn das Task kein Textured-Model hatte.

**Antwort bei Fehler (200, mit `status: "FAILURE"`):**

```json
{ "ready": true, "status": "FAILURE", "error": "Beschreibung..." }
```

#### `GET .../project/{project_pk}/tasks/{pk}/export/download/{kind}/`

Lädt eine der exportierten Dateien herunter. `kind` ∈ `laz | glb`.

**Erfolg:** `FileResponse` mit `Content-Disposition: attachment; filename="model_realigned.laz"`
(bzw. `.glb`). Content-Type `application/octet-stream`.

**Fehler:**

| Status | Bedingung |
| --- | --- |
| `400` | `kind` ist weder `laz` noch `glb` |
| `404` | Task nicht gefunden ODER Datei wurde noch nicht exportiert |

Roof-S kann diesen Endpoint direkt als `<a href>`-Download oder per
`fetch().then(r => r.blob())` konsumieren.

#### `POST .../project/{project_pk}/tasks/{pk}/apply/`

Wendet die realignten Files **dauerhaft** auf den Task an: Regeneriert das
Entwine-Octree aus der realignten LAZ, sichert die Originale und tauscht
die Asset-Dateien. Nach erfolgreichem Apply rendert WebODM/Roof-S das Modell
ohne Live-Matrix-Apply horizontal — auch nach Page-Reload.

Voraussetzung: `Export` muss vorher gelaufen sein (Datei `realigned/model_realigned.laz`
muss existieren). Andernfalls **400**.

Ablauf im Celery-Worker:

1. `entwine build -i realigned/model_realigned.laz -o realigned/entwine_pointcloud/`
2. Beim **ersten** Apply: Backup anlegen — `entwine_pointcloud/` →
   `entwine_pointcloud_original/`, `odm_textured_model_geo.glb` →
   `odm_textured_model_geo.original.glb`. Bei wiederholtem Apply (nach
   neuem Export) werden die zuvor angewendeten Dateien verworfen, das
   Backup bleibt unangetastet.
3. Move des neu erzeugten EPT auf den Ziel-Pfad, Copy der realignten GLB.
4. `project_data` alignment_transform: `data.applied = true`.

**Request Body:** leer (Matrix wird aus den Files genommen).

**Erfolg (200):**

```json
{ "celery_task_id": "abc-1234-..." }
```

**Fehler:**

| Status | Bedingung |
| --- | --- |
| `400` | `realigned/model_realigned.laz` existiert nicht (Export wurde nie ausgeführt) |
| `404` | Task nicht gefunden |
| `500` | Celery konnte den Task nicht queuen |

Laufzeit: 30 s bis mehrere Minuten (abhängig von Punktwolken-Größe — wegen
EPT-Generierung). Frontend MUSS pollen.

#### `GET .../project/{project_pk}/tasks/{pk}/apply-status/{celery_task_id}/`

Pollt den Status des Apply-Tasks. Antwort identisch zu `export/{id}/`,
nur ohne `output` (es gibt keine zusätzlichen Downloads).

**Antwort während Lauf (200):**

```json
{ "ready": false, "status": "Erzeuge Entwine-Octree…", "progress": 5 }
```

`progress` durchläuft: `5 → 60 → 80 → 95 → 100`.

**Antwort bei Erfolg (200):**

```json
{ "ready": true, "status": "SUCCESS", "progress": 100 }
```

Nach `ready: true` sollte das Roof-S-Frontend den eingebetteten WebODM-Viewer
neu laden (z.B. iframe.src neu setzen), damit die neuen EPT-Tiles geladen werden.

#### `POST .../project/{project_pk}/tasks/{pk}/revert/`

**Synchron** (rename-only, keine schwere Last). Stellt die Original-Dateien
aus den Backups wieder her:

- `entwine_pointcloud_original/` → `entwine_pointcloud/`
- `odm_textured_model_geo.original.glb` → `odm_textured_model_geo.glb`
- `project_data` alignment_transform: `data.applied = false`

**Request Body:** leer.

**Erfolg (200):**

```json
{ "ok": true }
```

**Fehler:**

| Status | Bedingung |
| --- | --- |
| `404` | Task nicht gefunden ODER kein Backup vorhanden (Apply wurde nie ausgeführt) |
| `500` | Filesystem-Fehler beim Rename |

Nach Revert sollte das Frontend ebenfalls den Viewer neu laden.

### project_data-Plugin-Endpunkte (für Roof-S)

Diese Endpunkte gehören zum `project_data`-Plugin, sind aber für Roof-S
relevant zum Lesen der gespeicherten Alignment-Matrix (z.B. wenn Roof-S das
Modell nochmal live anzeigen will statt den Export zu verwenden).

Komplette Doku in
[`coreplugins/project_data/DataPlugin_API.md`](coreplugins/project_data/DataPlugin_API.md).

Kurzfassung für `entry_type: "alignment_transform"`:

#### `GET /api/plugins/project_data/project/{project_pk}/entries/?type=alignment_transform&task={task_id}`

Liefert das Alignment-Eintrag (max. 1 pro Task — die Speichern-Logik im Plugin
PATCHt einen vorhandenen Eintrag statt einen neuen anzulegen).

**Antwort (Array, evtl. leer):**

```json
[
  {
    "id": "uuid-...",
    "project": "project-uuid",
    "task": "task-uuid",
    "entry_type": "alignment_transform",
    "title": "Alignment Transform",
    "content": "",
    "data": {
      "points": [
        [518823.53, 5370580.01, 420.80],
        [518827.03, 5370587.37, 420.78],
        [518827.20, 5370582.29, 420.91]
      ],
      "matrix": [/* 16 floats, column-major */],
      "options": { "reset_z_to_zero": false },
      "enabled": true
    },
    "created_at": "2026-05-11T17:38:50Z",
    "updated_at": "2026-05-11T17:38:50Z"
  }
]
```

Felder im `data`-Objekt:

| Feld | Typ | Bedeutung |
| --- | --- | --- |
| `points` | `[x, y, z][]` (genau 3) | UTM-Koordinaten der vom User gewählten Picker-Punkte |
| `matrix` | `number[16]` | 4×4-Matrix, column-major (Three.js-Format) |
| `options.reset_z_to_zero` | `boolean` | wenn `true`, wurde Z-Nullpunkt auf die gewählte Ebene gesetzt |
| `enabled` | `boolean` | UI-Zustand zum Zeitpunkt des Speicherns (Toggle "Aktiv") — Frontend-Hint, kein Server-State |
| `applied` | `boolean` (optional) | gesetzt vom `apply/`/`revert/`-Endpoint: `true` wenn EPT/GLB im Task aktuell die realignten Versionen sind, `false` nach `revert/`. Fehlt bei Tasks, auf die nie `apply/` aufgerufen wurde |

---

## TypeScript-Typen

Für Roof-S (TypeScript):

```ts
// ─── Realign Plugin ──────────────────────────────────────────────────────────

export type RealignExportStart = { matrix: number[] };   // length 16

export type RealignExportStartResponse = { celery_task_id: string };

export type RealignExportStatus =
  | { ready: false; status: string; progress: number }
  | {
      ready: true;
      status: "SUCCESS";
      progress: 100;
      output: { laz_url: string; glb_url?: string };
    }
  | { ready: true; status: "FAILURE"; error: string };

export type RealignApplyStartResponse = { celery_task_id: string };

export type RealignApplyStatus =
  | { ready: false; status: string; progress: number }
  | { ready: true; status: "SUCCESS"; progress: 100 }
  | { ready: true; status: "FAILURE"; error: string };

export type RealignRevertResponse = { ok: true };

// ─── Alignment Transform Entry (via project_data) ────────────────────────────

export interface AlignmentTransformData {
  points: [number, number, number][];   // exactly 3
  matrix: number[];                     // length 16, column-major
  options: { reset_z_to_zero: boolean };
  enabled: boolean;
  applied?: boolean;                    // set by realign /apply or /revert
}

export interface AlignmentTransformEntry {
  id: string;
  project: string;
  task: string;
  entry_type: "alignment_transform";
  title: string;
  content: string;
  data: AlignmentTransformData;
  created_at: string;
  updated_at: string;
}
```

---

## Roof-S Integration — Beispiel-Code

### Variante A: Realigned Files direkt herunterladen

```ts
async function downloadRealignedAssets(
  apiBase: string,           // z.B. "https://odm.netz-montageserver.de"
  jwt: string,
  projectId: string,
  taskId: string,
): Promise<{ laz: Blob; glb: Blob | null }> {
  const authH = { Authorization: `JWT ${jwt}` };
  const base = `${apiBase}/api/plugins/realign/project/${projectId}/tasks/${taskId}/export`;

  // LAZ holen
  const lazRes = await fetch(`${base}/download/laz/`, { headers: authH });
  if (!lazRes.ok) throw new Error(`LAZ download failed: ${lazRes.status}`);
  const laz = await lazRes.blob();

  // GLB holen (kann 404 sein wenn Task kein Textured Model hatte)
  const glbRes = await fetch(`${base}/download/glb/`, { headers: authH });
  const glb = glbRes.ok ? await glbRes.blob() : null;

  return { laz, glb };
}
```

### Variante B: Export anstossen + Status pollen

Falls Roof-S den Export selbst triggern soll (z.B. weil das WebODM-Frontend
nicht durchlaufen wurde):

```ts
async function triggerAndWaitExport(
  apiBase: string,
  jwt: string,
  projectId: string,
  taskId: string,
  matrix: number[],          // length 16, column-major
): Promise<{ lazUrl: string; glbUrl?: string }> {
  const authH = {
    Authorization: `JWT ${jwt}`,
    "Content-Type": "application/json",
  };
  const base = `${apiBase}/api/plugins/realign/project/${projectId}/tasks/${taskId}/export`;

  // 1. Start
  const startRes = await fetch(`${base}/`, {
    method: "POST",
    headers: authH,
    body: JSON.stringify({ matrix }),
  });
  if (!startRes.ok) {
    const err = await startRes.json().catch(() => ({}));
    throw new Error(`Export start failed: ${err.error || startRes.status}`);
  }
  const { celery_task_id } = (await startRes.json()) as RealignExportStartResponse;

  // 2. Poll bis ready
  while (true) {
    await new Promise((r) => setTimeout(r, 2000));
    const statusRes = await fetch(`${base}/${celery_task_id}/`, { headers: authH });
    if (!statusRes.ok) throw new Error(`Status fetch failed: ${statusRes.status}`);
    const st = (await statusRes.json()) as RealignExportStatus;

    if (!st.ready) continue;
    if (st.status === "FAILURE") throw new Error(`Export fehlgeschlagen: ${st.error}`);

    return {
      lazUrl: `${apiBase}${st.output.laz_url}`,
      glbUrl: st.output.glb_url ? `${apiBase}${st.output.glb_url}` : undefined,
    };
  }
}
```

### Variante C: Gespeicherte Matrix lesen statt Export

Wenn Roof-S das Modell live transformieren will (statt die exportierten Files
zu nutzen), kann es die Matrix direkt aus `project_data` lesen:

```ts
async function loadAlignmentMatrix(
  apiBase: string,
  jwt: string,
  projectId: string,
  taskId: string,
): Promise<number[] | null> {
  const url = new URL(
    `${apiBase}/api/plugins/project_data/project/${projectId}/entries/`,
  );
  url.searchParams.set("type", "alignment_transform");
  url.searchParams.set("task", taskId);

  const res = await fetch(url.toString(), {
    headers: { Authorization: `JWT ${jwt}` },
  });
  if (!res.ok) return null;
  const entries: AlignmentTransformEntry[] = await res.json();
  return entries.length > 0 ? entries[0].data.matrix : null;
}

// Anwendung im Three.js-Viewer:
const matrix = await loadAlignmentMatrix(apiBase, jwt, projectId, taskId);
if (matrix) {
  const M = new THREE.Matrix4().fromArray(matrix);     // accepts column-major
  modelGroup.applyMatrix4(M);
}
```

### Variante D: Persistent anwenden ohne neuen Task

Statt die Files herunterzuladen und in einem neuen Task zu importieren, kann
Roof-S sie **direkt im bestehenden WebODM-Task verankern**. Damit erscheint
das Modell nach Page-Reload automatisch horizontal — ohne Live-Matrix-Apply.

```ts
async function applyRealignment(
  apiBase: string,
  jwt: string,
  projectId: string,
  taskId: string,
): Promise<void> {
  const authH = { Authorization: `JWT ${jwt}`, "Content-Type": "application/json" };
  const base = `${apiBase}/api/plugins/realign/project/${projectId}/tasks/${taskId}`;

  // 1. Apply triggern (setzt voraus, dass Export schon gelaufen ist)
  const startRes = await fetch(`${base}/apply/`, { method: "POST", headers: authH });
  if (!startRes.ok) {
    const err = await startRes.json().catch(() => ({}));
    throw new Error(`Apply failed: ${err.error || startRes.status}`);
  }
  const { celery_task_id } = (await startRes.json()) as RealignApplyStartResponse;

  // 2. Polling
  while (true) {
    await new Promise((r) => setTimeout(r, 2000));
    const statusRes = await fetch(
      `${base}/apply-status/${celery_task_id}/`,
      { headers: authH },
    );
    if (!statusRes.ok) throw new Error(`Status fetch failed: ${statusRes.status}`);
    const st = (await statusRes.json()) as RealignApplyStatus;

    if (!st.ready) continue;
    if (st.status === "FAILURE") throw new Error(`Apply fehlgeschlagen: ${st.error}`);
    return;   // ready === true, status === SUCCESS
  }
}

async function revertRealignment(
  apiBase: string,
  jwt: string,
  projectId: string,
  taskId: string,
): Promise<void> {
  const res = await fetch(
    `${apiBase}/api/plugins/realign/project/${projectId}/tasks/${taskId}/revert/`,
    { method: "POST", headers: { Authorization: `JWT ${jwt}` } },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(`Revert failed: ${err.error || res.status}`);
  }
}
```

Nach `apply()` und `revert()` muss das Roof-S-Frontend den eingebetteten
Viewer (iframe) neu laden, damit die neuen EPT-Tiles und das neue GLB
geladen werden:

```ts
function reloadViewer(iframe: HTMLIFrameElement) {
  const src = iframe.src;
  iframe.src = "";
  setTimeout(() => { iframe.src = src; }, 50);
}
```

### Welche Variante wählen?

| Use-Case | Variante |
| --- | --- |
| Roof-S erzeugt einen neuen Task aus den ausgerichteten Files | **A** — Files herunterladen, in Roof-S-eigene Pipeline importieren |
| Roof-S triggert Ausrichtung selbst (z.B. eigenes Picker-UI) | **B** — Matrix erzeugen, POST Export, downloaden |
| Roof-S zeigt WebODM-Task live und ausgerichtet, ohne neue Files zu erzeugen | **C** — Matrix lesen, im Viewer applyen |
| Roof-S soll den bestehenden Task dauerhaft horizontal machen (auch nach Reload) | **D** — Export + Apply, dann Viewer-Reload |

Roof-S verwendet aktuell **Variante D** (Picker + Save in WebODM, Export +
Apply trigger über Roof-S-UI).

---

## Architektur & Mathematik

### Datenfluss Phase 1 (UI)

```text
[ModelView.jsx (Core, unverändert)]
   │ initialisiert Potree.Viewer + lädt Pointcloud + Mesh
   ▼
[realign/public/main.js]
   - synchrone Registrierung PluginsAPI.ModelView.addActionButton
     (KEIN deps-Array — Race-Condition-Workaround, siehe Erkenntnis #3)
   - parallel SystemJS.import('realign/build/app.js')
   ▼
[realign/public/app.jsx]
   - UI: Waage-Button + Modal (3-Punkt-Picker, Toggle, Save, Reset, Export)
   - Math: 3 Punkte → 4×4-Matrix (Three.js)
   - Persistenz: GET/POST/PATCH/DELETE über project_data-API
   - Apply: direkte Matrix-Schreibung auf Potree-Scene-Children
   - Export: POST realign/export → poll Status → Download-Buttons
```

### Datenfluss Phase 2 (Backend)

```text
POST /api/plugins/realign/.../export/
   │ body: { matrix: [16 floats] }
   ▼
[realign/api.py: ExportView]
   - Validiert Matrix
   - Findet laz_path, glb_path
   - Liest coords.txt → geo_offset = [X, Y] (UTM)
   - run_function_async(_run_export_task, ..., with_progress=True)
   ▼
[realign/tasks.py: _run_export_task] (Celery worker)
   1. M = numpy 4×4 aus dem column-major Array
   2. M_local = T(-offset) · M · T(offset)   für GLB-Lokalkoordinaten
   3. LAZ:  PDAL pipeline (filters.transformation mit M, row-major)
            → output_dir/model_realigned.laz
   4. GLB:  pygltflib.GLTF2.load(glb_path)
            → für jeden Root-Scene-Node: node.matrix = M_local · existing
            → gltf.save(out_glb)
   ▼
[realign/api.py: ExportStatusView] (vom Frontend gepollt)
   → output: { laz_url, glb_url }
   ▼
[realign/api.py: ExportDownloadView]
   → FileResponse(task.assets_path('realigned/model_realigned.{laz|glb}'))
```

### Mathematik

```js
// app.jsx: computeAlignmentMatrix(p1, p2, p3, resetZ)
const v1 = p2.clone().sub(p1);
const v2 = p3.clone().sub(p1);
const n  = v1.cross(v2).normalize();
if (n.z < 0) n.negate();
const up = new THREE.Vector3(0, 0, 1);
const q  = new THREE.Quaternion().setFromUnitVectors(n, up);
const R  = new THREE.Matrix4().makeRotationFromQuaternion(q);

const centroid = p1.clone().add(p2).add(p3).divideScalar(3);

// Rotation um den Centroid — kritisch bei UTM-Koordinaten (~518000, ~5370000):
// Eine Rotation um (0,0,0) ergäbe bei 1° Korrektur >100 km Translation,
// das Modell wäre außerhalb jedes Frustums.
const M = new THREE.Matrix4()
  .makeTranslation(centroid.x, centroid.y, centroid.z)
  .multiply(R)
  .multiply(new THREE.Matrix4().makeTranslation(-centroid.x, -centroid.y, -centroid.z));

if (resetZ) {
  M.premultiply(new THREE.Matrix4().makeTranslation(0, 0, -centroid.z));
}
```

`setFromUnitVectors(n, up)` erzeugt eine Rotation mit Achse = **n × (0,0,1)**.
Die Achse liegt immer in der XY-Ebene → **keine Z-Rotation**. Die Himmelsrichtung
des Gebäudes bleibt erhalten (kritisch für Roof-S).

### Koordinatensysteme (das Verständnis-Problem)

| System | Bereich (Beispiel) | Wer nutzt es |
| --- | --- | --- |
| **UTM (Welt)** | X≈518803-518834, Y≈5370572-5370600, Z≈416-432 | LAZ-File, Potree-Camera, Picker-Output |
| **GLB-Lokal** | X≈-8 - +12, Y≈-12 - +9, Z=absolute (417-431) | GLB-Vertices (Draco) |
| **EPT-Storage-Offset** | X+518818, Y+5370587, Z+424 | nur für Entwine-Integer-Kompression |
| **coords.txt-Offset** | 518815, 5370589 | OBJ-Modell-Translate (WebODM-Standard) |

Der Trick: Die Matrix M wird aus UTM-Picker-Koordinaten berechnet, muss aber
fürs GLB in lokale Koordinaten umgerechnet werden:

```text
M_local = T(-geo_offset) · M · T(geo_offset)
```

Damit wirkt `M_local` auf einen lokalen Vertex `v` wie `M` auf seinen
UTM-Welt-Vertex `v + offset`, das Ergebnis wieder lokalisiert. Für LAZ bleibt
M unverändert (Pointcloud ist in UTM).

---

## Erkenntnisse aus der Implementierung

Sortiert nach Wiederkehrwahrscheinlichkeit.

### 1. ODM exportiert GLB mit Draco-Kompression

`extensionsUsed: ['KHR_draco_mesh_compression', 'KHR_materials_unlit']`. Accessor
`bufferView` ist `None` (Daten stecken in der Draco-Extension), und ohne
DracoPy/draco-CLI kann pygltflib die Vertices nicht lesen oder modifizieren.

**Lösung:** Statt Vertices zu transformieren, M als **GLTF-Node-Matrix** in den
Scene-Graph schreiben. Der GLTF-Renderer (in WebODM/Roof-S/Cesium/modelviewer.dev)
wendet die Matrix beim Rendern an, inklusive korrekter Normal-Transformation
(inverse-transpose des Rotations-Anteils).

### 2. GLB-Vertices sind in lokalen Koordinaten, M wird aus UTM-Koordinaten berechnet

ODM speichert die GLB-Vertices in einem lokalen Koordinatensystem
(X, Y relativ zu `coords.txt`-Offset; Z absolut). Die Matrix M aus dem Picker
basiert aber auf UTM-Koordinaten. Direktes Anwenden von M auf einen lokalen
Vertex ergibt Translationen von ~100 km.

**Lösung:** `geo_offset` aus `coords.txt` lesen, `M_local = T(-offset) · M · T(offset)`
berechnen, **M_local** als Node-Matrix setzen. **M** (UTM) wird unverändert
auf die LAZ angewendet, da die Pointcloud in UTM gespeichert ist.

### 3. `THREE` muss zur Laufzeit gelesen werden

`main.js` lädt das Modul via `SystemJS.import(...)` parallel zum
addActionButton-Sync-Register. `window.THREE` ist zu diesem Zeitpunkt
evtl. noch nicht vom Haupt-Bundle gesetzt. Ein `const THREE = window.THREE;`
auf Top-Level würde `undefined` festfrieren.

**Lösung:** `function getTHREE() { return window.THREE; }` und in jeder
Funktion lokal lesen.

### 4. Plugin-Build-Webpack kennt `THREE` nicht als external

Haupt-Webpack hat `externals: { "THREE": "THREE" }`, das Plugin-Template
([`app/plugins/templates/webpack.config.js.tmpl`](app/plugins/templates/webpack.config.js.tmpl))
nicht. `import * as THREE from 'THREE'` knallt mit "Module not found" im
Plugin-Build. → Immer `window.THREE` direkt nutzen.

### 5. Race im `triggerAddActionButton`-Listener

[`app/static/app/js/classes/plugins/ApiFactory.js:69-79`](app/static/app/js/classes/plugins/ApiFactory.js#L69-L79):
der Response-Listener entfernt sich nach `setTimeout(0)` selbst. Plugins mit
`addActionButton(['deps'], cb)` (async dep-load) verpassen ihn.

**Lösung in main.js:** `addActionButton` **synchron ohne `deps`-Array**
registrieren. Modul parallel via `SystemJS.import` laden. Sync-Callback
liefert entweder direkt das Modul oder einen LazyLoader-Wrapper.

### 6. Korrekte Property-Namen in Potree

- `viewer.scene.scenePointCloud` — großes `C`, kein `s` am Ende.
- Pointcloud-Identifikation: `obj.pcoGeometry !== undefined`.
- Mess-Marker liegen in `viewer.scene.measuringTool.scene` (separate Scene).

### 7. Potree `MeasuringTool` braucht `maxMarkers >= 2`

`startInsertion({maxMarkers: 1})` hängt **keinen `mouseup`-Listener** an —
der initiale Marker bleibt als schwebender (0,0,0)-Cursor hängen. Mit
`maxMarkers: 2` setzt der erste Klick `points[0]`, `points[1]` ist der
Vorschau-Marker.

Korrektes Event: **`marker_dropped`** (Mouseup nach Drag), nicht `marker_added`
(feuert auch für initialen 0,0,0-Marker).

### 8. Auto-Apply (in Phase 1) ist deaktiviert

Potree manipuliert `pointcloud.position` mehrfach in den ersten Sekunden
nach Load. Jeder Auto-Apply wird überschrieben. **Manueller Klick** auf
"Anwenden" funktioniert (Potree-Init ist dann durch).

### 9. WebODM rebuilt JSX nur wenn `build/` fehlt

[`app/plugins/functions.py:132`](app/plugins/functions.py#L132). Bei
`app.jsx`-Änderungen also vor Container-Restart das `build/`-Verzeichnis löschen:

```bash
docker exec webapp rm -rf /webodm/coreplugins/realign/public/build
docker exec webapp kill -HUP $(docker exec webapp pgrep -f gunicorn | head -1)
```

Wenn der automatische Build via Gunicorn-Reload nicht durchläuft (Race mit
mehreren Workern), den Build manuell starten:

```bash
docker exec webapp bash -c "cd /webodm/coreplugins/realign/public && webpack-cli"
```

### 10. Mount NICHT `:ro` für JSX-Build-Plugins

WebODM schreibt eine generierte `webpack.config.js` in `public/`. Mit `:ro`-Mount
gibt es `OSError: Read-only file system`. In
[`docker-compose.override.yml`](docker-compose.override.yml):

```yaml
- ./coreplugins/realign:/webodm/coreplugins/realign     # ohne :ro
```

### 11. `pygltflib` muss im Worker-Container installiert sein

Der Plugin-Code läuft via `run_function_async` im Celery-Worker. Im
[`docker-compose.override.yml`](docker-compose.override.yml) wird `pygltflib`
beim Worker-Start installiert:

```yaml
worker:
  entrypoint: /bin/bash -c "pip install laspy pygltflib -q && ..."
```

Beim ersten Boot dauert das ~10 s. Logs prüfen: `docker logs worker | head -20`.

### 12. `entwine`-Binary muss im Worker-Container verfügbar sein

Für `POST /apply/` ruft der Worker `entwine build -i ... -o ...` per
`subprocess` auf. ODM nutzt EPT zwar schon zur EPT-Erzeugung beim
Task-Processing, aber das Binary könnte trotzdem fehlen oder in einem
anderen Pfad liegen.

Vor erstem Apply-Test prüfen:

```bash
docker exec worker which entwine
docker exec worker entwine --help 2>&1 | head -5
```

Falls das Binary fehlt: Install-Hook in `docker-compose.override.yml`
analog zu pygltflib ergänzen, oder per Conda/apt nachinstallieren.

### 13. Cloudflare cached `build/app.js`

Nach Plugin-Updates Cache purgen oder per `?v=N`-Buster testen.

---

## Deployment & Verifikation

### Plugin-Code-Änderungen deployen

Backend-only (api.py, tasks.py, plugin.py):

```bash
cd ~/WebODM
git fetch myfork && git reset --hard myfork/master
docker exec webapp kill -HUP $(docker exec webapp pgrep -f gunicorn | head -1)
```

Worker liest `tasks.py` zur Laufzeit via `inspect.getsource()` neu — kein
Worker-Restart nötig.

Frontend-Änderungen (app.jsx, main.js):

```bash
cd ~/WebODM
git fetch myfork && git reset --hard myfork/master
docker exec webapp rm -rf /webodm/coreplugins/realign/public/build
docker exec webapp bash -c "cd /webodm/coreplugins/realign/public && webpack-cli"
# Cloudflare-Cache für build/app.js purgen
```

Komplett-Restart (z.B. nach `docker-compose.override.yml`-Änderung):

```bash
cd ~/WebODM
git fetch myfork && git reset --hard myfork/master
docker compose down && docker compose up -d
# Nginx-Reverse-Proxy neu starten, falls 502:
docker restart roof-s-roofinspector_nginx-1
```

### Diagnose-Snippets

LAZ-Bounds prüfen:

```bash
LAZ=/webodm/app/media/project/<pid>/task/<tid>/assets/odm_georeferencing/odm_georeferenced_model.laz
docker exec worker pdal info "$LAZ" --summary 2>/dev/null | python3 -m json.tool | head -20
```

GLB-Node-Matrix prüfen (sollte nach Export kleine Werte zeigen, nicht UTM-Skala):

```bash
docker exec worker python3 -c "
import pygltflib
g = pygltflib.GLTF2.load('/webodm/.../realigned/model_realigned.glb')
scene = g.scenes[g.scene or 0]
for ni in (scene.nodes or []):
    n = g.nodes[ni]
    if n.matrix:
        m = n.matrix
        print(f'Node {ni} translation: [{m[12]:.3f}, {m[13]:.3f}, {m[14]:.3f}]')
"
```

Erwartete Werte: kleine Zahlen (±20m), **nicht** UTM-Skala (518000+).

### End-to-End-Test

1. Task in WebODM öffnen, Waage-Icon klicken.
2. 3 Punkte auf einer waagerechten Fläche wählen (z.B. Dach-First, zwei
   Dach-Eckpunkte).
3. "Speichern" → keine Fehlermeldung.
4. "Modell exportieren" klicken → Progress 0 → 50 → 100.
5. "LAZ" + "GLB" Download-Buttons erscheinen.
6. LAZ in CloudCompare öffnen → Pointcloud horizontal.
7. GLB in <https://modelviewer.dev/editor> öffnen → Modell sichtbar, horizontal,
   Himmelsrichtung unverändert.
8. Beide Files in Roof-S importieren → Modell wird im Roof-S-Viewer
   horizontal angezeigt, Himmelsrichtung wie im Original.

Erfolgreich verifiziert: Mai 2026 (Task `97bccb00-…`, Cloudflare-Tunnel
`odm.netz-montageserver.de`).
