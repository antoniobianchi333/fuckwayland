// fuckwayland-bridge — GNOME Shell extension that exports Mutter's window,
// workspace and monitor facts (and the matching actions) over D-Bus so that
// wdotool / wwmctl / wxprop / wxrandr can drive a GNOME Wayland session.
//
// Why: GNOME has no window-management protocol, org.gnome.Shell.Eval is
// disabled outside "unsafe mode", org.gnome.Shell.Introspect is read-only and
// sender-allowlisted. The only supported way in is an extension.
//
// Targets GNOME Shell 45..50 (Ubuntu 24.04 = 46, Ubuntu 26.04 = 50), ESM.
// Every Mutter API that drifted between those releases is feature-detected at
// runtime. Verified live on Ubuntu 24.04 (gnome-shell 46.0) and 26.04
// (gnome-shell 50.1); the few places that could not be exercised there are
// still marked TODO-VERIFY (see gnome/README.md "Verified live").
//
// Wire: well-known name org.fuckwayland.Bridge (the object also answers under
// org.gnome.Shell because it lives on gnome-shell's own connection), object
// path /org/fuckwayland/Bridge, interface org.fuckwayland.Bridge1. Structured
// results travel as JSON strings; errors are org.fuckwayland.Bridge1.NotFound,
// .Unsupported, .InvalidArgs or .Failed. Every method body is guarded: a
// broken call yields a D-Bus error, never a shell crash.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.fuckwayland.Bridge';
const OBJECT_PATH = '/org/fuckwayland/Bridge';
const IFACE_NAME = 'org.fuckwayland.Bridge1';
const VERSION = 1;

const ERR_NOT_FOUND = `${IFACE_NAME}.NotFound`;
const ERR_UNSUPPORTED = `${IFACE_NAME}.Unsupported`;
const ERR_INVALID = `${IFACE_NAME}.InvalidArgs`;
const ERR_FAILED = `${IFACE_NAME}.Failed`;

// Keep in sync with org.fuckwayland.Bridge1.xml next to this file.
const IFACE_XML = `<node>
  <interface name="org.fuckwayland.Bridge1">
    <!-- windows -->
    <method name="ListWindows">
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="GetWindow">
      <arg type="t" direction="in" name="id"/>
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="Activate"><arg type="t" direction="in" name="id"/></method>
    <method name="Focus"><arg type="t" direction="in" name="id"/></method>
    <method name="Close"><arg type="t" direction="in" name="id"/></method>
    <method name="Kill"><arg type="t" direction="in" name="id"/></method>
    <method name="Minimize"><arg type="t" direction="in" name="id"/></method>
    <method name="Unminimize"><arg type="t" direction="in" name="id"/></method>
    <method name="Raise"><arg type="t" direction="in" name="id"/></method>
    <method name="Lower"><arg type="t" direction="in" name="id"/></method>
    <method name="Move">
      <arg type="t" direction="in" name="id"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
    </method>
    <method name="Resize">
      <arg type="t" direction="in" name="id"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
    </method>
    <method name="MoveResize">
      <arg type="t" direction="in" name="id"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
    </method>
    <method name="SetState">
      <arg type="t" direction="in" name="id"/>
      <arg type="s" direction="in" name="state"/>
      <arg type="s" direction="in" name="action"/>
      <arg type="b" direction="out" name="applied"/>
    </method>
    <method name="MoveToWorkspace">
      <arg type="t" direction="in" name="id"/>
      <arg type="i" direction="in" name="index"/>
    </method>
    <method name="SelectWindow">
      <arg type="u" direction="in" name="timeout_ms"/>
      <arg type="t" direction="out" name="id"/>
    </method>
    <method name="WindowAt">
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="t" direction="out" name="id"/>
    </method>
    <!-- workspaces -->
    <method name="GetActiveWorkspace"><arg type="i" direction="out" name="index"/></method>
    <method name="SetActiveWorkspace"><arg type="i" direction="in" name="index"/></method>
    <method name="GetNWorkspaces"><arg type="i" direction="out" name="count"/></method>
    <method name="SetNWorkspaces"><arg type="i" direction="in" name="count"/></method>
    <method name="ListWorkspaces"><arg type="s" direction="out" name="json"/></method>
    <method name="ShowDesktop"><arg type="b" direction="in" name="show"/></method>
    <!-- screen -->
    <method name="DisplaySize">
      <arg type="i" direction="out" name="width"/>
      <arg type="i" direction="out" name="height"/>
    </method>
    <method name="GetPointer">
      <arg type="i" direction="out" name="x"/>
      <arg type="i" direction="out" name="y"/>
      <arg type="u" direction="out" name="modifiers"/>
    </method>
    <method name="ListMonitors"><arg type="s" direction="out" name="json"/></method>
    <method name="XInfo">
      <arg type="s" direction="out" name="display"/>
      <arg type="s" direction="out" name="xauthority"/>
    </method>
    <method name="ConfirmDisplayChange">
      <arg type="b" direction="in" name="keep"/>
      <arg type="b" direction="out" name="handled"/>
    </method>
    <!-- misc -->
    <method name="GetVersion"><arg type="u" direction="out" name="version"/></method>
    <property name="Version" type="u" access="read"/>
    <!-- events -->
    <signal name="WindowEvent">
      <arg type="t" name="id"/>
      <arg type="s" name="change"/>
    </signal>
    <signal name="WorkspaceEvent">
      <arg type="s" name="change"/>
    </signal>
  </interface>
</node>`;

// ---------------------------------------------------------------------------
// helpers

class BridgeError extends Error {
    constructor(name, message) {
        super(message);
        this.name = name;
    }
}

// debug(): G_LOG_LEVEL_DEBUG, hidden unless G_MESSAGES_DEBUG covers gjs.
// info(): G_LOG_LEVEL_MESSAGE, always in the journal (`journalctl --user
// _COMM=gnome-shell`); used for enable/disable, bus-name changes and
// unexpected (.Failed) errors only, so a polling client cannot flood it.
function debug(...args) {
    try {
        console.debug('[fuckwayland-bridge]', ...args);
    } catch (_e) {
        // logging must never be the thing that breaks
    }
}

function info(...args) {
    try {
        console.log('[fuckwayland-bridge]', ...args);
    } catch (_e) {
        // see above
    }
}

// Run fn(); return dflt when it throws or yields null/undefined.
function safe(fn, dflt = null) {
    try {
        const v = fn();
        return v === null || v === undefined ? dflt : v;
    } catch (_e) {
        return dflt;
    }
}

function isFn(obj, name) {
    return !!obj && typeof obj[name] === 'function';
}

// A real timestamp keeps Mutter's focus-stealing prevention happy; 0
// (META_CURRENT_TIME) makes it guess. get_current_time_roundtrip() returns the
// event time when called from an event, else a monotonic ms time on Wayland.
function now() {
    const d = global.display;
    if (isFn(d, 'get_current_time_roundtrip')) {
        const t = safe(() => d.get_current_time_roundtrip(), 0);
        if (t)
            return t;
    }
    return safe(() => global.get_current_time(), 0);
}

function settings(schemaId) {
    try {
        const src = Gio.SettingsSchemaSource.get_default();
        if (!src || !src.lookup(schemaId, true))
            return null;
        return new Gio.Settings({schema_id: schemaId});
    } catch (_e) {
        return null;
    }
}

function rectToJson(r) {
    if (!r)
        return {x: 0, y: 0, width: 0, height: 0};
    return {x: r.x | 0, y: r.y | 0, width: r.width | 0, height: r.height | 0};
}

const WINDOW_TYPE_NAMES = [
    'NORMAL', 'DESKTOP', 'DOCK', 'DIALOG', 'MODAL_DIALOG', 'TOOLBAR', 'MENU',
    'UTILITY', 'SPLASHSCREEN', 'DROPDOWN_MENU', 'POPUP_MENU', 'TOOLTIP',
    'NOTIFICATION', 'COMBO', 'DND', 'OVERRIDE_OTHER',
];
let windowTypeNames = null;

function windowTypeName(t) {
    if (!windowTypeNames) {
        windowTypeNames = new Map();
        for (const n of WINDOW_TYPE_NAMES) {
            const v = Meta.WindowType[n];
            if (typeof v === 'number')
                windowTypeNames.set(v, n);
        }
    }
    return windowTypeNames.get(t) ?? String(t);
}

function isX11(w) {
    if (isFn(w, 'get_client_type') && Meta.WindowClientType) {
        const ct = safe(() => w.get_client_type(), -1);
        if (ct === Meta.WindowClientType.X11)
            return true;
        if (ct === Meta.WindowClientType.WAYLAND)
            return false;
    }
    // Fallback: X11 windows describe themselves as "0x<xid>" (46) or
    // "0x<xid> (title)" (50); Wayland ones as "W<n>" / "W<n> (title)".
    return /^0x[0-9a-f]+/i.test(safe(() => w.get_description(), ''));
}

// X11 client window id of an XWayland window, 0 for native ones.
// VERIFIED (50.1): the id equals the window's _NET_CLIENT_LIST entry and
// xprop's WM_CLASS/_NET_WM_PID agree with wm_class_instance/wm_class/pid.
//
// meta_x11_display_lookup_xwindow(MetaX11Display*, MetaWindow*) -> Window is
// META_EXPORT in mutter 46 and 50 (meta-x11-display.h) and is the only public
// window -> xid route (meta_window_get_xwindow does not exist; the private
// meta_window_x11_get_xwindow is not in the GIR). global.display
// .get_x11_display() is null while Xwayland is not running, and then there
// are no X11 windows either. In 50 the declaration moved from display.h to
// meta-x11-display.h but it is still a Meta.Display method.
//
// Fallback: Mutter's window description (window.c meta_window_update_desc)
// is "0x%lx" on 46 and "0x%lx (title)" on 50 for X11 clients ("W<n>" /
// "W<n> (title)" for Wayland ones); the leading hex number is the xid.
function xidOf(w) {
    if (!isX11(w))
        return 0;
    const x11 = safe(() => (isFn(global.display, 'get_x11_display')
        ? global.display.get_x11_display() : null), null);
    if (x11 && isFn(x11, 'lookup_xwindow')) {
        const xid = safe(() => Number(x11.lookup_xwindow(w)), 0);
        if (xid > 0)
            return xid;
    }
    const m = /^0x([0-9a-f]+)/i.exec(safe(() => w.get_description(), ''));
    return m ? (parseInt(m[1], 16) || 0) : 0;
}

// [horizontally, vertically]
function maximizedFlags(w) {
    // The boolean GObject properties maximized-horizontally/-vertically are
    // defined by window.c on 46 and 50 alike. Fallbacks: get_maximized()
    // (flags, 46-48), get_maximize_flags() (49+), is_maximized() (49+).
    const h = safe(() => w.maximized_horizontally, null);
    const v = safe(() => w.maximized_vertically, null);
    if (typeof h === 'boolean' && typeof v === 'boolean')
        return [h, v];
    const F = Meta.MaximizeFlags || {};
    const H = F.HORIZONTAL ?? 1;
    const V = F.VERTICAL ?? 2;
    for (const getter of ['get_maximized', 'get_maximize_flags']) {
        if (isFn(w, getter)) {
            const f = safe(() => w[getter](), 0);
            return [!!(f & H), !!(f & V)];
        }
    }
    const m = safe(() => w.is_maximized(), false);
    return [m, m];
}

// Maximize/unmaximize per axis. Partial maximization exists on every target
// release: mutter 46-48 take the directions in maximize(flags) /
// unmaximize(flags); mutter 49+ moved them to set_maximize_flags(flags) /
// set_unmaximize_flags(flags) and maximize() there is literally
// set_maximize_flags(BOTH). Both generations g_assert() that at least one
// direction is set -- an empty flag set would abort gnome-shell, hence the
// guard, and the flags are never 0 for the SetState callers anyway.
function setMaximized(w, horz, vert, on) {
    const F = Meta.MaximizeFlags || {};
    const flags = (horz ? (F.HORIZONTAL ?? 1) : 0) | (vert ? (F.VERTICAL ?? 2) : 0);
    if (!flags)
        throw new BridgeError(ERR_INVALID, 'no maximize direction given');
    if (isFn(w, 'set_maximize_flags') && isFn(w, 'set_unmaximize_flags')) {
        // mutter 49+
        if (on)
            w.set_maximize_flags(flags);
        else
            w.set_unmaximize_flags(flags);
    } else if (on) {
        // mutter 46-48
        w.maximize(flags);
    } else {
        w.unmaximize(flags);
    }
}

function workspaceName(i) {
    if (isFn(Meta, 'prefs_get_workspace_name')) {
        const n = safe(() => Meta.prefs_get_workspace_name(i), '');
        if (n)
            return n;
    }
    return `Workspace ${i + 1}`;
}

// Newest $XDG_RUNTIME_DIR/.mutter-Xwaylandauth.XXXXXX: the cookie file
// Mutter writes for Xwayland (meta-xwayland.c prepare_auth_file, mkstemp).
// Only used when the shell process has no XAUTHORITY in its environment.
function findXauthority() {
    const dir = safe(() => GLib.get_user_runtime_dir(), '');
    if (!dir)
        return '';
    let best = '';
    let bestMtime = -1;
    let d = null;
    try {
        d = GLib.Dir.open(dir, 0);
        let name;
        while ((name = d.read_name()) !== null) {
            if (!name.startsWith('.mutter-Xwaylandauth.'))
                continue;
            const path = GLib.build_filenamev([dir, name]);
            const st = safe(() => Gio.File.new_for_path(path).query_info(
                'time::modified', Gio.FileQueryInfoFlags.NONE, null), null);
            const mtime = st ? Number(safe(() => st.get_attribute_uint64('time::modified'), 0)) : 0;
            if (mtime > bestMtime) {
                best = path;
                bestMtime = mtime;
            }
        }
    } catch (e) {
        debug(`scanning ${dir} failed: ${e}`);
    } finally {
        if (d)
            safe(() => d.close());
    }
    return best;
}

// The "Keep these display settings?" dialog is a ModalDialog created in
// gnome-shell's windowManager.js and not stored anywhere public; find it by
// class name in the modal dialog group.
// TODO-VERIFY (46+50): DisplayChangeDialog is a ModalDialog parented to
// Main.layoutManager.modalDialogGroup, its constructor name survives GObject
// registration, and _onSuccess/_onFailure are the Keep/Revert handlers.
function findDisplayChangeDialog() {
    const groups = [
        safe(() => Main.layoutManager.modalDialogGroup, null),
        safe(() => Main.uiGroup, null),
    ];
    for (const g of groups) {
        if (!g)
            continue;
        for (const c of safe(() => g.get_children(), [])) {
            const name = `${safe(() => c.constructor.name, '')} ${safe(() => c.constructor.$gtype.name, '')}`;
            if (/DisplayChangeDialog/.test(name) && isFn(c, '_onSuccess') && isFn(c, '_onFailure'))
                return c;
        }
    }
    return null;
}

const STATE_ACTIONS = ['add', 'remove', 'toggle'];

// Method table: name -> [out signature, implementation]. Implementations
// return an array of out values (or nothing for "()"); they run with `this`
// bound to the extension. Exceptions become D-Bus errors in _invoke().
const METHODS = {
    // -- windows ----------------------------------------------------------
    ListWindows: ['(s)', function () {
        return [JSON.stringify(this._listWindows())];
    }],
    GetWindow: ['(s)', function (id) {
        return [JSON.stringify(this._windowInfo(this._find(id)))];
    }],
    Activate: ['()', function (id) {
        this._find(id).activate(now());
    }],
    Focus: ['()', function (id) {
        const w = this._find(id);
        const active = safe(() => global.workspace_manager.get_active_workspace(), null);
        const showing = safe(() => w.showing_on_its_workspace(), false) &&
            (!active || safe(() => w.located_on_workspace(active), false));
        // xdotool windowfocus = XSetInputFocus: focus without raising. Fall
        // back to activate for windows that are not visible right now.
        if (showing && isFn(w, 'focus'))
            w.focus(now());
        else
            w.activate(now());
    }],
    Close: ['()', function (id) {
        this._find(id).delete(now());
    }],
    Kill: ['()', function (id) {
        this._find(id).kill();
    }],
    Minimize: ['()', function (id) {
        this._find(id).minimize();
    }],
    Unminimize: ['()', function (id) {
        this._find(id).unminimize();
    }],
    Raise: ['()', function (id) {
        this._find(id).raise();
    }],
    Lower: ['()', function (id) {
        this._find(id).lower();
    }],
    Move: ['()', function (id, x, y) {
        // Frame coordinates, logical pixels. Mutter's constraints silently
        // keep maximized/fullscreen/tiled windows where they are (as on X11).
        this._find(id).move_frame(true, x, y);
    }],
    Resize: ['()', function (id, width, height) {
        const w = this._find(id);
        const r = w.get_frame_rect();
        w.move_resize_frame(true, r.x, r.y, width, height);
    }],
    MoveResize: ['()', function (id, x, y, width, height) {
        this._find(id).move_resize_frame(true, x, y, width, height);
    }],
    SetState: ['(b)', function (id, state, action) {
        return [this._setState(this._find(id), String(state), String(action))];
    }],
    MoveToWorkspace: ['()', function (id, index) {
        const w = this._find(id);
        if (index < 0) {
            // _NET_WM_DESKTOP 0xFFFFFFFF: all desktops.
            w.stick();
            return;
        }
        const ws = global.workspace_manager.get_workspace_by_index(index);
        if (!ws)
            throw new BridgeError(ERR_NOT_FOUND, `workspace ${index} not found`);
        if (safe(() => w.is_on_all_workspaces(), false))
            safe(() => w.unstick());
        w.change_workspace_by_index(index, false);
    }],
    WindowAt: ['(t)', function (x, y) {
        return [this._windowAt(x, y)];
    }],
    // -- workspaces -------------------------------------------------------
    GetActiveWorkspace: ['(i)', function () {
        return [global.workspace_manager.get_active_workspace_index()];
    }],
    SetActiveWorkspace: ['()', function (index) {
        const ws = global.workspace_manager.get_workspace_by_index(index);
        if (!ws)
            throw new BridgeError(ERR_NOT_FOUND, `workspace ${index} not found`);
        ws.activate(now());
    }],
    GetNWorkspaces: ['(i)', function () {
        return [global.workspace_manager.get_n_workspaces()];
    }],
    SetNWorkspaces: ['()', function (count) {
        this._setNWorkspaces(count);
    }],
    ListWorkspaces: ['(s)', function () {
        return [JSON.stringify(this._listWorkspaces())];
    }],
    ShowDesktop: ['()', function (show) {
        this._showDesktop(!!show);
    }],
    // -- screen -----------------------------------------------------------
    DisplaySize: ['(ii)', function () {
        const [w, h] = global.display.get_size();
        return [w, h];
    }],
    GetPointer: ['(iiu)', function () {
        const [x, y, mods] = global.get_pointer();
        return [x, y, (mods >>> 0)];
    }],
    ListMonitors: ['(s)', function () {
        return [JSON.stringify(this._listMonitors())];
    }],
    XInfo: ['(ss)', function () {
        // Mutter g_setenv()s DISPLAY/XAUTHORITY in the shell process when it
        // binds the Xwayland sockets at startup (meta_xwayland_init), which
        // is before the lazy Xwayland spawn -- this is how a root caller
        // finds the X plane and its cookie file.
        // VERIFIED (46.0 and 50.1): both are set in gnome-shell's own
        // environment right after login (':0' and
        // $XDG_RUNTIME_DIR/.mutter-Xwaylandauth.XXXXXX). If DISPLAY ever
        // comes back empty the client falls back to /tmp/.X11-unix scanning
        // (wdotool.session.find_x_display); the XAUTHORITY fallback below
        // scans the runtime dir for Mutter's cookie file.
        const display = GLib.getenv('DISPLAY') || '';
        const xauth = GLib.getenv('XAUTHORITY') || findXauthority();
        return [display, xauth];
    }],
    ConfirmDisplayChange: ['(b)', function (keep) {
        return [this._confirmDisplayChange(!!keep)];
    }],
    // -- misc -------------------------------------------------------------
    GetVersion: ['(u)', function () {
        return [VERSION];
    }],
};

// ---------------------------------------------------------------------------

export default class FuckwaylandBridge extends Extension {
    // D-Bus property `Version` (the method is GetVersion: GJS looks both
    // methods and properties up on this object, so they cannot share a name).
    get Version() {
        return VERSION;
    }

    enable() {
        this._wins = new Map();        // id -> Meta.Window
        this._handlers = new Map();    // Meta.Window -> {id, ids: [handler ids]}
        this._dead = new WeakSet();    // unmanaged windows whose actor lingers
        this._selects = new Set();     // pending SelectWindow finishers
        this._showDesktopWins = [];    // windows minimized by ShowDesktop(true)
        this._displayIds = [];
        this._wmIds = [];

        this._installMethods();

        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
            () => info(`acquired ${BUS_NAME}`),
            () => info(`lost ${BUS_NAME}`));

        const connect = (obj, list, sig, cb) => {
            try {
                list.push(obj.connect(sig, (...a) => {
                    try {
                        cb(...a);
                    } catch (e) {
                        debug(`handler ${sig} failed: ${e}`);
                    }
                }));
            } catch (e) {
                debug(`connect ${sig} failed: ${e}`);
            }
        };
        const display = global.display;
        connect(display, this._displayIds, 'window-created', (_d, w) => {
            this._track(w);
            this._emitWindow(w, 'new');
        });
        connect(display, this._displayIds, 'notify::focus-window', () => {
            const w = safe(() => display.focus_window, null);
            this._emit('WindowEvent', '(ts)', [w ? this._idOf(w) : 0, 'focus']);
        });
        const wm = global.workspace_manager;
        connect(wm, this._wmIds, 'active-workspace-changed', () => this._emit('WorkspaceEvent', '(s)', ['switch']));
        connect(wm, this._wmIds, 'workspace-added', () => this._emit('WorkspaceEvent', '(s)', ['add']));
        connect(wm, this._wmIds, 'workspace-removed', () => this._emit('WorkspaceEvent', '(s)', ['remove']));

        for (const w of this._allWindows())
            this._track(w);
        info(`enabled (bridge v${VERSION}, gnome-shell ${safe(() => Config.PACKAGE_VERSION, '?')})`);
    }

    disable() {
        for (const finish of Array.from(this._selects ?? []))
            safe(() => finish(0));
        this._selects = null;

        for (const w of Array.from(this._handlers?.keys() ?? []))
            this._untrack(w);
        for (const h of this._displayIds ?? [])
            safe(() => global.display.disconnect(h));
        for (const h of this._wmIds ?? [])
            safe(() => global.workspace_manager.disconnect(h));
        this._displayIds = [];
        this._wmIds = [];

        if (this._nameId) {
            safe(() => Gio.bus_unown_name(this._nameId));
            this._nameId = 0;
        }
        if (this._dbus) {
            safe(() => this._dbus.flush());
            safe(() => this._dbus.unexport());
            this._dbus = null;
        }
        this._wins = null;
        this._handlers = null;
        this._dead = null;
        this._showDesktopWins = [];
        info('disabled');
    }

    // -- D-Bus plumbing -----------------------------------------------------

    // GJS' wrapJSObject calls `<Name>Async(params, invocation)` when no sync
    // `<Name>` exists. Going async for everything keeps error names, logging
    // and return packing under our control (a sync handler that throws makes
    // GJS logError() and invent an org.gnome.gjs.JSError.* name).
    _installMethods() {
        for (const [name, [sig, fn]] of Object.entries(METHODS)) {
            this[`${name}Async`] = (params, invocation) =>
                this._invoke(name, sig, fn, params, invocation);
        }
    }

    _invoke(name, sig, fn, params, invocation) {
        let out;
        try {
            out = fn.apply(this, Array.isArray(params) ? params : []);
        } catch (e) {
            this._returnError(invocation, name, e);
            return;
        }
        try {
            invocation.return_value(new GLib.Variant(sig, out ?? []));
        } catch (e) {
            this._returnError(invocation, name, new BridgeError(ERR_FAILED, `cannot pack reply ${sig}: ${e}`));
        }
    }

    _returnError(invocation, name, e) {
        const errName = (e && typeof e.name === 'string' && e.name.includes('.')) ? e.name : ERR_FAILED;
        const message = e && e.message ? String(e.message) : String(e);
        // Expected errors (NotFound, InvalidArgs, Unsupported) stay at debug
        // level; anything else is a bug worth a journal line.
        (errName === ERR_FAILED ? info : debug)(`${name}: ${errName}: ${message}`);
        try {
            invocation.return_dbus_error(errName, message);
        } catch (e2) {
            debug(`return_dbus_error failed: ${e2}`);
        }
    }

    _emit(signal, sig, values) {
        if (!this._dbus)
            return;
        try {
            this._dbus.emit_signal(signal, new GLib.Variant(sig, values));
        } catch (e) {
            debug(`emit ${signal} failed: ${e}`);
        }
    }

    _emitWindow(w, change) {
        const id = safe(() => this._idOf(w), 0);
        this._emit('WindowEvent', '(ts)', [id, change]);
    }

    // SelectWindow(u timeout_ms) -> t: resolve with the next window that gains
    // focus (like sway's "next focus event"; the user must focus a different
    // window), or 0 on timeout / disable. timeout_ms 0 = wait forever.
    SelectWindowAsync(params, invocation) {
        try {
            const timeoutMs = Number(params?.[0] ?? 0) >>> 0;
            let sigId = 0;
            let timerId = 0;
            let done = false;
            const finish = id => {
                if (done)
                    return;
                done = true;
                if (sigId)
                    safe(() => global.display.disconnect(sigId));
                if (timerId)
                    safe(() => GLib.source_remove(timerId));
                sigId = 0;
                timerId = 0;
                this._selects?.delete(finish);
                try {
                    invocation.return_value(new GLib.Variant('(t)', [Number(id) || 0]));
                } catch (e) {
                    debug(`SelectWindow reply failed: ${e}`);
                }
            };
            sigId = global.display.connect('notify::focus-window', () => {
                const w = safe(() => global.display.focus_window, null);
                if (!w)
                    return;   // focus went to nothing; keep waiting
                finish(safe(() => this._idOf(w), 0));
            });
            if (timeoutMs > 0) {
                timerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, timeoutMs, () => {
                    timerId = 0;
                    finish(0);
                    return GLib.SOURCE_REMOVE;
                });
            }
            this._selects.add(finish);
        } catch (e) {
            this._returnError(invocation, 'SelectWindow', e);
        }
    }

    // -- window bookkeeping -------------------------------------------------

    _idOf(w) {
        // get_id() is the 64-bit id Introspect uses; stable for the shell's
        // lifetime. Older typelibs without it fall back to the creation counter.
        if (isFn(w, 'get_id'))
            return Number(w.get_id());
        return Number(w.get_stable_sequence());
    }

    // Every managed window: actors first (bottom-to-top stacking), then
    // anything list_all_windows() knows that has no actor yet.
    _allWindows() {
        const seen = new Set();
        const out = [];
        const add = w => {
            if (w && !seen.has(w) && !this._dead.has(w)) {
                seen.add(w);
                out.push(w);
            }
        };
        for (const a of safe(() => global.get_window_actors(), []))
            add(safe(() => a.meta_window, null));
        for (const w of safe(() => global.display.list_all_windows(), []))
            add(w);
        return out;
    }

    _find(id) {
        id = Number(id);
        const cached = this._wins.get(id);
        if (cached && !this._dead.has(cached))
            return cached;
        for (const w of this._allWindows()) {
            if (safe(() => this._idOf(w), -1) === id) {
                this._track(w);
                return w;
            }
        }
        throw new BridgeError(ERR_NOT_FOUND, `window ${id} not found`);
    }

    _track(w) {
        if (!w || !this._wins || this._dead.has(w))
            return;
        const id = safe(() => this._idOf(w), null);
        if (id === null)
            return;
        this._wins.set(id, w);
        if (this._handlers.has(w))
            return;
        const ids = [];
        const on = (sig, cb) => {
            try {
                ids.push(w.connect(sig, () => {
                    try {
                        cb();
                    } catch (e) {
                        debug(`window ${sig} handler failed: ${e}`);
                    }
                }));
            } catch (e) {
                debug(`connect ${sig} failed: ${e}`);
            }
        };
        on('unmanaged', () => this._onUnmanaged(w));
        on('notify::title', () => this._emitWindow(w, 'title'));
        on('position-changed', () => this._emitWindow(w, 'move'));
        on('size-changed', () => this._emitWindow(w, 'move'));
        on('notify::fullscreen', () => this._emitWindow(w, 'fullscreen_mode'));
        on('notify::demands-attention', () => this._emitWindow(w, 'urgent'));
        on('notify::urgent', () => this._emitWindow(w, 'urgent'));
        on('workspace-changed', () => this._emitWindow(w, 'workspace'));
        on('notify::on-all-workspaces', () => this._emitWindow(w, 'workspace'));
        on('notify::minimized', () => this._emitWindow(w, 'minimized'));
        this._handlers.set(w, {id, ids});
    }

    _untrack(w) {
        const entry = this._handlers?.get(w);
        if (!entry)
            return;
        for (const h of entry.ids)
            safe(() => w.disconnect(h));
        this._handlers.delete(w);
        if (this._wins?.get(entry.id) === w)
            this._wins.delete(entry.id);
    }

    _onUnmanaged(w) {
        this._dead.add(w);
        this._emitWindow(w, 'close');
        this._untrack(w);
    }

    // -- window facts -------------------------------------------------------

    _listWindows() {
        const out = [];
        for (const a of safe(() => global.get_window_actors(), [])) {
            const w = safe(() => a.meta_window, null);
            if (!w || this._dead.has(w))
                continue;
            if (safe(() => w.is_override_redirect(), false))
                continue;
            this._track(w);
            try {
                out.push(this._windowInfo(w));
            } catch (e) {
                debug(`skipping window: ${e}`);
            }
        }
        return out;
    }

    _windowInfo(w) {
        const active = safe(() => global.workspace_manager.get_active_workspace(), null);
        const ws = safe(() => w.get_workspace(), null);
        const sticky = safe(() => w.is_on_all_workspaces(), false);
        const [maxH, maxV] = maximizedFlags(w);
        const transient = safe(() => w.get_transient_for(), null);
        const x11 = isX11(w);
        const frame = rectToJson(safe(() => w.get_frame_rect(), null));
        // No is_urgent() in either release; `urgent` and `demands-attention`
        // are GObject properties on both.
        const urgent = !!safe(() => w.urgent, false) || !!safe(() => w.demands_attention, false);
        return {
            id: this._idOf(w),
            xid: x11 ? xidOf(w) : 0,
            title: safe(() => w.get_title(), ''),
            wm_class: safe(() => w.get_wm_class(), ''),
            wm_class_instance: safe(() => w.get_wm_class_instance(), ''),
            gtk_app_id: safe(() => w.get_gtk_application_id(), ''),
            sandboxed_app_id: safe(() => w.get_sandboxed_app_id(), ''),
            desktop_id: safe(() => Shell.WindowTracker.get_default().get_window_app(w).get_id(), ''),
            role: safe(() => w.get_role(), ''),
            pid: safe(() => Number(w.get_pid()), 0) | 0,
            client_type: x11 ? 'x11' : 'wayland',
            window_type: windowTypeName(safe(() => w.get_window_type(), -1)),
            x: frame.x,
            y: frame.y,
            width: frame.width,
            height: frame.height,
            buffer_rect: rectToJson(safe(() => w.get_buffer_rect(), null)),
            focused: safe(() => w.has_focus(), false),
            minimized: !!safe(() => w.minimized, false),
            hidden: !safe(() => w.showing_on_its_workspace(), true),
            on_all_workspaces: sticky,
            workspace: sticky ? -1 : safe(() => ws.index(), -1),
            on_active_workspace: sticky || (!!active && safe(() => w.located_on_workspace(active), false)),
            monitor: safe(() => w.get_monitor(), -1),
            fullscreen: safe(() => w.is_fullscreen(), false),
            maximized_h: maxH,
            maximized_v: maxV,
            above: safe(() => w.is_above(), false),
            urgent,
            skip_taskbar: safe(() => w.is_skip_taskbar(), false),
            transient_for: transient ? safe(() => this._idOf(transient), 0) : 0,
            decorated: !!safe(() => w.decorated, true),
            stable_sequence: safe(() => w.get_stable_sequence(), 0),
        };
    }

    // Topmost non-desktop window showing on the active workspace whose frame
    // contains (x, y); 0 if none.
    _windowAt(x, y) {
        const active = safe(() => global.workspace_manager.get_active_workspace(), null);
        const actors = safe(() => global.get_window_actors(), []);
        for (let i = actors.length - 1; i >= 0; i--) {
            const w = safe(() => actors[i].meta_window, null);
            if (!w || this._dead.has(w))
                continue;
            if (safe(() => w.is_override_redirect(), false))
                continue;
            if (safe(() => w.get_window_type(), -1) === Meta.WindowType.DESKTOP)
                continue;
            if (safe(() => w.minimized, false))
                continue;
            if (!safe(() => w.showing_on_its_workspace(), true))
                continue;
            if (active && !safe(() => w.located_on_workspace(active), true))
                continue;
            const r = safe(() => w.get_frame_rect(), null);
            if (r && x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height)
                return this._idOf(w);
        }
        return 0;
    }

    // Returns true when the state was changed (or already as requested),
    // false for states Mutter cannot set. Never throws for unknown states.
    _setState(w, state, action) {
        const S = state.toUpperCase();
        const A = action.toLowerCase();
        if (!STATE_ACTIONS.includes(A))
            throw new BridgeError(ERR_INVALID, `action must be add|remove|toggle, got ${action}`);
        const want = cur => (A === 'add' ? true : A === 'remove' ? false : !cur);

        switch (S) {
        case 'FULLSCREEN': {
            if (want(safe(() => w.is_fullscreen(), false)))
                w.make_fullscreen();
            else
                w.unmake_fullscreen();
            return true;
        }
        case 'MAXIMIZED_HORZ':
        case 'MAXIMIZED_VERT':
        case 'MAXIMIZED': {
            const [h, v] = maximizedFlags(w);
            const horz = S !== 'MAXIMIZED_VERT';
            const vert = S !== 'MAXIMIZED_HORZ';
            const cur = S === 'MAXIMIZED_HORZ' ? h : S === 'MAXIMIZED_VERT' ? v : (h && v);
            setMaximized(w, horz, vert, want(cur));
            return true;
        }
        case 'HIDDEN': {
            if (want(!!safe(() => w.minimized, false)))
                w.minimize();
            else
                w.unminimize();
            return true;
        }
        case 'ABOVE': {
            if (want(safe(() => w.is_above(), false)))
                w.make_above();
            else
                w.unmake_above();
            return true;
        }
        case 'BELOW': {
            // Neither 46 nor 50 exports make_below(); wm_state_below is only
            // reachable through _NET_WM_STATE on X11 windows. Probed for free
            // in case a release adds one; otherwise "not applied".
            if (isFn(w, 'make_below') && isFn(w, 'unmake_below')) {
                if (want(!!safe(() => w.below, false)))
                    w.make_below();
                else
                    w.unmake_below();
                return true;
            }
            return false;
        }
        case 'STICKY': {
            if (want(safe(() => w.is_on_all_workspaces(), false)))
                w.stick();
            else
                w.unstick();
            return true;
        }
        case 'DEMANDS_ATTENTION': {
            if (want(!!safe(() => w.demands_attention, false)))
                w.set_demands_attention();
            else
                w.unset_demands_attention();
            return true;
        }
        case 'SHADED':
        case 'SKIP_TASKBAR':
        case 'SKIP_PAGER':
        case 'MODAL':
        default:
            return false;
        }
    }

    // -- workspaces ---------------------------------------------------------

    _listWorkspaces() {
        const wm = global.workspace_manager;
        const n = wm.get_n_workspaces();
        const active = wm.get_active_workspace_index();
        const out = [];
        for (let i = 0; i < n; i++) {
            const ws = wm.get_workspace_by_index(i);
            out.push({
                index: i,
                name: workspaceName(i),
                active: i === active,
                work_area: rectToJson(safe(() => ws.get_work_area_all_monitors(), null)),
                viewport: {x: 0, y: 0},
            });
        }
        return out;
    }

    _setNWorkspaces(count) {
        count = count | 0;
        if (count < 1 || count > 36)
            throw new BridgeError(ERR_INVALID, `workspace count must be 1..36, got ${count}`);
        const mutter = settings('org.gnome.mutter');
        if (mutter && safe(() => mutter.get_boolean('dynamic-workspaces'), false)) {
            throw new BridgeError(ERR_UNSUPPORTED,
                'dynamic workspaces are enabled (org.gnome.mutter dynamic-workspaces); ' +
                'the workspace count is managed by the shell');
        }
        const wm = global.workspace_manager;
        const ts = now();
        for (let guard = 0; guard < 64 && wm.get_n_workspaces() < count; guard++)
            wm.append_new_workspace(false, ts);
        for (let guard = 0; guard < 64 && wm.get_n_workspaces() > count; guard++) {
            const ws = wm.get_workspace_by_index(wm.get_n_workspaces() - 1);
            if (!ws)
                break;
            wm.remove_workspace(ws, ts);
        }
        // Keep the preference in step so Mutter's own handler agrees with us.
        const prefs = settings('org.gnome.desktop.wm.preferences');
        if (prefs)
            safe(() => prefs.set_int('num-workspaces', count));
        if (wm.get_n_workspaces() !== count)
            throw new BridgeError(ERR_FAILED, `workspace count is ${wm.get_n_workspaces()}, wanted ${count}`);
    }

    // wmctrl -k: Mutter's real show-desktop mode is not reachable from JS
    // (meta_workspace_manager_show_desktop is private), so minimize every
    // normal window on the active workspace and remember them for -k off.
    _showDesktop(show) {
        if (!show) {
            for (const w of this._showDesktopWins) {
                if (!this._dead.has(w) && safe(() => w.minimized, false))
                    safe(() => w.unminimize());
            }
            this._showDesktopWins = [];
            return;
        }
        const ws = global.workspace_manager.get_active_workspace();
        const done = [];
        for (const w of safe(() => ws.list_windows(), [])) {
            if (this._dead.has(w) || safe(() => w.is_override_redirect(), false))
                continue;
            if (safe(() => w.minimized, false))
                continue;
            const t = safe(() => w.get_window_type(), -1);
            if (t === Meta.WindowType.DESKTOP || t === Meta.WindowType.DOCK)
                continue;
            if (isFn(w, 'can_minimize') && !safe(() => w.can_minimize(), true))
                continue;
            try {
                w.minimize();
                done.push(w);
            } catch (e) {
                debug(`minimize failed: ${e}`);
            }
        }
        this._showDesktopWins = done;
    }

    // -- monitors -----------------------------------------------------------

    _listMonitors() {
        const mons = safe(() => Main.layoutManager.monitors, []);
        const primary = safe(() => Main.layoutManager.primaryIndex, -1);
        const connectors = this._connectorMap();
        return mons.map((m, i) => {
            const index = typeof m.index === 'number' ? m.index : i;
            return {
                index,
                x: m.x | 0,
                y: m.y | 0,
                width: m.width | 0,
                height: m.height | 0,
                scale: Number(m.geometry_scale ?? 1) || 1,
                primary: index === primary,
                connector: connectors.get(index) ?? '',
            };
        });
    }

    // index -> connector name, best-effort. MonitorManager
    // .get_monitor_for_connector(connector) (META_EXPORT on 46 and 50) returns
    // the logical monitor's number, i.e. the Main.layoutManager.monitors index;
    // MonitorManager.get_monitors() and Meta.Monitor.get_connector() (49+/50)
    // supply the connector names to feed it. GNOME 46 exports no way to
    // enumerate connectors from JS, so the map stays empty there and a client
    // matches DisplayConfig.GetCurrentState's logical monitors on x/y instead.
    // Mirrored monitors share a logical monitor: the first connector wins.
    _connectorMap() {
        const map = new Map();
        const mm = safe(() => global.backend.get_monitor_manager(), null);
        if (!mm)
            return map;
        // VERIFIED (50.1): get_monitors() yields Meta.Monitor objects with
        // get_connector() (mutter-18 typelib); ListMonitors reports
        // connector "Virtual-1" in the QEMU rig. On 46 this route is absent
        // and the connector stays "" (verified).
        if (isFn(mm, 'get_monitors') && isFn(mm, 'get_monitor_for_connector')) {
            for (const m of safe(() => mm.get_monitors(), [])) {
                const c = safe(() => m.get_connector(), '');
                const idx = c ? safe(() => mm.get_monitor_for_connector(c), -1) : -1;
                if (idx >= 0 && !map.has(idx))
                    map.set(idx, c);
            }
            if (map.size)
                return map;
        }
        // Fallback only (never needed on 50.1, where the route above already
        // filled the map): gjs.guide's 49 porting notes list
        // MonitorManager.get_logical_monitors(), LogicalMonitor.get_number()
        // and LogicalMonitor.get_monitors(); only get_logical_monitors() is in
        // the on-disk 50 header, so every call is probed.
        if (isFn(mm, 'get_logical_monitors')) {
            for (const lm of safe(() => mm.get_logical_monitors(), [])) {
                const idx = safe(() => lm.get_number(), -1);
                const first = safe(() => lm.get_monitors(), [])[0];
                const c = first ? safe(() => first.get_connector(), '') : '';
                if (idx >= 0 && c && !map.has(idx))
                    map.set(idx, c);
            }
        }
        return map;
    }

    // Best-effort: dismiss gnome-shell's "Keep these display settings?" dialog
    // (opened from Shell.WM 'confirm-display-change' after a persistent
    // ApplyMonitorsConfig). Returns true when a dialog was found and its
    // Keep/Revert action invoked. When none is found the verdict is still
    // forwarded to Mutter via global.window_manager.complete_display_change()
    // (a no-op when nothing is pending) and false is returned.
    _confirmDisplayChange(keep) {
        const dialog = findDisplayChangeDialog();
        if (dialog) {
            if (keep)
                dialog._onSuccess();
            else
                dialog._onFailure();
            return true;
        }
        // TODO-VERIFY (46+50): Shell.WM.complete_display_change(bool) is what
        // the dialog's _onSuccess/_onFailure call (shell-wm.c ->
        // meta_plugin_complete_display_change) and is a no-op with nothing
        // pending. Probed, so a missing method just means "not handled".
        if (isFn(global.window_manager, 'complete_display_change'))
            safe(() => global.window_manager.complete_display_change(keep));
        return false;
    }
}
