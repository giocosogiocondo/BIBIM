import json
import math
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import ifcopenshell
from ifc_sql_views import build, convert
from ifc_sql_views.reader import Database
from ifc_sql_views.catalog import Catalog
from ifc_sql_views.extractor import Extractor
from ifc_sql_views.semantic import Semantic
from ifc_sql_views.schemas import schema_name
from validate_entity_views import validate


def model():
    f = ifcopenshell.file(schema="IFC4X3_ADD2")
    unit = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    assignment = f.create_entity("IfcUnitAssignment", Units=[unit])
    f.create_entity(
        "IfcProject", GlobalId=ifcopenshell.guid.new(), Name="Project", UnitsInContext=assignment
    )
    return f


def axis(f, origin=(0.0, 0.0, 0.0), x=(1.0, 0.0, 0.0)):
    p = f.create_entity("IfcCartesianPoint", Coordinates=list(origin))
    z = f.create_entity("IfcDirection", DirectionRatios=[0.0, 0.0, 1.0])
    d = f.create_entity("IfcDirection", DirectionRatios=list(x))
    return f.create_entity("IfcAxis2Placement3D", Location=p, Axis=z, RefDirection=d)


def line(f):
    p = f.create_entity("IfcCartesianPoint", Coordinates=[100.0, 200.0, 0.0])
    d = f.create_entity("IfcDirection", DirectionRatios=[0.0, 1.0, 0.0])
    v = f.create_entity("IfcVector", Orientation=d, Magnitude=2.0)
    return f.create_entity("IfcLine", Pnt=p, Dir=v)


class Ifc4x3Test(unittest.TestCase):
    def converted(self, f, folder):
        source, output = Path(folder) / "source.ifc", Path(folder) / "output.sqlite"
        f.write(str(source))
        report = convert(source, output, log=lambda _: None)
        return source, output, report

    def row(self, output, name):
        with closing(sqlite3.connect(output)) as con:
            con.row_factory = sqlite3.Row
            r = dict(con.execute("SELECT * FROM " + name).fetchone())
        r.update(json.loads(r.pop("etc", None) or "{}"))
        return r

    def test_schema_alias_does_not_downgrade_or_accept_rc(self):
        self.assertEqual(schema_name("IFC4X3"), "IFC4X3_ADD2")
        self.assertEqual(schema_name("IFC4X3_ADD2"), "IFC4X3_ADD2")
        for bad in ("IFC4X3_RC4", "IFC4X3_ADD1", "IFC4X3_TC1", "IFC2X3_BOGUS"):
            with self.assertRaises(ValueError):
                schema_name(bad)

    def test_infrastructure_views_standard_custom_qto_and_plain_db(self):
        f = model()
        course = f.create_entity(
            "IfcCourse", GlobalId=ifcopenshell.guid.new(), Name="Course", PredefinedType="PAVEMENT"
        )
        f.create_entity("IfcKerb", GlobalId=ifcopenshell.guid.new(), Name="Kerb")
        f.create_entity("IfcRoad", GlobalId=ifcopenshell.guid.new(), Name="Road")
        length = f.create_entity(
            "IfcPropertySingleValue",
            Name="NominalLength",
            NominalValue=f.create_entity("IfcLengthMeasure", 2.0),
        )
        pset = f.create_entity(
            "IfcPropertySet",
            GlobalId=ifcopenshell.guid.new(),
            Name="Pset_CourseCommon",
            HasProperties=[length],
        )
        f.create_entity(
            "IfcRelDefinesByProperties",
            GlobalId=ifcopenshell.guid.new(),
            RelatedObjects=[course],
            RelatingPropertyDefinition=pset,
        )
        qty = f.create_entity("IfcQuantityVolume", Name="Volume", VolumeValue=3.0)
        qto = f.create_entity(
            "IfcElementQuantity",
            GlobalId=ifcopenshell.guid.new(),
            Name="Qto_CourseBaseQuantities",
            Quantities=[qty],
        )
        f.create_entity(
            "IfcRelDefinesByProperties",
            GlobalId=ifcopenshell.guid.new(),
            RelatedObjects=[course],
            RelatingPropertyDefinition=qto,
        )
        custom = f.create_entity(
            "IfcPropertySingleValue", Name="Code", NominalValue=f.create_entity("IfcLabel", "007")
        )
        cp = f.create_entity(
            "IfcPropertySet",
            GlobalId=ifcopenshell.guid.new(),
            Name="SiteCustom",
            HasProperties=[custom],
        )
        f.create_entity(
            "IfcRelDefinesByProperties",
            GlobalId=ifcopenshell.guid.new(),
            RelatedObjects=[course],
            RelatingPropertyDefinition=cp,
        )
        with tempfile.TemporaryDirectory() as folder:
            source, output, report = self.converted(f, folder)
            self.assertEqual(report["schema"], "IFC4X3_ADD2")
            for name in ("View_Course", "View_Kerb", "View_Road", "View_CourseCustomPset"):
                self.assertIn(name, report["views"])
            r = self.row(output, "View_Course")
            self.assertEqual(r["Pset_CourseCommon_NominalLength_m"], 2.0)
            # No volume unit exists in this project's unit assignment.
            self.assertEqual(r["Qto_CourseBaseQuantities_Volume"], 3.0)
            self.assertEqual(self.row(output, "View_CourseCustomPset")["SiteCustom_Code"], "007")
            independent = validate(
                source, output, Path(folder) / "audit", ["IfcCourse", "IfcKerb", "IfcRoad"]
            )
            self.assertTrue(
                all(s["value_agreement_percent"] == 100.0 for s in independent["entities"])
            )
            # Both IFC input and plain DB input use the ADD2 schema.
            second = Path(folder) / "plain_result.sqlite"
            with closing(sqlite3.connect(output)) as con:
                for name in report["views"]:
                    con.execute("DROP VIEW " + name)
                con.commit()
            build(output, second)
            self.assertEqual(
                self.row(second, "View_Course")["Pset_CourseCommon_NominalLength_m"], 2.0
            )

    def test_shared_property_is_not_an_owner_of_other_materials(self):
        f = model()
        shared = f.create_entity(
            "IfcPropertySingleValue",
            Name="CommonCode",
            NominalValue=f.create_entity("IfcLabel", "X"),
        )
        a = f.create_entity("IfcMaterial", Name="Material A")
        b = f.create_entity("IfcMaterial", Name="Material B")
        f.create_entity(
            "IfcMaterialProperties", Name="Definition A", Material=a, Properties=[shared]
        )
        f.create_entity(
            "IfcMaterialProperties", Name="Definition B", Material=b, Properties=[shared]
        )
        with tempfile.TemporaryDirectory() as folder:
            _, output, _ = self.converted(f, folder)
            db = Database(output)
            try:
                ext = Extractor(db, Catalog(db))
                sem = Semantic(ext)
                self.assertFalse(ext._owned_incoming.get(shared.id()))
                data = sem.resolved_resource(a.id())
                text = json.dumps(data)
                self.assertIn("Definition A", text)
                self.assertNotIn("Definition B", text)
                self.assertNotIn("Material B", text)
            finally:
                for obj in (db, ext, sem):
                    for name in dir(type(obj)):
                        method = getattr(type(obj), name, None)
                        if hasattr(method, "cache_clear"):
                            method.cache_clear()
                db.close()

    def test_linear_line_parameter_offsets_and_parent_rotation(self):
        for measure, distance in [("IfcLengthMeasure", 10.0), ("IfcParameterValue", 5.0)]:
            with self.subTest(measure=measure):
                f = model()
                point = f.create_entity(
                    "IfcPointByDistanceExpression",
                    DistanceAlong=f.create_entity(measure, distance),
                    OffsetLateral=2.0,
                    OffsetVertical=3.0,
                    OffsetLongitudinal=1.0,
                    BasisCurve=line(f),
                )
                relative = f.create_entity("IfcAxis2PlacementLinear", Location=point)
                parent = f.create_entity(
                    "IfcLocalPlacement",
                    RelativePlacement=axis(f, (10.0, 20.0, 0.0), (0.0, 1.0, 0.0)),
                )
                placement = f.create_entity(
                    "IfcLinearPlacement", RelativePlacement=relative, PlacementRelTo=parent
                )
                f.create_entity(
                    "IfcRail",
                    GlobalId=ifcopenshell.guid.new(),
                    Name="Rail",
                    ObjectPlacement=placement,
                )
                with tempfile.TemporaryDirectory() as folder:
                    _, output, report = self.converted(f, folder)
                    r = self.row(output, "View_Rail")
                    # Local origin (98,211,3), then parent +90 degree rotation.
                    self.assertEqual(
                        tuple(r["Placement_" + a] for a in "XYZ"), (-201.0, 118.0, 3.0)
                    )
                    self.assertAlmostEqual(r["Placement_XAxis_X"], -1.0)
                    self.assertAlmostEqual(r["Placement_XAxis_Y"], 0.0)
                    self.assertFalse(
                        any(d["code"] == "invalid_placement" for d in report["diagnostics"])
                    )

    def test_linear_polyline_circle_and_explicit_cartesian(self):
        for cls in ("IfcPolyline", "IfcCircle", "explicit_cartesian"):
            with self.subTest(curve=cls):
                f = model()
                if cls == "IfcPolyline":
                    points = [
                        f.create_entity("IfcCartesianPoint", Coordinates=v)
                        for v in ([0.0, 0.0], [10.0, 0.0], [10.0, 10.0])
                    ]
                    curve = f.create_entity("IfcPolyline", Points=points)
                    distance = 15.0
                    expected = (8.0, 5.0, 3.0)
                else:
                    curve = f.create_entity("IfcCircle", Position=axis(f), Radius=10.0)
                    distance = 5.0 * math.pi
                    expected = (0.0, 8.0, 3.0)
                point = f.create_entity(
                    "IfcPointByDistanceExpression",
                    DistanceAlong=f.create_entity("IfcLengthMeasure", distance),
                    OffsetLateral=2.0,
                    OffsetVertical=3.0,
                    BasisCurve=curve,
                )
                relative = f.create_entity("IfcAxis2PlacementLinear", Location=point)
                placement = f.create_entity("IfcLinearPlacement", RelativePlacement=relative)
                if cls == "explicit_cartesian":
                    placement.CartesianPosition = axis(f, (2.0, 4.0, 6.0))
                    expected = (2.0, 4.0, 6.0)
                f.create_entity(
                    "IfcRail", GlobalId=ifcopenshell.guid.new(), ObjectPlacement=placement
                )
                with tempfile.TemporaryDirectory() as folder:
                    _, output, _ = self.converted(f, folder)
                    r = self.row(output, "View_Rail")
                    for a, v in zip("XYZ", expected):
                        self.assertAlmostEqual(r["Placement_" + a], v)

    def test_unsupported_linear_curve_preserves_data_and_reports_no_invented_xyz(self):
        f = model()
        p = [
            f.create_entity("IfcCartesianPoint", Coordinates=v)
            for v in ([0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 10.0, 0.0])
        ]
        curve = f.create_entity(
            "IfcBSplineCurveWithKnots",
            Degree=2,
            ControlPointsList=p,
            CurveForm="UNSPECIFIED",
            ClosedCurve=False,
            SelfIntersect=False,
            KnotMultiplicities=[3, 3],
            Knots=[0.0, 1.0],
            KnotSpec="UNSPECIFIED",
        )
        point = f.create_entity(
            "IfcPointByDistanceExpression",
            DistanceAlong=f.create_entity("IfcLengthMeasure", 5.0),
            BasisCurve=curve,
        )
        relative = f.create_entity("IfcAxis2PlacementLinear", Location=point)
        placement = f.create_entity("IfcLinearPlacement", RelativePlacement=relative)
        f.create_entity("IfcRail", GlobalId=ifcopenshell.guid.new(), ObjectPlacement=placement)
        with tempfile.TemporaryDirectory() as folder:
            _, output, report = self.converted(f, folder)
            r = self.row(output, "View_Rail")
            self.assertNotIn("Placement_X", r)
            self.assertEqual(r["Placement_DistanceAlong_m"], 5.0)
            self.assertEqual(r["Placement_BasisCurveType"], "IfcBSplineCurveWithKnots")
            self.assertIn("unsupported_linear_curve", {d["code"] for d in report["diagnostics"]})

    def test_tilted_circle_is_diagnosed_instead_of_guessing_offset_frame(self):
        f = model()
        position = axis(f)
        position.Axis = f.create_entity("IfcDirection", DirectionRatios=[0.0, 1.0, 0.0])
        curve = f.create_entity("IfcCircle", Position=position, Radius=10.0)
        point = f.create_entity(
            "IfcPointByDistanceExpression",
            DistanceAlong=f.create_entity("IfcLengthMeasure", 5.0),
            BasisCurve=curve,
        )
        relative = f.create_entity("IfcAxis2PlacementLinear", Location=point)
        placement = f.create_entity("IfcLinearPlacement", RelativePlacement=relative)
        f.create_entity("IfcRail", GlobalId=ifcopenshell.guid.new(), ObjectPlacement=placement)
        with tempfile.TemporaryDirectory() as folder:
            _, output, report = self.converted(f, folder)
            self.assertNotIn("Placement_X", self.row(output, "View_Rail"))
            self.assertIn(
                "unsupported_linear_curve_orientation", {d["code"] for d in report["diagnostics"]}
            )
