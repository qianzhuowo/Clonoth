import copy
import unittest

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "napcat-security"
_spec = importlib.util.spec_from_file_location("napcat_ws_boundary", ROOT / "ws_boundary.py")
boundary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(boundary)

# Synthetic fixture, never used as a deployed credential.
TOKEN = 'ab' * 32


class BoundaryTests(unittest.TestCase):
    def test_environment_preserves_unrelated_settings_and_replaces_aliases(self):
        original = '# comment\nHOST=0.0.0.0\nPORT=8080\nCLONOTH_BASE_URL=http://localhost:7000\nONEBOT_ACCESS_TOKEN=old\n'
        result = boundary.patch_env(original, TOKEN)
        self.assertIn('CLONOTH_BASE_URL=http://localhost:7000', result)
        self.assertIn('# comment', result)
        self.assertNotIn('=old', result)
        self.assertEqual(result.count('HOST='), 1)
        self.assertIn(f'HOST={boundary.GATEWAY}\n', result)
        for key in boundary.TOKEN_KEYS:
            self.assertIn(f'{key}={TOKEN}\n', result)
        self.assertEqual(boundary.patch_env(result, TOKEN), result)

    def test_environment_rejects_ambiguity(self):
        for text in ('HOST=0.0.0.0\n', 'HOST=x\nhost=y\nPORT=8080\n'):
            with self.assertRaises(ValueError):
                boundary.patch_env(text, TOKEN)
        for token in ('', 'x' * 64, 'a' * 63, 'a' * 64 + '\nHOST=x'):
            with self.assertRaises(ValueError):
                boundary.patch_env('HOST=x\nPORT=8080\n', token)

    def config(self):
        return {'network': {'websocketClients': [
            {'name': 'clonoth', 'enable': True, 'url': boundary.OLD_URL,
             'token': '', 'reconnectInterval': 3000}], 'websocketServers': []},
                'parseMultMsg': False}

    def test_client_is_copied_and_unrelated_options_preserved(self):
        config = self.config()
        before = copy.deepcopy(config)
        result = boundary.patch_onebot(config, TOKEN)
        self.assertEqual(config, before)
        client = result['network']['websocketClients'][0]
        self.assertEqual(client['url'], boundary.URL)
        self.assertEqual(client['token'], TOKEN)
        self.assertEqual(client['reconnectInterval'], 3000)
        self.assertFalse(result['parseMultMsg'])
        self.assertEqual(boundary.patch_onebot(result, TOKEN), result)

    def test_unexpected_endpoints_rejected(self):
        config = self.config()
        config['network']['websocketServers'].append({'enable': True})
        with self.assertRaises(ValueError):
            boundary.patch_onebot(config, TOKEN)
        for field, value in [('name', 'other'), ('url', 'ws://example.org'), ('enable', False)]:
            config = self.config()
            config['network']['websocketClients'][0][field] = value
            with self.assertRaises(ValueError):
                boundary.patch_onebot(config, TOKEN)

    def test_subnet_overlap(self):
        boundary.check_subnet_conflicts(['172.17.0.0/16', '10.7.0.0/22', '0.0.0.0/0', '::/0'])
        for subnet in ['172.30.0.0/16', boundary.SUBNET, boundary.CLIENT + '/32']:
            with self.assertRaises(ValueError):
                boundary.check_subnet_conflicts([subnet])

    def test_exact_reversible_rules_and_correct_insertion_order(self):
        rules = boundary.input_rules()
        self.assertEqual(rules[0][-1], 'ACCEPT')
        self.assertIn(boundary.BRIDGE, rules[1])
        self.assertIn(boundary.CLIENT + '/32', rules[1])
        self.assertIn('REJECT', rules[-1])
        applied = boundary.rule_commands('apply')
        removed = boundary.rule_commands('remove')
        self.assertEqual([cmd[6:] for cmd in reversed(applied)], rules)
        self.assertEqual([cmd[5:] for cmd in removed], rules)
        for cmd in applied + removed:
            self.assertIn(boundary.GATEWAY + '/32', cmd)
            self.assertIn('8080', cmd)
            self.assertIn(boundary.COMMENT, cmd)
            self.assertNotIn('DOCKER-USER', cmd)
            self.assertNotIn('-F', cmd)
        with self.assertRaises(ValueError):
            boundary.rule_commands('flush')


if __name__ == '__main__':
    unittest.main()
