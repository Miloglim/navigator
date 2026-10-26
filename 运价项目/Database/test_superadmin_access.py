"""Super-administrator visibility and capability regressions."""
import tempfile
import unittest
from pathlib import Path

import manualdb
import manual_server
from ops import admin_accounts
from core.agent_intake_contracts import AGENT_INTAKE_ROUTE
from core.contracts import ROLE_SUPERADMIN
from services.access_policy import ROLE_VIEWS, account_identity
from services.admin_accounts import security_request
from transport.access_rules import authorize


class SuperadminAccessTests(unittest.TestCase):
    def test_superadmin_receives_every_view(self):
        identity = account_identity({
            'user_id': 'usr_superadmin', 'name': 'root@example.com',
            'display_name': '超级管理员', 'role': ROLE_SUPERADMIN,
            'must_change_password': False,
        })
        self.assertEqual(identity['views'], list(ROLE_VIEWS[ROLE_SUPERADMIN]))
        self.assertEqual(identity['views'], ['sales', 'ops', 'config', 'account', 'monitor'])

    def test_superadmin_can_reach_every_registered_human_route(self):
        for method, routes in (('GET', manual_server.GET_ROUTES), ('POST', manual_server.POST_ROUTES)):
            for route in routes:
                if route == AGENT_INTAKE_ROUTE:
                    continue
                self.assertTrue(authorize(method, route, ROLE_SUPERADMIN)['ok'], f'{method} {route}')

    def test_sqlite_contract_accepts_superadmin(self):
        with tempfile.TemporaryDirectory(prefix='navigator-superadmin-') as directory:
            connection = manualdb.connect(Path(directory) / 'superadmin.db')
            try:
                manualdb.save_operator(connection, {
                    'name': 'root@example.com', 'role': ROLE_SUPERADMIN,
                    'active': 1, 'password_hash': 'fixture-hash',
                })
                row = connection.execute(
                    "SELECT role FROM operators WHERE name='root@example.com'"
                ).fetchone()
                self.assertEqual(row['role'], ROLE_SUPERADMIN)
            finally:
                connection.close()

    def test_regular_admin_cannot_grant_superadmin(self):
        with self.assertRaisesRegex(ValueError, '角色无效'):
            security_request({
                'action': 'role', 'role': ROLE_SUPERADMIN,
                '_role': 'admin',
            })
        self.assertEqual(security_request({
            'action': 'role', 'role': ROLE_SUPERADMIN,
            '_role': ROLE_SUPERADMIN,
        }), 'role')

    def test_regular_admin_cannot_edit_superadmin_profile(self):
        with tempfile.TemporaryDirectory(prefix='navigator-superadmin-profile-') as directory:
            connection = manualdb.connect(Path(directory) / 'superadmin-profile.db')
            try:
                saved = manualdb.save_operator(connection, {
                    'name': 'root@example.com', 'role': ROLE_SUPERADMIN,
                    'active': 1, 'password_hash': 'fixture-hash',
                })
                detail = admin_accounts.detail(connection, saved['user_id'], {})['data']
                with self.assertRaisesRegex(PermissionError, '仅超级管理员'):
                    admin_accounts.save_section(connection, {
                        'user_id': saved['user_id'], 'section': 'profile',
                        'fields': {'display_name': '普通管理员不可改'},
                        'expected_signature': detail['signature'],
                        '_actor': 'admin', '_actor_id': 'usr_admin', '_role': 'admin',
                    }, '2026-10-26 11:30:00')
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
