"""Recreate the PlotDevice manual's window screenshots at 2x, in light and dark mode.

Reads manual/shots.json (one entry per image) and writes a pair of region grabs per shot and
appearance (over a white and a black backdrop) to PD_SHOTS_DIR/raw, and a shadowless pair to
PD_SHOTS_DIR/raw-flat, which matte.py turns into transparent PNGs. Configured like pd_shots (PD_SHOTS_DIR, PD_SHOTS_EXAMPLES).

Shot keys:
  name      output file name (dark versions get a -dark suffix)
  example   path within the examples dir, or
  script    file name within manual/scripts (its name sets the window title)
  content   [w, h] of the window's content area in points
  split     width of the canvas pane (the outer divider's position), or null
  output    height of the editor above the output pane (the inner divider's position), or null
  run       whether to run the script (default true), and settle: seconds to wait after
  seed      seed for the script's random() calls, so shots of the same script draw the same thing
  frames    stop an animation once it reaches this frame
  assets    {destination: path within manual/} files to put in place before running
  editor    {top: first visible line, cursor: [line, col], select: [[l1, c1], [l2, c2]]}
  sheet     {kind: image|movie, settings: {...}, where: folder} to open an export sheet
  menu      {bar: title, item: title} to open a menu bar menu and highlight an item (the menu bar &
            menu are grabbed apart from the window and composited onto it by matte.py)
  pdf       true to also save what Edit > Copy Image As PDF provides (to pdf/<name>.pdf)
  probe_menu  {bar: title, remove: title fragment}: an experiment that logs the menu's items rather than grabbing
  probe_context  true for an experiment that logs the context menus of the window's views rather than grabbing
  outline   true to also grab it without shadows, so matte.py --outline can keep just its edge
  panel     include the variables panel: {gap: points between the window's bottom edge & the panel's
            top (with their right edges aligned), click: a BUTTON variable to press first,
            only: true to shoot the panel alone, over: true to leave the document's canvas behind it,
            style: {titlebar: titled, toolbar: unified|compact, material: an NSVisualEffectMaterial
            name, blending: within, appearance: an NSAppearance name or null to follow the system}}
  hover_title  true to leave the pointer over the window's title (showing its chevron)
  zoom      the canvas's zoom level (1.0 is 100%)
  scroll    [x, y] of the canvas point to scroll to the pane's top-left
  prefs     true to shoot the preferences window instead of a document
  viewer    a script within manual/scripts to run with the command line tool, shooting its window
            from that process (PD_SHOTS_MODE=viewer) rather than the app's
  appearances  [light] and/or [dark] to shoot this one in, instead of the default (APPEARANCES)
"""
import os, sys, json, time, shutil, threading, traceback, subprocess

#
# running things on the main thread & in the editor
#

T0 = time.time()

def log(*args):
    print('[pd-shots %6.1fs]' % (time.time() - T0), *args, flush=True)

def on_main(fn, *args, timeout=30):
    """Run fn on the main thread and return its result"""
    from PyObjCTools import AppHelper
    done, box = threading.Event(), {}
    def call():
        try:
            box['val'] = fn(*args)
        except Exception as e:
            box['err'] = e
            traceback.print_exc()
        finally:
            done.set()
    AppHelper.callAfter(call)
    if not done.wait(timeout):
        raise RuntimeError('main thread did not respond in %is (calling %r)' % (timeout, fn))
    if 'err' in box:
        raise box['err']
    return box.get('val')

def settle(secs=2.0):
    time.sleep(secs)

def close_all():
    from AppKit import NSApp, NSDocumentController
    def closing():
        for doc in list(NSDocumentController.sharedDocumentController().documents()):
            ctrl = doc.windowControllers()[0] if doc.windowControllers() else None
            if ctrl is not None and getattr(ctrl, 'vm', None) is not None:
                ctrl.stopScript()
            doc.close()
    on_main(closing)
    settle(1)

def js(ctrl, src):
    """Evaluate src in the editor's webview and return its result (or error)"""
    done, box = threading.Event(), {}
    def finished(val, err):
        box['val'] = val if err is None else 'error: %s' % err
        done.set()
    on_main(lambda: ctrl.editorView.webview.evaluateJavaScript_completionHandler_(src, finished))
    done.wait(10)
    return box.get('val')

#
# screens, the 2x virtual display & backdrops
#

report = {}

keep = []

def note(key, value):
    report[key] = value
    log(key, json.dumps(value, default=str))

def screens():
    from AppKit import NSScreen
    def listing():
        out = []
        for s in NSScreen.screens():
            f = s.frame()
            out.append(dict(id=int(s.deviceDescription()['NSScreenNumber']), name=str(s.localizedName()),
                            frame=[f.origin.x, f.origin.y, f.size.width, f.size.height], scale=s.backingScaleFactor()))
        return out
    return on_main(listing)

def mode_info(m):
    import Quartz as Q
    return dict(w=Q.CGDisplayModeGetWidth(m), h=Q.CGDisplayModeGetHeight(m),
                pw=Q.CGDisplayModeGetPixelWidth(m), ph=Q.CGDisplayModeGetPixelHeight(m),
                hz=Q.CGDisplayModeGetRefreshRate(m), gui=bool(Q.CGDisplayModeIsUsableForDesktopGUI(m)))

def hidpi_modes(did):
    import Quartz as Q
    opts = {Q.kCGDisplayShowDuplicateLowResolutionModes: True}
    modes = Q.CGDisplayCopyAllDisplayModes(did, opts) or []
    return [(m, mode_info(m)) for m in modes]

def switch_to_2x(did, key):
    """Pick the largest 2x mode on a display and switch to it"""
    import Quartz as Q
    modes = hidpi_modes(did)
    note(key + '.modes', [i for _, i in modes])
    note(key + '.current', mode_info(Q.CGDisplayCopyDisplayMode(did)))
    doubled = [(m, i) for m, i in modes if i['pw'] == 2 * i['w'] and i['gui']]
    if not doubled:
        note(key + '.switch', 'no 2x modes')
        return False
    m, i = max(doubled, key=lambda mi: mi[1]['w'] * mi[1]['h'])
    err = Q.CGDisplaySetDisplayMode(did, m, None)
    settle(3)
    note(key + '.switch', dict(to=i, err=err, now=mode_info(Q.CGDisplayCopyDisplayMode(did))))
    return err == 0

def virtual():
    import objc
    import Quartz as Q
    Descriptor = objc.lookUpClass('CGVirtualDisplayDescriptor')
    Display = objc.lookUpClass('CGVirtualDisplay')
    Settings = objc.lookUpClass('CGVirtualDisplaySettings')
    Mode = objc.lookUpClass('CGVirtualDisplayMode')

    def creating():
        from dispatch import dispatch_get_main_queue
        desc = Descriptor.alloc().init()
        for sel in ('setDispatchQueue:', 'setQueue:'):
            if desc.respondsToSelector_(sel):
                getattr(desc, sel.replace(':', '_'))(dispatch_get_main_queue())
                break
        desc.setName_('pd-shots')
        desc.setMaxPixelsWide_(3840)
        desc.setMaxPixelsHigh_(2400)
        desc.setSizeInMillimeters_((520, 325))
        desc.setProductID_(0x5044)
        desc.setVendorID_(0x5053)
        desc.setSerialNum_(1)
        vd = Display.alloc().initWithDescriptor_(desc)
        if vd is None:
            return None, False
        settings = Settings.alloc().init()
        settings.setHiDPI_(1)
        settings.setModes_([Mode.alloc().initWithWidth_height_refreshRate_(w, h, 60)
                            for w, h in ((2880, 1800), (1440, 900))])
        return vd, bool(vd.applySettings_(settings))

    vd, applied = on_main(creating)
    if vd is None:
        note('virtual.create', 'initWithDescriptor returned nil')
        return
    keep.append(vd)
    did = int(vd.displayID())
    settle(3)
    note('virtual.create', dict(display_id=did, applied=applied))
    switch_to_2x(did, 'virtual')
    note('screens.after-virtual', screens())

def global_rect(scr_frames, frame, margin):
    """Convert a Cocoa frame into screencapture's top-left global coordinates"""
    H = scr_frames[0]['frame'][3] # the primary screen's height
    x, y, w, h = frame
    return '%d,%d,%d,%d' % (x - margin, H - (y + h) - margin, w + 2 * margin, h + 2 * margin)

def backdrop(rect_frame, color):
    from AppKit import NSWindow, NSColor, NSMakeRect, NSBackingStoreBuffered
    def making():
        x, y, w, h = rect_frame
        bg = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(x, y, w, h), 0, NSBackingStoreBuffered, False)
        bg.setReleasedWhenClosed_(False)
        bg.setBackgroundColor_(getattr(NSColor, color + 'Color')())
        bg.setLevel_(-1) # below normal windows
        bg.orderBack_(None)
        return bg
    return on_main(making)

def osa(body):
    """Run a System Events command against this process and return its output"""
    script = 'tell application "System Events" to tell (first process whose unix id is %d) to %s' % (os.getpid(), body)
    res = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=30)
    return res.stdout.strip() if res.returncode == 0 else 'error: ' + res.stderr.strip()


OUT = os.environ.get('PD_SHOTS_DIR')
EXAMPLES = os.environ.get('PD_SHOTS_EXAMPLES')
MANUAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'manual')
MARGIN = 60
results = []

# the manual uses the dark, shadowed versions; set these to also shoot light and/or flat ones
APPEARANCES = os.environ.get('PD_SHOTS_APPEARANCES', 'dark').split(',')
VARIANTS = ['raw', 'raw-flat'] if os.environ.get('PD_SHOTS_FLAT') else ['raw']

def grab(path, rect):
    res = subprocess.run(['screencapture', '-x', '-R', rect, path], capture_output=True, text=True)
    return res.returncode == 0 and os.path.exists(path)

def set_dark(dark):
    res = subprocess.run(['osascript', '-e', 'tell application "System Events" to tell appearance preferences to set dark mode to %s' % ('true' if dark else 'false')],
                         capture_output=True, text=True, timeout=30)
    settle(3)
    log('dark mode', dark, res.stderr.strip())

def open_doc(shot):
    from AppKit import NSApp
    path = os.path.join(EXAMPLES, shot['example']) if shot.get('example') else os.path.join(MANUAL, 'scripts', shot['script'])
    doc = on_main(lambda: NSApp().delegate().docFromTemplate_(path))
    ctrl = on_main(lambda: doc.windowControllers()[0])

    # wait for the editor's webview to load the script
    probe = "(typeof ace === 'undefined') ? 'no ace' : String(ace.edit('editor').getValue().length)"
    deadline, state = time.time() + 30, None
    while time.time() < deadline:
        state = js(ctrl, probe)
        if state not in (None, 'no ace', '0') and not str(state).startswith('error'):
            break
        time.sleep(1)
    ctrl.editor_state = dict(loaded=state, secs=round(30 - (deadline - time.time()), 1))
    settle(1)
    return doc, ctrl, on_main(ctrl.window)

def layout(ctrl, win, scr, shot):
    """Size the window so its content area matches the old screenshot, then set the dividers"""
    from AppKit import NSMakeRect, NSSplitView
    w, h = shot['content']
    sx, sy, sw, sh = scr['frame']
    def sizing():
        f, cl = win.frame(), win.contentLayoutRect()
        chrome = f.size.height - cl.size.height
        top = sy + sh - 120 # leave room above for the menu bar & shadow
        win.setFrame_display_(NSMakeRect(sx + 140, top - h - chrome, w, h + chrome), True)
        win.makeKeyAndOrderFront_(None)
        return chrome
    chrome = on_main(sizing)
    settle(1)

    def dividers():
        splits, v = {}, ctrl.editorView
        while v is not None:
            if isinstance(v, NSSplitView):
                splits['outer' if v.isVertical() else 'inner'] = v
            v = v.superview()
        if shot.get('split') is not None and 'outer' in splits:
            splits['outer'].setPosition_ofDividerAtIndex_(shot['split'], 0)
        if shot.get('output') is not None and 'inner' in splits:
            splits['inner'].setPosition_ofDividerAtIndex_(shot['output'], 0)
        cl = win.contentLayoutRect()
        return dict(chrome=chrome, content=[cl.size.width, cl.size.height], splits=sorted(splits))
    return on_main(dividers)

def editor_state(ctrl, ed):
    parts = ["var ed = ace.edit('editor')"]
    if ed.get('top'):
        parts.append('ed.scrollToLine(%d, false, false)' % (ed['top'] - 1))
    if ed.get('select'):
        (l1, c1), (l2, c2) = ed['select']
        parts.append('ed.selection.setRange(new (ace.require("ace/range").Range)(%d, %d, %d, %d))' % (l1 - 1, c1, l2 - 1, c2))
    elif ed.get('cursor'):
        l, c = ed['cursor']
        parts.append('ed.moveCursorTo(%d, %d); ed.clearSelection()' % (l - 1, c))
    if ed.get('select') or ed.get('cursor'):
        on_main(lambda: ctrl.window().makeFirstResponder_(ctrl.editorView.webview))
        parts.append('ed.focus(); ed.renderer.$cursorLayer.setBlinking(false)') # a steady caret
    else:
        parts.append('ed.blur()')
    parts.append('ed.renderer.updateFull(true); "ok"')
    return js(ctrl, '; '.join(parts))

def open_menu(menu):
    import Quartz as Q
    bar = menu['bar']
    click = subprocess.Popen(['osascript', '-e', 'tell application "System Events" to tell (first process whose unix id is %d) to click menu bar item "%s" of menu bar 1' % (os.getpid(), bar)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    settle(2)
    if menu.get('item'):
        vals = osa('get {position, size} of menu item "%s" of menu 1 of menu bar item "%s" of menu bar 1' % (menu['item'], bar))
        try:
            x, y, w, h = [float(v) for v in vals.split(',')]
            pt = (x + w / 2, y + h / 2)
            Q.CGWarpMouseCursorPosition(pt)
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, pt, 0))
            settle(1)
        except ValueError:
            log('menu item not found', menu, vals)
    return click

def close_menu(click):
    import Quartz as Q
    for down in (True, False):
        Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateKeyboardEvent(None, 53, down)) # escape
    settle(1.5)
    if click.poll() is None:
        click.kill()

def open_sheet(ctrl, win, sheet):
    settings = sheet.get('settings', {})
    def opening():
        target = ctrl.exportSheet.movie if sheet['kind'] == 'movie' else ctrl.exportSheet.image
        target.update(settings)

        # templates have no path, so name the export after the window instead of 'Untitled'
        folder = os.path.expanduser(sheet.get('where', '~/Documents'))
        os.makedirs(folder, exist_ok=True)
        ctrl.exportSheet.last = (folder, win.title())
        (ctrl.exportAsMovie_ if sheet['kind'] == 'movie' else ctrl.exportAsImage_)(None)
    on_main(opening)
    settle(3)

def point_at(x, y):
    """Move the pointer to a spot in screencapture's top-left global coordinates"""
    import Quartz as Q
    Q.CGWarpMouseCursorPosition((x, y))
    Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (x, y), 0))
    settle(0.5)

def place_pointer(win, scr, found, shot):
    """Hover over the window's title if asked to, otherwise park the pointer clear of every window"""
    H = found[0]['frame'][3]
    if shot.get('hover_title'):
        f = on_main(win.frame)
        point_at(f.origin.x + 120, H - (f.origin.y + f.size.height) + 20)
    else:
        sx, sy, sw, sh = scr['frame']
        point_at(sx + sw - 5, H - sy - 5) # the screen's bottom-right corner

def close_sheet(ctrl):
    on_main(lambda: ctrl.exportSheet.exportPanel.cancel_(None))
    settle(1.5)

def freeze_editor(ctrl):
    """Cover the editor's webview with a snapshot of it.

    On some runners webkit can't create the iosurfaces it composites its layers with, which leaves the
    editor blank on screen even though the page itself renders (and snapshots) correctly.
    """
    from AppKit import NSImageView, NSWindowAbove, NSImageScaleNone
    wv = ctrl.editorView.webview
    done, box = threading.Event(), {}
    def snapped(img, err):
        box['img'], box['err'] = img, err
        done.set()
    on_main(lambda: wv.takeSnapshotWithConfiguration_completionHandler_(None, snapped))
    if not done.wait(10) or box.get('img') is None:
        return 'no snapshot: %s' % box.get('err')
    def covering():
        iv = NSImageView.alloc().initWithFrame_(wv.frame())
        iv.setImage_(box['img'])
        iv.setImageScaling_(NSImageScaleNone)
        iv.setAutoresizingMask_(wv.autoresizingMask())
        wv.superview().addSubview_positioned_relativeTo_(iv, NSWindowAbove, wv)
        return [box['img'].size().width, box['img'].size().height]
    return on_main(covering)

def diagnose_webview(ctrl, name):
    """Record whether the editor's page rendered, and save an offscreen snapshot of it"""
    dom = js(ctrl, "JSON.stringify({text: (document.querySelector('.ace_text-layer') || {innerText: ''}).innerText.length, "
                   "lines: document.querySelectorAll('.ace_line').length, visibility: document.visibilityState, "
                   "size: [innerWidth, innerHeight], dpr: devicePixelRatio})")
    wv = ctrl.editorView.webview
    def native():
        f = wv.frame()
        return dict(frame=[f.origin.x, f.origin.y, f.size.width, f.size.height], hidden=bool(wv.isHiddenOrHasHiddenAncestor()),
                    alpha=wv.alphaValue(), layer=bool(wv.layer()), window=bool(wv.window()))
    done, box = threading.Event(), {}
    def snapped(img, err):
        box['err'] = str(err) if err else None
        if img is not None:
            from AppKit import NSBitmapImageRep, NSPNGFileType
            rep = NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
            rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(
                os.path.join(OUT, 'diag', name + '-webview.png'), True)
            box['size'] = [img.size().width, img.size().height]
        done.set()
    os.makedirs(os.path.join(OUT, 'diag'), exist_ok=True)
    on_main(lambda: wv.takeSnapshotWithConfiguration_completionHandler_(None, snapped))
    done.wait(10)
    return dict(dom=dom, native=on_main(native), snapshot=box or 'timed out')

def set_shadows(win, on):
    """Turn the shadows of every visible app window on or off (the document, its sheet, panels & menus)"""
    from AppKit import NSApp
    def setting():
        changed = []
        for w in NSApp().windows():
            if w.isVisible() and w.level() >= 0 and w.hasShadow() != on: # skipping the backdrop
                w.setHasShadow_(on)
                w.invalidateShadow()
                changed.append(str(w.className()))
        return changed
    try:
        changed = on_main(setting, timeout=5)
    except RuntimeError:
        changed = 'main thread busy' # e.g. while a menu is tracking
    settle(0.5)
    return changed

def region_for(win, scr, shot, extra=()):
    """The window's frame padded for its shadow, grown to cover any sheets and panels"""
    f = on_main(win.frame)
    x0, y0 = f.origin.x - MARGIN, f.origin.y - MARGIN
    x1, y1 = f.origin.x + f.size.width + MARGIN, f.origin.y + f.size.height + MARGIN
    for e in extra:
        x0, y0 = min(x0, e[0] - MARGIN), min(y0, e[1] - MARGIN)
        x1, y1 = max(x1, e[0] + e[2] + MARGIN), max(y1, e[1] + e[3] + MARGIN)
    return [x0, y0, x1 - x0, y1 - y0]

def style_panel(dash, panel, style):
    """Try out other looks for the variables panel: its title bar, a toolbar, its material & appearance"""
    import AppKit
    from AppKit import NSToolbar, NSWindowStyleMaskUtilityWindow
    def styling():
        view = panel.contentView()
        if style.get('titlebar') == 'titled':
            panel.setStyleMask_(panel.styleMask() & ~NSWindowStyleMaskUtilityWindow)
        if style.get('toolbar'):
            panel.setToolbar_(NSToolbar.alloc().initWithIdentifier_('pd-shots-empty'))
            panel.setToolbarStyle_(dict(unified=3, compact=4)[style['toolbar']])
        if style.get('material'):
            view.setMaterial_(getattr(AppKit, 'NSVisualEffectMaterial' + style['material']))
        if style.get('blending') == 'within':
            view.setBlendingMode_(AppKit.NSVisualEffectBlendingModeWithinWindow)
        if 'appearance' in style:
            # the nib forces darkAqua on both the panel and its content view; None follows the system
            look = AppKit.NSAppearance.appearanceNamed_(style['appearance']) if style['appearance'] else None
            panel.setAppearance_(look)
            view.setAppearance_(look)
        dash.updateInterface() # lay the rows out again for the new title bar
        f = panel.frame()
        return dict(mask=int(panel.styleMask()), material=int(view.material()), size=[f.size.width, f.size.height])
    return on_main(styling)

def scroll_canvas(ctrl, pt):
    """Scroll the canvas so a canvas point sits at the pane's top-left"""
    def scrolling():
        view = ctrl.graphicsView
        x, y = pt[0] * view.zoom, pt[1] * view.zoom
        if not view.isFlipped():
            y = view.bounds().size.height - y - view.visibleRect().size.height
        view.scrollPoint_((x, y))
        v = view.visibleRect()
        return [v.origin.x, v.origin.y]
    return on_main(scrolling)

def shoot(shot, appearance, scr, found):
    name = shot['name'] + ('-dark' if appearance == 'dark' else '')
    for dest, src in shot.get('assets', {}).items():
        shutil.copy(os.path.join(MANUAL, src), os.path.expanduser(dest))
    doc, ctrl, win = open_doc(shot)
    info = layout(ctrl, win, scr, shot)
    info['editor_load'] = ctrl.editor_state
    if shot.get('run', True):
        def running():
            if 'seed' in shot:
                import random
                random.seed(shot['seed']) # plotdevice's random() draws from the random module
            ctrl.runScript_(None)
        on_main(running)
        if shot.get('frames'):
            # halt the animation once it has drawn as many frames as the old screenshot did
            deadline = time.time() + 60
            while ctrl.vm._meta.frame <= shot['frames'] and time.time() < deadline: # the counter runs one ahead
                time.sleep(0.002)
            on_main(ctrl.stopScript)
            info['stopped_at'] = ctrl.vm._meta.frame
        settle(shot.get('settle', 3))
    if shot.get('probe_context'):
        # an experiment: right-click each part of the window and log the context menus (before the editor is frozen)
        info['context_menus'] = probe_context(ctrl, win, found)
        log('probed', name, json.dumps(info['context_menus']))
        results.append(dict(name=name, ok=True, **info))
        close_all()
        return
    if shot.get('editor'):
        info['editor'] = editor_state(ctrl, shot['editor'])
    else:
        # focus the canvas so the editor shows no caret
        js(ctrl, "ace.edit('editor').blur()")
        info['focus'] = bool(on_main(lambda: win.makeFirstResponder_(ctrl.graphicsView)))
    if shot.get('zoom'):
        on_main(lambda: setattr(ctrl.graphicsView, 'zoom', shot['zoom']))
        settle(1) # the new zoom applies once it re-renders
    if shot.get('scroll'):
        info['scroll'] = scroll_canvas(ctrl, shot['scroll'])
    settle(1)
    if os.environ.get('PD_SHOTS_DIAGNOSE'):
        info['webview'] = diagnose_webview(ctrl, name)
    info['frozen'] = freeze_editor(ctrl)
    settle(0.5)

    extra, key = [], None
    if shot.get('panel'):
        opts = shot['panel'] if isinstance(shot['panel'], dict) else {}
        dash = on_main(lambda: ctrl.dashboardController)
        panel = on_main(lambda: dash.panel) if dash is not None else None
        if panel is None or not on_main(panel.isVisible):
            info['panel'] = 'not visible'
        else:
            if opts.get('click'):
                on_main(dash.callHandler_, opts['click']) # what pressing the button does
                settle(1)
            if opts.get('style'):
                info['style'] = style_panel(dash, panel, opts['style'])
                settle(1)
            if opts.get('only'):
                # shoot the panel by itself, with the document moved out of its way (or, with over, behind it)
                def isolating():
                    pf, wf = panel.frame(), win.frame()
                    sx, sy, sw, sh = scr['frame']
                    panel.setFrameTopLeftPoint_((sx + 140, sy + sh - 120))
                    if opts.get('over'):
                        win.setFrameTopLeftPoint_((sx + 100, sy + sh - 60)) # the canvas fills in behind the panel
                    else:
                        win.setFrameTopLeftPoint_((sx + 140 + pf.size.width + 2 * MARGIN + 40, sy + sh - 120))
                    panel.makeKeyAndOrderFront_(None)
                    panel.makeFirstResponder_(None) # no focused field
                on_main(isolating)
                settle(1)
                capture(shot, name, panel, scr, found, info, key=panel)
                close_all()
                return
            def placing():
                # right-aligned with the window, just below it
                pf, wf = panel.frame(), win.frame()
                x = wf.origin.x + wf.size.width - pf.size.width
                y = wf.origin.y - opts.get('gap', 11) - pf.size.height
                panel.setFrameOrigin_((x, y))
                panel.orderFront_(None)
                pf = panel.frame()
                return [pf.origin.x, pf.origin.y, pf.size.width, pf.size.height]
            extra.append(on_main(placing))
            settle(1)
            key = panel # so its controls draw active rather than greyed out
    if shot.get('probe_menu'):
        # an experiment: log a menu's items (and whether removing one sticks) instead of grabbing anything
        info['menu_probe'] = probe_menu(shot['probe_menu'])
        log('probed', name, json.dumps(info['menu_probe']))
        results.append(dict(name=name, ok=True, **info))
        close_all()
        return
    if shot.get('pdf'):
        info['pdf'] = save_pdf(ctrl, name)
    if shot.get('menu'):
        capture_menu(shot, name, win, scr, found, info, ctrl)
    else:
        capture(shot, name, win, scr, found, info, ctrl, extra, key=key)
    close_all()

def describe_menu(menu, depth=0):
    """A menu's items (and their submenus') as plain data"""
    items = []
    for it in menu.itemArray():
        if it.isSeparatorItem():
            continue
        action = it.action()
        entry = dict(title=str(it.title()), id=str(it.identifier()) if it.identifier() else None, tag=int(it.tag()),
                     action=(action.decode() if isinstance(action, bytes) else str(action)) if action else None)
        if it.isHidden():
            entry['hidden'] = True
        if it.hasSubmenu() and depth < 2:
            entry['submenu'] = describe_menu(it.submenu(), depth + 1)
        items.append(entry)
    return items

def probe_context(ctrl, win, found):
    """Right-click the editor, canvas, output pane & a variables field, recording each context menu as it opens"""
    import Quartz as Q
    from AppKit import NSNotificationCenter
    seen, tracked, added = [], [], []
    def began(note):
        tracked.append(note.object())
        seen.append(describe_menu(note.object()))
    def adding(note):
        menu, idx = note.object(), note.userInfo()['NSMenuItemIndex']
        it = menu.itemAtIndex_(idx)
        added.append(dict(menu=str(menu.title()), index=int(idx), title=str(it.title()) if it else None,
                          id=str(it.identifier()) if it and it.identifier() else None, cls=str(it.className()) if it else None))
    nc = on_main(NSNotificationCenter.defaultCenter)
    observer = on_main(lambda: nc.addObserverForName_object_queue_usingBlock_('NSMenuDidBeginTrackingNotification', None, None, began))
    add_observer = on_main(lambda: nc.addObserverForName_object_queue_usingBlock_('NSMenuDidAddItemNotification', None, None, adding))
    H = found[0]['frame'][3]

    def center(view):
        def finding():
            r = view.window().convertRectToScreen_(view.convertRect_toView_(view.visibleRect(), None))
            return (r.origin.x + r.size.width / 2, H - (r.origin.y + r.size.height / 2))
        return on_main(finding)

    def click(pt, right=True):
        down, up = (Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp) if right else (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp)
        button = Q.kCGMouseButtonRight if right else Q.kCGMouseButtonLeft
        Q.CGWarpMouseCursorPosition(pt)
        for kind in (down, up):
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, kind, pt, button))
            time.sleep(0.05)

    def context(label, view, before=None):
        if view is None:
            return dict(view=label, menus='no such view')
        on_main(lambda: win.makeKeyAndOrderFront_(None))
        settle(0.5)
        if before:
            before()
            settle(0.5)
        del seen[:], tracked[:], added[:]
        click(center(view))
        settle(1.5)
        # the same menu objects again, while they're still open (anything added after tracking began shows up here)
        try:
            later = on_main(lambda: [describe_menu(m) for m in tracked], timeout=5)
        except RuntimeError:
            later = 'main thread busy'
        for down in (True, False):
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateKeyboardEvent(None, 53, down)) # escape
        settle(1)
        return dict(view=label, menus=list(seen), while_open=later, added=list(added))

    out = []
    webview = ctrl.editorView.webview if ctrl.editorView else None
    out.append(context('editor', webview))
    out.append(context('editor with a selection', webview, lambda: js(ctrl, "ace.edit('editor').selectAll(); ace.edit('editor').focus()")))
    out.append(context('canvas', ctrl.graphicsView))
    output = ctrl.outputView
    out.append(context('output', output))
    out.append(context('output with a selection', output, lambda: on_main(lambda: (win.makeFirstResponder_(output), output.selectAll_(None)))))

    # a TEXT variable's field in the variables panel, focused first
    dash = ctrl.dashboardController
    from plotdevice.context import TEXT
    rows = on_main(lambda: [r for r in dash.rows.values() if r.type is TEXT] if dash else [])
    field = rows[0].control if rows else None
    if field is not None:
        on_main(lambda: dash.panel.makeKeyAndOrderFront_(None))
        settle(0.5)
        click(center(field), right=False)
        settle(0.5)
        del seen[:]
        click(center(field))
        settle(1.5)
        for down in (True, False):
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateKeyboardEvent(None, 53, down))
        settle(1)
        out.append(dict(view='variables text field', menus=list(seen)))
    on_main(lambda: (nc.removeObserver_(observer), nc.removeObserver_(add_observer)))
    return out

def probe_menu(probe):
    """List a menu bar menu's items before and after it's first opened, then see whether removing one sticks"""
    from AppKit import NSApp
    bar = probe['bar']
    def listing():
        menu = NSApp().mainMenu().itemWithTitle_(bar).submenu()
        items = []
        for it in menu.itemArray():
            action = it.action()
            items.append(dict(title=str(it.title()), action=(action.decode() if isinstance(action, bytes) else str(action)) if action else None,
                              identifier=str(it.identifier()) if it.identifier() else None, hidden=bool(it.isHidden()),
                              separator=bool(it.isSeparatorItem()), submenu=bool(it.hasSubmenu()), cls=str(it.className()),
                              image=it.image() is not None,
                              visibility=int(it.preferredImageVisibility()) if it.respondsToSelector_('preferredImageVisibility') else None))
        return items
    def opening():
        click = open_menu(dict(bar=bar))
        close_menu(click)
    out = dict(launch=on_main(listing))
    opening()
    out['opened'] = on_main(listing)
    def removing():
        menu = NSApp().mainMenu().itemWithTitle_(bar).submenu()
        gone = [str(it.title()) for it in list(menu.itemArray()) if probe['remove'].lower() in str(it.title()).lower()]
        for it in list(menu.itemArray()):
            if probe['remove'].lower() in str(it.title()).lower():
                menu.removeItem_(it)
        return gone
    if not probe.get('remove'):
        return out
    out['removed'] = on_main(removing)
    opening()
    out['reopened'] = on_main(listing)
    opening()
    out['reopened_again'] = on_main(listing)
    return out

def save_pdf(ctrl, name):
    """Save the PDF that Edit > Copy Image As PDF puts on the pasteboard"""
    from AppKit import NSPasteboard, NSPasteboardTypePDF
    def copying():
        ctrl.copyImageAsPDF_(None)
        return NSPasteboard.generalPasteboard().dataForType_(NSPasteboardTypePDF) # provided lazily, by the view
    data = on_main(copying)
    if data is None:
        return 'nothing on the pasteboard'
    os.makedirs(os.path.join(OUT, 'pdf'), exist_ok=True)
    path = os.path.join(OUT, 'pdf', name + '.pdf')
    data.writeToFile_atomically_(path, True)
    return os.path.getsize(path)

def menu_windows():
    """The frames (in screencapture's top-left coordinates) of the app's open menus"""
    import Quartz as Q
    out = []
    for w in Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID) or []:
        if w.get('kCGWindowOwnerPID') == os.getpid() and w.get('kCGWindowLayer', 0) >= 101:
            b = w['kCGWindowBounds']
            out.append([b['X'], b['Y'], b['Width'], b['Height']])
    return out

def capture_menu(shot, name, win, scr, found, info, ctrl):
    """Grab the window and the menu bar (with its open menu) separately, for matte.py to composite.

    A window flush with the screen's left edge loses its shadow there, so instead the window is grabbed
    on its own (with its shadow), then moved aside while the menu bar and menu are grabbed from the
    screen's edge. matte.py lines the menu bar's left end up with the window's left edge.
    """
    from AppKit import NSColor, NSScreen
    plain = {k: v for k, v in shot.items() if k != 'menu'}
    capture(plain, name, win, scr, found, info, ctrl)

    # move the window clear of the menu (it stays key, so the menu's items stay enabled)
    f = on_main(win.frame)
    sx, sy, sw, sh = scr['frame']
    on_main(lambda: win.setFrameOrigin_((sx + sw - f.size.width - 20, sy + 20)))
    on_main(lambda: win.makeKeyAndOrderFront_(None))
    settle(1)

    # measure the open menu, then size the region from the screen's top-left corner to cover it & the window's width
    click = open_menu(shot['menu'])
    menus = menu_windows()
    close_menu(click)
    H = found[0]['frame'][3]
    top = H - (sy + sh)
    right = max([m[0] + m[2] for m in menus] + [sx + f.size.width]) + MARGIN
    bottom = max([m[1] + m[3] for m in menus] + [top + 40]) + MARGIN
    pad = [sx, sy + sh - (bottom - top), right - sx, bottom - top]
    rect = global_rect(found, pad, 0)
    bg = backdrop(pad, 'white')
    ok = True
    for color in ('white', 'black'):
        on_main(lambda: bg.setBackgroundColor_(getattr(NSColor, color + 'Color')()))
        on_main(lambda: win.makeKeyAndOrderFront_(None))
        settle(1)
        click = open_menu(shot['menu'])
        ok = grab(os.path.join(OUT, 'raw', '%s~menu@%s.png' % (name, color)), rect) and ok
        close_menu(click)
    on_main(bg.close)

    # how matte.py should put the two together (in points; it scales by the screen's backing factor)
    def bar():
        screen = next(s for s in NSScreen.screens() if int(s.deviceDescription()['NSScreenNumber']) == scr['id'])
        v = screen.visibleFrame()
        return (sy + sh) - (v.origin.y + v.size.height)
    geometry = dict(scale=scr['scale'], margin=MARGIN, menubar=on_main(bar), window=[f.size.width, f.size.height], layer=name + '~menu')
    with open(os.path.join(OUT, 'raw', name + '.composite.json'), 'w') as fp:
        json.dump(geometry, fp)
    info['menu'] = dict(geometry, menus=menus, ok=ok)
    log('captured menu layer' if ok else 'FAILED menu layer', name)

def capture(shot, name, win, scr, found, info, ctrl=None, extra=(), key=None):
    """Grab the window (with any sheet, menu, or panel) over white & black backdrops, with and without shadows"""
    extra = list(extra)
    if shot.get('sheet'):
        # a sheet can hang below a short window, so measure it before sizing the region
        open_sheet(ctrl, win, shot['sheet'])
        sf = on_main(lambda: win.attachedSheet().frame() if win.attachedSheet() else None)
        if sf is not None:
            extra.append([sf.origin.x, sf.origin.y, sf.size.width, sf.size.height])
        close_sheet(ctrl)
    pad = region_for(win, scr, shot, extra)
    rect = global_rect(found, pad, 0)
    bg = backdrop(pad, 'white')
    from AppKit import NSColor
    canvas = getattr(ctrl, 'graphicsView', None) if ctrl is not None else None
    ok = True
    # an outline (the window's edge without its shadow) is made from a shadowed & a flat grab
    variants = VARIANTS + (['raw-flat'] if shot.get('outline') and 'raw-flat' not in VARIANTS else [])
    for raw in variants:
        for color in ('white', 'black'):
            on_main(lambda: bg.setBackgroundColor_(getattr(NSColor, color + 'Color')()))
            on_main(lambda: win.makeKeyAndOrderFront_(None))
            if canvas is not None and not shot.get('editor'):
                on_main(lambda: win.makeFirstResponder_(canvas))
            if key is not None:
                on_main(lambda: key.makeKeyAndOrderFront_(None))
            settle(1)
            place_pointer(win, scr, found, shot)
            if shot.get('sheet'):
                open_sheet(ctrl, win, shot['sheet'])
            click = open_menu(shot['menu']) if shot.get('menu') else None
            info[raw + '.shadows'] = set_shadows(win, raw == 'raw')
            ok = grab(os.path.join(OUT, raw, '%s@%s.png' % (name, color)), rect) and ok
            if click is not None:
                close_menu(click)
            if shot.get('sheet'):
                close_sheet(ctrl)
    set_shadows(win, True)
    on_main(bg.close)
    log('captured' if ok else 'FAILED', name, json.dumps(info, default=str))
    results.append(dict(name=name, ok=ok, **info))

def shoot_prefs(shot, appearance, scr, found):
    """The preferences window"""
    from AppKit import NSApp, NSMakeRect
    name = shot['name'] + ('-dark' if appearance == 'dark' else '')
    delegate = on_main(NSApp().delegate)
    on_main(delegate.showPreferencesPanel_, None)
    settle(2)
    win = on_main(lambda: delegate._prefsController.window())
    def placing():
        f = win.frame()
        sx, sy, sw, sh = scr['frame']
        win.setFrame_display_(NSMakeRect(sx + 140, sy + sh - 120 - f.size.height, f.size.width, f.size.height), True)
        win.makeKeyAndOrderFront_(None)
        return [f.size.width, f.size.height]
    info = dict(frame=on_main(placing))
    settle(1)
    capture(shot, name, win, scr, found, info)
    on_main(win.close)
    settle(1)

def viewer_main():
    """Run inside the command line tool's process: shoot its viewer window in light & dark"""
    from AppKit import NSApp
    name = os.environ.get('PD_SHOTS_VIEWER')
    try:
        with open(os.path.join(MANUAL, 'shots.json')) as f:
            shot = next(s for s in json.load(f) if s['name'] == name)
        for _ in range(60):
            time.sleep(1)
            delegate = NSApp().delegate() if NSApp() is not None else None
            if delegate is not None and getattr(delegate, 'window', None) is not None:
                break
        settle(shot.get('settle', 3)) # let the script run
        log('viewer is up', name)

        # the virtual display lives as long as the process that made it, so this one needs its own
        virtual()
        found = screens()
        retina = [s for s in found if s['scale'] >= 2]
        if not retina:
            log('no 2x screen, bailing', found)
            return
        scr = retina[0]
        win, ctrl = on_main(lambda: (NSApp().delegate().window, NSApp().delegate().script))
        for appearance in shot.get('appearances', APPEARANCES):
            set_dark(appearance == 'dark')
            on_main(lambda: NSApp().activateIgnoringOtherApps_(True))
            info = layout(ctrl, win, scr, shot) if shot.get('content') else {}
            settle(1)
            capture(shot, name + ('-dark' if appearance == 'dark' else ''), win, scr, found, info, ctrl)
        log('done')
    except Exception:
        traceback.print_exc()
    finally:
        with open(os.path.join(OUT, 'viewer-%s.json' % name), 'w') as f:
            json.dump(dict(report, results=results), f, indent=2, default=str)
        sys.stdout.flush()
        os._exit(0)

def start_viewer():
    os.makedirs(os.path.join(OUT, 'raw'), exist_ok=True)
    os.makedirs(os.path.join(OUT, 'raw-flat'), exist_ok=True)
    threading.Thread(target=viewer_main, daemon=True).start()
    timer = threading.Timer(float(os.environ.get('PD_SHOTS_TIMEOUT', 600)), lambda: os._exit(2))
    timer.daemon = True
    timer.start()

def main():
    from AppKit import NSApp
    try:
        for _ in range(60):
            time.sleep(1)
            app = NSApp()
            if app is not None and app.isRunning() and app.delegate() is not None:
                break
        settle(3)
        log('app is up')
        on_main(lambda: NSApp().activateIgnoringOtherApps_(True))
        close_all()
        virtual()
        found = screens()
        retina = [s for s in found if s['scale'] >= 2]
        if not retina:
            log('no 2x screen, bailing', found)
            return
        scr = retina[0]

        with open(os.path.join(MANUAL, 'shots.json')) as f:
            shots = json.load(f)
        only = set(filter(None, os.environ.get('PD_SHOTS_ONLY', '').split(',')))
        # a shot's own appearances override the default, so cover every one asked for
        wanted = set(APPEARANCES).union(*(s.get('appearances', []) for s in shots))
        for appearance in [a for a in ('light', 'dark') if a in wanted]:
            set_dark(appearance == 'dark')
            on_main(lambda: NSApp().activateIgnoringOtherApps_(True))
            for shot in shots:
                if only and shot['name'] not in only:
                    continue
                if appearance not in shot.get('appearances', APPEARANCES):
                    continue
                if shot.get('viewer'):
                    continue # shot by the command line tool (see start_viewer)
                try:
                    (shoot_prefs if shot.get('prefs') else shoot)(shot, appearance, scr, found)
                except Exception as e:
                    traceback.print_exc()
                    results.append(dict(name=shot['name'], appearance=appearance, ok=False, error=repr(e)))
                    close_all()
        log('done')
    except Exception:
        traceback.print_exc()
    finally:
        with open(os.path.join(OUT, 'manual.json'), 'w') as f:
            json.dump(dict(report, results=results), f, indent=2, default=str)
        sys.stdout.flush()
        os._exit(0)

def start():
    os.makedirs(os.path.join(OUT, 'raw'), exist_ok=True)
    os.makedirs(os.path.join(OUT, 'raw-flat'), exist_ok=True)
    threading.Thread(target=main, daemon=True).start()

    def watchdog():
        log('timed out, bailing')
        with open(os.path.join(OUT, 'manual.json'), 'w') as f:
            json.dump(dict(report, results=results, timeout=True), f, indent=2, default=str)
        os._exit(2)
    timer = threading.Timer(float(os.environ.get('PD_SHOTS_TIMEOUT', 2400)), watchdog)
    timer.daemon = True
    timer.start()
