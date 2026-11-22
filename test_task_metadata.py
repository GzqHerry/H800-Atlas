import json
import tempfile
import unittest
from pathlib import Path

import task_metadata as meta


class TaskMetadataTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.group = self.root/'train_any_name'
        self.checkpoint = self.group/'checkpoint-12'
        self.config = self.group/'launch_configs'
        self.dataset = self.root/'work/abcdef01-2345-6789-abcd-0123456789ab/dataset'
        self.write(self.config/'model.yaml', {'model': {'model_name_or_path': '/models/pi05_base', 'rlt_enabled': True,
                                                       'pi0_use_training_rtc': True, 'use_visual_memory': True}})
        self.write(self.config/'runtime.yaml', {'training': {'num_train_epochs': 6, 'per_device_train_batch_size': 16,
                                                           'gradient_accumulation_steps': 2},
                                                'runtime': {'objectives': {'action': 1}}})
        self.write(self.config/'accelerate.yaml', {'num_processes': 8})
        self.write(self.config/'data.yaml', {'datasets': [{'path': str(self.dataset)}]})
        self.write(self.dataset/'meta/info.json', {'robot_type': 'ARX_X5'})
        self.write(self.checkpoint/'trainer_state.json', {'num_train_epochs': 6})

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')

    def safe(self, path):
        path = path.resolve(strict=True)
        if self.root not in path.parents:
            raise PermissionError('outside test root')
        return path

    def describe(self):
        return meta.describe(self.group, [self.checkpoint], self.safe, self.safe)

    @unittest.skipIf(meta.yaml is None, 'PyYAML unavailable')
    def test_configuration_not_directory_spelling(self):
        m = self.describe()
        self.assertEqual([m[k] for k in ('epochs', 'gpus', 'batch', 'globalBatch')], [6, 8, 16, 256])
        self.assertEqual(m['base'], 'pi05_base')
        self.assertEqual(m['method'], 'SFT')
        self.assertEqual(m['robots'], ['ARX X5'])
        self.assertEqual(m['datasetIds'], ['abcdef01-2345-6789-abcd-0123456789ab'])

    @unittest.skipIf(meta.yaml is None, 'PyYAML unavailable')
    def test_saved_false_overrides_launch_true_and_inference_rtc(self):
        self.write(self.checkpoint/'config.json', {'rlt': {'enabled': False}, 'visual_memory': {'enabled': False},
                   'dit_config': {'generator_cfg': {'pi0_use_training_rtc': False, 'pi0_use_rtc': True}}})
        m = self.describe()
        self.assertIs(m['rlt'], False)
        self.assertIs(m['rtc'], False)
        self.assertIs(m['mem'], False)
        self.assertIs(meta.merge(m, {'rlt': True, 'mem': True, 'rtc': True})['rlt'], False)

    @unittest.skipIf(meta.yaml is None, 'PyYAML unavailable')
    def test_effective_arguments_and_mismatched_run(self):
        run = self.group/'wandb/run-20260101-good/files/config.yaml'
        self.write(run, {k: {'value': v} for k, v in {'output_dir': str(self.group), 'num_train_epochs': 9,
                   'per_device_train_batch_size': 4, 'world_size': 16, 'gradient_accumulation_steps': 3}.items()})
        self.write(self.group/'wandb/run-20260102-wrong/files/config.yaml',
                   {'output_dir': {'value': '/another/experiment'}, 'num_train_epochs': {'value': 1000}})
        m = self.describe()
        self.assertEqual([m[k] for k in ('epochs', 'gpus', 'batch', 'globalBatch')], [9, 16, 4, 192])

    @unittest.skipIf(meta.yaml is None, 'PyYAML unavailable')
    def test_multiple_robot_types_not_hardcoded(self):
        other = self.root/'other-dataset'
        self.write(other/'meta/info.json', {'robot_type': 'SO_101'})
        self.write(self.config/'data.yaml', {'datasets': [{'path': str(self.dataset)}, {'path': str(other)}]})
        self.assertEqual(self.describe()['robots'], ['ARX X5', 'SO 101'])

    def test_missing_config_does_not_invent_labels(self):
        m = meta.describe(self.root/'train_pi05_sft_rlt_arx_x5_deadbeef_e6_g8_b16', [], self.safe, self.safe)
        self.assertTrue(all(m[k] is None for k in ('base', 'method', 'epochs', 'batch', 'rlt', 'rtc', 'mem')))
        self.assertEqual(m['robots'], [])
        self.assertEqual(m['datasetIds'], [])

    def test_shell_is_never_executed(self):
        env = meta.assignments('RUN_ROOT=/safe\nCONFIG_ROOT="$RUN_ROOT/launch_configs"\nexport BATCH_SIZE=4 NUM_NODES=2\nBAD=$(touch /tmp/oops)\nBAD2=`id`')
        self.assertEqual(env['CONFIG_ROOT'], '/safe/launch_configs')
        self.assertNotIn('BAD', env)
        self.assertNotIn('BAD2', env)

    @unittest.skipIf(meta.yaml is None, 'PyYAML unavailable')
    def test_broken_yaml_and_unsafe_yaml(self):
        for raw in ('[broken:', '!!python/object/apply:os.system ["echo unsafe"]'):
            (self.config/'model.yaml').write_text(raw, encoding='utf-8')
            self.assertIsNone(self.describe()['base'])

    def test_verified_archive_uses_original_source_identity(self):
        archive = self.root/'archive/task'
        self.write(archive/'launch_configs/launch_manifest.json', {'output_dir': '/original/task',
                   'initialization_recipe': 'another_base', 'epochs': 7, 'gpus': 2, 'per_gpu_batch': 3})
        m = meta.describe(archive, [], self.safe, source_group='/original/task')
        self.assertEqual(m['base'], 'another_base')
        self.assertEqual(m['epochs'], 7)
        wrong = meta.describe(archive, [], self.safe, source_group='/different/task')
        self.assertIsNone(wrong['base'])


if __name__ == '__main__':
    unittest.main()
