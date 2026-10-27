# BIBIM

Document-driven IFC-to-SQLite semantic views (`IFC2X3`, `IFC4`, `IFC4X3_ADD2`), independent source-to-view validation, schema-wide column/view description catalogs, and local Text-to-SQL evaluation.

## Setup

Use Python 3.12 or newer:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Convert an IFC file or raw Ifc2Sql database

`build_documented_views.py` accepts either a `.ifc` file or a raw Ifc2Sql `.sqlite` database and builds the public `View_<Entity>` and `View_<Entity>CustomPset` views along with the `.build.json` manifest:

```powershell
.venv\Scripts\python build_documented_views.py data/model.ifc outputs/demo/model.sqlite
```

## Run Text-to-SQL

Copy `configs/backend.example.json` and `configs/pipeline.example.json` to `configs/backend.local.json` and `configs/pipeline.local.json`, then configure your GGUF model, `llama-server`, questions CSV, and database paths:

```powershell
.venv\Scripts\python text_to_sql.py prepare
.venv\Scripts\python text_to_sql.py check
.venv\Scripts\python text_to_sql.py run
```
