"""Portable inputs and matched context controls; no model or network calls."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from run_context_ablation import ControlledRouter, RoutingMismatch
from experiments.text_to_sql_top3.custom_input import prepare_custom
from experiments.text_to_sql_top3.schema import load_config, load_prepared_sql

ROOT = Path(__file__).resolve().parents[1]


class PortableReleaseTests(unittest.TestCase):
    def test_matched_router_retains_actual_score(self):
        router = Mock()
        router.score.return_value = {'probabilities': {'View_Door': .7, 'View_Wall': .2, 'View_Space': .1}}
        score = ControlledRouter(router, ['View_Door','View_Wall','View_Space']).score('Doors?')
        self.assertIs(score, router.score.return_value)
        router.score.assert_called_once()

    def test_changed_top3_is_stopped_before_sql(self):
        router = Mock()
        router.score.return_value = {'probabilities': {'View_Door': .1, 'View_Wall': .7, 'View_Space': .2}}
        with self.assertRaises(RoutingMismatch):
            ControlledRouter(router, ['View_Door','View_Wall','View_Space']).score('Doors?')

    def test_local_inputs_prepare_without_historical_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root/'model.sqlite'
            with closing(sqlite3.connect(db)) as con:
                con.execute('CREATE TABLE objects(ifc_id INTEGER, Name TEXT)')
                con.execute("INSERT INTO objects VALUES (1, 'Door 1')")
                con.execute('CREATE VIEW View_Door AS SELECT * FROM objects')
                con.commit()
            db.with_suffix('.build.json').write_text(json.dumps({'schema':'IFC4','views':{'View_Door':{'columns':['ifc_id','Name']}}}))
            question = root/'questions.csv'
            question.write_text('id,question,project,ifc_model,category\n1,How many doors?,demo,model,2\n')
            model, server = root/'fixture.gguf', root/'fixture-server.exe'
            model.write_bytes(b'unit-test-model');server.write_bytes(b'unit-test-server')
            backend = json.loads((ROOT/'configs/backend.example.json').read_text())
            backend.update(model_path=str(model),llama_server=str(server),cuda_dll_directory=str(root))
            backend_file = root/'backend.json';backend_file.write_text(json.dumps(backend))
            config = json.loads((ROOT/'configs/pipeline.example.json').read_text())
            config.update(question_csv=str(question),database_map={'demo/model':str(db)},backend_config=str(backend_file),prepared_directory=str(root/'prepared'))
            config_file = root/'pipeline.json';config_file.write_text(json.dumps(config))
            loaded = load_config(config_file)
            with patch('experiments.text_to_sql_top3.custom_input.read_metadata',return_value={}), patch('experiments.text_to_sql_top3.custom_input.label_pool',return_value=[{'code':'A','token_ids':[1]}]):
                prepare_custom(loaded,log=lambda _:None)
            manifest, inventory, cases, schemas = load_prepared_sql(loaded)
            self.assertEqual(manifest['input_mode'],'custom_live')
            self.assertEqual(cases[0]['question'],'How many doors?')
            self.assertEqual(schemas['demo/model']['View_Door'],['ifc_id','Name'])
            self.assertEqual(inventory['demo/model']['database']['path'],str(db.resolve()))


if __name__ == '__main__':unittest.main()
