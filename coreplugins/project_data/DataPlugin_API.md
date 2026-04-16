# Project Data Plugin – API-Referenz

Dieses Dokument beschreibt die REST-API des **project_data**-Plugins für WebODM.
Das Plugin stellt Endpunkte bereit, um projektspezifische Daten zu speichern:
Anmerkungen, Messungen, Bilder, Texte/Berichte und beliebige Key-Value-Paare.

---

## Inhaltsverzeichnis

1. [Basis-URL und Authentifizierung](#1-basis-url-und-authentifizierung)
2. [Datenmodell](#2-datenmodell)
3. [Endpunkte](#3-endpunkte)
   - [Entries auflisten](#31-entries-auflisten)
   - [Entry erstellen](#32-entry-erstellen)
   - [Entry abrufen](#33-entry-abrufen)
   - [Entry aktualisieren](#34-entry-aktualisieren)
   - [Entry löschen](#35-entry-löschen)
   - [Datei anhängen](#36-datei-anhängen)
   - [Datei herunterladen](#37-datei-herunterladen)
4. [TypeScript-Typen](#4-typescript-typen)
5. [Anwendungsbeispiele](#5-anwendungsbeispiele)
6. [Fehlerbehandlung](#6-fehlerbehandlung)

---

## 1. Basis-URL und Authentifizierung

**Basis-URL aller Endpunkte:**
```
/api/plugins/project_data/
```

**Authentifizierung:** Alle Endpunkte verwenden WebODM-JWT-Token (gleich wie die übrige WebODM-API).

```http
Authorization: JWT <token>
```

Der Token wird via `POST /api/token-auth/` bezogen und in `localStorage` als `ri_jwt` gespeichert.

**Berechtigungen:**
- `GET`-Anfragen erfordern `view_project`-Berechtigung auf dem Projekt.
- `POST`, `PUT`, `PATCH`, `DELETE` erfordern `change_project`-Berechtigung.
- Bei fehlender Berechtigung oder ungültigem Token wird **404** (nicht 403) zurückgegeben – WebODM-Konvention.

---

## 2. Datenmodell

### ProjectEntry

| Feld | Typ | Beschreibung |
|------|-----|--------------|
| `id` | `string` (UUID) | Primärschlüssel, wird automatisch vergeben |
| `project` | `number` | ID des übergeordneten WebODM-Projekts |
| `task` | `string` (UUID) \| `null` | Optionale Zuordnung zu einem WebODM-Task |
| `created_by` | `string` \| `null` | Benutzername des Erstellers |
| `entry_type` | `string` | Typ des Eintrags (siehe unten) |
| `title` | `string` | Kurzer Titel (max. 255 Zeichen) |
| `content` | `string` | Freitext-Inhalt, z. B. HTML (für `text`, `report`) |
| `data` | `object` | Strukturierte Daten als JSON (für `measurement`, `annotation`, `keyvalue`) |
| `created_at` | `string` (ISO 8601) | Erstellungszeitpunkt, read-only |
| `updated_at` | `string` (ISO 8601) | Letzter Update, read-only |
| `attachments` | `Attachment[]` | Liste der angehängten Dateien, read-only |

**Schreibbare Felder** beim Erstellen/Aktualisieren: `entry_type`, `title`, `content`, `data`, `task`

### Entry-Typen

| `entry_type` | Verwendungszweck | Empfohlene `data`-Struktur |
|-------------|-----------------|---------------------------|
| `annotation` | 3D-Anmerkung auf dem Modell | `{ position: [x,y,z], description: string, cameraUrl?: string }` |
| `measurement` | Geometrische Messung | `{ type: string, points: [x,y,z][], value?: number, unit?: string }` |
| `image` | Einzelbild mit Metadaten | `{ caption?: string, coordinates?: [x,y,z] }` + Anhang |
| `text` | Freitext-Notiz | `content`-Feld für HTML, `data` für Metadaten |
| `report` | Fertiggestellter Bericht | `content`-Feld für HTML-Inhalt des Berichts |
| `keyvalue` | Beliebige Schlüssel-Wert-Paare | `{ key1: value1, key2: value2, ... }` |

### ProjectEntryAttachment

| Feld | Typ | Beschreibung |
|------|-----|--------------|
| `id` | `string` (UUID) | Primärschlüssel |
| `filename` | `string` | Originaler Dateiname |
| `mime_type` | `string` | MIME-Typ, z. B. `image/jpeg` |
| `url` | `string` | Absoluter Download-URL (inkl. Domain) |
| `created_at` | `string` (ISO 8601) | Hochlade-Zeitpunkt |

---

## 3. Endpunkte

### 3.1 Entries auflisten

```
GET /api/plugins/project_data/project/{projectId}/entries/
```

Gibt alle Entries des Projekts zurück, absteigend sortiert nach `created_at`.

**Query-Parameter:**

| Parameter | Typ | Beschreibung |
|-----------|-----|--------------|
| `type` | `string` | Filtert nach `entry_type` (z. B. `?type=measurement`) |
| `task` | `string` (UUID) | Filtert nach Task-ID (z. B. `?task=abc-123`) |

**Beispiel-Request:**
```http
GET /api/plugins/project_data/project/42/entries/?type=measurement
Authorization: JWT eyJ0...
```

**Beispiel-Response `200 OK`:**
```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "project": 42,
    "task": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "created_by": "inspector",
    "entry_type": "measurement",
    "title": "Dachneigung Nordseite",
    "content": "",
    "data": {
      "type": "angle",
      "points": [[10.5, 20.3, 5.1], [11.0, 20.3, 6.2]],
      "value": 32.5,
      "unit": "deg"
    },
    "created_at": "2026-04-16T09:15:00Z",
    "updated_at": "2026-04-16T09:15:00Z",
    "attachments": []
  }
]
```

---

### 3.2 Entry erstellen

```
POST /api/plugins/project_data/project/{projectId}/entries/
```

Erstellt einen neuen Entry. Unterstützt sowohl JSON als auch `multipart/form-data` (mit optionaler Datei im gleichen Request).

**Request-Body (JSON):**
```json
{
  "entry_type": "measurement",
  "title": "Dachneigung Nordseite",
  "content": "",
  "data": {
    "type": "angle",
    "points": [[10.5, 20.3, 5.1], [11.0, 20.3, 6.2]],
    "value": 32.5,
    "unit": "deg"
  },
  "task": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

**Request-Body (multipart/form-data mit Datei):**
```
entry_type = image
title      = Rissbildung Ostwand
data       = {"coordinates": [10.5, 20.3, 5.1]}
task       = 3fa85f64-5717-4562-b3fc-2c963f66afa6
file       = <binary>
```

**Response `201 Created`:** Vollständiges Entry-Objekt (inkl. `attachments`, falls Datei mitgeschickt).

---

### 3.3 Entry abrufen

```
GET /api/plugins/project_data/project/{projectId}/entries/{entryId}/
```

**Response `200 OK`:** Vollständiges Entry-Objekt.

---

### 3.4 Entry aktualisieren

```
PUT   /api/plugins/project_data/project/{projectId}/entries/{entryId}/
PATCH /api/plugins/project_data/project/{projectId}/entries/{entryId}/
```

- `PUT`: Alle schreibbaren Felder müssen mitgeschickt werden.
- `PATCH`: Nur die zu ändernden Felder werden übermittelt.

**Beispiel PATCH (Titel umbenennen):**
```json
{ "title": "Neuer Titel" }
```

**Beispiel PATCH (data erweitern):**
```json
{
  "data": {
    "type": "angle",
    "value": 33.1,
    "unit": "deg",
    "note": "Nachmessung"
  }
}
```

**Response `200 OK`:** Aktualisiertes Entry-Objekt.

---

### 3.5 Entry löschen

```
DELETE /api/plugins/project_data/project/{projectId}/entries/{entryId}/
```

Löscht den Entry und alle zugehörigen Anhänge.

**Response `204 No Content`**

---

### 3.6 Datei anhängen

```
POST /api/plugins/project_data/project/{projectId}/entries/{entryId}/attach/
Content-Type: multipart/form-data
```

Hängt eine Datei (Bild, PDF, etc.) an einen bestehenden Entry an. Ein Entry kann mehrere Anhänge haben.

**Form-Felder:**

| Feld | Pflicht | Beschreibung |
|------|---------|--------------|
| `file` | ja | Die Datei als Binary |

**Response `201 Created`:**
```json
{
  "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "filename": "riss_ostwand.jpg",
  "mime_type": "image/jpeg",
  "url": "https://odm.example.com/api/plugins/project_data/project/42/entries/550e8400.../attachments/riss_ostwand.jpg/",
  "created_at": "2026-04-16T09:30:00Z"
}
```

---

### 3.7 Datei herunterladen

```
GET /api/plugins/project_data/project/{projectId}/entries/{entryId}/attachments/{filename}/
```

Liefert die Datei direkt zurück (mit korrektem `Content-Type`). Der `url`-Wert aus dem Attachment-Objekt ist direkt verwendbar.

**Response `200 OK`:** Datei als Binärantwort mit `Content-Disposition: inline; filename="..."`.

---

## 4. TypeScript-Typen

```typescript
// -------------------------------------------------------
// Typen für die project_data Plugin API
// -------------------------------------------------------

export type EntryType =
  | 'annotation'
  | 'measurement'
  | 'image'
  | 'text'
  | 'report'
  | 'keyvalue';

export interface Attachment {
  id: string;
  filename: string;
  mime_type: string;
  url: string;
  created_at: string;
}

export interface ProjectEntry {
  id: string;
  project: number;
  task: string | null;
  created_by: string | null;
  entry_type: EntryType;
  title: string;
  content: string;
  data: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  attachments: Attachment[];
}

export interface CreateEntryPayload {
  entry_type: EntryType;
  title?: string;
  content?: string;
  data?: Record<string, unknown>;
  task?: string | null;
}

export interface UpdateEntryPayload {
  entry_type?: EntryType;
  title?: string;
  content?: string;
  data?: Record<string, unknown>;
  task?: string | null;
}

// Empfohlene data-Strukturen pro Typ (nicht erzwungen, nur Konvention)

export interface MeasurementData {
  type: 'distance' | 'area' | 'height' | 'angle' | 'azimuth';
  points: [number, number, number][];
  value?: number;
  unit?: string;
  note?: string;
}

export interface AnnotationData {
  position: [number, number, number];
  description?: string;
  cameraUrl?: string;    // ohne JWT – wird zur Laufzeit ergänzt
}

export interface KeyValueData {
  [key: string]: string | number | boolean | null;
}
```

---

## 5. Anwendungsbeispiele

### API-Client (wiederverwendbar)

```typescript
const BASE = '/api/plugins/project_data';

function authHeader(token: string): HeadersInit {
  return { Authorization: `JWT ${token}` };
}

export const DataPluginApi = {
  listEntries: (projectId: number, token: string, type?: EntryType, taskId?: string) => {
    const params = new URLSearchParams();
    if (type)   params.set('type', type);
    if (taskId) params.set('task', taskId);
    const query = params.toString() ? `?${params}` : '';
    return fetch(`${BASE}/project/${projectId}/entries/${query}`, {
      headers: authHeader(token),
    }).then(r => r.json() as Promise<ProjectEntry[]>);
  },

  createEntry: (projectId: number, token: string, payload: CreateEntryPayload) =>
    fetch(`${BASE}/project/${projectId}/entries/`, {
      method: 'POST',
      headers: { ...authHeader(token), 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then(r => r.json() as Promise<ProjectEntry>),

  createEntryWithFile: (projectId: number, token: string, payload: CreateEntryPayload, file: File) => {
    const form = new FormData();
    (Object.entries(payload) as [string, unknown][]).forEach(([k, v]) => {
      if (v !== undefined && v !== null) {
        form.append(k, typeof v === 'object' ? JSON.stringify(v) : String(v));
      }
    });
    form.append('file', file);
    return fetch(`${BASE}/project/${projectId}/entries/`, {
      method: 'POST',
      headers: authHeader(token),
      body: form,
    }).then(r => r.json() as Promise<ProjectEntry>);
  },

  updateEntry: (projectId: number, entryId: string, token: string, payload: UpdateEntryPayload) =>
    fetch(`${BASE}/project/${projectId}/entries/${entryId}/`, {
      method: 'PATCH',
      headers: { ...authHeader(token), 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then(r => r.json() as Promise<ProjectEntry>),

  deleteEntry: (projectId: number, entryId: string, token: string) =>
    fetch(`${BASE}/project/${projectId}/entries/${entryId}/`, {
      method: 'DELETE',
      headers: authHeader(token),
    }),

  attachFile: (projectId: number, entryId: string, token: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return fetch(`${BASE}/project/${projectId}/entries/${entryId}/attach/`, {
      method: 'POST',
      headers: authHeader(token),
      body: form,
    }).then(r => r.json() as Promise<Attachment>);
  },
};
```

---

### Messung per Button speichern

```typescript
async function saveMeasurement(
  projectId: number,
  taskId: string,
  token: string,
  measurement: { name: string; type: string; points: [number, number, number][] }
) {
  return DataPluginApi.createEntry(projectId, token, {
    entry_type: 'measurement',
    title: measurement.name,
    task: taskId,
    data: {
      type: measurement.type,
      points: measurement.points,
    } satisfies MeasurementData,
  });
}
```

---

### Anmerkung mit Kamerabild speichern

```typescript
async function saveAnnotationWithImage(
  projectId: number,
  taskId: string,
  token: string,
  position: [number, number, number],
  title: string,
  description: string,
  cameraImageFile: File
) {
  return DataPluginApi.createEntryWithFile(
    projectId,
    token,
    {
      entry_type: 'annotation',
      title,
      task: taskId,
      data: {
        position,
        description,
        // cameraUrl wird ohne JWT gespeichert – zur Laufzeit ergänzen
      } satisfies AnnotationData,
    },
    cameraImageFile
  );
}
```

---

### Bericht aus dem Texteditor speichern

```typescript
async function saveReport(
  projectId: number,
  taskId: string,
  token: string,
  title: string,
  htmlContent: string    // z. B. aus CKEditor: editor.getData()
) {
  return DataPluginApi.createEntry(projectId, token, {
    entry_type: 'report',
    title,
    task: taskId,
    content: htmlContent,
    data: {
      generated_at: new Date().toISOString(),
    },
  });
}
```

---

### Alle Einträge eines Tasks laden

```typescript
async function loadTaskEntries(projectId: number, taskId: string, token: string) {
  const entries = await DataPluginApi.listEntries(projectId, token, undefined, taskId);

  const measurements = entries.filter(e => e.entry_type === 'measurement');
  const annotations  = entries.filter(e => e.entry_type === 'annotation');
  const reports      = entries.filter(e => e.entry_type === 'report');

  return { measurements, annotations, reports };
}
```

---

### Attachment-URL mit JWT für `<img>` verwenden

Attachment-URLs aus der API enthalten **keinen JWT**. Für `<img src="...">` muss der Token als Query-Parameter ergänzt werden (WebODM-Konvention für Mediendateien):

```typescript
function attachmentSrcUrl(url: string, token: string): string {
  return `${url}?jwt=${token}`;
}

// Verwendung:
// <img src={attachmentSrcUrl(attachment.url, token)} />
```

> **Hinweis:** Der `Authorization`-Header funktioniert für direkte Browser-Requests (z. B. `<img>`, `<a>`) nicht. Der JWT als Query-Parameter ist bei diesen Endpunkten korrekt.

---

## 6. Fehlerbehandlung

| HTTP-Status | Bedeutung |
|-------------|-----------|
| `200 OK` | Erfolgreiche GET/PUT/PATCH-Anfrage |
| `201 Created` | Entry oder Anhang erfolgreich erstellt |
| `204 No Content` | Entry erfolgreich gelöscht |
| `400 Bad Request` | Validierungsfehler (Antwort enthält Fehlerobjekt) |
| `404 Not Found` | Projekt/Entry nicht gefunden **oder** keine Berechtigung (WebODM-Konvention) |

**400-Antwort-Beispiel:**
```json
{
  "entry_type": ["Invalid entry_type. Must be one of: annotation, measurement, image, text, report, keyvalue"]
}
```

**Empfohlenes Fehler-Handling:**

```typescript
async function apiCall<T>(fn: () => Promise<Response>): Promise<T> {
  const res = await fn();
  if (!res.ok) {
    if (res.status === 404) throw new Error('Nicht gefunden oder kein Zugriff');
    const body = await res.json().catch(() => ({}));
    throw new Error(JSON.stringify(body));
  }
  return res.json();
}
```

---

*Dieses Dokument bezieht sich auf Plugin-Version `1.0.0` (WebODM ≥ 2.5.0).*
