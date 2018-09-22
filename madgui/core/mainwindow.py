"""
Main window component for madgui.
"""

__all__ = [
    'MainWindow',
]

import glob
import os
import logging
import time
from functools import partial

import numpy as np

from madgui.qt import Qt, QtCore, QtGui, load_ui
from madgui.core.base import Signal
from madgui.util.collections import Selection, Boxed
from madgui.util.misc import SingleWindow, logfile_name, try_import, relpath
from madgui.util.qt import notifyCloseEvent
from madgui.util.undo import UndoStack
from madgui.widget.dialog import Dialog
from madgui.widget.log import LogRecord

import madgui.online.control as control
import madgui.core.config as config
import madgui.core.menu as menu
import madgui.util.yaml as yaml


def expand_ext(path, *exts):
    for ext in exts:
        if os.path.isfile(path+ext):
            return path+ext
    return path


class MainWindow(QtGui.QMainWindow):

    ui_file = 'mainwindow.ui'

    # Basic setup

    def __init__(self, options, *args, **kwargs):
        super().__init__(*args, **kwargs)
        load_ui(self, __package__, self.ui_file)
        self.context = {
            'frame': self,
        }
        self.options = options
        self.config = config.load(options['--config'])
        self.session_file = self.config.session_file
        self.matcher = None
        self.model = Boxed(None)
        self.model.changed.connect(self._on_model_changed)
        self.control = control.Control(self)
        self.initUI()
        # Defer `loadDefault` to avoid creation of a AsyncRead thread before
        # the main loop is entered: (Being in the mainloop simplifies
        # terminating the AsyncRead thread via the QApplication.aboutToQuit
        # signal. Without this, if the setup code excepts after creating the
        # thread the main loop will never be entered and thus aboutToQuit
        # never be emitted, even when pressing Ctrl+C.)
        QtCore.QTimer.singleShot(0, self.loadDefault)
        # This is required to make the thread exit (and hence allow the
        # application to close) by calling app.quit() on Ctrl-C:
        QtGui.qApp.aboutToQuit.connect(self.destroyModel)

    def configure(self):
        runtime = self.config.get('runtime_path', [])
        runtime = [runtime] if isinstance(runtime, str) else runtime
        for path in runtime:
            os.environ['PATH'] += os.pathsep + os.path.abspath(path)
        self.folder = self.config.model_path
        self.exec_folder = self.config.exec_folder
        self.str_folder = self.config.str_folder
        config.number = self.config.number
        np.set_printoptions(**self.config['printoptions'])
        exec(self.config.onload, self.context)

    def session_data(self):
        open_plot_windows = list(map(self._save_plot_window, self.views))
        folder = self.config.model_path or self.folder
        default = self.model() and relpath(self.model().filename, folder)
        return {
            'mainwindow': {
                'init_size': [self.size().width(), self.size().height()],
                'init_pos': [self.pos().x(), self.pos().y()],
            },
            'online_control': {
                'connect': self.control.loader_name,
                'monitors': self.config.online_control['monitors'],
                'offsets': self.config.online_control['offsets'],
                'settings': self.control.export_settings() or {},
            },
            'logging': {
                'enable': self.log_window.logging_enabled,
                'level': self.log_window.loglevel,
                'times': {
                    'enable': self.log_window.infobar.show_time,
                    'format': self.log_window.infobar.time_format,
                },
                'madx': {
                    'in': self.log_window.enabled('SEND'),
                    'out': self.log_window.enabled('MADX'),
                }
            },
            'model_path': folder,
            'load_default': default,
            'exec_folder': self.exec_folder,
            'str_folder': self.str_folder,
            'number': self.config['number'],
            'plot_windows': open_plot_windows + self.config.plot_windows,
        }

    def initUI(self):
        self.views = []
        self.createMenu()
        self.createControls()
        self.configure()
        self.initPos()

    def initPos(self):
        self.resize(*self.config.mainwindow.init_size)
        self.move(*self.config.mainwindow.init_pos)

    def loadDefault(self):
        filename = self.options['FILE'] or self.config.load_default
        if filename:
            self.loadFile(self.searchFile(filename))
        else:
            logging.info('Welcome to madgui. Type <Ctrl>+O to open a file.')

    def createMenu(self):
        control = self.control
        Menu, Item, Separator = menu.Menu, menu.Item, menu.Separator
        menubar = self.menuBar()
        items = menu.extend(self, menubar, [
            Menu('&File', [
                Item('&Open', 'Ctrl+O',
                     'Load model or open new model from a MAD-X file.',
                     self.fileOpen,
                     QtGui.QStyle.SP_DialogOpenButton),
                Separator,
                Item('&Initial conditions', 'Ctrl+I',
                     'Modify the initial conditions, beam, and parameters.',
                     self.editInitialConditions.create),
                Separator,
                Item('&Execute MAD-X file', 'Ctrl+E',
                     'Execute MAD-X file in current context.',
                     self.execFile),
                Separator,
                Item('&Quit', 'Ctrl+Q',
                     'Close window.',
                     self.close,
                     QtGui.QStyle.SP_DialogCloseButton),
            ]),
            Menu('&View', [
                Item('Plo&t window', 'Ctrl+T',
                     'Open a new plot window.',
                     self.showTwiss),
                Item('&Python shell', 'Ctrl+P',
                     'Show a python shell.',
                     self.viewShell.toggle,
                     checked=self.viewShell.holds_value),
                Item('&Floor plan', 'Ctrl+F',
                     'Show a 2D floor plan of the lattice.',
                     self.viewFloorPlan),
            ]),
            Menu('&Export', [
                Item('&Strengths', None,
                     'Export magnet strengths.',
                     self.saveStrengths),
                Item('&Beam', None,
                     'Export beam settings.',
                     self.saveBeam),
                Item('&Twiss && orbit', None,
                     'Export initial twiss parameters.',
                     self.saveTwiss),
            ]),
            Menu('&Import', [
                Item('&Strengths', None,
                     'Load .str file (simplified syntax).',
                     self.loadStrengths),
                Item('&Beam', None,
                     'Import beam settings.',
                     self.loadBeam),
                Item('&Twiss && orbit', None,
                     'Import initial twiss parameters.',
                     self.loadTwiss),
            ]),
            Menu('&Settings', [
                Item('&Number format', None,
                     'Set the number format/precision used in dialogs',
                     self.setNumberFormat),
                Item('&Spin box', None,
                     'Display spinboxes for number input controls',
                     self.setSpinBox, checked=self.config.number.spinbox),
            ]),
            Menu('&Online control', [
                Item('Disconnect', None,
                     'Disconnect online control interface',
                     control.disconnect,
                     enabled=control.is_connected),
                Separator,
                Item('&Read strengths', None,
                     'Read magnet strengths from the online database',
                     control.on_read_all,
                     enabled=control.has_sequence),
                Item('&Write strengths', None,
                     'Write magnet strengths to the online database',
                     control.on_write_all,
                     enabled=control.has_sequence),
                Item('Read &beam', None,
                     'Read beam settings from the online database',
                     control.on_read_beam,
                     enabled=control.has_sequence),
                Separator,
                Item('Beam &diagnostic', None,
                     'Beam position and emittance diagnostics',
                     control.monitor_widget.create,
                     enabled=control.has_sequence),
                Separator,
                Item('ORM measurement', None,
                     'Measure ORM for later analysis',
                     control.orm_measure_widget.create,
                     enabled=control.has_sequence),
                Separator,
                menu.Menu('&Orbit correction', [
                    Item('Optic &variation', 'Ctrl+V',
                         'Perform orbit correction via 2-optics method',
                         control.on_correct_optic_variation_method,
                         enabled=control.has_sequence),
                    Item('Multi &grid', 'Ctrl+G',
                         'Perform orbit correction via 2-grids method',
                         control.on_correct_multi_grid_method,
                         enabled=control.has_sequence),
                ]),
                Separator,
                menu.Menu('&Settings', []),
            ]),
            Menu('&Help', [
                Item('About &madgui', None,
                     'About the madgui GUI application.',
                     self.helpAboutMadGUI.create),
                try_import('cpymad') and
                Item('About &cpymad', None,
                     'About the cpymad python binding to MAD-X.',
                     self.helpAboutCPyMAD.create),
                try_import('cpymad') and
                Item('About MAD-&X', None,
                     'About the included MAD-X backend.',
                     self.helpAboutMadX.create),
                Separator,
                Item('About &Python', None,
                     'About the currently running python interpreter.',
                     self.helpAboutPython.create),
                Item('About Q&t', None,
                     'About Qt.',
                     self.helpAboutQt),
            ]),
        ])
        self.csys_menu = items[-2]
        self.dc_action = self.csys_menu.actions()[0]
        self.csys_settings_menu = self.csys_menu.children()[-1]
        self.csys_settings_menu.setEnabled(False)

    def add_online_plugin(self, loader, name=None):
        name = name or getattr(loader, 'name', loader.__name__)
        if loader.check_avail():
            self.csys_menu.insertAction(self.dc_action, menu.Item(
                'Connect ' + loader.title, loader.hotkey,
                'Connect ' + loader.descr,
                partial(self.control.connect, name, loader),
                enabled=self.control.can_connect).action(self.menuBar()))
            if self.config.online_control.connect == name and \
                    not self.control.is_connected():
                self.control.connect(name, loader)

    dataReceived = Signal(object)

    def createControls(self):
        QColor = QtGui.QColor
        self.log_window.highlight('SEND',     QColor(Qt.yellow).lighter(160))
        self.log_window.highlight('MADX',     QColor(Qt.lightGray))

        self.log_window.highlight('DEBUG',    QColor(Qt.blue).lighter(180))
        self.log_window.highlight('INFO',     QColor(Qt.green).lighter(150))
        self.log_window.highlight('WARNING',  QColor(Qt.yellow))
        self.log_window.highlight('ERROR',    QColor(Qt.red))
        self.log_window.highlight('CRITICAL', QColor(Qt.red))

        log_conf = self.config.logging
        self.log_window.setup_logging(logging.DEBUG)
        self.log_window.infobar.enable_timestamps(log_conf.times.enable)
        self.log_window.infobar.set_timeformat(log_conf.times.format)
        self.log_window.enable_logging(log_conf.enable)
        self.log_window.set_loglevel(log_conf.level)
        self.log_window.enable('SEND', log_conf.madx['in'])
        self.log_window.enable('MADX', log_conf.madx['out'])

        self.dataReceived.connect(partial(self.log_window.recv_log, 'MADX'))

        self.checkbox_time.setChecked(self.log_window.infobar.show_time)
        self.checkbox_logging.setChecked(self.log_window.logging_enabled)
        self.combobox_loglevel.setEnabled(self.log_window.logging_enabled)
        self.combobox_loglevel.setCurrentText(self.log_window.loglevel)
        self.checkbox_madx_input.setChecked(self.log_window.enabled('SEND'))
        self.checkbox_madx_output.setChecked(self.log_window.enabled('MADX'))

        self.checkbox_time.clicked.connect(
            self.log_window.infobar.enable_timestamps)
        self.checkbox_logging.clicked.connect(
            self.log_window.enable_logging)
        self.combobox_loglevel.currentTextChanged.connect(
            self.log_window.set_loglevel)
        self.checkbox_madx_input.clicked.connect(
            partial(self.log_window.enable, 'SEND'))
        self.checkbox_madx_output.clicked.connect(
            partial(self.log_window.enable, 'MADX'))

        style = self.style()
        self.undo_stack = undo_stack = UndoStack()
        self.undo_action = undo_stack.createUndoAction(self)
        self.redo_action = undo_stack.createRedoAction(self)
        self.undo_action.setShortcut(QtGui.QKeySequence.Undo)
        self.redo_action.setShortcut(QtGui.QKeySequence.Redo)
        self.undo_action.setIcon(style.standardIcon(QtGui.QStyle.SP_ArrowBack))
        self.redo_action.setIcon(style.standardIcon(QtGui.QStyle.SP_ArrowForward))
        undo_history_action = QtGui.QAction(
            style.standardIcon(QtGui.QStyle.SP_ToolBarVerticalExtensionButton),
            "List", self)
        undo_history_action.triggered.connect(self.createUndoView.create)
        self.toolbar.addAction(self.undo_action)
        self.toolbar.addAction(self.redo_action)
        self.toolbar.addAction(undo_history_action)

    def log_command(self, text):
        text = text.rstrip()
        self.logfile.write(text + '\n')
        self.logfile.flush()
        self.log_window.records.append(LogRecord(
            time.time(), 'SEND', text))

    @SingleWindow.factory
    def createUndoView(self):
        widget = QtGui.QUndoView(self.undo_stack, self)
        widget.setEmptyLabel("<Unmodified>")
        dialog = Dialog(self)
        dialog.setWidget(widget)
        dialog.setWindowTitle("Change history")
        return widget

    # Menu actions

    def fileOpen(self):
        from madgui.widget.filedialog import getOpenFileName
        filters = [
            ("Model files", "*.cpymad.yml"),
            ("MAD-X files", "*.madx", "*.str", "*.seq"),
            ("All files", "*"),
        ]
        filename = getOpenFileName(
            self, 'Open file', self.folder, filters)
        if filename:
            self.loadFile(filename)

    def execFile(self):
        from madgui.widget.filedialog import getOpenFileName
        filters = [
            ("All MAD-X files", "*.madx", "*.str", "*.seq"),
            ("Strength files", "*.str"),
            ("All files", "*"),
        ]
        folder = self.exec_folder or self.folder
        filename = getOpenFileName(
            self, 'Open MAD-X file', folder, filters)
        if filename:
            self.model().call(filename)
            self.exec_folder = os.path.dirname(filename)

    def loadStrengths(self):
        self._import("Import magnet strengths", [
            ("YAML files", "*.yml", "*.yaml"),
            ("Strength files", "*.str"),
            ("All MAD-X files", "*.madx", "*.str", "*.seq"),
            ("All files", "*"),
        ], self.model().update_globals, data_key='globals')

    def loadBeam(self):
        self._import("Import beam parameters", [
            ("YAML files", "*.yml", "*.yaml"),
            ("All files", "*"),
        ], self.model().update_beam, data_key='beam')

    def loadTwiss(self):
        self._import("Import initial twiss parameters", [
            ("YAML files", "*.yml", "*.yaml"),
            ("All files", "*"),
        ], self.model().update_twiss, data_key='twiss')

    def _import(self, title, filters, callback, data_key):
        from madgui.widget.filedialog import getOpenFileName
        folder = self.str_folder or self.folder
        filename = getOpenFileName(self, title, folder, filters)
        if filename:
            from madgui.widget.params import import_params
            callback(import_params)
            self.str_folder = os.path.dirname(filename)

    def saveStrengths(self):
        self._export("Save MAD-X strengths file", [
            ("YAML files", "*.yml", "*.yaml"),
            ("Strength files", "*.str"),
            ("All files", "*"),
        ], self.model().export_globals, data_key='globals')

    def saveBeam(self):
        # TODO: import/export MAD-X file (with only BEAM command)
        self._export("Export initial BEAM settings", [
            ("YAML files", "*.yml", "*.yaml"),
            ("All files", "*"),
        ], self.model().export_beam, data_key='beam')

    def saveTwiss(self):
        # TODO: import/export MAD-X file (with only TWISS command)
        self._export("Export initial TWISS settings", [
            ("YAML files", "*.yml", "*.yaml"),
            ("All files", "*"),
        ], self.model().export_twiss, data_key='twiss')

    def _export(self, title, filters, fetch_data, data_key):
        from madgui.widget.filedialog import getSaveFileName
        folder = self.str_folder or self.folder
        filename = getSaveFileName(self, title, folder, filters)
        if filename:
            from madgui.widget.params import export_params
            data = fetch_data()
            export_params(filename, data, data_key=data_key)
            self.str_folder = os.path.dirname(filename)

    @SingleWindow.factory
    def editInitialConditions(self):
        from madgui.widget.params import (
            TabParamTables, ParamTable, GlobalsEdit)
        from madgui.widget.elementinfo import EllipseWidget

        class InitEllipseWidget(EllipseWidget):
            def update(self):
                super().update(0)

        model = self.model()
        widget = TabParamTables([
            ('Twiss', ParamTable(model.fetch_twiss, model.update_twiss_args,
                                 data_key='twiss')),
            ('Beam', ParamTable(model.fetch_beam, model.update_beam,
                                data_key='beam')),
            ('Globals', GlobalsEdit(model, data_key='globals')),
            ('Ellipse', InitEllipseWidget(model)),
        ])
        widget.update()
        # NOTE: Ideally, we'd like to update after changing initial conditions
        # (rather than after twiss), but changing initial conditions usually
        # implies also updating twiss, so this is a good enough approximation
        # for now:
        model.twiss.updated.connect(widget.update)

        dialog = Dialog(self)
        dialog.setSimpleExportWidget(widget, self.folder)
        dialog.setWindowTitle("Initial conditions")
        return widget

    @SingleWindow.factory
    def viewShell(self):
        return self._createShell()

    def viewFloorPlan(self):
        from madgui.widget.floor_plan import LatticeFloorPlan, Selector
        latview = LatticeFloorPlan()
        latview.setModel(self.model())
        selector = Selector(latview)
        dock = Dialog(self)
        dock.setWidget([latview, selector], tight=True)
        dock.setWindowTitle("2D floor plan")
        dock.show()
        return dock

    @SingleWindow.factory
    def viewMatchDialog(self):
        from madgui.widget.match import MatchWidget
        widget = MatchWidget(self.matcher)
        dialog = Dialog(self)
        dialog.setWidget(widget, tight=True)
        dialog.setWindowTitle("Matching constraints.")
        return dialog

    def setNumberFormat(self):
        fmtspec, ok = QtGui.QInputDialog.getText(
            self, "Set number format", "Number format:",
            text=self.config.number.fmtspec)
        if not ok:
            return
        try:
            format(1.1, fmtspec)
        except ValueError:
            # TODO: show warning
            return
        self.config.number.fmtspec = fmtspec

    def setSpinBox(self):
        # TODO: sync with menu state
        self.config.number.spinbox = not self.config.number.spinbox

    @SingleWindow.factory
    def helpAboutMadGUI(self):
        """Show about dialog."""
        import madgui
        return self._showAboutDialog(madgui)

    @SingleWindow.factory
    def helpAboutCPyMAD(self):
        """Show about dialog."""
        import cpymad
        return self._showAboutDialog(cpymad)

    @SingleWindow.factory
    def helpAboutMadX(self):
        """Show about dialog."""
        import cpymad.madx
        return self._showAboutDialog(cpymad.madx.metadata)

    @SingleWindow.factory
    def helpAboutPython(self):
        """Show about dialog."""
        import sys
        import site     # adds builtins.license/copyright/credits
        site            # silence pyflakes (suppress unused import warning)
        import builtins

        class About:
            __uri__ = "https::/www.python.org"
            __title__ = 'python'
            __version__ = ".".join(map(str, sys.version_info))
            __summary__ = sys.version + "\n\nPath: " + sys.executable
            __credits__ = str(builtins.credits)
            get_copyright_notice = lambda: sys.copyright
        return self._showAboutDialog(About)

    def helpAboutQt(self):
        QtGui.QMessageBox.aboutQt(self)

    def _showAboutDialog(self, module):
        import madgui.core.about as about
        info = about.VersionInfo(module)
        return about.AboutDialog(info, self)

    # Update state

    known_extensions = ['.cpymad.yml', '.init', '.lat', '.madx']

    def searchFile(self, path):
        for path in [path, os.path.join(self.folder or '.', path)]:
            if os.path.isdir(path):
                models = (glob.glob(os.path.join(path, '*.cpymad.yml')) +
                          glob.glob(os.path.join(path, '*.init')))
                if models:
                    path = models[0]
            path = expand_ext(path, '', *self.known_extensions)
            if os.path.isfile(path):
                return path
        raise OSError("File not found: {!r}".format(path))

    def loadFile(self, filename):
        """Load the specified model and show plot inside the main window."""
        exts = ('.cpymad.yml', '.madx', '.str', '.seq')
        if not any(map(filename.endswith, exts)):
            raise NotImplementedError("Unsupported file format: {}"
                                      .format(filename))
        from madgui.model.madx import Model
        self.destroyModel()
        filename = os.path.abspath(filename)
        self.folder, name = os.path.split(filename)
        base, ext = os.path.splitext(name)
        logfile = logfile_name(self.folder, base, '.commands.madx')
        self.logfile = open(logfile, 'wt')
        logging.info('Loading {}'.format(filename))
        logging.info('Logging commands to: {}'.format(logfile))
        self.model.set(Model(filename,
                             command_log=self.log_command,
                             stdout=self.dataReceived.emit,
                             undo_stack=self.undo_stack))
        self.showTwiss()

    def _on_model_changed(self, model):
        self.destroyModel()
        self.context['model'] = model

        if model is None:
            self.matcher = None
            self.setWindowTitle("madgui")
            return

        from madgui.model.match import Matcher
        self.matcher = Matcher(model, self.config['matching'])

        self.context['madx'] = model.madx
        self.context['twiss'] = model.twiss.data
        exec(model.data.get('onload', ''), self.context)

        model.twiss.updated.connect(self.update_twiss)

        from madgui.widget.elementinfo import InfoBoxGroup
        model.selection = Selection()
        model.box_group = InfoBoxGroup(self, model.selection)

        self.setWindowTitle(model.name)

    def destroyModel(self):
        model = self.context.get('model')
        if model is None:
            return
        model.twiss.updated.disconnect(self.update_twiss)
        del model.selection.elements[:]
        try:
            model.destroy()
        except IOError:
            # The connection may already be terminated in case MAD-X crashed.
            pass
        self.context['model'] = None
        self.context['twiss'] = None
        self.logfile.close()

    def update_twiss(self):
        self.context['twiss'] = self.model().twiss.data

    def showTwiss(self, name=None):
        import madgui.plot.matplotlib as plt
        import madgui.plot.twissfigure as twissfigure

        model = self.model()
        config = self.config.line_view
        graphs = self.config['graphs']

        # update twiss *before* creating the figure to avoid immediate
        # unnecessary redraws:
        model.twiss()

        # NOTE: using the plot_windows list as a stack with its top at 0:
        settings = (self.config.plot_windows and
                    self.config.plot_windows.pop(0) or {})

        # indicators require retrieving data for all elements which can be too
        # time consuming for large lattices:
        show_indicators = len(model.elements) < 500

        figure = plt.MultiFigure()
        plot = plt.PlotWidget(figure)

        scene = twissfigure.TwissFigure(
            figure, model, config, graphs, self.matcher)
        scene.show_indicators = show_indicators
        scene.set_graph(name or settings.get('graph') or config.default_graph)
        scene.attach(plot)

        # for convenience when debugging:
        self.context.update({
            'plot': plot,
            'figure': figure.backend_figure,
            'canvas': plot.canvas,
            'scene': scene,
        })

        menubar = QtGui.QMenuBar()
        select = twissfigure.PlotSelector(scene)
        widget = Dialog(self)
        widget.setWidget([select, plot], tight=True)
        widget.layout().setMenuBar(menubar)
        size = settings.get('size')
        pos = settings.get('pos')
        if not size:
            size = (self.size().width(), widget.sizeHint().height())
        widget.resize(*size)
        if pos:
            widget.move(*pos)
        widget.show()

        def update_window_title():
            widget.setWindowTitle("{1} ({0})".format(
                self.model().name, scene.graph_name))
        scene.graph_changed.connect(update_window_title)
        update_window_title()

        self.model.changed_singleshot(widget.close)

        def destroyed():
            if scene in self.views:
                self.config.plot_windows.insert(
                    0, self._save_plot_window(scene))
                scene.destroy()
                self.views.remove(scene)

        notifyCloseEvent(widget, destroyed)

        def toggleShareAxes():
            scene.figure.share_axes = not scene.figure.share_axes
            scene.relayout()

        def toggleIndicators():
            scene.show_indicators = not scene.show_indicators

        Menu, Item = menu.Menu, menu.Item
        menu.extend(widget, menubar, [
            Menu('&View', [
                # TODO: dynamic checked state
                Item('&Shared plot', 'Ctrl+M',
                     'Plot all curves into the same axes.',
                     toggleShareAxes, checked=False),
                # TODO: dynamic checked state
                Item('Element &indicators', None,
                     'Show element indicators',
                     toggleIndicators, checked=show_indicators),
                Item('Manage curves', None,
                     'Select which data sets are shown',
                     scene._curveManager.toggle,
                     checked=scene._curveManager.holds_value),
            ]),
        ])
        self.views.append(scene)
        return scene

    def _save_plot_window(self, scene):
        widget = scene.figure.canvas.window()
        return {
            'graph': scene.graph_name,
            'size': [widget.size().width(), widget.size().height()],
            'pos': [widget.pos().x(), widget.pos().y()],
        }

    def graphs(self, name):
        return [scene for scene in self.views if scene.graph_name == name]

    def open_graph(self, name):
        if name in self.graphs(name):
            return
        if self.views:
            self.views[-1].set_graph(name)
        else:
            self.showTwiss(name)

    def add_curve(self, name, data, style):
        from madgui.plot.twissfigure import TwissFigure
        for i, (n, d, s) in enumerate(TwissFigure.loaded_curves):
            if n == name:
                TwissFigure.loaded_curves[i][1].update(data)
                for scene in self.views:
                    if i in scene.shown_curves:
                        j = scene.shown_curves.index(i)
                        scene.user_curves.items[j].update()
                break
        else:
            TwissFigure.loaded_curves.append((name, data, style))

    def del_curve(self, name):
        from madgui.plot.twissfigure import TwissFigure
        for i, (n, d, s) in enumerate(TwissFigure.loaded_curves):
            if n == name:
                del TwissFigure.loaded_curves[i]

    def _createShell(self):
        """Create a python shell widget."""
        import madgui.core.pyshell as pyshell
        self.shell = pyshell.create(self.context)
        dock = QtGui.QDockWidget()
        dock.setWidget(self.shell)
        dock.setWindowTitle("python shell")
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.shell.exit_requested.connect(dock.close)
        return dock

    def closeEvent(self, event):
        if self.session_file:
            self.save_session(self.session_file)
        # Terminate the remote session, otherwise `_readLoop()` may hang:
        self.destroyModel()
        event.accept()

    def save_session(self, filename):
        data = self.session_data()
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        with open(filename, 'wt') as f:
            yaml.safe_dump(data, f, default_flow_style=False)
