# Usage

## Inputs and conversion

`python build_documented_views.py INPUT.ifc OUTPUT.sqlite` imports an IFC file (`IFC2X3`, `IFC4`, or `IFC4X3_ADD2`) and builds the public entity Views. It also accepts a plain Ifc2Sql SQLite database (`python build_documented_views.py INPUT.sqlite OUTPUT.sqlite`). Existing output files require an explicit `--overwrite`. The adjacent `.build.json` records schema, View definitions, and source identity; keep it next to the database for validation and Text-to-SQL.

`View_Door` and its optional `View_DoorCustomPset` companion form one routing group. Type/style attributes are folded into occurrence rows. Empty columns are removed; sparse (`<= 10%`) or SQLite-overflow properties are packed into `etc`. Measurement values retain their original units; unit suffixes do not imply rescaling. Positions use native IFC model coordinates, not a geographic CRS or inferred walking routes.

## Local inference

Download a GGUF model and a compatible `llama-server` separately. Put the server and its backend libraries in `runtime/`, or set paths in `configs/backend.local.json`. By default, `read_metadata` checks for `gemma4`, or you can set `"required_architecture": "any"` in `backend.local.json` to use another GGUF architecture.

Copy `configs/backend.example.json` and `configs/pipeline.example.json` to their `.local.json` names. Copy the sample CSV to `data/questions.csv`, or point the pipeline to your own file. A CSV row selects one database through `project/ifc_model` and `database_map`; optional ground truth stays out of generation prompts.

- `python text_to_sql.py prepare` freezes schema, model/server identities, inputs, and routing labels without loading the LLM.
- `python text_to_sql.py check` verifies an existing preparation without inference.
- `python text_to_sql.py run` starts the local server and saves a timestamped result folder (`--resume PATH`, `--ids 1,2`, and `--limit 3` are supported).

## Dynamic Context Ablation

`python run_context_ablation.py --config configs/pipeline.local.json --conditions ABC` runs three conditions sequentially on the prepared questions:

- `A`: column names only.
- `B`: names and observed types, top-3 non-NULL values/counts, NULL counts, and View row counts.
- `C`: `B` plus the validated English column descriptions.

Catalogs can be verified or rebuilt offline from the pinned IfcOpenShell installation with `python tools/build_column_description_catalog.py --check` and `python tools/build_view_description_catalog.py --check`.

## Validate a database

Run `python validate_entity_views.py --help` for the independent original-IFC versus View comparison, or `python validate_all_entity_views.py --help` for a batch. The test suite creates small synthetic IFC models and uses mocked inference (`python -m unittest discover -s tests`).
