"""Schema-based regressions, independent of benchmark filenames/product names."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import ifcopenshell

from ifc_sql_views.catalog import Catalog
from ifc_sql_views.extractor import Extractor
from ifc_sql_views.lifecycle import clear_method_caches
from ifc_sql_views.pipeline import build
from ifc_sql_views.raw_import import raw_database
from ifc_sql_views.reader import Database
from ifc_sql_views.semantic import Semantic
from ifc_sql_views.conversion import convert

SCHEMAS = ("IFC2X3", "IFC4", "IFC4X3_ADD2")


def fixture(schema="IFC4", count=10):
    f = ifcopenshell.file(schema=schema)
    def root(cls, **attrs):
        return f.create_entity(cls, GlobalId=ifcopenshell.guid.new(), **attrs)
    unit = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Prefix="MILLI", Name="METRE")
    units = f.create_entity("IfcUnitAssignment", Units=[unit])
    project = root("IfcProject", Name="Independent fixture", UnitsInContext=units)
    level = root("IfcBuildingStorey", Name="Level A", Elevation=3100.)
    first = root("IfcSpace", Name="Room A", LongName="Office")
    second = root("IfcSpace", Name="Room A", LongName="Corridor")
    root("IfcRelAggregates", RelatingObject=project, RelatedObjects=[level])
    root("IfcRelAggregates", RelatingObject=level, RelatedObjects=[first, second])
    beams = [root("IfcBeam", Name=f"Element {i}") for i in range(count)]
    assembly = root("IfcElementAssembly", Name="Assembly")
    root("IfcRelContainedInSpatialStructure", RelatedElements=[assembly], RelatingStructure=first)
    root("IfcRelAggregates", RelatingObject=assembly, RelatedObjects=[beams[0]])
    for space in [first, second, first]:
        root("IfcRelSpaceBoundary", RelatingSpace=space, RelatedBuildingElement=beams[0],
             PhysicalOrVirtualBoundary="PHYSICAL", InternalOrExternalBoundary="INTERNAL")
    # An explicit reference is not containment. Never infer the first room for beam1.
    root("IfcRelReferencedInSpatialStructure", RelatedElements=[beams[1]], RelatingStructure=first)
    material = f.create_entity("IfcMaterial", Name="Finish")
    core = f.create_entity("IfcMaterial", Name="Core")
    a = f.create_entity("IfcMaterialLayer", Material=material, LayerThickness=13.)
    b = f.create_entity("IfcMaterialLayer", Material=core, LayerThickness=100.)
    a2 = f.create_entity("IfcMaterialLayer", Material=material, LayerThickness=13.)
    layers = f.create_entity("IfcMaterialLayerSet", MaterialLayers=[a, a2, b, a], LayerSetName="Repeated")
    usage = f.create_entity("IfcMaterialLayerSetUsage", ForLayerSet=layers,
                            LayerSetDirection="AXIS2", DirectionSense="POSITIVE", OffsetFromReferenceLine=0.)
    root("IfcRelAssociatesMaterial", RelatedObjects=[beams[0]], RelatingMaterial=usage)
    singleton = f.create_entity("IfcMaterialLayerSet", MaterialLayers=[b])
    root("IfcRelAssociatesMaterial", RelatedObjects=[beams[1]], RelatingMaterial=singleton)
    return f, root, project, level, first, second, beams


class ViewSemanticsTest(unittest.TestCase):
    def semantic(self, model):
        folder = tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        path = Path(folder.name)/"raw.sqlite"
        raw_database(model, path)
        db = Database(path);cat = Catalog(db);ext = Extractor(db, cat);sem = Semantic(ext)
        def close():
            clear_method_caches(sem, ext, cat, db);db.close()
        self.addCleanup(close)
        return path, db, ext, sem

    def test_material_list_positions_and_equal_distinct_layers_survive_all_schemas(self):
        for schema in SCHEMAS:
            with self.subTest(schema=schema):
                f, _, _, _, _, _, beams = fixture(schema)
                _, _, _, sem = self.semantic(f)
                row = sem.object_attributes(beams[0].id())
                layers = row["Material_ForLayerSet_MaterialLayers"]
                self.assertEqual([x["LayerThickness"] for x in layers], [13.,13.,100.,13.])
                self.assertEqual(sum(x["LayerThickness"] for x in layers),139.)
                self.assertEqual([x["Material"]["Name"] for x in layers], ["Finish","Finish","Core","Finish"])
                self.assertNotIn("ifc_id", json.dumps(layers))
                single = sem.object_attributes(beams[1].id())["Material_MaterialLayers"]
                self.assertIsInstance(single,list);self.assertEqual(len(single),1)

    def test_equal_constituents_are_distinct_members_without_invented_positions(self):
        for schema in SCHEMAS[1:]:
            with self.subTest(schema=schema):
                f, root, _, _, _, _, beams = fixture(schema)
                mat = f.create_entity("IfcMaterial",Name="Same")
                a = f.create_entity("IfcMaterialConstituent",Name="Same",Material=mat)
                b = f.create_entity("IfcMaterialConstituent",Name="Same",Material=mat)
                material_set = f.create_entity("IfcMaterialConstituentSet",MaterialConstituents=[a,b])
                root("IfcRelAssociatesMaterial",RelatedObjects=[beams[2]],RelatingMaterial=material_set)
                _, _, _, sem = self.semantic(f)
                members = sem.object_attributes(beams[2].id())["Material_MaterialConstituents"]
                self.assertEqual(len(members),2);self.assertEqual(members[0],members[1])
                self.assertNotIn("Position",json.dumps(members))

    def test_explicit_space_parent_chain_and_boundary_identity_all_schemas(self):
        for schema in SCHEMAS:
            with self.subTest(schema=schema):
                f, _, _, level, first, second, beams = fixture(schema)
                _, _, _, sem = self.semantic(f)
                row = sem.object_attributes(beams[0].id())
                self.assertEqual(row["Storey_Name"],level.Name)
                self.assertEqual(row["ContainedSpace_Name"],first.Name)
                self.assertEqual(row["ContainedSpace_GlobalId"],first.GlobalId)
                self.assertEqual(len(row["BoundarySpaces"]),2)
                self.assertEqual({x["GlobalId"] for x in row["BoundarySpaces"]},{first.GlobalId,second.GlobalId})
                self.assertEqual([x["Name"] for x in row["BoundarySpaces"]],["Room A","Room A"])
                self.assertNotIn("ContainedSpace_Name",sem.object_attributes(beams[1].id()))
                self.assertNotIn("ContainedSpace_Name",sem.object_attributes(first.id()))

    def test_multiple_parent_storeys_are_preserved_and_cycles_terminate(self):
        f, root, _, first_level, first_space, _, beams = fixture()
        other = root("IfcBuildingStorey",Name=first_level.Name,Elevation=6200.)
        root("IfcRelContainedInSpatialStructure",RelatedElements=[beams[0]],RelatingStructure=other)
        # Malformed decomposition must not hang or choose an arbitrary parent.
        root("IfcRelAggregates",RelatingObject=beams[0],RelatedObjects=[first_space])
        _, db, _, sem = self.semantic(f)
        row = sem.object_attributes(beams[0].id())
        self.assertEqual(len(row["Storey_GlobalId"]),2)
        self.assertEqual(set(row["Storey_GlobalId"]),{first_level.GlobalId,other.GlobalId})
        self.assertEqual(row["Storey_Name"],["Level A","Level A"])
        self.assertTrue(any(x["code"]=="multiple_storeys" for x in db.diagnostics))

    def test_spatial_fields_follow_exact_ten_percent_policy_and_preserve_rows(self):
        for count,packed in [(10,True),(9,False)]:
            with self.subTest(count=count):
                f, _, _, _, first, _, beams = fixture(count=count)
                path, _, _, _ = self.semantic(f)
                target=path.with_name("public.sqlite")
                report=build(path,target,classes=["IfcBeam"])
                with closing(sqlite3.connect(target)) as c:
                    c.row_factory=sqlite3.Row
                    row=dict(c.execute('select * from View_Beam where ifc_id=?',(beams[0].id(),)).fetchone())
                    self.assertEqual(c.execute('select count(distinct ifc_id) from View_Beam').fetchone()[0],count)
                    if packed:
                        self.assertNotIn("ContainedSpace_Name",row)
                        detail=json.loads(row['etc'])
                        self.assertEqual(detail['ContainedSpace_Name'],first.Name)
                        self.assertEqual(detail['Storey_Name'],'Level A')
                        self.assertEqual(len(detail['BoundarySpaces']),2)
                    else:
                        self.assertEqual(row['ContainedSpace_Name'],first.Name)
                        self.assertEqual(len(json.loads(row['BoundarySpaces'])),2)
                    self.assertNotIn("View_GeneralInfo",report['views'])

    def test_absent_spatial_fields_removed_not_fabricated(self):
        f, _, _, _, _, _, beams=fixture()
        path, _, _, _=self.semantic(f)
        target=path.with_name('only_unknown.sqlite')
        # Select a class that has no explicit spatial parent or boundary.
        build(path,target,classes=['IfcProject'])
        with closing(sqlite3.connect(target)) as c:
            cols={x[1] for x in c.execute('pragma table_info(View_Project)')}
            self.assertNotIn('ContainedSpace_Name',cols);self.assertNotIn('BoundarySpaces',cols)


class FileHeaderTest(unittest.TestCase):
    def test_original_header_and_project_fields_survive_ifc_db_db_roundtrip(self):
        for schema in SCHEMAS:
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as temp:
                p=Path(temp);f, _, project, _, _, _, _=fixture(schema)
                f.header.file_name.name='Original source.ifc'
                f.header.file_name.time_stamp='2019-01-02T03:04:05'
                f.header.file_name.author=('007','작성자')
                f.header.file_name.organization=('Office',)
                f.header.file_name.preprocessor_version='Original writer 1.2'
                f.header.file_name.originating_system='Original exporter 3.4'
                f.header.file_name.authorization='Approved'
                f.header.file_description.description=('First description','Second description')
                source=p/'input.ifc';f.write(str(source))
                public=p/'public.sqlite';convert(source,public,log=lambda _:None)
                copy=p/'rebuilt.sqlite';build(public,copy)
                for database in [public,copy]:
                    with closing(sqlite3.connect(database)) as c:
                        c.row_factory=sqlite3.Row
                        header=dict(c.execute('select * from file_header').fetchone())
                        self.assertEqual(header['file_timestamp'],'2019-01-02T03:04:05')
                        self.assertEqual(json.loads(header['author']),['007','작성자'])
                        self.assertEqual(header['authorization'],'Approved')
                        row=dict(c.execute('select * from View_Project').fetchone())
                        self.assertEqual(row['Source_IFCSchema'],schema)
                        self.assertEqual(row['Source_FileName'],'Original source.ifc')
                        self.assertEqual(row['Source_OriginatingSystem'],'Original exporter 3.4')
                        self.assertEqual(row['Source_PreprocessorVersion'],'Original writer 1.2')
                        self.assertEqual(row['Source_FileTimestamp'],'2019-01-02T03:04:05')
                        self.assertEqual(json.loads(row['Source_FileDescription']),['First description','Second description'])
                        self.assertIn('BIBIM',c.execute('select preprocessor from metadata').fetchone()[0])
                        self.assertEqual(c.execute('pragma integrity_check').fetchone()[0],'ok')

    def test_legacy_db_exposes_known_schema_description_and_does_not_guess_file_name(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'raw.sqlite';f, *_=fixture();raw_database(f,source)
            with closing(sqlite3.connect(source)) as c:
                c.execute('drop table file_header');c.commit()
            target=source.with_name('public.sqlite');build(source,target)
            with closing(sqlite3.connect(target)) as c:
                c.row_factory=sqlite3.Row
                row=dict(c.execute('select * from View_Project').fetchone())
                self.assertEqual(row['Source_IFCSchema'],'IFC4')
                self.assertEqual(json.loads(row['Source_FileDescription']),list(f.header.file_description.description))
                for key in ['Source_FileName','Source_FileTimestamp','Source_PreprocessorVersion','Source_OriginatingSystem']:
                    self.assertNotIn(key,row)

    def test_legacy_mvd_label_is_not_invented_as_a_complete_description_list(self):
        from ifc_sql_views.file_header import project_fields
        for value in ['ReferenceView', '42', '{"View":"test"}']:
            self.assertIsNone(project_fields({},'IFC4',value)['Source_FileDescription'])


if __name__=='__main__':unittest.main()
