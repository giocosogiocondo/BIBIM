## Setup

Use Python 3.12 or newer. The Windows GUI also needs Tkinter.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Convert an IFC file

```powershell
.venv\Scripts\python benchmark_tools.py convert data/model.ifc outputs/demo/model.sqlite
```

Or run `run_gui.bat` to choose files in the GUI. IFC2X3, IFC4 and IFC4X3 ADD2 are supported.

## Ask questions

Copy the two JSON examples in `configs/` to `backend.local.json` and `pipeline.local.json`. Set your GGUF, CUDA-enabled `llama-server`, questions CSV and database paths. `examples/questions.example.csv` shows the CSV format.

```powershell
.venv\Scripts\python text_to_sql.py prepare
.venv\Scripts\python text_to_sql.py check
.venv\Scripts\python text_to_sql.py run
```