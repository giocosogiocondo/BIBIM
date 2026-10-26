from __future__ import annotations

MEASURE_UNITS = {
    "IfcLengthMeasure": "LENGTHUNIT",
    "IfcPositiveLengthMeasure": "LENGTHUNIT",
    "IfcNonNegativeLengthMeasure": "LENGTHUNIT",
    "IfcAreaMeasure": "AREAUNIT",
    "IfcVolumeMeasure": "VOLUMEUNIT",
    "IfcMassMeasure": "MASSUNIT",
    "IfcTimeMeasure": "TIMEUNIT",
    "IfcPlaneAngleMeasure": "PLANEANGLEUNIT",
    "IfcPositivePlaneAngleMeasure": "PLANEANGLEUNIT",
    "IfcThermodynamicTemperatureMeasure": "THERMODYNAMICTEMPERATUREUNIT",
    "IfcThermalTransmittanceMeasure": "THERMALTRANSMITTANCEUNIT",
    "IfcMassDensityMeasure": "MASSDENSITYUNIT",
    "IfcPressureMeasure": "PRESSUREUNIT",
    "IfcForceMeasure": "FORCEUNIT",
    "IfcEnergyMeasure": "ENERGYUNIT",
    "IfcPowerMeasure": "POWERUNIT",
    "IfcMonetaryMeasure": "MONETARYUNIT",
}
PREFIXES = {
    "EXA": "E",
    "PETA": "P",
    "TERA": "T",
    "GIGA": "G",
    "MEGA": "M",
    "KILO": "k",
    "HECTO": "h",
    "DECA": "da",
    "DECI": "d",
    "CENTI": "c",
    "MILLI": "m",
    "MICRO": "u",
    "NANO": "n",
    "PICO": "p",
    "FEMTO": "f",
    "ATTO": "a",
}
SYMBOLS = {
    "METRE": "m",
    "SQUARE_METRE": "m2",
    "CUBIC_METRE": "m3",
    "GRAM": "g",
    "SECOND": "s",
    "RADIAN": "rad",
    "KELVIN": "K",
    "DEGREE_CELSIUS": "degC",
    "NEWTON": "N",
    "PASCAL": "Pa",
    "JOULE": "J",
    "WATT": "W",
    "AMPERE": "A",
    "VOLT": "V",
    "HERTZ": "Hz",
}


class Units:
    """Preserve native values with unambiguous unit columns; never infer lost custom measure types."""

    def __init__(self, db):
        self.db = db
        self.project_units = {}
        assignments = set()
        for iid in db.ids("IfcProject"):
            assignments.update(db.refs(iid, "UnitsInContext"))
        if len(assignments) == 1:
            for unit_id in db.refs(next(iter(assignments)), "Units"):
                node = db.nodes[unit_id]
                key = node.get("UnitType") or (
                    "MONETARYUNIT" if node["ifc_class"] == "IfcMonetaryUnit" else None
                )
                if key:
                    self.project_units[key] = unit_id
        elif len(assignments) > 1:
            db.note("multiple_project_unit_assignments", 0)

    def label(self, iid, active=frozenset()):
        if not iid or iid not in self.db.nodes:
            return None
        if iid in active:
            self.db.note("unit_cycle", iid)
            return None
        node = self.db.nodes[iid]
        cls = node["ifc_class"]
        if cls == "IfcSIUnit":
            return PREFIXES.get(node.get("Prefix"), node.get("Prefix") or "") + SYMBOLS.get(
                node.get("Name"), node.get("Name") or ""
            )
        if cls == "IfcMonetaryUnit":
            return node.get("Currency")
        if self.db.is_a(cls, "IfcConversionBasedUnit") or cls == "IfcContextDependentUnit":
            return node.get("Name")
        if cls == "IfcDerivedUnit":
            parts = []
            for element in self.db.refs(iid, "Elements"):
                n = self.db.nodes[element]
                ids = self.db.refs(element, "Unit")
                label = self.label(ids[0], active | {iid}) if ids else None
                if label:
                    parts.append(f"{label}^{n.get('Exponent')}")
            return "*".join(parts) or node.get("UserDefinedType") or node.get("UnitType")
        return node.get("Name")

    def for_value(self, measure=None, explicit_id=None):
        if explicit_id:
            return self.label(explicit_id)
        key = MEASURE_UNITS.get(measure)
        if not key and measure and measure.startswith("Ifc") and measure.endswith("Measure"):
            key = measure[3:-7].upper() + "UNIT"
        return self.label(self.project_units.get(key))
