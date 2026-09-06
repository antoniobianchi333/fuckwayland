// The decisions the overlap extension makes, with nothing to make them about:
// no gi, no compositor, no memory.  Everything in this file is a pure function
// of its arguments, which is why tests/test_gnome_overlap.py can run it under
// plain node and check it against the Python that already implements the same
// rules (wxrandr/monitors_xml.py), on a machine with no GNOME anywhere.
//
// extension.js does the parts that cannot be pure -- reading /proc/self/maps,
// copying bounded byte ranges, writing the two words -- and asks this file
// every question that has a right answer.

// -- which builds are known --------------------------------------------------

// GNOME Shell major -> libmutter generation, for the two releases whose
// private MetaMonitorsConfig layout has been measured (docs/Technical.md
// section 6).  An allowlist, deliberately: a wrong offset does not raise an
// error, it writes into the compositor's heap.
export const SUPPORTED = {46: 14, 50: 18};

export function generationFor(shellVersion) {
    const major = parseInt(`${shellVersion}`.split('.')[0], 10);
    return SUPPORTED[major] || null;
}

// -- Mutter's own geometry rule ----------------------------------------------
//
// meta_verify_logical_monitor_config_list(): every logical monitor shares an
// exact integer edge with another, none overlaps another, and the layout is
// anchored at 0,0.  Adjacency is tested first, which is why one sentence comes
// back for a gap and for an overlap alike.
//
// It is reimplemented here for one purpose only: to refuse a layout Mutter
// would have accepted.  A request GNOME will take belongs on the ordinary
// DisplayConfig path, which validates, arms a revert timer and can be undone
// from a dialog -- there is no reason to write into a compositor's heap for it.

export const overlaps = (a, b) =>
    a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

export const adjacent = (a, b) => {
    const [ax2, ay2, bx2, by2] = [a.x + a.w, a.y + a.h, b.x + b.w, b.y + b.h];
    if ((a.x === bx2 || ax2 === b.x) && !(ay2 <= b.y || by2 <= a.y))
        return true;
    return (a.y === by2 || ay2 === b.y) && !(ax2 <= b.x || bx2 <= a.x);
};

export function mutterFault(rects) {
    if (!rects.length)
        return null;
    if (rects.length > 1) {
        for (const r of rects) {
            if (!rects.some(o => o !== r && adjacent(r, o)))
                return 'logical monitors not adjacent';
        }
    }
    for (let i = 0; i < rects.length; i++) {
        if (rects.slice(0, i).some(o => overlaps(rects[i], o)))
            return 'logical monitors overlap';
    }
    if (Math.min(...rects.map(r => r.x)) !== 0 || Math.min(...rects.map(r => r.y)) !== 0)
        return 'logical monitors are not anchored at 0,0';
    return null;
}

// -- the one thing that must not be on screen ---------------------------------
//
// A pending display change is the single window in which a configuration this
// extension has mutated can reach Mutter's *writer*: answering "Keep changes?"
// with Keep makes Mutter save whatever configuration is current, and after an
// overlap apply that is the overlapping one.  It lands in
// ~/.config/monitors.xml, whose reader runs the validator this whole feature
// exists to get past, and which discards the entire file -- every other saved
// arrangement -- at every boot, for ever.  That is not hypothetical: it was
// measured happening on both releases, at a time when this check could not
// fire.
//
// The count, and not the dialog: gnome-shell 46 and 50 build their
// DisplayChangeDialog in windowManager.js and keep no reference to it anywhere
// a reader can find.  `Main.wm._displayChangeDialog`, which an earlier version
// of this check read, exists on neither -- so the check passed always, which is
// worse than having no check at all, because a check that cannot fire is
// believed.  What the dialog does do is take a modal grab, and `Main.modalCount`
// goes 0 -> 1 while it is up (measured, 46.0 and 50.1).  Other things take a
// modal grab too -- the overview, an open menu, the Alt+F2 dialog -- and
// refusing on those as well is the price of a check that does not depend on
// naming a dialog this shell will not name.
//
// It fails closed.  A count that is not a number is a refusal, so a shell that
// renames the field stops the feature instead of quietly disarming its most
// consequential guard.
//
// It has been measured refusing when nothing was on screen: once, on the first
// call after a post-update login on GNOME 46, with no dialog, no menu and no
// overview anywhere (screenshot checked), and the very next call seconds later
// applied normally.  A grab held for a moment while a session finishes coming
// up is not the thing this guard is for, but it is indistinguishable from it
// through `Main.modalCount`, and the cost of the two mistakes is not
// symmetrical: a false refusal costs one retry, a false pass costs
// ~/.config/monitors.xml for ever.  So the check was left exactly as strict as
// it was and the *message* was fixed, because what it said then -- answer the
// dialog -- named something the user could not see.  It now says the count, and
// says that a grab nobody can see goes away on its own.

export function modalVerdict(modalCount) {
    if (typeof modalCount !== 'number' || !Number.isInteger(modalCount)) {
        return 'this shell does not report Main.modalCount as a whole number ' +
               `(got ${typeof modalCount}), so whether GNOME is asking ` +
               '"Keep changes?" cannot be read, and an unreadable check ' +
               'refuses rather than guesses';
    }
    if (modalCount < 0) {
        return `Main.modalCount is ${modalCount}, which is not a number of ` +
               'modal grabs that can exist; this check refuses rather than ' +
               'reads a shell it does not understand';
    }
    if (modalCount > 0) {
        return `something holds a modal grab on the shell (Main.modalCount is ` +
               `${modalCount}).  If that is GNOME asking whether to keep a ` +
               'display change, confirming it while this had moved a monitor ' +
               'is the one way an overlapping layout could reach ' +
               '~/.config/monitors.xml and stay there.  Nothing was read and ' +
               'nothing was written.  Answer it -- or close the overview, or ' +
               'the menu -- and run this again.  If there is nothing on screen ' +
               'to answer, this is a grab that has not been released yet, ' +
               'which was measured in the first seconds of a fresh session: ' +
               'wait a moment and run the same command again';
    }
    return null;
}

// -- the library this session is actually running -----------------------------
//
// Measured, on both releases: `apt upgrade` of libmutter under a live session
// leaves the session running the library it started with -- same gnome-shell
// pid, old code mapped -- while the new one is already on disk.  Nothing
// anywhere noticed, and nothing had to: the checks all run against the *mapped*
// library, so what they measured is what gets written to.
//
// It is still worth saying out loud, for two reasons.  The layout that applies
// now is the old library's, and the next login runs the new one, where this may
// refuse.  And the recorded agreement (wxrandr/gnome_overlap.py) names a build:
// a libmutter swapped under an unchanged GNOME Shell version string is exactly
// the change `ShellVersion` cannot see, and it happens -- noble carried mutter
// 46.2 under shell 46.0 for most of its life, and 46.0 -> 46.2 under one
// unchanged shell version was measured applying here.
//
// This is a note, never a refusal.  The library in memory is the one every
// guard just checked; a new file on disk says nothing about it.

// /proc/self/maps, as the mappings of one libmutter generation.  A pure
// function of the file's text, and here rather than in extension.js because it
// is a thing with a right answer that has to be checked against real lines: the
// path a stable release maps is `libmutter-14.so.0.0.0`, not the soname, and a
// pattern anchored on `.so.0` silently matches nothing at all -- which is how
// the first cut of this reported "no build id" on every machine it ran on.
//
// [{gen, path, inode, deleted}], first mapping of each path.  Everything it
// feeds is bookkeeping, so a line it cannot read is simply not in the list.

export function parseMutterMappings(mapsText) {
    const out = new Map();
    for (const line of `${mapsText}`.split('\n')) {
        // address perms offset dev inode pathname
        const m = line.match(/^[0-9a-f]+-[0-9a-f]+ \S+ \S+ \S+ +(\d+) +(\/\S*libmutter-(\d+)\.so\.0\S*?)( \(deleted\))?$/);
        if (m && !out.has(m[2]))
            out.set(m[2], {gen: Number(m[3]), path: m[2], inode: Number(m[1]), deleted: !!m[4]});
    }
    return [...out.values()];
}

export function libmutterNote(ident) {
    if (!ident || !ident.replaced)
        return null;
    return ('libmutter has been replaced on disk since this session started (' +
            `${ident.path || 'the mapped library'} is no longer the file this ` +
            'gnome-shell has mapped).  The checks ran against the library ' +
            'this session is running, which is the one being written to, so ' +
            'this changes nothing now -- but the next login runs the new one, ' +
            'and this feature may refuse there');
}

// -- shapes ------------------------------------------------------------------

export const key = group => (group.connectors || []).slice().sort().join('+');

export const cmpRegion = (a, b) => a.x - b.x || a.y - b.y ||
    key(a).localeCompare(key(b));

// Exactly four bytes, always: the typelib declares the write's length
// parameter as the length *of this array*, so gjs derives it from here and
// extension.js passes no count of its own.
export const le32 = v => [v & 255, (v >> 8) & 255, (v >> 16) & 255, (v >>> 24) & 255];

// -- the comparison ----------------------------------------------------------
//
// The check that decides whether what was read out of Mutter's private
// structures is really Mutter's monitors: every field, against the answer
// Mutter gives publicly.  A wrong offset that survived the struct-size gate and
// the sentinel dies here, because garbage does not agree with the public view
// on count, geometry, scale, primary and connector names all at once.
//
// Connector names are compared only where the public side has them: GNOME 46
// exposes no way to name a monitor from JS, and the caller's relayed
// DisplayConfig names are then all there is (extension.js says so in its
// answer).  Everything else is compared unconditionally.

export function compare(priv, pub) {
    const diffs = [];
    if (priv.length !== pub.length)
        diffs.push(`private read has ${priv.length} monitors, Mutter reports ${pub.length}`);
    for (let i = 0; i < Math.min(priv.length, pub.length); i++) {
        for (const k of ['x', 'y', 'w', 'h', 'scale', 'primary']) {
            if (String(priv[i][k]) !== String(pub[i][k]))
                diffs.push(`monitor ${i}: ${k} reads ${priv[i][k]}, Mutter says ${pub[i][k]}`);
        }
        const p = (pub[i].connectors || []).join(',');
        const q = (priv[i].connectors || []).join(',');
        if (p && q !== p)
            diffs.push(`monitor ${i}: connectors read ${q || '(none)'}, Mutter says ${p}`);
    }
    return diffs;
}

// What the configuration must look like once the two words have been written:
// the requested positions, and every other field exactly as it was.  Anything
// else means the write went somewhere it was not aimed, and the caller puts
// the old bytes back rather than applying.

export function drift(after, want) {
    const diffs = [];
    if (after.length !== want.length)
        diffs.push(`${after.length} monitors after the write, ${want.length} expected`);
    for (let i = 0; i < Math.min(after.length, want.length); i++) {
        for (const k of ['x', 'y', 'w', 'h', 'scale', 'transform', 'primary']) {
            if (String(after[i][k]) !== String(want[i][k]))
                diffs.push(`monitor ${i}: ${k} is ${after[i][k]}, expected ${want[i][k]}`);
        }
        if ((after[i].connectors || []).join(',') !== (want[i].connectors || []).join(','))
            diffs.push(`monitor ${i}: connectors changed`);
    }
    return diffs;
}
