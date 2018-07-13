"""
Multi grid correction method.
"""

# TODO:
# - use CORRECT command from MAD-X rather than custom numpy method?
# - combine with optic variation method

from functools import partial

import yaml

from madgui.qt import QtCore, QtGui, load_ui

from madgui.core.unit import ui_units, change_unit, get_raw_label
from madgui.util.collections import List
from madgui.util.layout import VBoxLayout, HBoxLayout
from madgui.util.qt import fit_button, monospace
from madgui.widget.tableview import ColumnInfo
from madgui.widget.edit import LineNumberBar

from .orbit import fit_particle_orbit, MonitorReadout
from .match import Matcher, Constraint, variable_from_knob, Variable


class Corrector(Matcher):

    """
    Single target orbit correction via optic variation.
    """

    mode = 'xy'

    def __init__(self, control, configs):
        super().__init__(control.model(), None)
        self.control = control
        self.configs = configs
        self._knobs = {knob.name.lower(): knob for knob in control.get_knobs()}
        # save elements
        self.design_values = {}
        self.monitors = List()
        self.readouts = List()
        self._offsets = control._frame.config['online_control']['offsets']
        QtCore.QTimer.singleShot(0, partial(control._frame.open_graph, 'orbit'))

    def setup(self, name, dirs=None):
        dirs = dirs or self.mode

        selected = self.selected = self.configs[name]
        monitors = selected['monitors']
        steerers = sum([selected['steerers'][d] for d in dirs], [])
        targets  = selected['targets']

        self.method = selected.get('method', ('jacobian', {}))
        self.active = name
        self.mode = dirs
        self.match_names = [s for s in steerers if isinstance(s, str)]
        self.assign = {k: v for s in steerers if isinstance(s, dict)
                       for k, v in s.items()}

        self.monitors[:] = monitors
        elements = self.model.elements
        self.constraints[:] = sorted([
            Constraint(elements[target], elements[target].position, key, float(value))
            for target, values in targets.items()
            for key, value in values.items()
            if key[-1] in dirs
        ], key=lambda c: c.pos)
        self.variables[:] = [
            variable_from_knob(self, knob, self._knobs[knob.lower()])
            for knob in self.match_names + list(self.assign)
            if knob.lower() in self._knobs
        ]

    def update(self):
        self.update_readouts()
        self.update_fit()

    def update_readouts(self):
        self.readouts[:] = [
            # NOTE: this triggers model._retrack in stub!
            MonitorReadout(monitor, self.control.read_monitor(monitor))
            for monitor in self.monitors
        ]

    def update_fit(self):
        self.fit_results = None
        if len(self.readouts) < 2:
            return
        self.control.read_all()
        init_orbit, chi_squared, singular = \
            self.fit_particle_orbit()
        if singular:
            return
        self.fit_results = init_orbit
        self.model.update_twiss_args(init_orbit)

    # computations

    def fit_particle_orbit(self):
        return fit_particle_orbit(self.model, self._offsets, self.readouts)[0]

    def compute_steerer_corrections(self, init_orbit):

        """
        Compute corrections for the x_steerers, y_steerers.

        :param dict init_orbit: initial conditions as returned by the fit
        """

        def offset(c):
            dx, dy = self._offsets.get(c.elem.name.lower(), (0, 0))
            if c.axis in ('x', 'posx'): return dx
            if c.axis in ('y', 'posy'): return dy
            return 0
        constraints = [
            (c.elem, None, c.axis, c.value+offset(c))
            for c in self.constraints
        ]
        model = self.model
        with model.undo_stack.rollback("Orbit correction"):
            model.update_globals(self.assign)
            model.update_twiss_args(init_orbit)
            model.match(
                vary=self.match_names,
                method=self.method,
                weight={'x': 1e3, 'y':1e3, 'px':1e2, 'py':1e2},
                constraints=constraints)
            return {
                knob: model.read_param(knob)
                for knob in self.match_names + list(self.assign)
                if knob.lower() in self._knobs
            }


def format_knob(cell):
    return cell.data.knob

def format_final(cell):
    var = cell.data
    return change_unit(var.value, var.info.unit, var.info.ui_unit)

def format_initial(cell):
    var = cell.data
    return change_unit(var.design, var.info.unit, var.info.ui_unit)

def format_unit(cell):
    return get_raw_label(cell.data.info.ui_unit)

def set_constraint_value(cell, value):
    widget, c, i = cell.context, cell.data, cell.row
    widget.corrector.constraints[i] = Constraint(c.elem, c.pos, c.axis, value)

class CorrectorWidget(QtGui.QWidget):

    steerer_corrections = None

    ui_file = 'mgm_dialog.ui'

    readout_columns = ("Monitor", "X", "Y"), [
        ColumnInfo('name'),
        ColumnInfo('posx', convert=True),
        ColumnInfo('posy', convert=True),
    ]

    constraint_columns = ("Element", "Param", "Value", "Unit"), [
        ColumnInfo(lambda c: c.data.elem.node_name),
        ColumnInfo('axis'),
        ColumnInfo('value', set_constraint_value, convert='axis'),
        ColumnInfo(lambda c: ui_units.label(c.data.axis)),
    ]

    steerer_columns = ("Steerer", "Initial", "Final", "Unit"), [
        ColumnInfo(format_knob),
        ColumnInfo(format_initial),
        ColumnInfo(format_final),
        ColumnInfo(format_unit),
    ]

    def __init__(self, corrector):
        super().__init__()
        load_ui(self, __package__, self.ui_file)
        self.corrector = corrector
        self.corrector.start()
        self.init_controls()
        self.set_initial_values()
        self.connect_signals()

    def closeEvent(self, event):
        self.corrector.stop()

    def on_execute_corrections(self):
        """Apply calculated corrections."""
        self.corrector.model.write_params(self.steerer_corrections.items())
        self.corrector.control.write_params(self.steerer_corrections.items())
        self.corrector.apply()

    def init_controls(self):
        for tab in (self.mon_tab, self.con_tab, self.var_tab):
            tab.header().setHighlightSections(False)
            tab.setSelectionBehavior(QtGui.QAbstractItemView.SelectRows)
            tab.setSelectionMode(QtGui.QAbstractItemView.ExtendedSelection)
        corr = self.corrector
        self.mon_tab.set_columns(self.readout_columns, corr.readouts, self)
        self.var_tab.set_columns(self.steerer_columns, corr.variables, self)
        self.con_tab.set_columns(self.constraint_columns, corr.constraints, self)
        self.combo_config.addItems(list(self.corrector.configs))
        self.combo_config.setCurrentText(self.corrector.active)
        fit_button(self.btn_edit_conf)

    def set_initial_values(self):
        self.update_fit_button.setFocus()
        self.radio_mode_xy.setChecked(True)
        self.corrector.update()

    def connect_signals(self):
        self.update_fit_button.clicked.connect(self.update_fit)
        self.execute_corrections.clicked.connect(self.on_execute_corrections)
        self.combo_config.currentIndexChanged.connect(self.on_change_config)
        self.btn_edit_conf.clicked.connect(self.edit_config)
        self.radio_mode_x.clicked.connect(partial(self.on_change_mode, 'x'))
        self.radio_mode_y.clicked.connect(partial(self.on_change_mode, 'y'))
        self.radio_mode_xy.clicked.connect(partial(self.on_change_mode, 'xy'))

    def update_fit(self):
        """Calculate initial positions / corrections."""
        self.corrector.update()

        if not self.corrector.fit_results or not self.corrector.variables:
            self.steerer_corrections = None
            self.execute_corrections.setEnabled(False)
            return
        self.steerer_corrections = \
            self.corrector.compute_steerer_corrections(self.corrector.fit_results)
        self.execute_corrections.setEnabled(True)
        # update table view
        with self.corrector.model.undo_stack.rollback("Knobs for corrected orbit"):
            self.corrector.model.write_params(self.steerer_corrections.items())
            self.corrector.variables[:] = [
                Variable(v.knob, v.info, val,
                         self.corrector.design_values.setdefault(v.knob, val))
                for v in self.corrector.variables
                for val in [self.steerer_corrections.get(v.knob)]
            ]
        self.var_tab.resizeColumnToContents(0)

    def on_change_config(self, index):
        name = self.combo_config.itemText(index)
        self.corrector.setup(name, self.corrector.mode)
        self.corrector.update()

    def on_change_mode(self, dirs):
        self.corrector.setup(self.corrector.active, dirs)
        self.corrector.update()

        # TODO: make 'optimal'-column in var_tab editable and update
        #       self.execute_corrections.setEnabled according to its values

    def edit_config(self):
        dialog = EditConfigDialog(self.corrector.model, self.corrector)
        dialog.exec_()


class EditConfigDialog(QtGui.QDialog):

    def __init__(self, model, matcher):
        super().__init__()
        self.model = model
        self.matcher = matcher
        self.textbox = QtGui.QPlainTextEdit()
        self.textbox.setFont(monospace())
        self.linenos = LineNumberBar(self.textbox)
        buttons = QtGui.QDialogButtonBox()
        buttons.addButton(buttons.Ok).clicked.connect(self.accept)
        self.setLayout(VBoxLayout([
            HBoxLayout([self.linenos, self.textbox], tight=True),
            buttons,
        ]))
        self.setSizeGripEnabled(True)
        self.resize(QtCore.QSize(600,400))
        self.setWindowTitle(self.model.filename)

        with open(model.filename) as f:
            text = f.read()
        self.textbox.appendPlainText(text)

    def accept(self):
        if self.apply():
            super().accept()

    def apply(self):
        text = self.textbox.toPlainText()
        try:
            data = yaml.safe_load(text)
        except yaml.error.YAMLError:
            QtGui.QMessageBox.critical(
                self,
                'Syntax error in YAML document',
                'There is a syntax error in the YAML document, please edit.')
            return False

        configs = data.get('multi_grid')
        if not configs:
            QtGui.QMessageBox.critical(
                self,
                'No config defined',
                'No multi grid configuration defined.')
            return False

        with open(self.model.filename, 'w') as f:
            f.write(text)

        self.matcher.configs = configs
        self.model.data['multi_grid'] = configs

        conf = self.matcher.active if self.matcher.active in configs else next(iter(configs))
        self.matcher.setup(conf)
        self.matcher.update()

        return True
