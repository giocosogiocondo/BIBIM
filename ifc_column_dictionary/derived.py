"""Descriptions of converter semantics, explicitly not official SQL definitions."""

from ifc_sql_views.rules import RELATION_ROLES

EXACT = {
    "ifc_id": "Numeric STEP instance identifier preserved in the raw database; join a main View and its CustomPset companion on this key.",
    "ifc_class": "Actual IFC entity class of this row, including specialized classes folded into the main View.",
    "etc": "JSON object containing populated sparse fields and fields exceeding SQLite's column limit; its keys depend on this database.",
    "Placement_Type": "IFC object placement class; identifies the placement representation rather than a building element type.",
    "Placement_Unit": "Native project length unit used for the interpreted placement coordinates.",
    "Placement_BasisCurveType": "IFC class of the explicit basis curve used by a linear placement.",
    "Owner_Name": "Owning person's given name and family name combined, with the person's identification as fallback.",
    "Owner_Organization": "Name of the organization associated with the owning user in OwnerHistory.",
    "LastModifiedBy_Name": "Last modifying person's given name and family name combined, with identification as fallback.",
    "LastModifiedBy_Organization": "Organization name associated with the last modifying user in OwnerHistory.",
    "Representation_Identifier": "Identifier of the object's shape representation, such as Body or Axis; multiple representations may produce multiple values.",
    "Representation_Type": "Representation method recorded in IFC, such as SweptSolid or Brep; this is not the element's functional type.",
    "Representation_ItemTypes": "IFC classes of explicit representation items; no tessellated geometry is inferred.",
    "Storey_GlobalId": "GlobalId of a storey reached through explicit containment, aggregate or nesting ancestry; multiple storeys are retained.",
    "Storey_Name": "Name of a storey reached through explicit containment, aggregate or nesting ancestry; no geometric membership is inferred.",
    "Storey_Elevation": "Recorded elevation of the explicitly associated storey; this is not a geometry-derived element elevation.",
    "ContainedSpace_GlobalId": "GlobalId of a space reached through explicit containment or its aggregate/nest ancestry; not a spatial overlap result.",
    "ContainedSpace_Name": "Name of a space reached through explicit containment or its aggregate/nest ancestry; no geometric containment is inferred.",
    "BoundarySpaces": "JSON array of distinct spaces linked by explicit space-boundary relationships, including GlobalId, Name and LongName; not necessarily the containing space.",
    "Source_FileName": "FILE_NAME.name stored in the source IFC header; not the current SQLite filename.",
    "Source_FileTimestamp": "Timestamp recorded in the source IFC FILE_NAME header; not the database conversion time.",
    "Source_PreprocessorVersion": "Preprocessor version recorded in the source IFC FILE_NAME header.",
    "Source_OriginatingSystem": "Originating software system recorded in the source IFC FILE_NAME header.",
    "Source_FileDescription": "Description entries from the source IFC FILE_DESCRIPTION header, including an MVD declaration when present.",
    "Source_IFCSchema": "Schema identifier recorded in the source IFC FILE_SCHEMA header; do not infer an exact release beyond that identifier.",
}
for component in "XYZ":
    EXACT["Placement_" + component] = (
        f"Model-coordinate {component} position after composing explicit parent placements and rotations; native project length unit, without CRS/map conversion."
    )
    for axis in ("XAxis", "YAxis", "ZAxis"):
        EXACT[f"Placement_{axis}_{component}"] = (
            f"Dimensionless {component} component of the transformed local {axis} direction in model coordinates; unavailable when a valid orientation cannot be resolved."
        )


def definitions():
    return {
        "format": "BIBIM-derived-column-definitions/1",
        "language": "en",
        "description_method": "bibim_implementation_definition",
        "exact_columns": EXACT,
        "patterns": {
            "<TypeOrStyle>_<field>": "Attribute of the assigned IFC type/style, folded into the occurrence row; multiple assignments can yield multiple values.",
            "<field>_<uniform-unit>": "Original measurement value with its uniform unit encoded in the physical column name; values are not rescaled.",
            "<field>_Unit": "Unit label associated with the measurement field; a separate column remains when uniform-unit folding is not applicable.",
            "Owner_<date>_UTC": "OwnerHistory timestamp interpreted as an ISO-format UTC date and time.",
            "<resource-path>_<attribute>": "Forward resource attribute flattened into the owning object's row, bounded by the converter's resource traversal rules.",
            "<role>_Count": "Count of distinct relationship target IDs for this role, not a count of arbitrary nested JSON entries.",
            "<role>_Names": "Names of relationship targets; these are labels and are not guaranteed to be unique identifiers.",
            "<role>": "Recorded relationship targets as an object or JSON array; inspect the representation and preserve relevant membership multiplicity.",
            "<inverse>_<endpoint>_<field>": "Field reached through an EXPRESS-declared inverse and a relationship endpoint; no universal meaning is assigned from name similarity.",
        },
        "relationship_roles": [
            {
                "relationship": cls,
                "incoming_attribute": incoming,
                "role": role,
                "target_attribute": endpoint,
                "single_role": single,
                "description": f"{role} targets reached through {cls}.{endpoint} when the current object is referenced by {incoming}.",
            }
            for (cls, incoming), (role, endpoint, single) in sorted(RELATION_ROLES.items())
        ],
        "custom_properties": "User/exporter-defined field: retain its exact name and observed values/statistics; its semantic meaning is not inferred from an official IFC field with a similar name.",
        "limitations": [
            "These are BIBIM implementation definitions, not official buildingSMART SQL column definitions.",
            "Property availability, units, cardinality and JSON packing depend on the input database.",
            "LIST order and repeated material members are preserved; SET presentation order is not construction order.",
        ],
    }
