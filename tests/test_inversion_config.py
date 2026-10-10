import importlib
import tempfile
import unittest
from pathlib import Path


class ConfigTests(unittest.TestCase):
    def test_example_is_disabled_without_resolving_secrets(self):
        try: config = importlib.import_module('backend.inversion.config')
        except ModuleNotFoundError: self.fail('optional inversion configuration required')
        self.assertIsNone(config.load_inversion_config(Path('config/inversion.example.toml')))

    def test_unknown_configuration_is_sanitized(self):
        from backend.inversion.config import load_inversion_config, ConfigurationError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[inversion]\nenabled=true\npassword="private-value"\n', encoding='utf-8')
            with self.assertRaises(ConfigurationError) as caught: load_inversion_config(path)
            self.assertNotIn('private-value', str(caught.exception))
