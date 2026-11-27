# encoding: utf-8
import sys
import os
import time
import objc
from glob import glob
from ..lib.cocoa import *
from PyObjCTools import AppHelper
from .preferences import PlotDevicePreferencesController, get_default
from .document import ScriptController
from ..lib.io import ExportSession
from . import bundle_path, set_timeout

LIB_DIR_README = """"You can put PlotDevice libraries In this directory to make them available to your scripts.

You can also install anything you find on https://pypi.org here using the command line tool:
  plotdevice --install <package-name>
"""

class PlotDeviceAppDelegate(NSObject):
    examplesMenu = IBOutlet()
    updatesMenu = IBOutlet()

    def awakeFromNib(self):
        self._prefsController = None
        self._docsController = NSDocumentController.sharedDocumentController()
        libDir = os.path.join(os.getenv("HOME"), "Library", "Application Support", "PlotDevice")
        try:
            if not os.path.exists(libDir):
                os.mkdir(libDir)
                f = open(os.path.join(libDir, "README.txt"), "w")
                f.write(LIB_DIR_README)
                f.close()
        except OSError: pass
        except IOError: pass

    @objc.python_method
    def hideIcons(self, menu):
        # opt out of Tahoe's menu icon travesty except for the window-tiling submenus where they're actually useful
        actions = [item.action() for item in menu.itemArray() if item.action()]
        if actions and all(a.startswith(('_zoom', '_tile')) for a in actions):
            return
        for item in menu.itemArray():
            if item.respondsToSelector_("setPreferredImageVisibility:"):
                item.setPreferredImageVisibility_(2) # NSMenuItemImageVisibilityHidden, which our SDK predates
            if item.hasSubmenu():
                self.hideIcons(item.submenu())

    def menuDidAddItem_(self, note):
        item = note.object().itemAtIndex_(note.userInfo()["NSMenuItemIndex"])

        # catch AppKit's autmatically populated menus (e.g., Window) and remove their icons
        root = note.object()
        while root.supermenu():
            root = root.supermenu()
        if root == NSApp().mainMenu():
            self.hideIcons(note.object())

        # hide Autofill in context menus
        inserts = [NSSelectorFromString(s) for s in ("_handleInsertFromContactsCommand:",
                   "_handleInsertFromPasswordsCommand:", "_handleInsertFromCreditCardsCommand:")]
        if item.hasSubmenu() and any(sub.action() in inserts for sub in item.submenu().itemArray()):
            item.setHidden_(True)
        if item.identifier() == "_NSMenuItemAutoFillIdentifier" or 'AutoFill' in str(item.title()):
            print('[autofill] added to %r: %r id=%s submenu=%s hidden=%s' % (str(note.object().title()), str(item.title()), item.identifier(),
                  [sub.action() for sub in item.submenu().itemArray()] if item.hasSubmenu() else None, item.isHidden()), flush=True)

    def menuDidBeginTracking_(self, note):
        # re-hide icons each time the menu opens (since AppKit will auto-restore some of them)
        if note.object() == NSApp().mainMenu():
            self.hideIcons(note.object())

    def applicationSupportsSecureRestorableState_(self, app):
        return True

    def applicationWillFinishLaunching_(self, note):
        # drop the autofill items appkit adds to the edit menu and context menus, and hide menu bar icons
        print('[autofill] observers registered', flush=True)
        nc = NSNotificationCenter.defaultCenter()
        nc.addObserver_selector_name_object_(self, "menuDidAddItem:", NSMenuDidAddItemNotification, None)
        nc.addObserver_selector_name_object_(self, "menuDidBeginTracking:", NSMenuDidBeginTrackingNotification, None)

    def applicationDidFinishLaunching_(self, note):
        mm=NSApp().mainMenu()

        # probe: is the edit menu's autofill item gone by now?
        for it in mm.itemAtIndex_(2).submenu().itemArray():
            if it.identifier() == "_NSMenuItemAutoFillIdentifier" or 'AutoFill' in str(it.title()):
                print('[autofill] at didFinishLaunching: %r id=%s hidden=%s' % (str(it.title()), it.identifier(), it.isHidden()), flush=True)

        # add a hidden item to the menus that can be triggered internally by the editor
        for menu in mm.itemArray()[2:5]:
            flicker = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('Flash This Menu', None, '')
            flicker.setEnabled_(True)
            flicker.setHidden_(True)
            menu.submenu().insertItem_atIndex_(flicker,0)

        # If the sparkle framework was installed in our bundle, init an updater
        self.sparkle = None
        sparkle_path = bundle_path(fmwk='Sparkle')
        if os.path.exists(sparkle_path):
            objc.loadBundle('Sparkle', globals(), bundle_path=sparkle_path)
            self.sparkle = objc.lookUpClass('SUUpdater').sharedUpdater()
            self.updatesMenu.setTarget_(self.sparkle)
            self.updatesMenu.setAction_("checkForUpdates:")
            self.updatesMenu.setHidden_(False)


    def applicationWillBecomeActive_(self, note):
        # rescan the examples dir every time?
        self.updateExamples()

    def updateExamples(self):
        examples_folder = bundle_path(rsrc="examples")
        pyfiles = glob('%s/*/*.pv'%examples_folder)
        categories = self.examplesMenu.submenu()
        folders = {}
        for item in categories.itemArray():
            item.submenu().removeAllItems()
            folders[item.title()] = item.submenu()
        for fn in sorted(pyfiles):
            cat = os.path.basename(os.path.dirname(fn))
            example = os.path.basename(fn)
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(example[:-3], "openExample:", "")
            item.setRepresentedObject_(fn)
            folders[cat].addItem_(item)
        self.examplesMenu.setHidden_(not pyfiles)

    @IBAction
    def newSketch_(self, sender):
        kind = ['sketch','anim','tab','ottobot'][sender.tag()]
        doc = self.docFromTemplate_('TMPL:'+kind)
        if kind=='ottobot':
            AppHelper.callLater(0.1, doc.script.runScript)

    @IBAction
    def openExample_(self, sender):
        tmpl = sender.representedObject()
        self.docFromTemplate_(tmpl)

    def docFromTemplate_(self, tmpl):
        """Open a doc with no undo state which contains either an example, or a new-sketch template"""
        doc, err = self._docsController.makeUntitledDocumentOfType_error_("io.plotdevice.document", None)
        doc.stationery = tmpl
        self._docsController.addDocument_(doc)
        doc.makeWindowControllers()
        doc.showWindows()
        return doc

    @IBAction
    def showPreferencesPanel_(self, sender):
        if self._prefsController is None:
            self._prefsController = PlotDevicePreferencesController.alloc().init()
        self._prefsController.showWindow_(sender)

    @IBAction
    def showHelp_(self, sender):
        url = NSURL.URLWithString_("http://plotdevice.io/manual")
        NSWorkspace.sharedWorkspace().openURL_(url)

    @IBAction
    def showSite_(self, sender):
        url = NSURL.URLWithString_("http://plotdevice.io/")
        NSWorkspace.sharedWorkspace().openURL_(url)

    def applicationShouldTerminate_(self, app):
        # try to end exports cleanly so they can write complete files
        for win in app.windows():
            controller = win.windowController()
            open_script = isinstance(controller, ScriptController) and controller.vm is not None
            if open_script and controller.vm.session:
                controller.finishExport()
        if not ExportSession.unfinished:
            return NSTerminateNow
        self._quitBy = time.time() + 10
        set_timeout(self, 'quitOnceWritten:', 0.1, repeat=True)
        return NSTerminateLater

    def quitOnceWritten_(self, timer):
        writing = ExportSession.writing()
        if not writing or time.time() > self._quitBy:
            timer.invalidate()
            NSApplication.sharedApplication().replyToApplicationShouldTerminate_(True)

    def applicationWillTerminate_(self, note):
        import atexit
        atexit._run_exitfuncs()
