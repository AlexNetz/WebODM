# Automatische Dachflächen-Erkennung für Roof-S / WebODM

## Ziel

Aus der vorhandenen Punktwolke automatisch dominante Dachebenen erkennen und als Linien-Overlay im OrthoViewer darstellen (First, Grat, Kehle, Traufe).

---

## Ansatz: RANSAC Plane Detection (Server-seitig)

### Stack
- Python 3.x
- `open3d` oder `pyransac3d` — Ebenensuche
- `numpy`, `scipy` — Geometrie, Schnittlinien
- `shapely` — Alpha-Shape für Grundriss
- WebODM Plugin API — Endpoint + project_data Speicherung

---

## Ablauf

**1. Datei-Zugriff**
Punktwolke liegt unter:
`/webodm/media/project/<id>/task/<id>/assets/odm_georeferencing/odm_georeferenced_model.laz`

Laden via `open3d.t.io.read_point_cloud()` oder `laspy`.

**2. Vorverarbeitung**
- Boden-Ebene (RANSAC, horizontal) entfernen
- Punkte unter Traufhöhe abschneiden
- Downsample auf ~50k Punkte (Voxel-Grid) für Performance

**3. RANSAC Ebenen-Iteration**
```
while verbleibende_punkte > threshold:
    ebene = ransac_fit(punkte)
    if ebene.inlier_count > min_punkte:
        dachflächen.append(ebene)
        punkte -= ebene.inliers
    else:
        break
```
Typisch 4–12 Ebenen für ein Wohnhaus.

**4. Schnittlinien berechnen**
- Paarweise Schnittlinie je zweier benachbarter Ebenen (3D-Linie)
- Auf konvexe Hülle der jeweiligen Inlier-Punkte clippen
- First, Grat, Kehle als 3D-Liniensegmente

**5. Grundriss (optional)**
- Alle Punkte auf XY projizieren
- Alpha-Shape → Polygon = Gebäudegrundriss / Trauflinie

**6. Ergebnis speichern**
Als `project_data`-Entry (`entry_type='roof_outline'`) im bestehenden Plugin:
```json
{
  "planes": [...],
  "edges": [{"start": [x,y,z], "end": [x,y,z], "type": "ridge|hip|valley|eave"}],
  "footprint": [[lat,lng], ...]
}
```

---

## Frontend (OrthoViewer)

- Ergebnis-Linien als SVG-Overlay (bestehender Overlay-Mechanismus)
- In 3D: Potree `THREE.Line`-Objekte in die Scene einfügen
- Toggle-Button in Toolbar: „Dachkanten"

---

## Aufwand-Schätzung

| Schritt | Aufwand |
|---|---|
| Python-Endpoint + RANSAC | ~1 Tag |
| Schnittlinien-Berechnung | ~0.5 Tage |
| Frontend-Overlay | ~0.5 Tage |
| Grundriss (Alpha-Shape) | ~0.5 Tage |
| **Gesamt** | **~2.5 Tage** |

---

## Kritische Unbekannte

- Punktdichte variiert je nach Befliegung — RANSAC-Parameter müssen ggf. pro Projekt getunt werden
- Komplexe Dächer (viele Gauben) → viele kleine Ebenen, Filterung nötig
- LV03-Koordinatensystem muss bei GeoJSON-Export berücksichtigt werden (EPSG:21781 → WGS84)
