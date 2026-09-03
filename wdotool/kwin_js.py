"""The one KWin script wdotool loads, as a string.

It is pushed into KWin with org.kde.kwin.Scripting.loadScript(), which is
unprivileged on Plasma 5.27 and 6 alike -- nothing has to be installed for
the user, unlike the GNOME bridge extension. KWin gives the script a fresh
QJSEngine with `workspace`, `options`, the `KWin` enums, QTimer and exactly
these globals: readConfig, callDBus, registerShortcut, register*ScreenEdge,
registerUserActionsMenu (+ console.*). There is no print(), no way to return
a value from run() and no way to register a D-Bus object, so results go out
through callDBus() to a name the Python side owns.

The script is a single constant: the caller prepends one `var A = {...}`
line (see backend_kwin._source) naming the destination, a per-call token and
the operation. Every path answers exactly once -- a JS exception is caught
and reported as {ok:false}, because KWin logs script errors to the journal
only and still replies OK to run().

Version differences live here, not in Python:
  windowList()/clientList(), activeWindow/activeClient, currentDesktop as a
  VirtualDesktop object (6) or a 1-based int (5.27), w.desktops[] (6) vs
  w.desktop (5.27), captionNormal (6) vs caption, w.windowId (X11 windows on
  5.27, gone on 6), w.shade (5.27, shading removed in 6), workspace.
  raiseWindow (6, absent on 5.27).
"""

SCRIPT = r"""
/* ---------------------------------------------------------------- plumbing */
function _ret(x) {
  callDBus(A.dest, A.path, A.iface, "Result", A.token, JSON.stringify(x));
}
function _ev(u, c) {
  callDBus(A.dest, A.path, A.iface, "Event", A.token, u, c);
}
var SIX = (typeof workspace.windowList === "function");

function wins() {
  var l = SIX ? workspace.windowList() : workspace.clientList();
  return l ? l : [];
}
function uuid(w) {
  return String(w.internalId).replace(/[{}]/g, "").toLowerCase();
}
function num(v) {
  return (typeof v === "number" && isFinite(v)) ? Math.round(v) : 0;
}
function str(v) {
  return (typeof v === "string") ? v : (v === undefined || v === null ? "" : String(v));
}
function desktopList() {
  /* 6: list<VirtualDesktop>; 5.27: an int count. */
  var d = workspace.desktops;
  return (d && typeof d.length === "number") ? d : null;
}
function dnum(w) {                       /* 0-based index, -1 sticky/unknown */
  if (w.onAllDesktops) {
    return -1;
  }
  if (SIX) {
    var d = w.desktops;
    return (d && d.length) ? num(d[0].x11DesktopNumber) - 1 : -1;
  }
  return w.desktop > 0 ? w.desktop - 1 : -1;
}
function oncur(w) {
  if (w.onAllDesktops) {
    return true;
  }
  var c = workspace.currentDesktop;
  if (SIX) {
    var d = w.desktops;
    if (!d) {
      return false;
    }
    for (var i = 0; i < d.length; i++) {
      if (String(d[i].id) === String(c.id)) {
        return true;
      }
    }
    return false;
  }
  return w.desktop === c;
}
function title(w) {
  if (typeof w.captionNormal === "string") {
    return w.captionNormal;                   /* 6.x: no suffix, by design */
  }
  /* 5.27 has no captionNormal property. caption is captionNormal plus KWin's
     disambiguation suffix for duplicate titles: " <2>" and a U+200E left-to-
     right mark, which no client ever puts at the end of its own title. */
  return str(w.caption).replace(/ <\d+>\u200e$/, "");
}
function find(u) {
  var l = wins();
  for (var i = 0; i < l.length; i++) {
    if (uuid(l[i]) === u) {
      return l[i];
    }
  }
  return null;
}

/* ------------------------------------------------------------------- facts */
function info(w) {
  var g = w.frameGeometry || {};
  var xid = w.windowId;                       /* X11Window, Plasma 5.27 only */
  var tf = w.transientFor;
  var out = w.output;
  return {
    u: uuid(w), t: title(w), c: str(w.resourceClass), n: str(w.resourceName),
    p: num(w.pid),
    x: num(g.x), y: num(g.y), w: num(g.width), h: num(g.height),
    f: !!w.active, m: !!w.minimized, hi: !!w.hidden,
    d: dnum(w), oc: oncur(w), st: !!w.onAllDesktops,
    fs: !!w.fullScreen, mm: num(w.maximizeMode),
    ka: !!w.keepAbove, kb: !!w.keepBelow,
    sk: !!w.skipTaskbar, sp: !!w.skipPager, at: !!w.demandsAttention,
    nb: !!w.noBorder, sh: (typeof w.shade === "boolean") ? w.shade : null,
    ty: num(w.windowType), ly: num(w.layer), so: num(w.stackingOrder),
    tf: tf ? uuid(tf) : "", df: str(w.desktopFileName), ro: str(w.windowRole),
    xid: (typeof xid === "number" && xid > 0) ? xid : 0,
    o: (out && out.name) ? str(out.name) : ""
  };
}
function listAll() {
  var l = wins(), out = [];
  for (var i = 0; i < l.length; i++) {
    out.push(info(l[i]));
  }
  out.sort(function (a, b) { return a.so - b.so; });   /* bottom -> top */
  return out;
}
function screenInfo() {
  var s = workspace.virtualScreenSize || {};
  var wa = (typeof KWin !== "undefined" && KWin.WorkArea !== undefined)
           ? KWin.WorkArea : 5;
  var areas = [], d = desktopList();
  var n = d ? d.length : num(workspace.desktops);
  for (var i = 0; i < n; i++) {
    var r = null;
    try {
      r = SIX ? workspace.clientArea(wa, workspace.activeScreen, d[i])
              : workspace.clientArea(wa, workspace.activeScreen, i + 1);
    } catch (e) {
      r = null;
    }
    areas.push(r ? [num(r.x), num(r.y), num(r.width), num(r.height)]
                 : [0, 0, 0, 0]);
  }
  return {w: num(s.width), h: num(s.height), areas: areas};
}

/* ----------------------------------------------------------------- actions */
function unclamp(w) {
  /* moveResize() is clamped or ignored for maximized, quick-tiled, custom-
     tiled and fullscreen windows: undo those first, or the write is a no-op. */
  try {
    if (w.maximizeMode) {
      w.setMaximize(false, false);
    }
  } catch (e) { }
  try {
    if (w.tile) {
      w.tile = null;
    }
  } catch (e) { }
  try {
    if (w.fullScreen) {
      w.fullScreen = false;
    }
  } catch (e) { }
}
function geom(w, x, y, ww, hh) {
  unclamp(w);
  var g = w.frameGeometry;
  w.frameGeometry = {
    x: (x === null) ? g.x : x, y: (y === null) ? g.y : y,
    width: (ww === null) ? g.width : ww, height: (hh === null) ? g.height : hh
  };
  var r = w.frameGeometry;
  return {x: num(r.x), y: num(r.y), w: num(r.width), h: num(r.height)};
}
function activate(w) {
  if (SIX) {
    workspace.activeWindow = w;
  } else {
    workspace.activeClient = w;
  }
}
function toDesktop(w, n) {
  if (n < 0) {
    w.onAllDesktops = true;
    return;
  }
  var d = desktopList();
  if (SIX) {
    if (!d || n >= d.length) {
      throw new Error("nodesktop");
    }
    w.onAllDesktops = false;
    w.desktops = [d[n]];
  } else {
    if (n >= num(workspace.desktops)) {
      throw new Error("nodesktop");
    }
    w.onAllDesktops = false;
    w.desktop = n + 1;
  }
}
function readState(w, s) {
  if (s === "MAXIMIZED_VERT") { return !!(num(w.maximizeMode) & 1); }
  if (s === "MAXIMIZED_HORZ") { return !!(num(w.maximizeMode) & 2); }
  var name = _STATE_PROPS[s];
  if (!name) {
    throw new Error("nostate");
  }
  if (typeof w[name] !== "boolean") {
    throw new Error(s === "SHADED" ? "noshade" : "nostate");
  }
  return w[name];
}
var _STATE_PROPS = {
  FULLSCREEN: "fullScreen", HIDDEN: "minimized", ABOVE: "keepAbove",
  BELOW: "keepBelow", STICKY: "onAllDesktops", SKIP_TASKBAR: "skipTaskbar",
  SKIP_PAGER: "skipPager", DEMANDS_ATTENTION: "demandsAttention",
  SHADED: "shade"
};
function setBool(w, s, v) {
  /* Assigning a property the window does not have (shade on Plasma 6, which
     dropped shading) silently creates a plain JS property on the wrapper and
     even reads back -- so refuse before writing, never after. */
  var name = _STATE_PROPS[s];
  if (typeof w[name] !== "boolean") {
    throw new Error(s === "SHADED" ? "noshade" : "nostate");
  }
  w[name] = v;
}
function writeState(w, s, v) {
  if (s === "MAXIMIZED_VERT") {
    w.setMaximize(v, !!(num(w.maximizeMode) & 2));
    return;
  }
  if (s === "MAXIMIZED_HORZ") {
    w.setMaximize(!!(num(w.maximizeMode) & 1), v);
    return;
  }
  if (!_STATE_PROPS[s]) {
    throw new Error("nostate");
  }
  setBool(w, s, v);
}

/* ------------------------------------------------------------------ events */
function on(obj, name, fn) {
  try {
    var s = obj[name];
    if (s && typeof s.connect === "function") {
      s.connect(fn);
      return true;
    }
  } catch (e) { }
  return false;
}
function hook(w) {
  var u = uuid(w);
  function ev(c) {
    return function () { _ev(u, c); };
  }
  on(w, "captionChanged", ev("title"));
  on(w, "minimizedChanged", ev("minimized"));
  on(w, "fullScreenChanged", ev("fullscreen_mode"));
  on(w, "desktopsChanged", ev("workspace"));            /* 6 */
  on(w, "desktopChanged", ev("workspace"));             /* 5.27 */
  on(w, "frameGeometryChanged", ev("move"));
  on(w, "demandsAttentionChanged", ev("urgent"));
}
function hookEvents() {
  var l = wins();
  for (var i = 0; i < l.length; i++) {
    hook(l[i]);
  }
  function added(w) {
    if (w) {
      _ev(uuid(w), "new");
      hook(w);
    }
  }
  function removed(w) {
    if (w) {
      _ev(uuid(w), "close");
    }
  }
  function activated(w) {
    if (w) {
      _ev(uuid(w), "focus");
    }
  }
  on(workspace, "windowAdded", added);                  /* 6 */
  on(workspace, "clientAdded", added);                  /* 5.27 */
  on(workspace, "windowRemoved", removed);
  on(workspace, "clientRemoved", removed);
  on(workspace, "windowActivated", activated);
  on(workspace, "clientActivated", activated);
  function ws() {
    _ev("", "workspace");
  }
  on(workspace, "currentDesktopChanged", ws);
  on(workspace, "desktopsChanged", ws);                 /* 6 */
  on(workspace, "numberDesktopsChanged", ws);           /* 5.27 */
  return {hooked: l.length};
}

/* -------------------------------------------------------------------- main */
function main() {
  var op = A.op;
  if (op === "list") {
    return listAll();
  }
  if (op === "screen") {
    return screenInfo();
  }
  if (op === "events") {
    return hookEvents();
  }
  var w = find(A.uuid);
  if (!w) {
    throw new Error("nowindow");
  }
  if (op === "activate") {
    w.minimized = false;
    if (!oncur(w)) {
      toCurrent(w);
    }
    activate(w);
    return {f: !!w.active};
  }
  if (op === "focus") {
    activate(w);
    return {f: !!w.active};
  }
  if (op === "close") {
    w.closeWindow();
    return {};
  }
  if (op === "minimize") {
    w.minimized = true;
    return {};
  }
  if (op === "unminimize") {
    w.minimized = false;
    return {};
  }
  if (op === "raise") {
    if (typeof workspace.raiseWindow === "function") {
      workspace.raiseWindow(w);                         /* 6: a real raise */
      return {how: "raise"};
    }
    if (w.active) {
      workspace.slotWindowRaise();
      return {how: "raise"};
    }
    activate(w);                                        /* 5.27: no API */
    return {how: "activate"};
  }
  if (op === "lower") {
    if (w.active) {
      workspace.slotWindowLower();                      /* a real lower */
      return {how: "lower"};
    }
    w.keepAbove = false;
    w.keepBelow = true;                                 /* approximation */
    return {how: "keepBelow"};
  }
  if (op === "geometry") {
    return geom(w, A.x, A.y, A.w, A.h);
  }
  if (op === "state") {
    var want = (A.action === 2) ? !readState(w, A.state) : (A.action === 1);
    writeState(w, A.state, want);
    return {applied: readState(w, A.state)};
  }
  if (op === "desktop") {
    toDesktop(w, A.n);
    return {d: dnum(w)};
  }
  if (op === "info") {
    return info(w);
  }
  throw new Error("noop");
}
function toCurrent(w) {
  /* activate() on a window parked elsewhere: bring its desktop up first, so
     `windowactivate` lands the window on screen like xdotool's does. */
  var n = dnum(w);
  if (n < 0) {
    return;
  }
  var d = desktopList();
  if (SIX) {
    if (d && n < d.length) {
      workspace.currentDesktop = d[n];
    }
  } else {
    workspace.currentDesktop = n + 1;
  }
}

try {
  _ret({ok: true, v: main()});
} catch (e) {
  _ret({ok: false, err: (e && e.message) ? String(e.message) : String(e)});
}
"""
