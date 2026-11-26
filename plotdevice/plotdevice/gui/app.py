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

def tiling_menu(menu):
    # the Window menu's Move & Resize and Full Screen Tile submenus, whose icons diagram each layout (every
    # command in them is a _zoom… or _tile… action, unlike the Window menu itself, which only has a few)
    actions = [a.decode() if isinstance(a, bytes) else str(a) for a in (it.action() for it in menu.itemArray()) if a]
    return bool(actions) and all(a.startswith(('_zoom', '_tile')) for a in actions)

def hide_icons(item):
    # macOS 27 draws icons beside some standard menu items (Settings, Share, the help book, the Window
    # menu's window list); 2 is NSMenuItemImageVisibilityHidden, which our SDK predates
    if item.respondsToSelector_("setPreferredImageVisibility:"):
        item.setPreferredImageVisibility_(2)
    if item.hasSubmenu() and not tiling_menu(item.submenu()):
        for sub in item.submenu().itemArray():
            hide_icons(sub)

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

    def menuDidAddItem_(self, note):
        item = note.object().itemAtIndex_(note.userInfo()["NSMenuItemIndex"])

        # hide the icons of items appkit adds to the menu bar (e.g., the window list)
        root = note.object()
        while root.supermenu():
            root = root.supermenu()
        if root == NSApp().mainMenu() and not tiling_menu(note.object()):
            hide_icons(item)

        # context menus get an autofill item (with no identifier) merged in as they open; hide it on the way in
        inserts = [NSSelectorFromString(s) for s in ("_handleInsertFromContactsCommand:",
                   "_handleInsertFromPasswordsCommand:", "_handleInsertFromCreditCardsCommand:")]
        if item.hasSubmenu() and any(sub.action() in inserts for sub in item.submenu().itemArray()):
            item.setHidden_(True)

    def menuDidBeginTracking_(self, note):
        # appkit restores some of the window menu's icons after the fact, so hide them again as the menu bar opens
        if note.object() == NSApp().mainMenu():
            for item in note.object().itemArray():
                hide_icons(item)

    def applicationSupportsSecureRestorableState_(self, app):
        return True

    def applicationDidFinishLaunching_(self, note):
        mm=NSApp().mainMenu()

        # disable the autofill item in the edit menu
        edmenu = mm.itemAtIndex_(2).submenu()
        for it in edmenu.itemArray():
            if it.identifier() == "_NSMenuItemAutoFillIdentifier":
                edmenu.removeItem_(it)

        # drop the autofill item that appkit adds to context menus as they open, and hide menu bar icons
        nc = NSNotificationCenter.defaultCenter()
        nc.addObserver_selector_name_object_(self, "menuDidAddItem:", NSMenuDidAddItemNotification, None)
        nc.addObserver_selector_name_object_(self, "menuDidBeginTracking:", NSMenuDidBeginTrackingNotification, None)

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
