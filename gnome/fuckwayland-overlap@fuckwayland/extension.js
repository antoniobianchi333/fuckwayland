// fuckwayland-overlap -- put two GNOME monitors on top of each other.
//
// THIS IS NOT THE BRIDGE.  `fuckwayland-bridge@fuckwayland` is plain
// feature-detected JavaScript against public APIs and works on six Shell
// versions; it is what wdotool, wwmctl and wxprop need and it is safe to leave
// installed for ever.  This extension is a different kind of thing: it ships a
// compiled type description pinned to a *private* structure layout inside
// libmutter, and it writes into gnome-shell's own heap.  A wrong layout is a
// dead compositor, and on Wayland a dead compositor is the whole session.
//
// Three rules hold it together.  See docs/Technical.md section 6.
//
//   1. IT DOES NOTHING AT LOGIN.  enable() exports one D-Bus object and stops.
//      No typelib is loaded, no libmutter symbol is touched, nothing is read
//      and nothing is written until somebody calls a method.  The catastrophic
//      failure is a crash at session start with no way to reach a setting to
//      switch this off, so there is no code that can run then.
//   2. EVERY CHECK RUNS ON EVERY CALL, never once at install: a distribution
//      upgrade can replace libmutter under a running session.
//   3. NO POINTER IS EVER DEREFERENCED BY THE TYPE SYSTEM.  The shipped type
//      description declares every pointer as guint64, so reading one yields a
//      number; each step to the next struct is g_memdup2(ptr, n) with the
//      address range-checked against /proc/self/maps first.  A wrong offset
//      then reads garbage, which the comparison against Mutter's public view
//      rejects, instead of walking a wild pointer into a SIGSEGV.
//
// It can never cause ~/.config/monitors.xml to be written: the type
// description does not declare meta_monitor_config_manager_save_current (or
// any other writer), the apply method is the constant METHOD_TEMPORARY, and
// the file's digest is taken before and after and returned to the caller.

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import GObject from 'gi://GObject';
import GIRepository from 'gi://GIRepository';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// Every decision with a right answer lives next door, in a file with no gi
// imports, so that tests/test_gnome_overlap.py can run it under plain node.
import {SUPPORTED, generationFor, mutterFault, key, cmpRegion, le32, compare, drift} from './rules.js';

const BUS_NAME = 'org.fuckwayland.Overlap';
const OBJECT_PATH = '/org/fuckwayland/Overlap';
const IFACE_NAME = 'org.fuckwayland.Overlap1';
const VERSION = 1;

// META_MONITORS_CONFIG_METHOD_TEMPORARY.  The one and only value this file
// ever passes to meta_monitor_manager_apply_monitors_config: 2 (PERSISTENT) is
// what makes Mutter write ~/.config/monitors.xml, and that must be
// unreachable, not merely unused.  tests/test_gnome_overlap.py asserts that no
// other constant reaches the apply.
const METHOD_TEMPORARY = 1;

const MAX_MONITORS = 16;        // list walks are bounded; nothing here loops on heap data
const MAX_CONNECTOR = 63;       // g_strndup bound for a connector name
const SENTINEL = 0x5f5a;        // written through Mutter's own setter, read back at our offset

const IFACE_XML = `
<node>
  <interface name="${IFACE_NAME}">
    <method name="Probe">
      <arg type="s" name="request" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="ApplyOverlap">
      <arg type="s" name="request" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <property name="Version" type="u" access="read"/>
  </interface>
</node>`;

// -- refusals ----------------------------------------------------------------

class Refused extends Error {
    constructor(check, detail) {
        super(detail);
        this.check = check;
    }
}

const refuse = (check, detail) => {
    throw new Refused(check, detail);
};

// -- /proc/self/maps ---------------------------------------------------------
//
// The only thing standing between a wrong offset and a compositor crash.  An
// address that is not inside a readable mapping is never handed to g_memdup2.

class Maps {
    constructor() {
        this._ranges = [];
        this.reload();
    }

    reload() {
        const [ok, bytes] = GLib.file_get_contents('/proc/self/maps');
        if (!ok)
            refuse('maps', 'cannot read /proc/self/maps');
        const ranges = [];
        for (const line of new TextDecoder().decode(bytes).split('\n')) {
            const m = line.match(/^([0-9a-f]+)-([0-9a-f]+) (....)/);
            if (m && m[3][0] === 'r')
                ranges.push([parseInt(m[1], 16), parseInt(m[2], 16)]);
        }
        this._ranges = ranges;
        this.text = new TextDecoder().decode(bytes);
    }

    holds(p, n) {
        if (!Number.isSafeInteger(p) || p < 0x1000 || n <= 0)
            return false;
        const hit = () => this._ranges.some(([a, b]) => p >= a && p + n <= b);
        if (hit())
            return true;
        this.reload();          // the heap may have grown since we last looked
        return hit();
    }

    // Every libmutter mapped into this process, by soname.
    mutters() {
        const found = new Set();
        for (const m of this.text.matchAll(/\/\S*libmutter-(\d+)\.so\.0/g))
            found.add(m[1]);
        return [...found];
    }
}

// -- the reader --------------------------------------------------------------
//
// Everything below reads through `lib`, the type description the extension
// ships, in which no pointer is a pointer.

class Reader {
    constructor(lib, maps) {
        this.lib = lib;
        this.maps = maps;
        this.refusals = [];
    }

    _at(what, p, n) {
        if (this.maps.holds(p, n))
            return true;
        this.refusals.push(`${what}: 0x${(p || 0).toString(16)}+${n} is not in a readable mapping`);
        return false;
    }

    // A bounded copy of n bytes at p, read as the named record, or null.
    copy(kind, what, p, n) {
        if (!this._at(what, p, n))
            return null;
        return this.lib[kind](p, n);
    }

    string(what, p) {
        if (!this._at(what, p, 1))
            return null;
        return this.lib.strn(p, MAX_CONNECTOR);
    }

    // The logical monitors of a MetaMonitorsConfig, read through the offsets
    // the shipped description believes, or null with a reason in refusals.
    logicalMonitors(cfgAddr, instanceSize) {
        const cfg = this.copy('dup_cfg', 'config', cfgAddr, instanceSize);
        if (cfg === null)
            return null;
        this.tail = {
            layout_mode: cfg.layout_mode,
            switch_config: cfg.switch_config,
            list: cfg.logical_monitor_configs,
        };
        const out = [];
        let node = cfg.logical_monitor_configs;
        for (let i = 0; node && i < MAX_MONITORS; i++) {
            const n = this.copy('dup_node', `node[${i}]`, node, 24);
            if (n === null)
                return null;
            const d = this.copy('dup_lmc', `logical[${i}]`, n.data, 40);
            if (d === null)
                return null;
            const connectors = [];
            let mnode = d.monitor_configs;
            for (let j = 0; mnode && j < MAX_MONITORS; j++) {
                const mn = this.copy('dup_node', `member[${i}][${j}]`, mnode, 24);
                if (mn === null)
                    return null;
                const mc = this.copy('dup_mc', `monitor[${i}][${j}]`, mn.data, 24);
                if (mc === null)
                    return null;
                const ms = this.copy('dup_ms', `spec[${i}][${j}]`, mc.monitor_spec, 32);
                if (ms === null)
                    return null;
                const name = this.string(`connector[${i}][${j}]`, ms.connector);
                if (name === null)
                    return null;
                connectors.push(name);
                mnode = mn.next;
            }
            out.push({
                addr: n.data,
                x: d.x, y: d.y, w: d.width, h: d.height,
                scale: Math.round(d.scale * 1000) / 1000,
                transform: d.transform,
                primary: !!d.is_primary,
                connectors: connectors.slice().sort(),
            });
            node = n.next;
        }
        return out.sort(cmpRegion);
    }
}

// -- ~/.config/monitors.xml --------------------------------------------------

function savedConfigDigest() {
    const path = GLib.build_filenamev([GLib.get_user_config_dir(), 'monitors.xml']);
    if (!GLib.file_test(path, GLib.FileTest.EXISTS))
        return {path, state: 'absent'};
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        if (!ok)
            return {path, state: 'unreadable'};
        return {
            path,
            state: 'present',
            sha256: GLib.compute_checksum_for_data(GLib.ChecksumType.SHA256, bytes),
            size: bytes.length,
        };
    } catch (e) {
        return {path, state: 'unreadable'};
    }
}

const sameDigest = (a, b) =>
    a.state === b.state && a.sha256 === b.sha256 && a.size === b.size;

// -- the guarded session -----------------------------------------------------

class Guarded {
    constructor(extensionPath) {
        this.path = extensionPath;
        this.checks = [];
    }

    _pass(name, detail) {
        this.checks.push({name, ok: true, detail: `${detail}`});
    }

    // 1. Is this a build whose private layout has been measured?
    version() {
        const shell = `${Config.PACKAGE_VERSION}`;
        const generation = generationFor(shell);
        if (!generation) {
            refuse('shell-version',
                   `GNOME Shell ${shell}: this extension knows the private ` +
                   `layout of ${Object.keys(SUPPORTED).join(' and ')} only`);
        }
        const maps = new Maps();
        const loaded = maps.mutters();
        if (loaded.length !== 1 || Number(loaded[0]) !== generation) {
            refuse('libmutter',
                   `GNOME Shell ${shell} should carry libmutter-${generation}, ` +
                   `this process has [${loaded.join(', ')}]`);
        }
        const repo = GIRepository.Repository.dup_default
            ? GIRepository.Repository.dup_default() : GIRepository.Repository.get_default();
        const meta = repo.get_version ? repo.get_version('Meta') : null;
        if (meta !== null && Number(meta) !== generation) {
            refuse('meta-typelib',
                   `the Meta typelib says ${meta}, libmutter says ${generation}`);
        }
        this.shell = shell;
        this.generation = generation;
        this.maps = maps;
        this.repo = repo;
        this._pass('shell-version', `GNOME Shell ${shell}, libmutter-${generation}`);
        return this;
    }

    // 2. Load our own type description, and refuse unless the size it declares
    //    for MetaMonitorsConfig is the size the GType registry reports.  Read
    //    back from our own typelib, so there is no constant to go stale.
    typelib() {
        const ns = `FwOverlap${this.generation}`;
        const dir = GLib.build_filenamev([this.path, 'typelib']);
        if (!GLib.file_test(GLib.build_filenamev([dir, `${ns}-1.0.typelib`]),
                            GLib.FileTest.EXISTS))
            refuse('typelib', `${ns}-1.0.typelib is not installed in ${dir}`);
        try {
            if (this.repo.prepend_search_path)
                this.repo.prepend_search_path(dir);
            else
                GIRepository.Repository.prepend_search_path(dir);
        } catch (e) {
            refuse('typelib', `cannot prepend ${dir}: ${e}`);
        }
        let lib;
        try {
            lib = globalThis.imports.gi[ns];
        } catch (e) {
            refuse('typelib', `${ns} did not load: ${e}`);
        }
        // Touching a declared function whose symbol is missing throws a
        // catchable GLib.Error rather than crashing, so every symbol can be
        // probed.  A libmutter that dropped one of these is not one to write to.
        for (const fn of ['dup_cfg', 'dup_node', 'dup_lmc', 'dup_mc', 'dup_ms',
                          'strn', 'addr', 'unref_addr', 'type_name',
                          'get_config_manager', 'get_current', 'create_linear',
                          'get_switch_config', 'set_switch_config',
                          'verify', 'apply', 'wr']) {
            if (typeof lib[fn] !== 'function')
                refuse('symbols', `${ns}.${fn} is not callable`);
        }
        const declared = structSize(this.repo, ns, 'ConfigN');
        if (!declared)
            refuse('struct-size', `cannot read the size of ${ns}.ConfigN back from its own typelib`);
        const gtype = GObject.type_from_name('MetaMonitorsConfig');
        if (!gtype)
            refuse('struct-size', 'MetaMonitorsConfig is not a registered GType');
        const actual = GObject.type_query(gtype).instance_size;
        if (declared !== actual) {
            refuse('struct-size',
                   `this build's MetaMonitorsConfig is ${actual} bytes, the ` +
                   `description shipped for libmutter-${this.generation} is ${declared}`);
        }
        this.lib = lib;
        this.instanceSize = actual;
        this._pass('typelib', `${ns}, MetaMonitorsConfig ${actual} bytes as declared`);
        return this;
    }

    // 3. A value written through Mutter's own exported setter has to appear at
    //    the offset we believe.  Done on a throwaway config built for the
    //    purpose, never on the one the session is running.
    sentinel() {
        const lib = this.lib;
        const mm = global.backend.get_monitor_manager();
        const cm = lib.get_config_manager(mm);
        if (!cm)
            refuse('sentinel', 'meta_monitor_manager_get_config_manager returned nothing');
        const throwaway = lib.create_linear(cm);
        if (!throwaway)
            refuse('sentinel', 'meta_monitor_config_manager_create_linear returned nothing');
        const name = lib.type_name(throwaway);
        if (name !== 'MetaMonitorsConfig')
            refuse('sentinel', `create_linear returned a ${name}`);
        lib.set_switch_config(throwaway, SENTINEL);
        if (lib.get_switch_config(throwaway) !== SENTINEL)
            refuse('sentinel', 'Mutter did not read back its own switch_config');
        const addr = lib.addr(throwaway);
        lib.unref_addr(addr);
        const reader = new Reader(lib, this.maps);
        const copy = reader.copy('dup_cfg', 'sentinel config', addr, this.instanceSize);
        if (copy === null)
            refuse('sentinel', reader.refusals.join('; '));
        if (copy.switch_config !== SENTINEL) {
            refuse('sentinel',
                   `switch_config reads ${copy.switch_config} at the offset this ` +
                   `description believes, not ${SENTINEL}: the tail of ` +
                   'MetaMonitorsConfig is not where it was measured');
        }
        this.mm = mm;
        this.cm = cm;
        this._pass('sentinel', `switch_config round-tripped at the declared offset`);
        return this;
    }

    // 3b. Refuse while GNOME is asking "Keep changes?".
    //
    // A pending display change is the one window in which a configuration we
    // mutated could reach Mutter's writer: confirming the dialog makes Mutter
    // save whatever configuration is current, and after our apply that is the
    // overlapping one.  wxrandr cannot arm such a change together with this
    // flag (--persistent is refused), but gnome-control-center can have one up
    // in another window.  Reading a JS field on the window manager cannot crash
    // anything, and an undefined field is a no.
    noPendingDialog() {
        let dialog;
        try {
            dialog = Main.wm && Main.wm._displayChangeDialog;
        } catch (e) {
            dialog = null;
        }
        if (dialog) {
            refuse('pending-dialog',
                   'GNOME is asking whether to keep a display change; answer ' +
                   'that first -- confirming it while this had moved a monitor ' +
                   'would be the one way an overlapping layout could reach ' +
                   '~/.config/monitors.xml');
        }
        this._pass('pending-dialog', 'no "Keep changes?" dialog is open');
        return this;
    }

    // 4. Read the live configuration, bounded, and refuse unless it is exactly
    //    what Mutter says publicly.
    read(layoutMode, expect) {
        const lib = this.lib;
        const cfg = lib.get_current(this.cm);
        if (!cfg)
            refuse('current', 'this session has no current monitors configuration');
        const name = lib.type_name(cfg);
        if (name !== 'MetaMonitorsConfig')
            refuse('current', `the current configuration is a ${name}`);
        const addr = lib.addr(cfg);
        lib.unref_addr(addr);
        const reader = new Reader(lib, this.maps);
        const priv = reader.logicalMonitors(addr, this.instanceSize);
        if (priv === null)
            refuse('bounded-read', reader.refusals.join('; '));
        if (layoutMode && reader.tail.layout_mode !== layoutMode) {
            refuse('layout-mode',
                   `layout_mode reads ${reader.tail.layout_mode} at the offset this ` +
                   `description believes; DisplayConfig says ${layoutMode}`);
        }
        const pub = this.publicView(expect);
        const diffs = compare(priv, pub);
        if (diffs.length)
            refuse('public-view', diffs.join('; '));
        this.cfg = cfg;
        this.cfgAddr = addr;
        this.reader = reader;
        this.private = priv;
        this.public = pub;
        this._pass('bounded-read', `${priv.length} logical monitors, every address range-checked`);
        this._pass('public-view', `identical to Mutter's public view (${pub.source})`);
        return this;
    }

    // Mutter's public answer about the same monitors.  Geometry, scale and
    // primary come from global.display, which is public API on every version.
    // Connector names come from MetaMonitorManager where it enumerates them
    // (GNOME 50), and otherwise from resolving the names the caller read out
    // of DisplayConfig -- Mutter answering, not the caller.
    publicView(expect) {
        const mm = this.mm;
        const byIndex = {};
        let source = 'global.display';
        if (typeof mm.get_monitors === 'function' &&
            typeof mm.get_monitor_for_connector === 'function') {
            for (const m of mm.get_monitors()) {
                const c = m.get_connector();
                const i = mm.get_monitor_for_connector(c);
                (byIndex[i] = byIndex[i] || []).push(c);
            }
            source = 'global.display + MetaMonitorManager.get_monitors';
        } else if (typeof mm.get_monitor_for_connector === 'function') {
            for (const group of expect || []) {
                for (const c of group.connectors || []) {
                    const i = mm.get_monitor_for_connector(c);
                    if (!(i >= 0))
                        refuse('connectors', `Mutter does not know a connector named ${c}`);
                    (byIndex[i] = byIndex[i] || []).push(c);
                }
            }
            source = 'global.display + get_monitor_for_connector on the requested names';
        } else if ((expect || []).length) {
            // GNOME 46 exposes no way to name a monitor from JS at all (and a
            // synchronous DisplayConfig call from inside gnome-shell deadlocks,
            // because gnome-shell is what serves it).  Fall back to attaching
            // the names the caller read out of DisplayConfig to the geometry
            // global.display reports, and say in the result that that is what
            // happened: the geometry, scale and primary half of the comparison
            // is still Mutter's own answer, independent of the caller.
            const byXY = {};
            for (const group of expect)
                byXY[`${group.x},${group.y}`] = (group.connectors || []).slice().sort();
            this._byXY = byXY;
            source = 'global.display + connector names relayed from DisplayConfig by the caller';
        } else {
            refuse('connectors', 'this Shell exposes no way to name a monitor from JS');
        }
        const primary = global.display.get_primary_monitor();
        const out = [];
        for (let i = 0; i < global.display.get_n_monitors(); i++) {
            const g = global.display.get_monitor_geometry(i);
            out.push({
                x: g.x, y: g.y, w: g.width, h: g.height,
                scale: Math.round(global.display.get_monitor_scale(i) * 1000) / 1000,
                primary: i === primary,
                connectors: (byIndex[i] ||
                             (this._byXY ? this._byXY[`${g.x},${g.y}`] : null) ||
                             []).slice().sort(),
            });
        }
        out.sort(cmpRegion);
        out.source = source;
        return out;
    }

    monitors() {
        return this.private.map(m => ({
            connectors: m.connectors, x: m.x, y: m.y, w: m.w, h: m.h,
            scale: m.scale, transform: m.transform, primary: m.primary,
        }));
    }

    result(extra) {
        return Object.assign({
            ok: true,
            version: VERSION,
            shell: this.shell,
            libmutter: this.generation,
            checks: this.checks,
            monitors: this.monitors(),
        }, extra || {});
    }
}

function structSize(repo, ns, record) {
    // GIRepository's own API moved between glib 2.80 and 2.88; ask every shape
    // rather than pick one, and refuse if none of them answers.  The size has
    // to come out of the shipped typelib itself: a constant written here could
    // go stale against the description beside it, which is the whole failure
    // this check exists to catch.
    let info = null;
    for (const find of [() => repo.find_by_name(ns, record),
                        () => GIRepository.Repository.find_by_name(repo, ns, record)]) {
        try {
            info = find();
        } catch (e) {
            info = null;
        }
        if (info)
            break;
    }
    if (!info)
        return 0;
    for (const get of [() => info.get_size(),
                       () => GIRepository.struct_info_get_size(info),
                       () => GIRepository.StructInfo.get_size(info)]) {
        try {
            const n = get();
            if (Number.isInteger(n) && n > 0)
                return n;
        } catch (e) {
            // try the next shape
        }
    }
    return 0;
}

// -- the extension -----------------------------------------------------------

export default class FwOverlap extends Extension {
    // Nothing here reads a monitor, loads a typelib or touches libmutter.  If
    // this method ever grows any of that, an enabled extension can kill the
    // session at login, which is the one failure this design exists to prevent.
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, null, null);
        console.log('fuckwayland-overlap: enabled (idle; it acts only when called)');
    }

    disable() {
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = 0;
        }
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    get Version() {
        return VERSION;
    }

    // Run every check, read the live configuration, write nothing.
    Probe(request) {
        return this._answer(request, false);
    }

    // Run every check, then move the logical monitors the caller asked for.
    ApplyOverlap(request) {
        return this._answer(request, true);
    }

    _answer(request, write) {
        const checks = [];
        try {
            const req = JSON.parse(request || '{}');
            const g = new Guarded(this.path);
            g.version().typelib().sentinel().noPendingDialog()
                .read(req.layout_mode, req.expect);
            checks.push(...g.checks);
            if (!write)
                return JSON.stringify(g.result({wrote: false}));
            return JSON.stringify(this._apply(g, req));
        } catch (e) {
            const refused = e instanceof Refused;
            if (!refused)
                console.error(`fuckwayland-overlap: ${e}\n${e.stack || ''}`);
            return JSON.stringify({
                ok: false,
                version: VERSION,
                check: refused ? e.check : 'internal',
                reason: `${e.message || e}`,
                checks,
            });
        }
    }

    _apply(g, req) {
        const want = req.want || [];
        const expect = req.expect || [];
        const live = g.private;

        // The caller's picture of the layout has to be this one, exactly.
        const liveKeys = live.map(key).sort().join(' ');
        if (expect.map(key).sort().join(' ') !== liveKeys)
            refuse('request',
                   `this session has monitors [${liveKeys}], the request `
                   + `names [${expect.map(key).sort().join(' ')}]`);
        if (want.map(key).sort().join(' ') !== liveKeys)
            refuse('request', 'the requested layout does not name the same monitors');
        for (const e of expect) {
            const m = live.find(l => key(l) === key(e));
            if (m.x !== e.x || m.y !== e.y) {
                refuse('request',
                       `${key(e)} is at +${m.x}+${m.y}, the request was built when it ` +
                       `was at +${e.x}+${e.y}: re-read the layout and try again`);
            }
        }

        // What is being asked for must be a layout Mutter refuses.  Anything
        // else belongs on the ordinary DisplayConfig path, which validates,
        // reverts on a timer and can be undone by a dialog; there is no reason
        // to write into the compositor's heap for a layout it would accept.
        const targets = want.map(w => {
            const m = live.find(l => key(l) === key(w));
            return {x: w.x | 0, y: w.y | 0, w: m.w, h: m.h};
        });
        const fault = mutterFault(targets);
        if (!fault) {
            refuse('not-an-overlap',
                   'Mutter accepts this layout: apply it the ordinary way, ' +
                   'without this extension');
        }

        const before = savedConfigDigest();
        const undo = [];
        let out;
        try {
            for (const w of want) {
                const m = live.find(l => key(l) === key(w));
                if (m.x === (w.x | 0) && m.y === (w.y | 0))
                    continue;
                // The address came out of a read the range check already
                // cleared; check it again anyway, immediately before writing.
                if (!g.maps.holds(m.addr, 8))
                    refuse('write', `0x${m.addr.toString(16)} is no longer readable`);
                undo.push({addr: m.addr, x: m.x, y: m.y});
                g.lib.wr(m.addr, le32(w.x | 0), 4);          // MetaLogicalMonitorConfig.x, +0
                g.lib.wr(m.addr + 4, le32(w.y | 0), 4);      // .y, +4
            }
            if (!undo.length)
                refuse('request', 'that is the layout this session already has');

            // Read it all back the same bounded way: the write must have
            // changed the two words it was aimed at and nothing else.
            const after = new Reader(g.lib, g.maps).logicalMonitors(g.cfgAddr, g.instanceSize);
            if (after === null)
                refuse('read-back', 'the configuration no longer reads back');
            const wantRects = want.map(w => {
                const m = live.find(l => key(l) === key(w));
                return {x: w.x | 0, y: w.y | 0, w: m.w, h: m.h, scale: m.scale,
                        transform: m.transform, primary: m.primary,
                        connectors: m.connectors};
            }).sort(cmpRegion);
            const moved = drift(after, wantRects);
            if (moved.length)
                refuse('read-back', moved.join('; '));

            // Positive control: Mutter's own validator has to refuse what we
            // just built.  If it accepts it, the write did not land on the
            // field the validator reads and nothing here means what it says.
            let verdict;
            try {
                const okay = g.lib.verify(g.cfg, g.mm);
                verdict = okay ? 'accepted' : 'refused without a message';
            } catch (e) {
                verdict = `refused: ${e.message}`;
            }
            if (verdict === 'accepted') {
                refuse('positive-control',
                       'Mutter validated the mutated configuration, so the write did ' +
                       'not land where its validator reads: nothing was applied');
            }

            let applied;
            try {
                applied = !!g.lib.apply(g.mm, g.cfg, METHOD_TEMPORARY);
            } catch (e) {
                refuse('apply', `${e.message}`);
            }
            if (!applied)
                refuse('apply', 'meta_monitor_manager_apply_monitors_config returned false');

            out = g.result({
                wrote: true,
                applied: true,
                fault,
                verify: verdict,
                wrote_words: undo.length * 2,
                monitors: after.map(m => ({
                    connectors: m.connectors, x: m.x, y: m.y, w: m.w, h: m.h,
                    scale: m.scale, transform: m.transform, primary: m.primary,
                })),
                public: g.publicView(want).map(m => ({
                    connectors: m.connectors, x: m.x, y: m.y, w: m.w, h: m.h,
                })),
            });
        } catch (e) {
            // Put the words back exactly as they were.  Nothing has been
            // applied on this path, so the session is untouched either way.
            for (const u of undo.reverse()) {
                if (g.maps.holds(u.addr, 8)) {
                    g.lib.wr(u.addr, le32(u.x), 4);
                    g.lib.wr(u.addr + 4, le32(u.y), 4);
                }
            }
            throw e;
        }
        const after = savedConfigDigest();
        out.saved_config = {
            path: before.path,
            before: before.state === 'present' ? before.sha256 : before.state,
            after: after.state === 'present' ? after.sha256 : after.state,
            unchanged: sameDigest(before, after),
        };
        if (!out.saved_config.unchanged)
            out.ok = false;
        return out;
    }
}
