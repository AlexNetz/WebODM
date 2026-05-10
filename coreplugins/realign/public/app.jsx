import React from 'react';
import $ from 'jquery';

// THREE is provided as a global by WebODM's main bundle (see webpack.config.js
// externals: { "THREE": "THREE" }). The plugin-build webpack.config.js.tmpl
// does not list THREE as external, so we cannot `import * as THREE from 'THREE'`
// here without bundling another copy. Read the global directly instead.
const THREE = window.THREE;

const ENTRY_TYPE = 'alignment_transform';

function parseTaskFromUrl() {
  const m = window.location.pathname.match(/\/project\/([^\/]+)\/task\/([^\/]+)/);
  if (!m) return { projectId: null, taskId: null };
  return { projectId: m[1], taskId: m[2] };
}

function computeAlignmentMatrix(p1, p2, p3, resetZ) {
  const v1 = new THREE.Vector3().subVectors(p2, p1);
  const v2 = new THREE.Vector3().subVectors(p3, p1);
  const n = new THREE.Vector3().crossVectors(v1, v2).normalize();
  if (n.z < 0) n.negate();
  const up = new THREE.Vector3(0, 0, 1);
  const q = new THREE.Quaternion().setFromUnitVectors(n, up);
  const M = new THREE.Matrix4().makeRotationFromQuaternion(q);
  if (resetZ) {
    const c = new THREE.Vector3()
      .add(p1).add(p2).add(p3)
      .divideScalar(3)
      .applyMatrix4(M);
    M.premultiply(new THREE.Matrix4().makeTranslation(0, 0, -c.z));
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
    };
    this.activeMeasure = null;
  }

  componentDidMount() {
    this.loadStored();
  }

  componentWillUnmount() {
    this.cancelPicking();
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

  loadStored = () => {
    if (!this.props.projectId || !this.props.taskId) return;
    $.ajax({
      url: this.apiBase(),
      data: { type: ENTRY_TYPE, task: this.props.taskId },
      method: 'GET',
    }).done((entries) => {
      if (!Array.isArray(entries) || entries.length === 0) return;
      const entry = entries[0];
      const data = entry.data || {};
      const points = (data.points || []).map((p) => new THREE.Vector3(p[0], p[1], p[2]));
      while (points.length < 3) points.push(null);
      this.setState({
        entryId: entry.id,
        points: points.slice(0, 3),
        resetZ: !!(data.options && data.options.reset_z_to_zero),
        enabled: !!data.enabled,
      });
      if (data.enabled && data.matrix && data.matrix.length === 16) {
        this.props.controller.applyMatrix(data.matrix);
      }
    });
  };

  cancelPicking = () => {
    if (this.activeMeasure) {
      try {
        this.props.viewer.scene.removeMeasurement(this.activeMeasure);
      } catch (e) {}
      this.activeMeasure = null;
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
    const viewer = this.props.viewer;
    const measure = viewer.measuringTool.startInsertion({
      showDistances: false,
      showAngles: false,
      showCoordinates: true,
      showArea: false,
      showHeight: false,
      closed: false,
      maxMarkers: 1,
      name: 'Realign-Punkt ' + (index + 1),
    });
    this.activeMeasure = measure;
    this.setState({ pickingIndex: index });

    const finalize = () => {
      if (!measure.points || measure.points.length < 1) return;
      const pos = measure.points[0].position.clone();
      const newPoints = this.state.points.slice();
      newPoints[index] = pos;
      try {
        viewer.scene.removeMeasurement(measure);
      } catch (e) {}
      this.activeMeasure = null;
      this.setState({ points: newPoints, pickingIndex: -1 });
    };

    if (typeof measure.addEventListener === 'function') {
      const handler = () => {
        if (measure.points && measure.points.length >= 1) {
          measure.removeEventListener('marker_added', handler);
          finalize();
        }
      };
      measure.addEventListener('marker_added', handler);
    } else {
      const poll = setInterval(() => {
        if (!this.activeMeasure) {
          clearInterval(poll);
          return;
        }
        if (measure.points && measure.points.length >= 1) {
          clearInterval(poll);
          finalize();
        }
      }, 200);
    }
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

  reset = () => {
    if (this.state.busy) return;
    this.cancelPicking();
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

  fmt = (p) => {
    if (!p) return '–';
    return p.x.toFixed(2) + ', ' + p.y.toFixed(2) + ', ' + p.z.toFixed(2);
  };

  render() {
    const { points, pickingIndex, resetZ, enabled, busy, message, messageType } = this.state;
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
      message ? React.createElement('div', { style: messageStyle }, message) : null
    );
  }
}

class RealignButton extends React.Component {
  constructor(props) {
    super(props);
    this.state = { open: false };
  }

  componentDidMount() {
    this.props.controller.autoLoad();
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
  }

  _captureOriginal(obj) {
    if (!obj._realignOriginalMatrix) {
      obj._realignOriginalMatrix = obj.matrix.clone();
      obj._realignOriginalAuto = obj.matrixAutoUpdate;
    }
  }

  _applyToObject(obj, M) {
    this._captureOriginal(obj);
    obj.matrixAutoUpdate = false;
    obj.matrix.copy(obj._realignOriginalMatrix);
    obj.matrix.premultiply(M);
    obj.updateMatrixWorld(true);
  }

  _revertObject(obj) {
    if (obj._realignOriginalMatrix) {
      obj.matrix.copy(obj._realignOriginalMatrix);
      obj.matrixAutoUpdate = obj._realignOriginalAuto !== false;
      obj.updateMatrixWorld(true);
    }
  }

  _collectTargets() {
    const targets = [];
    if (this.viewer.scene && this.viewer.scene.scenePointclouds) {
      this.viewer.scene.scenePointclouds.children.forEach((pc) => targets.push(pc));
    }
    if (this.viewer.scene && this.viewer.scene.scene) {
      this.viewer.scene.scene.children.forEach((obj) => {
        if (this._looksLikeMesh(obj)) targets.push(obj);
      });
    }
    return targets;
  }

  _looksLikeMesh(obj) {
    if (!obj || obj.type === 'Light' || obj.type === 'AmbientLight' ||
        obj.type === 'DirectionalLight' || obj.type === 'PointLight') return false;
    if (obj.isLight) return false;
    let hasMesh = false;
    obj.traverse((child) => {
      if (child.isMesh) hasMesh = true;
    });
    return hasMesh;
  }

  applyMatrix(matrixArray) {
    const M = new THREE.Matrix4().fromArray(matrixArray);
    this.currentMatrix = M;
    this.applied = true;
    this._collectTargets().forEach((obj) => this._applyToObject(obj, M));
    this._startMeshWatcher();
  }

  revertMatrix() {
    this.applied = false;
    this._stopMeshWatcher();
    this._collectTargets().forEach((obj) => this._revertObject(obj));
  }

  _startMeshWatcher() {
    if (this.meshWatcher) return;
    this.meshWatcher = setInterval(() => {
      if (!this.applied || !this.currentMatrix) return;
      const sceneRoot = this.viewer.scene && this.viewer.scene.scene;
      if (!sceneRoot) return;
      sceneRoot.children.forEach((obj) => {
        if (this.knownMeshes.has(obj)) return;
        if (this._looksLikeMesh(obj)) {
          this.knownMeshes.add(obj);
          this._applyToObject(obj, this.currentMatrix);
        }
      });
    }, 1000);
  }

  _stopMeshWatcher() {
    if (this.meshWatcher) {
      clearInterval(this.meshWatcher);
      this.meshWatcher = null;
    }
  }

  autoLoad() {}
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
