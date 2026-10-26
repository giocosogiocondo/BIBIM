"""Validator facade: source extraction stays independent from the converter."""

from collections import defaultdict
import ifcopenshell
from .values import unit_label
from .source import IndependentSource
from .comparison import ViewComparison
from .entities import EntityValidation


class Validator(IndependentSource, ViewComparison, EntityValidation):
    def __init__(self, model, connection, report, tolerance):
        self.model, self.con, self.report, self.tolerance = model, connection, report, tolerance
        self.details, self.checked_columns = [], defaultdict(set)
        self.units = {}
        for project in model.by_type("IfcProject"):
            for u in getattr(getattr(project, "UnitsInContext", None), "Units", ()) or ():
                self.units[getattr(u, "UnitType", "CURRENCY")] = unit_label(u)
        self.schema = ifcopenshell.schema_by_name(model.schema)
        self.views = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='view'")
        }
