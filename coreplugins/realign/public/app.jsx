import React from 'react';
import $ from 'jquery';

// THREE is provided as a global by WebODM's main bundle (see webpack.config.js
// externals: { "THREE": "THREE" }). The plugin-build webpack.config.js.tmpl
// does not list THREE as external, so we cannot `import * as THREE from 'THREE'`
// here without bundling another copy.
//
// IMPORTANT: We must read window.THREE *at runtime*, not at module-load time.
// main.js triggers SystemJS.import('.../app.js') early on (to win the
// addActionButton race), so this module can be evaluated *before* the main
// bundle has set window.THREE. Reading it on top-level would freeze it as
// undefined permanently.
function getTHREE() { return window.THREE; }

const ENTRY_TYPE = 'alignment_transform';

function parseTaskFromUrl() {
  const m = window.location.pathname.match(/\/project\/([^\/]+)\/task\/([^\/]+)/);
  if (!m) return { projectId: null, taskId: null };
  return { projectId: m[1], taskId: m[2] };
}

function computeAlignmentMatrix(p1, p2, p3, resetZ) {
  const THREE = getTHREE();
  // Rotation um den Centroid der drei Punkte, NICHT um den Welt-Origin —
  // bei UTM-Koordinaten (~518000, ~5370000) würde eine Origin-Rotation das
  // Modell weit aus dem Sichtbereich katapultieren.
  const v1 = new THREE.Vector3().subVectors(p2, p1);
  const v2 = new THREE.Vector3().subVectors(p3, p1);
  const n = new THREE.Vector3().crossVectors(v1, v2).normalize();
  if (n.z < 0) n.negate();
  const up = new THREE.Vector3(0, 0, 1);
  const q = new THREE.Quaternion().setFromUnitVectors(n, up);
  const R = new THREE.Matrix4().makeRotationFromQuaternion(q);

  const centroid = new THREE.Vector3()
    .add(p1).add(p2).add(p3)
    .divideScalar(3);

  // M = T(centroid) · R · T(-centroid)
  const M = new THREE.Matrix4()
    .makeTranslation(centroid.x, centroid.y, centroid.z)
    .multiply(R)
    .multiply(new THREE.Matrix4().makeTranslation(-centroid.x, -centroid.y, -centroid.z));

  if (resetZ) {
    // Centroid bleibt nach R an seiner Z-Position (weil wir um ihn rotiert haben).
    // Verschiebe das ganze Modell um -centroid.z, damit die gewählte Ebene auf Z=0 liegt.
    M.premultiply(new THREE.Matrix4().makeTranslation(0, 0, -centroid.z));
  }

  return M;
}

class RealignPanel extends React.Component {
  constructor(props) {
    super(props);
    this.state = {
      points: [null, null, null],
      resetZ: false,
      enabled: false,
      entryId: null,
      pickingIndex: -1,
      busy: false,
      message: '',
      messageType: '',
      exportStatus: null,   // null | 'running' | 'done' | 'failed'
      exportProgress: 0,
      exportTaskId: null,
      exportUrls: null,     // { laz_url?, glb_url? }
      exportError: null,
    };
    this.activeMeasure = null;
    this.persistentMeasures = [null, null, null];
    this._exportPollTimer = null;
  }

  componentDidMount() {
    const ctrl = this.props.controller;
    const hydrate = () => {
      if (!ctrl.lastData) return;
      const THREE = getTHREE();
      const data = ctrl.lastData;
      const points = (data.points || []).map((p) => new THREE.Vector3(p[0], p[1], p[2]));
      while (points.length < 3) points.push(null);
      this.setState({
        entryId: ctrl.lastEntryId,
        points: points.slice(0, 3),
        resetZ: !!(data.options && data.options.reset_z_to_zero),
        enabled: ctrl.applied,  // wird derzeit immer false sein, da Auto-Apply aus
      });
    };
    if (ctrl.isLoaded()) {
      hydrate();
    } else {
      ctrl.loadFromBackend(this.props.projectId, this.props.taskId, hydrate);
    }
  }

  componentWillUnmount() {
    this.cancelPicking();
    this.clearPersistentMarkers();
    if (this._exportPollTimer) {
      clearInterval(this._exportPollTimer);
      this._exportPollTimer = null;
    }
  }

  setMessage = (text, type = 'info', timeout = 4000) => {
    this.setState({ message: text, messageType: type });
    if (timeout > 0) {
      setTimeout(() => {
        this.setState((s) =>
          s.message === text ? { message: '', messageType: '' } : null
        );
      }, timeout);
    }
  };

  apiBase = () => `/api/plugins/project_data/project/${this.props.projectId}/entries/`;

  removeMeasureSafely = (m) => {
    if (!m) return;
    try { this.props.viewer.scene.removeMeasurement(m); } catch (e) {}
  };

  cancelPicking = () => {
    if (this.activeMeasure) {
      this.removeMeasureSafely(this.activeMeasure);
      this.activeMeasure = null;
    }
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
    if (this.state.pickingIndex !== -1) this.setState({ pickingIndex: -1 });
  };

  startPicking = (index) => {
    if (this.state.enabled) {
      this.props.controller.revertMatrix();
      this.setState({ enabled: false });
      this.setMessage(
        'Korrektur temporär deaktiviert, damit Punkte im Original-Koordinatensystem gepickt werden.',
        'info'
      );
    }
    this.cancelPicking();

    if (this.persistentMeasures[index]) {
      this.removeMeasureSafely(this.persistentMeasures[index]);
      this.persistentMeasures[index] = null;
    }

    const viewer = this.props.viewer;
    // maxMarkers=2: erst dann hängt Potrees MeasuringTool den Mouseup-Listener an
    // (siehe potree.js: `if (measure.maxMarkers > 1)`). Bei maxMarkers=1 bleibt der
    // initiale (0,0,0)-Marker als schwebender Cursor — kein Klick wird erfasst.
    // Mit 2 setzt der erste Klick den fixen Marker[0], Marker[1] ist der neue
    // (hängengebliebene) Vorschau-Marker.
    const measure = viewer.measuringTool.startInsertion({
      showDistances: false,
      showAngles: false,
      showCoordinates: true,
      showArea: false,
      showHeight: false,
      closed: false,
      maxMarkers: 2,
      name: 'Realign-Punkt ' + (index + 1),
    });
    this.activeMeasure = measure;
    this.setState({ pickingIndex: index });

    let finalized = false;
    const extractPosition = (m) => {
      // Position aus dem Marker lesen — primär points[0].position, fallback sphere[0].position
      if (!m) return null;
      if (m.points && m.points[0] && m.points[0].position && typeof m.points[0].position.clone === 'function') {
        const p = m.points[0].position;
        if (p.x !== 0 || p.y !== 0 || p.z !== 0) return p.clone();
      }
      if (m.spheres && m.spheres[0] && m.spheres[0].position && typeof m.spheres[0].position.clone === 'function') {
        const p = m.spheres[0].position;
        if (p.x !== 0 || p.y !== 0 || p.z !== 0) return p.clone();
      }
      return null;
    };

    const finalize = () => {
      if (finalized) return;
      const m = measure;
      const pos = extractPosition(m);
      if (!pos) return;
      finalized = true;
      try {
        while (m.points && m.points.length > 1) m.removeMarker(m.points.length - 1);
      } catch (e) {}
      this.persistentMeasures[index] = m;
      this.activeMeasure = null;
      if (this._pollTimer) {
        clearInterval(this._pollTimer);
        this._pollTimer = null;
      }
      try { measure.removeEventListener('marker_dropped', onDropped); } catch (e) {}
      const newPoints = this.state.points.slice();
      newPoints[index] = pos;
      this.setState({ points: newPoints, pickingIndex: -1 });
    };

    const onDropped = (e) => {
      if (finalized) return;
      // marker_dropped feuert beim Mouseup nach Drag — wir warten kurz, damit
      // Potrees insertionCallback (der den Vorschau-Marker hinzufügt + Position
      // finalisiert) garantiert vor unserem extractPosition durch ist.
      setTimeout(() => { if (!finalized) finalize(); }, 50);
    };
    if (typeof measure.addEventListener === 'function') {
      measure.addEventListener('marker_dropped', onDropped);
    }

    // Polling-Fallback, falls das Event aus irgendeinem Grund nicht feuert
    this._pollTimer = setInterval(() => {
      if (!this.activeMeasure || finalized) {
        if (this._pollTimer) {
          clearInterval(this._pollTimer);
          this._pollTimer = null;
        }
        return;
      }
      if (measure.points && measure.points.length >= 2) {
        finalize();
      }
    }, 200);
  };

  toggleApply = () => {
    const next = !this.state.enabled;
    if (next) {
      const M = this.currentMatrix();
      if (!M) {
        this.setMessage('Bitte zuerst alle drei Punkte wählen.', 'warning');
        return;
      }
      this.props.controller.applyMatrix(M.toArray());
    } else {
      this.props.controller.revertMatrix();
    }
    this.setState({ enabled: next });
  };

  currentMatrix = () => {
    const [p1, p2, p3] = this.state.points;
    if (!p1 || !p2 || !p3) return null;
    return computeAlignmentMatrix(p1, p2, p3, this.state.resetZ);
  };

  save = () => {
    if (this.state.busy) return;
    const M = this.currentMatrix();
    if (!M) {
      this.setMessage('Bitte zuerst alle drei Punkte wählen.', 'warning');
      return;
    }
    const payload = {
      entry_type: ENTRY_TYPE,
      title: 'Alignment Transform',
      task: this.props.taskId,
      data: {
        points: this.state.points.map((p) => [p.x, p.y, p.z]),
        matrix: M.toArray(),
        options: { reset_z_to_zero: this.state.resetZ },
        enabled: this.state.enabled,
      },
    };
    this.setState({ busy: true });
    const onDone = (entry) => {
      this.props.controller.lastData = entry.data || {};
      this.props.controller.lastEntryId = entry.id;
      this.setState({ entryId: entry.id, busy: false });
      this.setMessage('Gespeichert.', 'success');
    };
    const onFail = (xhr) => {
      this.setState({ busy: false });
      this.setMessage('Speichern fehlgeschlagen: ' + (xhr.statusText || xhr.status), 'error');
    };
    if (this.state.entryId) {
      $.ajax({
        url: this.apiBase() + this.state.entryId + '/',
        method: 'PATCH',
        contentType: 'application/json',
        data: JSON.stringify(payload),
      }).done(onDone).fail(onFail);
    } else {
      $.ajax({
        url: this.apiBase(),
        method: 'POST',
        contentType: 'application/json',
        data: JSON.stringify(payload),
      }).done(onDone).fail(onFail);
    }
  };

  clearPersistentMarkers = () => {
    for (let i = 0; i < this.persistentMeasures.length; i++) {
      if (this.persistentMeasures[i]) {
        this.removeMeasureSafely(this.persistentMeasures[i]);
        this.persistentMeasures[i] = null;
      }
    }
  };

  reset = () => {
    if (this.state.busy) return;
    this.cancelPicking();
    this.clearPersistentMarkers();
    this.props.controller.revertMatrix();
    if (!this.state.entryId) {
      this.setState({
        points: [null, null, null],
        resetZ: false,
        enabled: false,
      });
      return;
    }
    this.setState({ busy: true });
    $.ajax({
      url: this.apiBase() + this.state.entryId + '/',
      method: 'DELETE',
    }).always(() => {
      this.props.controller.lastData = null;
      this.props.controller.lastEntryId = null;
      this.setState({
        points: [null, null, null],
        resetZ: false,
        enabled: false,
        entryId: null,
        busy: false,
      });
      this.setMessage('Zurückgesetzt.', 'success');
    });
  };

  exportApiBase = () =>
    `/api/plugins/realign/project/${this.props.projectId}/tasks/${this.props.taskId}/export/`;

  startExport = () => {
    const ctrl = this.props.controller;
    if (!ctrl.lastData || !ctrl.lastData.matrix) {
      this.setMessage('Keine gespeicherte Matrix. Bitte erst speichern.', 'warning');
      return;
    }
    this.setState({ exportStatus: 'running', exportProgress: 0, exportUrls: null, exportError: null });
    $.ajax({
      url: this.exportApiBase(),
      method: 'POST',
      contentType: 'application/json',
      data: JSON.stringify({ matrix: ctrl.lastData.matrix }),
    }).done((resp) => {
      this.setState({ exportTaskId: resp.celery_task_id });
      this._startExportPoll(resp.celery_task_id);
    }).fail((xhr) => {
      const err = (xhr.responseJSON && xhr.responseJSON.error) || xhr.statusText || 'Unbekannter Fehler';
      this.setState({ exportStatus: 'failed', exportError: 'Export-Start fehlgeschlagen: ' + err });
    });
  };

  _startExportPoll = (celeryTaskId) => {
    if (this._exportPollTimer) clearInterval(this._exportPollTimer);
    this._exportPollTimer = setInterval(() => {
      $.ajax({
        url: this.exportApiBase() + celeryTaskId + '/',
        method: 'GET',
      }).done((resp) => {
        if (!resp.ready) {
          this.setState({ exportProgress: resp.progress || 0 });
          return;
        }
        clearInterval(this._exportPollTimer);
        this._exportPollTimer = null;
        if (resp.status === 'SUCCESS') {
          this.setState({ exportStatus: 'done', exportProgress: 100, exportUrls: resp.output || {} });
        } else {
          this.setState({ exportStatus: 'failed', exportError: resp.error || 'Export fehlgeschlagen.' });
        }
      });
      // Transient poll errors are silently ignored — next tick retries.
    }, 2000);
  };

  fmt = (p) => {
    if (!p) return '–';
    return p.x.toFixed(2) + ', ' + p.y.toFixed(2) + ', ' + p.z.toFixed(2);
  };

  render() {
    const {
      points, pickingIndex, resetZ, enabled, busy, message, messageType,
      exportStatus, exportProgress, exportUrls, exportError,
    } = this.state;
    const allPicked = points.every((p) => !!p);

    const pointRow = (i) => (
      React.createElement('div', { key: i, style: { display: 'flex', alignItems: 'center', marginBottom: 6 } },
        React.createElement('button', {
          type: 'button',
          className: 'btn btn-sm btn-secondary',
          style: { width: 110, marginRight: 8 },
          onClick: () => this.startPicking(i),
          disabled: busy,
        },
          React.createElement('i', {
            className: pickingIndex === i ? 'fa fa-crosshairs fa-spin' : 'fa fa-crosshairs',
            style: { marginRight: 6 },
          }),
          'Punkt ' + (i + 1)
        ),
        React.createElement('span', {
          style: {
            fontFamily: 'monospace',
            fontSize: 11,
            color: points[i] ? '#fff' : '#888',
          },
        }, this.fmt(points[i]))
      )
    );

    const messageStyle = {
      marginTop: 8,
      padding: '4px 8px',
      borderRadius: 3,
      fontSize: 12,
      backgroundColor:
        messageType === 'error' ? '#5a1f1f' :
        messageType === 'warning' ? '#5a4a1f' :
        messageType === 'success' ? '#1f5a2a' : '#1f3a5a',
    };

    return React.createElement('div', {
      style: {
        position: 'fixed',
        bottom: 70,
        right: 16,
        width: 320,
        maxHeight: 'calc(100vh - 100px)',
        overflowY: 'auto',
        backgroundColor: 'rgba(40, 40, 40, 0.95)',
        color: '#fff',
        padding: 12,
        borderRadius: 4,
        zIndex: 1000,
        fontSize: 13,
        boxShadow: '0 2px 8px rgba(0,0,0,0.5)',
      },
    },
      React.createElement('div', {
        style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 },
      },
        React.createElement('strong', null, 'Ausrichtung'),
        React.createElement('button', {
          type: 'button',
          className: 'btn btn-sm btn-link',
          style: { color: '#aaa', padding: 0 },
          onClick: this.props.onClose,
          title: 'Schliessen',
        }, React.createElement('i', { className: 'fa fa-times' }))
      ),
      React.createElement('div', { style: { fontSize: 11, color: '#bbb', marginBottom: 8 } },
        'Drei Punkte auf einer waagerechten Fläche wählen.'
      ),
      pointRow(0),
      pointRow(1),
      pointRow(2),
      React.createElement('label', {
        style: { display: 'flex', alignItems: 'center', marginTop: 8, cursor: 'pointer' },
      },
        React.createElement('input', {
          type: 'checkbox',
          checked: resetZ,
          onChange: (e) => this.setState({ resetZ: e.target.checked }),
          style: { marginRight: 6 },
        }),
        React.createElement('span', { style: { fontSize: 12 } },
          'Z-Nullpunkt auf gewählte Ebene setzen'
        )
      ),
      React.createElement('div', { style: { display: 'flex', gap: 6, marginTop: 12, flexWrap: 'wrap' } },
        React.createElement('button', {
          type: 'button',
          className: 'btn btn-sm ' + (enabled ? 'btn-success' : 'btn-primary'),
          onClick: this.toggleApply,
          disabled: !allPicked || busy,
          style: { flex: '1 1 auto' },
        },
          React.createElement('i', {
            className: enabled ? 'fa fa-eye' : 'fa fa-eye-slash',
            style: { marginRight: 4 },
          }),
          enabled ? 'Aktiv' : 'Anwenden'
        ),
        React.createElement('button', {
          type: 'button',
          className: 'btn btn-sm btn-secondary',
          onClick: this.save,
          disabled: !allPicked || busy,
        },
          React.createElement('i', {
            className: busy ? 'fa fa-circle-notch fa-spin' : 'fa fa-save',
            style: { marginRight: 4 },
          }),
          'Speichern'
        ),
        React.createElement('button', {
          type: 'button',
          className: 'btn btn-sm btn-danger',
          onClick: this.reset,
          disabled: busy,
          title: 'Löscht die Ausrichtung',
        }, React.createElement('i', { className: 'fa fa-trash' }))
      ),
      message ? React.createElement('div', { style: messageStyle }, message) : null,

      // ── Export section ────────────────────────────────────────────────────
      React.createElement('hr', { style: { borderColor: '#555', margin: '10px 0 8px' } }),
      React.createElement('button', {
        type: 'button',
        className: 'btn btn-sm btn-info',
        onClick: this.startExport,
        disabled: !this.state.entryId || exportStatus === 'running',
        style: { width: '100%' },
      },
        React.createElement('i', {
          className: exportStatus === 'running' ? 'fa fa-circle-notch fa-spin' : 'fa fa-download',
          style: { marginRight: 4 },
        }),
        'Modell exportieren'
      ),

      // Progress bar (visible while Celery task runs)
      exportStatus === 'running' ? React.createElement('div', { style: { marginTop: 6 } },
        React.createElement('div', { style: { background: '#444', borderRadius: 3, height: 8 } },
          React.createElement('div', {
            style: {
              background: '#17a2b8',
              borderRadius: 3,
              height: 8,
              width: (exportProgress || 0) + '%',
              transition: 'width 0.3s',
            },
          })
        ),
        React.createElement('div', { style: { fontSize: 11, color: '#aaa', marginTop: 2, textAlign: 'center' } },
          (exportProgress || 0) + '%'
        )
      ) : null,

      // Download buttons (visible when done)
      exportStatus === 'done' && exportUrls ? React.createElement('div', {
        style: { marginTop: 6, display: 'flex', gap: 6 },
      },
        exportUrls.laz_url ? React.createElement('a', {
          href: exportUrls.laz_url,
          className: 'btn btn-sm btn-outline-success',
          style: { flex: '1 1 auto', textAlign: 'center' },
          download: true,
        },
          React.createElement('i', { className: 'fa fa-cloud-download', style: { marginRight: 4 } }),
          'LAZ'
        ) : null,
        exportUrls.glb_url ? React.createElement('a', {
          href: exportUrls.glb_url,
          className: 'btn btn-sm btn-outline-success',
          style: { flex: '1 1 auto', textAlign: 'center' },
          download: true,
        },
          React.createElement('i', { className: 'fa fa-cube', style: { marginRight: 4 } }),
          'GLB'
        ) : null,
      ) : null,

      // Export error
      exportStatus === 'failed' ? React.createElement('div', {
        style: { marginTop: 6, padding: '4px 8px', borderRadius: 3, fontSize: 12, backgroundColor: '#5a1f1f' },
      }, exportError || 'Export fehlgeschlagen.') : null,
    );
  }
}

class RealignButton extends React.Component {
  constructor(props) {
    super(props);
    this.state = { open: false };
  }

  componentDidMount() {
    this.props.controller.loadFromBackend(this.props.projectId, this.props.taskId);
  }

  toggleOpen = () => this.setState({ open: !this.state.open });
  close = () => this.setState({ open: false });

  render() {
    const { open } = this.state;
    const { projectId, taskId, viewer, controller } = this.props;
    return React.createElement('div', { style: { display: 'inline-block' } },
      React.createElement('button', {
        type: 'button',
        className: 'btn btn-sm btn-secondary',
        title: 'Modell ausrichten',
        style: { padding: '5px 9px' },
        onClick: this.toggleOpen,
      }, React.createElement('i', { className: 'fa fa-balance-scale' })),
      open ? React.createElement(RealignPanel, {
        projectId,
        taskId,
        viewer,
        controller,
        onClose: this.close,
      }) : null
    );
  }
}

class RealignController {
  constructor(viewer) {
    this.viewer = viewer;
    this.currentMatrix = null;
    this.applied = false;
    this.meshWatcher = null;
    this.knownMeshes = new WeakSet();
    this.lastData = null;
    this.lastEntryId = null;
    this.projectId = null;
    this.taskId = null;
    this._loadCallbacks = [];
    this._loaded = false;
  }

  apiBase() {
    return '/api/plugins/project_data/project/' + this.projectId + '/entries/';
  }

  loadFromBackend(projectId, taskId, callback) {
    this.projectId = projectId;
    this.taskId = taskId;
    if (callback) this._loadCallbacks.push(callback);
    if (!projectId || !taskId) {
      this._loaded = true;
      this._fireCallbacks();
      return;
    }
    $.ajax({
      url: this.apiBase(),
      data: { type: 'alignment_transform', task: taskId },
      method: 'GET',
    }).done((entries) => {
      if (Array.isArray(entries) && entries.length > 0) {
        const entry = entries[0];
        this.lastData = entry.data || {};
        this.lastEntryId = entry.id;
        // Auto-Apply ist temporär deaktiviert — Potrees Init-Sequence
        // (loadProject, pointcloud_added, internal pos-resets) kollidiert
        // mit unserem applyMatrix. Der User muss manuell "Aktiv" klicken.
      }
      this._loaded = true;
      this._fireCallbacks();
    }).fail(() => {
      this._loaded = true;
      this._fireCallbacks();
    });
  }

  _scheduleAutoApply(matrix) {
    // Auto-Apply muss warten bis:
    // (a) Die Pointcloud zur scenePointCloud hinzugefügt wurde, UND
    // (b) Potree's loadProject() gelaufen ist (setzt pointcloud.position aus
    //     gespeicherter potree_scene). loadProject läuft in einem `update`-Hook
    //     einmalig, daher 5 s Buffer nach pointcloud_added.
    // Sonst capturt _captureOriginal eine Zwischen-Position als "Original" und
    // die Pointcloud bleibt unsichtbar an der falschen Stelle.
    const apply = () => {
      setTimeout(() => {
        if (!this.applied) this.applyMatrix(matrix);
      }, 15000);
    };
    const scene = this.viewer && this.viewer.scene;
    if (!scene) { apply(); return; }
    const hasPC = scene.pointclouds && scene.pointclouds.length > 0;
    if (hasPC) {
      apply();
      return;
    }
    const onAdded = () => {
      try { scene.removeEventListener('pointcloud_added', onAdded); } catch (e) {}
      apply();
    };
    try { scene.addEventListener('pointcloud_added', onAdded); }
    catch (e) { setTimeout(apply, 3000); }
  }

  _fireCallbacks() {
    const cbs = this._loadCallbacks;
    this._loadCallbacks = [];
    cbs.forEach((cb) => { try { cb(); } catch (e) {} });
  }

  isLoaded() { return this._loaded; }

  _captureOriginal(obj) {
    if (!obj._realignOriginalCaptured) {
      obj._realignOriginalMatrix = obj.matrix.clone();
      obj._realignOriginalAutoUpdate = obj.matrixAutoUpdate;
      obj._realignOriginalFrustumCulled = obj.frustumCulled;
      obj._realignOriginalCaptured = true;
    }
  }

  _applyToObject(obj, M) {
    this._captureOriginal(obj);
    const THREE = getTHREE();
    // newMatrix = M · originalMatrix
    const newM = new THREE.Matrix4().copy(M).multiply(obj._realignOriginalMatrix);
    obj._realignTargetMatrix = newM;
    obj.frustumCulled = false;
    obj.matrixAutoUpdate = false;
    obj.matrix.copy(newM);
    obj.updateMatrixWorld(true);
  }

  _revertObject(obj) {
    if (obj._realignOriginalCaptured) {
      obj._realignTargetMatrix = null;
      obj.matrix.copy(obj._realignOriginalMatrix);
      obj.matrixAutoUpdate = obj._realignOriginalAutoUpdate;
      obj.frustumCulled = obj._realignOriginalFrustumCulled;
      obj.updateMatrixWorld(true);
    }
  }

  _installFrameHook() {
    if (this._frameHookInstalled) return;
    this._frameHookInstalled = true;
    // Bei jedem Render-Frame zwingen wir matrix auf unsere Ziel-Matrix zurück.
    // Potree setzt zwischendurch position/quaternion, was bei matrixAutoUpdate=false
    // zwar matrix nicht überschreiben sollte — aber updateMatrix wird intern oft
    // gerufen. Mit dem update-Hook sind wir robust.
    this._updateListener = () => {
      if (!this.applied) return;
      this._collectTargets().forEach((obj) => {
        if (obj._realignTargetMatrix) {
          obj.matrix.copy(obj._realignTargetMatrix);
          obj.matrixAutoUpdate = false;
          obj.matrixWorldNeedsUpdate = true;
        }
      });
    };
    try { this.viewer.addEventListener('update', this._updateListener); }
    catch (e) {}
  }

  _uninstallFrameHook() {
    if (!this._frameHookInstalled) return;
    try { this.viewer.removeEventListener('update', this._updateListener); }
    catch (e) {}
    this._frameHookInstalled = false;
    this._updateListener = null;
  }

  _collectTargets() {
    // Wir iterieren Children einzeln statt die Top-Level-Scenes als Ganzes zu
    // transformieren — Three.js-`Scene`-Objects in Potree's Setup zeigen sonst
    // gar nicht mehr (Renderer scheint sie als Identity zu erwarten).
    //
    // - scenePointCloud.children: PointCloudOctree(s) + referenceFrame + Lichter
    //   → nur PointCloudOctree erkennen (pcoGeometry-Property), referenceFrame
    //     hat matrixAutoUpdate=false und ist Potree-internes Coord-System.
    // - scene.children: texturiertes Mesh + Camera-Marker + Lichter
    //   → alles außer Lichtern transformieren (Camera-Marker mit-rotieren ist OK,
    //     sie repräsentieren Drohnen-Positionen und sollen mit dem Modell drehen).
    const targets = [];
    const s = this.viewer && this.viewer.scene;
    if (!s) return targets;
    if (s.scenePointCloud && Array.isArray(s.scenePointCloud.children)) {
      s.scenePointCloud.children.forEach((obj) => {
        if (this._isPointCloud(obj)) targets.push(obj);
      });
    }
    if (s.scene && Array.isArray(s.scene.children)) {
      s.scene.children.forEach((obj) => {
        if (this._isLight(obj)) return;
        targets.push(obj);
      });
    }
    return targets;
  }

  _isPointCloud(obj) {
    return obj && obj.pcoGeometry !== undefined;
  }

  _isLight(obj) {
    if (!obj) return true;
    if (obj.isLight) return true;
    const t = obj.type;
    return t === 'AmbientLight' || t === 'DirectionalLight' ||
           t === 'PointLight' || t === 'SpotLight' || t === 'HemisphereLight';
  }

  applyMatrix(matrixArray) {
    const THREE = getTHREE();
    const M = new THREE.Matrix4().fromArray(matrixArray);
    this.currentMatrix = M;
    this.applied = true;
    if (!this.knownTargets) this.knownTargets = new WeakSet();
    this._collectTargets().forEach((obj) => {
      this.knownTargets.add(obj);
      this._applyToObject(obj, M);
    });
    this._installFrameHook();
    this._startWatcher();
  }

  revertMatrix() {
    this.applied = false;
    this._uninstallFrameHook();
    this._stopWatcher();
    // Captured Targets sind auf der Pointcloud / Mesh markiert. Iterieren wir
    // auch über bekannte Targets aus dem WeakSet kann nicht — also alles
    // sammeln, was potentiell betroffen ist.
    const all = this._collectTargets();
    all.forEach((obj) => this._revertObject(obj));
  }

  _startWatcher() {
    if (this.watcher) return;
    if (!this.knownTargets) this.knownTargets = new WeakSet();
    this._collectTargets().forEach((t) => this.knownTargets.add(t));
    // Erfasst nachträglich geladene Targets (z.B. Mesh nach Toggle) und wrappt sie.
    // Mit dem Wrapper-Pattern brauchen wir kein Force-Pose mehr — der Wrapper bleibt
    // unangetastet von Potree.
    this.watcher = setInterval(() => {
      if (!this.applied || !this.currentMatrix) return;
      const targets = this._collectTargets();
      targets.forEach((obj) => {
        if (this.knownTargets.has(obj)) return;
        this.knownTargets.add(obj);
        this._applyToObject(obj, this.currentMatrix);
      });
    }, 1000);
  }

  _stopWatcher() {
    if (this.watcher) {
      clearInterval(this.watcher);
      this.watcher = null;
    }
  }
}

export default class App {
  constructor(viewer) {
    this.viewer = viewer;
    const ids = parseTaskFromUrl();
    this.projectId = ids.projectId;
    this.taskId = ids.taskId;
    this.controller = new RealignController(viewer);
  }

  render() {
    if (!this.projectId || !this.taskId) return null;
    return React.createElement(RealignButton, {
      projectId: this.projectId,
      taskId: this.taskId,
      viewer: this.viewer,
      controller: this.controller,
    });
  }
}
