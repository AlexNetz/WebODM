// Race-Condition-Workaround:
// PluginsAPI.ModelView.triggerAddActionButton entfernt seinen Response-Listener nach
// dem ersten ::Response. Wenn ein Plugin async deps lädt (`addActionButton([deps], cb)`),
// kommt sein Response zu spät und der Button geht verloren. Wir registrieren deshalb
// SYNCHRON ohne deps — und laden das Modul parallel im main.js. Falls der Trigger
// kommt, bevor das Modul ready ist, returnt der Callback einen LazyLoader, der das
// React-Element nachträglich mountet.
(function () {
  var App = null;
  var pendingLoaders = [];

  SystemJS.import('realign/build/app.js').then(function (m) {
    App = (m && m.default) || null;
    pendingLoaders.forEach(function (l) { l.notify(); });
    pendingLoaders.length = 0;
  }).catch(function (e) {
    console.error('[realign] Module load failed:', e);
  });

  class RealignLazyLoader extends React.Component {
    constructor(props) {
      super(props);
      this.state = { ready: !!App };
      this._mounted = false;
      this._notify = this._notify.bind(this);
    }

    componentDidMount() {
      this._mounted = true;
      if (!this.state.ready) {
        pendingLoaders.push({ notify: this._notify });
      }
    }

    componentWillUnmount() {
      this._mounted = false;
    }

    _notify() {
      if (this._mounted) this.setState({ ready: true });
    }

    render() {
      if (!this.state.ready || !App) return null;
      if (!this._appInstance) {
        this._appInstance = new App(this.props.viewer);
      }
      return this._appInstance.render();
    }
  }

  PluginsAPI.ModelView.addActionButton(function (options) {
    if (App) {
      return new App(options.viewer).render();
    }
    return React.createElement(RealignLazyLoader, { viewer: options.viewer });
  });
})();
