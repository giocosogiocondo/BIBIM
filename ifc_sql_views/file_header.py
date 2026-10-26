"""Original STEP header storage, separate from importer and object history."""

import json

TABLE = "file_header"
ARRAY_FIELDS = {"author", "organization", "schema_identifiers", "descriptions"}
FIELDS = (
    "file_name", "file_timestamp", "author", "organization",
    "preprocessor_version", "originating_system", "authorization",
    "schema_identifiers", "descriptions", "implementation_level",
)
PROJECT_FIELDS = {
    "Source_FileName": "file_name",
    "Source_FileTimestamp": "file_timestamp",
    "Source_PreprocessorVersion": "preprocessor_version",
    "Source_OriginatingSystem": "originating_system",
    "Source_FileDescription": "descriptions",
}


def store_header(con, model):
    name = model.header.file_name
    description = model.header.file_description
    values = {
        "file_name": name.name,
        "file_timestamp": name.time_stamp,
        "author": list(name.author),
        "organization": list(name.organization),
        "preprocessor_version": name.preprocessor_version,
        "originating_system": name.originating_system,
        "authorization": name.authorization,
        "schema_identifiers": list(model.header.file_schema.schema_identifiers),
        "descriptions": list(description.description),
        "implementation_level": description.implementation_level,
    }
    con.execute(f"CREATE TABLE {TABLE} (" + ",".join(field + " TEXT" for field in FIELDS) + ")")
    con.execute(
        f"INSERT INTO {TABLE} VALUES (" + ",".join("?" for _ in FIELDS) + ")",
        [json.dumps(values[field], ensure_ascii=False) if field in ARRAY_FIELDS else values[field]
         for field in FIELDS],
    )


def read_header(con, tables):
    if TABLE not in tables:
        return {}
    rows = con.execute(f"SELECT * FROM {TABLE} LIMIT 2").fetchall()
    if len(rows) > 1:
        raise ValueError("Expected one original file header per database")
    if not rows:
        return {}
    header = dict(rows[0])
    for field in ARRAY_FIELDS:
        if header.get(field) is not None:
            header[field] = json.loads(header[field])
    return header


def project_fields(header, legacy_schema=None, legacy_descriptions=None):
    result = {column: header.get(field) for column, field in PROJECT_FIELDS.items()}
    if "descriptions" not in header:
        # This importer has always stored FILE_DESCRIPTION's list in metadata.mvd.
        # A plain MVD label from another importer is not a complete header list.
        try:
            descriptions = json.loads(legacy_descriptions) if legacy_descriptions else None
        except (ValueError, TypeError):
            descriptions = None
        if isinstance(descriptions, list) and all(isinstance(v, str) for v in descriptions):
            result["Source_FileDescription"] = descriptions
    schemas = header.get("schema_identifiers")
    # Old databases retain the source schema in metadata, not FILE_NAME fields.
    # An explicit parser override alone is not evidence of an original FILE_SCHEMA.
    result["Source_IFCSchema"] = (
        schemas[0] if schemas and len(schemas) == 1 else schemas or legacy_schema
    )
    return result
