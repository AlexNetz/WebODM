# Roof-S Alignment-Feature (Modell horizontal ausrichten)

## Context

Alex möchte 3D-Modelle direkt in Roof-S horizontal ausrichten können, ohne als neues Projekt zu importieren (verliert Kameraaufnahmen).
Das WebODM-Realign-Plugin existiert bereits (Phase 1: Picker + Matrix, Phase 2: LAZ+GLB-Export) und wird um EPT-Regenerierung + Apply-Endpunkt erweitert.

**Workflow:** Waage-Icon → Modal im rechten Sidebar → 3 Punkte picken → Matrix speichern → "Vorschau" / "Modell exportieren" / "Dauerhaft anwenden".

---

## Architektur

```
┌──────────────────────────────────────────────────────────────────────┐
│  ROOF-S FRONTEND                                                     │
│   ┌──────────────┐    ┌─────────────────────────────────────────┐  │
│   │ OrthoViewer  │───▶│ AlignmentModal (right sidebar)          │  │
│   │ + Waage-Btn  │    │  - 3-Punkt-Picker (measuringTool,M=3)   │  │
│   │              │    │  - Matrix-Vorschau (apply auf Scene)    │  │
│   │              │    │  - Speichern → project_data             │  │
│   │              │    │  - Exportieren → realign POST export    │  │
│   │              │    │  - Dauerhaft anwenden → realign apply   │  │
│   └──────────────┘    └─────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│  WEBODM (Backend) — Erweiterung Realign-Plugin                       │
│   • bestehend: POST /export, GET /status, GET /download/{laz|glb}    │
│   • NEU: POST /apply  → Entwine-EPT-Regenerierung + File-Swap        │
│   • NEU: POST /revert → Original-EPT/GLB zurücktauschen              │
│   • NEU: GET  /apply-status/{task_id} → Progress des apply           │
│   • bestehend: project_data 'alignment_transform' Eintrag            │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Phase A: WebODM Realign-Plugin Backend-Erweiterung

**Verantwortung Alex (in WebODM-Fork).** Folgende neue Endpunkte unter `/api/plugins/realign/project/{pid}/tasks/{tid}/`:

### A1. `POST .../apply/`

Ablauf im Celery-Worker:
1. Liest Matrix aus body (oder aus project_data alignment_transform falls leer)
2. Falls noch keine `realigned/model_realigned.laz` vorhanden → erst Export laufen lassen
3. **EPT regenerieren:** `entwine build -i realigned/model_realigned.laz -o realigned/entwine_pointcloud/`
4. **Backup anlegen** (idempotent — nur beim ersten Apply):
   - `entwine_pointcloud/` → `entwine_pointcloud_original/`
   - `odm_texturing/odm_textured_model_geo.glb` → `odm_textured_model_geo.original.glb`
5. **File-Swap:**
   - `realigned/entwine_pointcloud/` → `entwine_pointcloud/`
   - `realigned/model_realigned.glb` → `odm_texturing/odm_textured_model_geo.glb`
6. project_data alignment_transform: `data.applied: true` setzen

Antwort: `{ celery_task_id: string }` (wie bei Export).

### A2. `POST .../revert/`

1. `entwine_pointcloud/` löschen (oder als `_realigned` archivieren)
2. `entwine_pointcloud_original/` → `entwine_pointcloud/`
3. `odm_textured_model_geo.original.glb` → `odm_textured_model_geo.glb`
4. project_data alignment_transform: `data.applied: false`

Antwort: `{ ok: true }` (synchron, einfache rename-Ops).

### A3. `GET .../apply-status/{celery_task_id}/`

Pollt Apply-Celery-Task. Antwort identisch zu `export-status` (`ready`, `status`, `progress`).

### A4. Schema-Erweiterung `alignment_transform.data`

```ts
{
  points:  [number,number,number][]  // 3 UTM-Punkte (bestehend)
  matrix:  number[16]                 // (bestehend)
  options: { reset_z_to_zero: boolean }
  enabled: boolean
  applied: boolean   // NEU — true wenn EPT/GLB getauscht sind
}
```

### A5. Hinweise / Risiken

- `entwine build` benötigt im worker-Container installiertes `entwine` binary (vermutlich schon da, da ODM EPT erzeugt; sonst per `docker-compose.override.yml` Install-Hook ergänzen)
- EPT-Build dauert je nach Pointcloud-Größe 30 s – mehrere Minuten → muss Celery sein, kein synchroner Request
- Pfade in WebODM: Task assets liegen unter `app/media/project/{pid}/task/{tid}/assets/`
- Bei `apply` mit Backup-Logik: idempotent halten (zweimaliges Apply überschreibt nicht das Original-Backup)

---

## Phase B: Roof-S Frontend (Implementierung)

### B1. Neue Datei: `frontend/src/lib/alignment.ts`

Matrix-Math (Three.js):

```ts
import * as THREE from 'three'  // bereits transitiv da
export function computeAlignmentMatrix(
  p1: [number, number, number],
  p2: [number, number, number],
  p3: [number, number, number],
  resetZ: boolean,
): number[] {
  const v1 = new THREE.Vector3(...p2).sub(new THREE.Vector3(...p1))
  const v2 = new THREE.Vector3(...p3).sub(new THREE.Vector3(...p1))
  const n  = v1.cross(v2).normalize()
  if (n.z < 0) n.negate()
  const q = new THREE.Quaternion().setFromUnitVectors(n, new THREE.Vector3(0, 0, 1))
  const R = new THREE.Matrix4().makeRotationFromQuaternion(q)
  const c = new THREE.Vector3(
    (p1[0] + p2[0] + p3[0]) / 3,
    (p1[1] + p2[1] + p3[1]) / 3,
    (p1[2] + p2[2] + p3[2]) / 3,
  )
  const M = new THREE.Matrix4()
    .makeTranslation(c.x, c.y, c.z)
    .multiply(R)
    .multiply(new THREE.Matrix4().makeTranslation(-c.x, -c.y, -c.z))
  if (resetZ) M.premultiply(new THREE.Matrix4().makeTranslation(0, 0, -c.z))
  return M.toArray()  // column-major
}

// Applies matrix to Potree scene children (pointcloud + mesh)
// for live preview only. Returns cleanup function that restores identity.
export function applyMatrixToPotreeScene(
  iwin: Window,
  matrix: number[],
): () => void { /* siehe B5 */ }
```

### B2. API-Erweiterung `frontend/src/api/webodm.ts`

Neue Funktionen (nutzen das vorhandene `projectData.list/create/update/delete` und neuen `realign`-Namespace):

```ts
export type AlignmentTransformData = {
  points:  [number, number, number][]
  matrix:  number[]      // length 16
  options: { reset_z_to_zero: boolean }
  enabled: boolean
  applied: boolean
}

export const alignment = {
  load(projectId: number, taskId: string): Promise<AlignmentTransformData | null>,
  save(projectId: number, taskId: string, data: AlignmentTransformData): Promise<void>,
  clear(projectId: number, taskId: string): Promise<void>,
}

export const realign = {
  export(projectId: number, taskId: string, matrix: number[]):     Promise<{ celery_task_id: string }>,
  exportStatus(projectId: number, taskId: string, celeryId: string): Promise<RealignExportStatus>,
  apply(projectId: number, taskId: string):                          Promise<{ celery_task_id: string }>,
  applyStatus(projectId: number, taskId: string, celeryId: string):  Promise<RealignExportStatus>,
  revert(projectId: number, taskId: string):                         Promise<{ ok: boolean }>,
  downloadUrl(projectId: number, taskId: string, kind: 'laz'|'glb'): string,  // mit JWT
}
```

`alignment.load/save/clear` ist Wrapper über existierende `projectData.*` API mit `entry_type='alignment_transform'` (Upsert-Pattern wie bei `report` in webodm.ts).

### B3. Neue Datei: `frontend/src/components/AlignmentModal.tsx`

Rechtes Sidebar-Panel (Pattern wie `ReportBuilderModal`/`MeasurementDetailModal`):

```
┌────────────────────────────────────────────────┐
│  ⚖  Modell ausrichten              [×]         │
├────────────────────────────────────────────────┤
│                                                │
│  1. Drei Punkte auf einer waagrechten          │
│     Fläche wählen                              │
│                                                │
│     [● Punkte wählen]   3/3 gesetzt ✓         │
│                                                │
│  2. Optionen                                   │
│     □ Z-Achse auf Null setzen                  │
│                                                │
│  3. Matrix:  applied / saved / unsaved         │
│     [Berechnete Werte (read-only) …]          │
│                                                │
│  ── Vorschau ───────────────────────────       │
│     [Vorschau anwenden] [Vorschau zurücks.]   │
│                                                │
│  ── Persistenz ─────────────────────────       │
│     [Speichern]       [Löschen]                │
│                                                │
│  ── Files erzeugen ─────────────────────       │
│     [Realignte Files erzeugen]                 │
│     Progress: ░░░░░░░░░░ 0%                    │
│     [↓ LAZ]  [↓ GLB]   (nach Erfolg)           │
│                                                │
│  ── Anzeige ───────────────────────────────    │
│     Status: ● Nicht angewendet                 │
│     [Dauerhaft anwenden]                       │
│     Progress: ░░░░░░░░░░ 0%                    │
│     [Original wiederherstellen]                │
│                                                │
└────────────────────────────────────────────────┘
```

**Props:**
```ts
interface AlignmentModalProps {
  projectId: number
  taskId:    string
  iframeRef: React.RefObject<HTMLIFrameElement | null>
  onClose:   () => void
  onReloadViewer: () => void  // nach apply/revert iframe neu laden
}
```

**State:**
```ts
const [picking,     setPicking]     = useState(false)
const [points,      setPoints]      = useState<[number,number,number][]>([])
const [resetZ,      setResetZ]      = useState(false)
const [previewOn,   setPreviewOn]   = useState(false)
const [saved,       setSaved]       = useState<AlignmentTransformData | null>(null)
const [exporting,   setExporting]   = useState(false)
const [exportProgress, setExportProgress] = useState(0)
const [exportUrls,  setExportUrls]  = useState<{ laz: string; glb?: string } | null>(null)
const [applying,    setApplying]    = useState(false)
const [applyProgress, setApplyProgress] = useState(0)
```

**Init:** beim Mount `alignment.load(projectId, taskId)` → falls vorhanden, `points` / `resetZ` aus saved laden.

### B4. Neue Datei: `frontend/src/components/AlignmentModal.module.css`

Pattern wie `MeasurementDetailModal.module.css` / `ReportBuilderModal.module.css` — rechte Sidebar 420 px breit, `var(--bg-elevated)`, scrollbar bei Bedarf.

### B5. Live-Preview-Mechanismus

Matrix wird auf Potree-Scene angewendet:
```ts
const win = iframeRef.current?.contentWindow as PotreeWindow | null
const T   = win?.THREE
if (!win?.viewer || !T) return
const M = new T.Matrix4().fromArray(matrix)  // column-major
// Pointcloud
const pc = win.viewer.scene.scenePointCloud.children.find((o: any) => o.pcoGeometry)
if (pc) { pc.matrix.copy(M); pc.matrixAutoUpdate = false }
// Mesh (textured model)
const meshScene = win.viewer.scene.scene as any   // THREE.Scene
const mesh = meshScene.children.find(...)         // selector tbd via DevTools
if (mesh) { mesh.matrix.copy(M); mesh.matrixAutoUpdate = false }
```

Cleanup (Vorschau zurücksetzen): `matrix.identity()` + `matrixAutoUpdate = true`.

Hinweis: Live-Preview funktioniert nur **nach** Potrees Init (analog Erkenntnis #8 im Plugin-Doc — Manuelles Apply funktioniert, nur Auto-Apply auf Load nicht). Beim Mounting des Modals ist der Viewer schon initialisiert, daher kein Timing-Problem.

### B6. 3-Punkt-Picker

Wenn `picking === true` und User klickt "Punkte wählen":
```ts
win.viewer.measuringTool.startInsertion({
  showDistances: false,
  showArea:      false,
  closed:        false,
  maxMarkers:    3,
  name:          'Alignment',
})
```

Listener auf `measurement_added` Event (analog bestehender Mess-Logik in OrthoViewer):
```ts
scene.addEventListener('marker_dropped', (e) => {
  // e.measurement.points[i].position
  if (e.measurement.name === 'Alignment') {
    setPoints(prev => {
      const next = [...prev, [e.position.x, e.position.y, e.position.z]]
      if (next.length === 3) {
        // Auto-finish: Mess-Objekt aus scene entfernen (war nur fürs Picken)
        win.viewer.scene.removeMeasurement(e.measurement)
        setPicking(false)
      }
      return next
    })
  }
})
```

Alternative: nutze nicht-finished measuring tool — User klickt 3-mal, dann automatisch beendet (maxMarkers handlet das schon).

### B7. OrthoViewer-Integration

In `frontend/src/components/OrthoViewer.tsx`:

**Import (neuer Icon):**
```tsx
import { Scale } from 'lucide-react'  // bereits Lucide-Bibliothek
// alternativ: Compass (schon importiert) oder Ruler-Variante
```

**State (~Zeile 320):**
```tsx
const [alignmentOpen, setAlignmentOpen] = useState(false)
```

**Toolbar-Button (in der 3D-Tools-Sektion, ~Zeile 2750):**
```tsx
<button
  className={`${styles.btn} ${alignmentOpen ? styles.active : ''}`}
  onClick={() => setAlignmentOpen(o => !o)}
  title="Modell horizontal ausrichten"
>
  <Scale size={13} /> Ausrichten
</button>
```

**Modal-Render (im JSX ~Zeile 3625, neben anderen Modal-Renders):**
```tsx
{is3D && alignmentOpen && (
  <AlignmentModal
    projectId={projectId}
    taskId={taskId}
    iframeRef={iframeRef}
    onClose={() => setAlignmentOpen(false)}
    onReloadViewer={() => {
      // iframe neu laden nach apply/revert
      if (iframeRef.current) {
        const src = iframeRef.current.src
        iframeRef.current.src = ''
        setTimeout(() => { if (iframeRef.current) iframeRef.current.src = src }, 50)
      }
    }}
  />
)}
```

### B8. Polling für Export / Apply

```ts
async function pollUntilDone(
  fn: () => Promise<RealignExportStatus>,
  onProgress: (p: number) => void,
): Promise<RealignExportStatus & { ready: true }> {
  while (true) {
    await new Promise(r => setTimeout(r, 2000))
    const s = await fn()
    if (!s.ready) { onProgress(s.progress); continue }
    return s
  }
}
```

Bei Apply nach `ready: true` → `onReloadViewer()` aufrufen (lädt iframe neu, neue EPT-Tiles werden geladen).

---

## Kritische Dateien

| Datei | Aktion |
|---|---|
| `frontend/src/lib/alignment.ts` | **NEU** — Matrix-Math + Apply-Helper |
| `frontend/src/components/AlignmentModal.tsx` | **NEU** — UI |
| `frontend/src/components/AlignmentModal.module.css` | **NEU** — Styles |
| `frontend/src/api/webodm.ts` | **Ändern** — `alignment.*` + `realign.*` Namespaces ergänzen |
| `frontend/src/components/OrthoViewer.tsx` | **Ändern** — Waage-Button + Modal-Render + State |
| WebODM `coreplugins/realign/api.py` | **Ändern (Alex)** — `apply/`, `revert/`, `apply-status/` URLs |
| WebODM `coreplugins/realign/tasks.py` | **Ändern (Alex)** — `_run_apply_task` (Entwine + File-Swap) |
| WebODM `coreplugins/realign/plugin.py` | **Ändern (Alex)** — URL-Patterns |

---

## Verification

### Frontend isoliert
1. `npx tsc --noEmit` — kein Fehler
2. Roof-S öffnen → Waage-Icon erscheint in 3D-Toolbar
3. Klick → AlignmentModal öffnet sich im rechten Sidebar
4. "Punkte wählen" → 3 Klicks auf Dachfläche → 3/3 Punkte ✓
5. "Vorschau anwenden" → Modell richtet sich sichtbar aus
6. "Vorschau zurücksetzen" → Modell zurück auf Original
7. "Speichern" → project_data `alignment_transform` Entry vorhanden (API-Test)
8. Modal schließen + öffnen → gespeicherte Werte werden geladen

### Mit Backend (nach Plugin-Update auf Server)
9. "Realignte Files erzeugen" → Progress bis 100%, LAZ + GLB Download-Buttons erscheinen
10. LAZ downloaden → in CloudCompare öffnen → horizontal
11. "Dauerhaft anwenden" → Progress (Entwine läuft), bei 100% iframe-Reload → Modell wird sofort horizontal angezeigt
12. WebODM-Asset-Pfade prüfen: `entwine_pointcloud/` ist nun das realignte, `entwine_pointcloud_original/` existiert als Backup
13. "Original wiederherstellen" → iframe-Reload → Modell wieder im Original-Zustand
14. Backup-Ordner wieder weg, Original-EPT an seinem Platz

---

## Offene Punkte / Risiken

- **Entwine-Binary im Worker:** vor Phase A muss verifiziert werden, dass `entwine` im Celery-Worker verfügbar ist. Falls nicht: Install-Hook in `docker-compose.override.yml` analog zu pygltflib (Erkenntnis #11).
- **EPT-Generierungszeit:** bei großen Modellen (>500 MB Pointcloud) kann Apply mehrere Minuten dauern. UI muss klar kommunizieren ("Bitte nicht schließen").
- **Mesh-Selektor:** im Live-Preview-Code (B5) muss der genaue THREE.Scene-Selektor fürs Textured-Model per DevTools verifiziert werden. Fallback: alle non-pointcloud Object3D durchgehen.
- **Cleanup beim Abbruch:** wenn User Modal schließt während Picking aktiv → measuringTool.cancel + Vorschau zurücksetzen.
- **applied vs enabled:** zwei separate Flags im project_data — `enabled` (UI-Toggle, derzeit für ggf. Live-Apply ohne Backend), `applied` (Backend-File-Swap-Status). Im Roof-S-Flow nutzen wir primär `applied`; `enabled` kann später für eine Frontend-Toggle-Variante dienen falls EPT-Regenerierung nicht gewünscht ist.
