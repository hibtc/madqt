"""
UI for matching.
"""

from madgui.qt import QtGui, load_ui
from madgui.core.unit import ui_units
from madgui.widget.tableview import TableItem
from madgui.correct.match import variable_from_knob, Constraint
from madgui.util.enum import make_enum


Button = QtGui.QDialogButtonBox


def parse_knob(model, text):
    if ':' in text:
        elem, attr = text.split(':')
    elif '->' in text:
        elem, attr = text.split('->')
    elif text in model.globals:
        return text
    else:
        return None     # TODO: logging
    elem = elem.strip()
    attr = attr.strip()
    try:
        knobs = model._get_knobs(model.elements[elem], attr)
    except KeyError:    # missing attribute
        return None     # TODO: logging
    if knobs:
        return knobs[0]
    return None         # TODO: logging


class MatchWidget(QtGui.QWidget):

    ui_file = 'match.ui'

    def __init__(self, matcher):
        super().__init__()
        load_ui(self, __package__, self.ui_file)
        self.matcher = matcher
        self.model = model = matcher.model
        local_constraints = ['envx', 'envy'] + model.config['constraints']
        local_constraints = sorted(local_constraints)
        self.elem_enum = make_enum('Elem', model.el_names)
        self.lcon_enum = make_enum('Local', local_constraints, strict=False)
        self.knob_enum = make_enum('Knobs', matcher.knobs, strict=False)
        self.init_controls()
        self.set_initial_values()
        self.connect_signals()

    # columns

    def cons_items(self, item):
        c = item.data
        elem = self.elem_enum(c.elem.node_name if c.elem else "(global)")
        name = self.lcon_enum(c.axis)
        unit = ui_units.label(c.axis, c.value)
        def set_elem(cell, name):
            if name is not None:
                el = self.model.elements[str(name)]
                self.matcher.constraints[cell.row] = \
                    Constraint(el, el.position+el.length, c.axis, c.value)
        def set_name(cell, axis):
            if axis is not None:
                value = self.model.get_twiss(c.elem.node_name, str(axis), c.pos)
                self.matcher.constraints[cell.row] = \
                    Constraint(c.elem, c.pos, str(axis), value)
        def set_value(cell, value):
            if value is not None:
                self.matcher.constraints[cell.row] = \
                    Constraint(c.elem, c.pos, c.axis, value)
        return [
            TableItem(elem, set_value=set_elem),
            TableItem(name, set_value=set_name),
            TableItem(c.value, set_value=set_value, name=c.axis),
            TableItem(unit),
        ]

    cons_sections = ("Element", "Name", "Target", "Unit")

    def var_items(self, item):
        v = item.data
        def set_knob(cell, text):
            if text is not None:
                knob = parse_knob(self.model, str(text))
                if knob:
                    self.matcher.variables[cell.row] = \
                        variable_from_knob(self.matcher, knob)
        return [
            TableItem(self.knob_enum(v.knob), set_value=set_knob),
            TableItem(v.design),
            TableItem(v.value),
        ]

    var_sections = ("Knob", "Initial", "Final")

    # The three steps of UI initialization

    def init_controls(self):
        self.ctab.header().setHighlightSections(False)
        self.vtab.header().setHighlightSections(False)
        self.ctab.setSelectionBehavior(QtGui.QAbstractItemView.SelectRows)
        self.vtab.setSelectionBehavior(QtGui.QAbstractItemView.SelectRows)
        self.ctab.setSelectionMode(QtGui.QAbstractItemView.ExtendedSelection)
        self.vtab.setSelectionMode(QtGui.QAbstractItemView.ExtendedSelection)
        self.ctab.set_rowgetter(
            self.cons_sections, self.cons_items, self.matcher.constraints)
        self.vtab.set_rowgetter(
            self.var_sections, self.var_items, self.matcher.variables)

    def set_initial_values(self):
        self.check_mirror.setChecked(self.matcher.mirror_mode)
        self.update_buttons()

    def connect_signals(self):
        self.ctab.connectButtons(
            self.button_remove_constraint,
            self.button_clear_constraint)
        self.vtab.connectButtons(
            self.button_remove_variable,
            self.button_clear_variable)
        self.button_add_constraint.clicked.connect(self.add_constraint)
        self.button_add_variable.clicked.connect(self.add_variable)
        self.matcher.constraints.update_after.connect(self.on_update_constraints)
        self.matcher.variables.update_after.connect(self.on_update_variables)
        self.buttonBox.button(Button.Ok).clicked.connect(self.accept)
        self.buttonBox.button(Button.Reset).clicked.connect(self.matcher.reset)
        self.button_match.clicked.connect(self.matcher.match)
        self.check_mirror.clicked.connect(self.on_change_mirror)
        # TODO: connect self.matcher.finished?

    def on_update_constraints(self, *args):
        self.ctab.resizeColumnToContents(1)
        self.ctab.resizeColumnToContents(2)
        self.update_buttons()

    def on_update_variables(self, *args):
        self.vtab.resizeColumnToContents(1)
        self.vtab.resizeColumnToContents(2)
        self.update_buttons()

    def update_buttons(self):
        num_vars = len(self.matcher.variables)
        num_cons = len(self.matcher.constraints)
        # TODO: the last condition should be relaxed when we support methods
        # other than LMDIF:
        self.button_match.setEnabled(
            num_vars > 0 and
            num_cons > 0 and
            num_vars == num_cons)

    def showEvent(self, event):
        self.matcher.start()

    def hideEvent(self, event):
        self.matcher.stop()

    def accept(self):
        self.matcher.apply()
        self.window().accept()

    def add_constraint(self):
        el   = self.elem_enum._values[0]
        elem = self.model.elements[el]
        axis = self.lcon_enum._values[0]  # TODO: -> curve.y_name?
        pos  = elem.position + elem.length
        value = self.model.get_twiss(el, axis, pos)
        self.matcher.constraints.append(Constraint(
            elem, pos, axis, value))

    def add_variable(self):
        self.matcher.variables.append(
            self.matcher.next_best_variable())

    def on_change_mirror(self, checked):
        # TODO: add/remove mirrored constraints (if untouched by the user)?
        self.matcher.mirror_mode = checked
