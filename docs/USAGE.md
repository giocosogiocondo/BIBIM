# Usage

## Inputs and conversion

`benchmark_tools.py convert INPUT.ifc OUTPUT.sqlite` imports a real IFC file and builds the public entity Views. The adjacent `.build.json` records schema, View definitions and source identity. Keep it next to the database; the SQL pipeline needs it.

For a plain Ifc2Sql database, use `python build_documented_views.py INPUT.sqlite OUTPUT.sqlite`. Existing output files require an explicit `--overwrite`. Source inputs are preserved.

`View_Door` and its optional `View_DoorCustomPset` companion are one routing group. Type/style attributes are folded into occurrence rows. Empty columns are removed; sparse properties can be packed into `etc`. Measurement values retain their original units; unit suffixes do not imply rescaling. Positions use native IFC model coordinates, not a geographic CRS or inferred walking routes.

## Local inference

Download the GGUF and a compatible CUDA `llama-server` separately. Put the server and its CUDA backend DLLs in `runtime/`, or set absolute paths in `configs/backend.local.json`. The backend uses native completion/tokenization/probability endpoints and requires the flags in `ManagedServer`; it checks candidate token probabilities rather than silently substituting a prose router.

Copy `configs/backend.example.json` and `configs/pipeline.example.json` to their `.local.json` names. Copy the sample CSV to `data/questions.csv`, or point the pipeline to your own file. A CSV row selects exactly one database through `project/ifc_model` and `database_map`; it never joins other models. Optional ground truth stays out of generation prompts.

`prepare` freezes schema, model/server identities, inputs and routing labels without loading the LLM. Use a new `prepared_directory` if any pinned input changes. `check` verifies an existing preparation without inference. `run` starts the local server and saves a new timestamped result folder. Use the existing CLI's `--resume PATH` with unchanged configuration/code/data to resume an interrupted run. `--ids 1,2` and `--limit 3` restrict a run.

There is one live routing call, one main SQL generation/execution and one final-answer generation for a normal completed question. No inspection queries, rewriting or Top6 expansion are enabled. Input or output failures are recorded; no silent context truncation occurs. The included code retains earlier workflow helpers for regression tests, but the example configuration selects the 3.4.7.1 single-pass path.

## Dynamic Context

`python run_context_ablation.py --config configs/pipeline.local.json --conditions ABC` runs three conditions sequentially on the prepared questions:

- A: column names only.
- B: names and observed types, top-3 non-NULL values/counts, NULL counts and View row counts.
- C: B plus the included validated English column descriptions.

All other model and workflow settings are retained. Outputs are separated by condition. No historical local results are reused in this public helper. Routing is fresh in each condition. The first condition establishes the selected Top3 for every question; later conditions verify the same ordered Top3 before SQL generation and stop if it differs. This recreates the control policy on your own inputs, without reusing private results.

The description catalogs cover schema-defined attributes, Pset/Qto fields and documented derived columns. Unmatched custom meanings are not invented. `etc` is visible as a column, but its contents/keys are not exposed by the statistics summary. Catalogs can be rebuilt offline from the pinned IfcOpenShell installation with `python tools/build_column_description_catalog.py` and `python tools/build_view_description_catalog.py`.

## Validate a database

Run `python validate_entity_views.py --help` for the independent original-IFC versus View comparison, or `python validate_all_entity_views.py --help` for a batch. The test suite creates small synthetic IFC models and uses mocked inference; it does not need model weights or an API key.

## Research evaluation

Private benchmark snapshots, manually reviewed SQL, baseline checkouts, answers, API keys and paid judging launchers are deliberately excluded. Obtain IFC-Bench and its official split from the dataset authors. The original external judging protocol is in Sylvain Hellin's Cobbie repository, branch `revision-agentic-search`, commit `b8d2f291780d45939acbdc6a29dea2d882520856`. Its `judge.py` and `judge_batch.py` use the final answer, reference, question and category. Keep generation errors in the full question denominator when reporting this pipeline's accuracy.

For the local experiment, start from the example settings and the official question IDs supplied by the dataset. Rebuild prepared metadata for your own paths; private prepared files contain machine-specific pins and should not be copied between checkouts. The source version's conversion and inference logic is retained, while old absolute-path launchers are replaced by these portable entry points.
