"""End-to-end PostgreSQL adapter tests against an isolated temporary schema."""
from __future__ import annotations

import os
import json
import secrets
import tempfile
import threading
import unittest
import uuid
import urllib.request
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

import manualdb
import manual_server
import postgresdb
from ops.postgres_migrate import migrate
from ops.postgres_sync import load_dsn

POSTGRES_TEST_ENV = "FREIGHT_POSTGRES_INTEGRATION_TEST"


@unittest.skipUnless(os.environ.get(POSTGRES_TEST_ENV) == "1", "requires an explicit local PG test opt-in")
class PostgresRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_dsn = load_dsn(postgresdb.DEFAULT_ENV_FILE)
        cls.schema = "freight_test_" + uuid.uuid4().hex[:12]
        with psycopg.connect(cls.base_dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.test_dsn = make_conninfo(cls.base_dsn, options=f"-c search_path={cls.schema}")
        try:
            result = migrate(manualdb.DEFAULT_DB, cls.test_dsn,
                             include_business_data=False, replace_target=False)
            if not result["ok"]:
                raise AssertionError("configuration migration into test schema failed")
            fixtures = postgresdb.Connection(cls.test_dsn)
            try:
                cls.route_fixtures = [dict(row) for row in fixtures.execute('SELECT * FROM routes')]
            finally:
                fixtures.close()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "schema"):
            return
        if hasattr(cls, "test_dsn"):
            postgresdb.close_pool(cls.test_dsn)
        with psycopg.connect(cls.base_dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        connection = postgresdb.Connection(self.test_dsn)
        try:
            for table in ("auth_sessions", "item_feedback", "freight_items", "pending_records", "pending_shadow", "rule_candidates", "account_notifications", "announcements"):
                connection.execute("DELETE FROM " + table)
            # Profile tests replace routes; restore baseline configuration so quote
            # and legacy snapshot tests do not inherit a prior test's master edits.
            manualdb.sync_rows(connection, 'routes', self.route_fixtures, commit=False)
            connection.commit()
            manualdb.invalidate_master()
        finally:
            connection.close()

    def test_sales_sort_global_pagination(self):
        from test_sales_sort import verify_sales_sort
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_sales_sort(self, connection)
        finally:
            connection.close()
            manualdb._GROUPED_CACHE.clear()

    def test_grouped_query_pragmas_do_not_abort_transaction(self):
        connection = postgresdb.Connection(self.test_dsn)
        try:
            result = manualdb.query_grouped(
                connection, manualdb.get_master(connection), "rates", {"match": "web", "limit": "1"})
            self.assertIn("groups", result)
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
        finally:
            connection.close()
            manualdb._GROUPED_CACHE.clear()

    def test_45_container_filters(self):
        from test_container_features import verify_container_features
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_container_features(self, connection)
        finally:
            connection.close()
            manualdb._GROUPED_CACHE.clear()

    def test_port_group_aliases(self):
        from test_port_group_aliases import verify_group_aliases
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_group_aliases(self, connection)
        finally:
            connection.close()
            manualdb.invalidate_master()
            manualdb._GROUPED_CACHE.clear()

    def test_master_resolution_read_only(self):
        from core.master_resolution_contracts import MASTER_RESOLVE_ROUTE
        from transport.master_resolution_routes import build_master_resolution_routes
        connection = postgresdb.Connection(self.test_dsn)
        try:
            master = manualdb.Master(connection)
            name = next(port['name'] for port in master.ports
                        if master.name_resolver.resolve('pod',port['name'])['status'] == 'resolved')
            tables = ('ports','port_groups','alias_scope','freight_items','pending_records')
            before = {table:connection.execute('SELECT COUNT(*) AS n FROM '+table).fetchone()['n'] for table in tables}
            routes = build_master_resolution_routes(lambda:postgresdb.Connection(self.test_dsn), manualdb.get_master)
            result = routes[MASTER_RESOLVE_ROUTE]({'items':[{'type':'pod','value':name}, {'type':'pod','value':'UNKNOWN_RESOLUTION_FIXTURE'}]})
            self.assertEqual(result['data'][0]['canonical'],name)
            self.assertEqual(result['data'][1]['status'],'unknown')
            after = {table:connection.execute('SELECT COUNT(*) AS n FROM '+table).fetchone()['n'] for table in tables}
            self.assertEqual(before,after)
        finally:
            connection.close()

    def test_item_feedback_isolated_schema(self):
        from ops import item_feedback
        connection = postgresdb.Connection(self.test_dsn)
        try:
            connection.execute("INSERT INTO freight_items(entry_id,content_key,record_type,pol,pod,carrier,container_type,price_usd,status) "
                               "VALUES(?,?,?,?,?,?,?,?,?)",
                               ("frt_pgfeedback", "pgfeedback", "rates", "SHA", "HAM", "MSC", "40HQ", 2650, "当前生效"))
            connection.commit()
            result = item_feedback.create(connection, "frt_pgfeedback", {"price_usd": "2550"},
                                          {"price_usd": "2650"}, "客户依据", "usr_pg", "销售", manualdb.now_cn())
            self.assertEqual(item_feedback.read(connection, "frt_pgfeedback")["feedback"][0]["suggested_value"], "2550")
            self.assertEqual(connection.execute("SELECT price_usd FROM freight_items WHERE entry_id='frt_pgfeedback'").fetchone()[0], 2650)
            item_feedback.dismiss(connection, result["submission_id"], "withdraw", "usr_pg", "sales", manualdb.now_cn())
            self.assertFalse(item_feedback.read(connection, "frt_pgfeedback")["feedback"])
        finally:
            connection.close()

    def test_account_profile_http(self):
        from test_account_profile import verify_profile
        verify_profile(self, lambda: postgresdb.pooled_connect(self.test_dsn))

    def test_account_growth_http(self):
        from test_account_growth import verify_growth
        verify_growth(self, lambda: postgresdb.pooled_connect(self.test_dsn))

    def test_account_selfservice_http_lifecycle(self):
        from test_account_selfservice import verify_selfservice
        verify_selfservice(self, lambda: postgresdb.pooled_connect(self.test_dsn))

    def test_notice_board_http(self):
        from test_notices import verify_notices
        verify_notices(self, lambda: postgresdb.pooled_connect(self.test_dsn))

    def test_notice_multi_target_scopes_http(self):
        from test_notices import verify_notice_scopes
        verify_notice_scopes(self, lambda: postgresdb.pooled_connect(self.test_dsn))

    def test_notice_archive_migration(self):
        from transport.notice_routes import build_notice_routes
        schema = 'notice_archive_' + uuid.uuid4().hex[:12]
        with psycopg.connect(self.base_dsn, autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        archive_dsn = make_conninfo(self.base_dsn, options='-c search_path='+schema)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'archive.db'
                connection = manualdb.connect(path)
                connection.close()
                _, post = build_notice_routes(lambda:manualdb.connect(path), manualdb.now_cn, lambda *args:None)
                result = post['/api/notices/save']({'title':'Archived notice','body_html':'<p>Archive</p>',
                    '_actor':'fixture','_actor_id':'fixture','_actor_display':'Fixture','_role':'sales'})
                self.assertTrue(migrate(path,archive_dsn,include_business_data=True,replace_target=False)['ok'])
                target = postgresdb.Connection(archive_dsn)
                try:
                    self.assertEqual(target.execute('SELECT id FROM announcements').fetchone()[0],result['data']['id'])
                    snapshot = manualdb.export_all(target)
                    manualdb.load_snapshot(target,snapshot)
                    self.assertEqual(target.execute('SELECT COUNT(*) FROM announcements').fetchone()[0],1)
                finally:
                    target.close()
                connection = manualdb.connect(path)
                try:
                    connection.execute('DROP TABLE announcements'); connection.commit()
                finally:
                    connection.close()
                legacy = migrate(path,archive_dsn,include_business_data=True,replace_target=True)
                self.assertTrue(legacy['ok'])
                self.assertEqual(legacy['tables']['announcements']['target_rows'],0)
        finally:
            with psycopg.connect(self.base_dsn,autocommit=True) as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    def test_notice_expiry_schema_upgrade(self):
        connection = postgresdb.Connection(self.test_dsn)
        try:
            connection.execute('ALTER TABLE announcements DROP COLUMN expires_on')
            connection.execute("INSERT INTO announcements(id,title,body_html,tags_json,author_id,author_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ('legacy','Old notice','<p>Kept</p>','[]','owner','Owner','2026-09-29','2026-09-29'))
            connection.commit()
            connection.ensure_identity_schema()
            row = connection.execute('SELECT title,expires_on FROM announcements WHERE id=?',('legacy',)).fetchone()
            self.assertEqual((row['title'],row['expires_on']),('Old notice',None))
        finally:
            connection.close()

    def test_pooled_connections_reset_failed_and_uncommitted_transactions(self):
        first = postgresdb.pooled_connect(self.test_dsn)
        raw = first._conn
        first.execute("INSERT INTO operators(name,user_id,role,active) VALUES(?,?,?,?)", ("pool-uncommitted", "usr_pool", "sales", 1))
        first.close()
        first.close()  # Releasing twice must not lend the same socket twice.
        second = postgresdb.pooled_connect(self.test_dsn)
        self.assertIs(second._conn, raw)
        self.assertIsNone(second.execute("SELECT name FROM operators WHERE name=?", ("pool-uncommitted",)).fetchone())
        with self.assertRaises(psycopg.Error):
            second.execute("SELECT 1/0")
        second.close()
        third = postgresdb.pooled_connect(self.test_dsn)
        self.assertEqual(third.execute("SELECT 1").fetchone()[0], 1)
        third._conn.close()  # Simulate a disconnected idle socket.
        third.close()
        fourth = postgresdb.pooled_connect(self.test_dsn)
        self.assertIsNot(fourth._conn, raw)
        self.assertEqual(fourth.execute("SELECT 1").fetchone()[0], 1)
        fourth.close()

    def test_single_agent_correction_then_failed_pending_for_human_http(self):
        import sys
        from unittest.mock import patch
        scripts = Path(__file__).resolve().parent.parent / 'Skill/freight-rate-heartbeat/scripts'
        with patch.object(sys, 'path', [str(scripts), *sys.path]):
            import pipeline as pipeline
        with tempfile.TemporaryDirectory(prefix='freight-single-repair-') as directory:
            original = dict(manual_server.CFG)
            manual_server.CFG['token'] = 'frtm_' + secrets.token_hex(16)
            def connect():
                return postgresdb.Connection(self.test_dsn)
            with patch.object(manual_server, 'conn', side_effect=connect):
                server = manual_server.FreightHTTPServer(('127.0.0.1', 0), manual_server.Handler)
                thread = threading.Thread(target=server.serve_forever); thread.start()
                base = 'http://127.0.0.1:%d' % server.server_port
                def call(path, payload=None):
                    body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
                    request = urllib.request.Request(base + path, data=body, headers={'x-push-token': manual_server.CFG['token']})
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return json.load(response)
                try:
                    record = {'起运港':'天津','目的港':'Unmatched isolated fixture port','船司':'达飞','柜型':'20GP',
                              '海运费USD':200,'有效期船期':'10.1-10.14','消息ID':'single-repair-fixture',
                              '消息原文':'isolated quote','消息时间':'2026-09-28 17:00','来源群':'土耳其价格更新','发送人':'fixture'}
                    state = {'stage':'PRECHECK','submit_done':[],'new_ids':['single-repair-fixture'],
                             'submit_queue':[{'kind':'rates','record':record,'confidence':'low'}]}
                    with patch.object(pipeline, 'WORK', directory),patch.object(pipeline, 'publisher_policy_changed', return_value=False), \
                         patch.object(pipeline, 'commit_watermark') as watermark,patch.object(pipeline, 'emit'), \
                         patch.object(pipeline.db_client, 'master', side_effect=lambda:call('/api/master?full=1')), \
                         patch.object(pipeline.db_client, 'validate', side_effect=lambda k,r:call('/api/validate', {'kind':k,'records':r})) as validate, \
                         patch.object(pipeline.db_client, 'submit', side_effect=lambda k,r:call('/api/pending', {'kind':k,'origin':'agent','records':r})) as submit:
                        pipeline.advance_precheck('fixture',state)
                        self.assertEqual(state['stage'],pipeline.AGENT_REPAIR_STAGE);submit.assert_not_called()
                        suffix=pipeline.P.sfx();source=Path(directory)/('msgs_fixture'+suffix);source.mkdir()
                        (source/'turkey_price.json').write_text(json.dumps([{'id':record['消息ID'],'t':record['消息时间'],'s':'fixture','c':'isolated quote'}]),encoding='utf-8')
                        output=Path(directory)/('llm_out_fixture'+suffix);output.mkdir()
                        corrected={k:v for k,v in record.items() if k not in ('消息ID','消息原文','消息时间','来源群','发送人')}
                        corrected['目的港']='Still unmatched after one correction'
                        corrected['_table']='rate'
                        (output/'fixture.json').write_text(json.dumps({'msg_id':record['消息ID'],'group':'turkey_price','decision':'records','rule_basis':'extraction-rules.md#1','confidence':'low','records':[corrected]}),encoding='utf-8')
                        with patch.object(pipeline.subprocess,'run'),patch.object(pipeline,'_load_draft',return_value=[]):
                            pipeline.step_build('fixture',state)
                        self.assertEqual(state['stage'],'SUBMIT');pipeline.advance_submit('fixture',state)
                        self.assertEqual(validate.call_count,1);self.assertEqual(submit.call_count,1)
                        self.assertEqual(state['stage'],'DONE');self.assertEqual(state['records_check_failed'],1)
                        pending=call('/api/pending?status=all&message_id=single-repair-fixture')
                        self.assertEqual(pending['total'],1);row=pending['rows'][0]
                        self.assertEqual(row['review_status'],'awaiting');self.assertEqual(row['check_status'],'failed')
                        self.assertTrue(row['errors']);self.assertEqual(row['payload']['目的港'],corrected['目的港'])
                        pipeline.advance_submit('fixture',state)
                        self.assertEqual(submit.call_count,1);self.assertEqual(validate.call_count,1)
                        self.assertTrue(watermark.called)
                        connection=connect()
                        try:self.assertEqual(connection.execute('SELECT count(*) FROM freight_items').fetchone()[0],0)
                        finally:connection.close()
                finally:
                    server.shutdown();thread.join(timeout=10);server.server_close()
                    manual_server.CFG.clear();manual_server.CFG.update(original);manualdb.invalidate_master()

    def test_lan_accounts_scope_images_and_throttle(self):
        from test_lan_accounts import verify_lan_accounts
        verify_lan_accounts(self, lambda: postgresdb.Connection(self.test_dsn))

    def test_locode_lists_and_service_coexistence(self):
        from test_locode_lists import verify_locode_lists
        connection=postgresdb.Connection(self.test_dsn)
        try:verify_locode_lists(self,connection)
        finally:connection.close();manualdb.invalidate_master()

    def test_sailing_identity(self):
        from test_sailing_identity import verify_sailing_identity
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_sailing_identity(self, connection)
        finally:
            connection.close()

    def test_agent_repair(self):
        from test_agent_repair import verify_agent_repair
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_agent_repair(self, connection)
        finally:
            connection.close()
            manualdb.invalidate_master()

    def test_account_ownership(self):
        from test_account_ownership import verify_ownership
        connection = postgresdb.Connection(self.test_dsn)
        try:verify_ownership(self,connection)
        finally:connection.close();manualdb.invalidate_master()

    def test_admin_terminal_http(self):
        from test_admin_terminal import verify_terminal
        verify_terminal(self,lambda:postgresdb.Connection(self.test_dsn))

    def test_review_scope_and_metrics(self):
        from test_review_mechanisms import verify_scope, verify_stats
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_scope(self, connection)
            verify_stats(self, connection)
        finally:
            connection.close()
            manualdb.invalidate_master()

    def test_source_scoped_pending(self):
        from test_agent_repair import verify_source_scoped_pending
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_source_scoped_pending(self, connection)
        finally:
            connection.close()
            manualdb.invalidate_master()

    def test_ops_id_query_filters(self):
        from test_ops_id_filters import verify_ops_id_filters
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_ops_id_filters(self, connection)
        finally:
            connection.close()

    def test_quote_variant_products(self):
        from test_quote_variants import verify_quote_variants
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_quote_variants(self, connection)
        finally:
            connection.close()

    def test_account_role_http_flow(self):
        from test_account_auth import verify_account_http
        verify_account_http(self, lambda: postgresdb.Connection(self.test_dsn))

    def test_account_legacy_postgres_schema_upgrade(self):
        connection = postgresdb.Connection(self.test_dsn)
        try:
            connection.execute("DELETE FROM auth_sessions")
            connection.execute("DELETE FROM operators")
            connection.execute("ALTER TABLE operators DROP COLUMN user_id")
            connection.execute("ALTER TABLE operators DROP CONSTRAINT operators_role_check")
            connection.execute("ALTER TABLE operators ADD CONSTRAINT operators_role_check CHECK(role IN ('admin','operator'))")
            connection.execute("INSERT INTO operators(name,role,password_hash) VALUES(?,?,?)", ("legacy-pg-admin", "admin", "fixture-hash"))
            connection.commit()
            self.assertEqual(manualdb.ensure_account_schema(connection)["data"]["identities_backfilled"], 1)
            first = connection.execute("SELECT user_id FROM operators WHERE name='legacy-pg-admin'").fetchone()[0]
            self.assertEqual(manualdb.ensure_account_schema(connection)["data"]["identities_backfilled"], 0)
            manualdb.save_operator(connection, {"name": "legacy-pg-sales", "role": "sales"})
            self.assertEqual(connection.execute("SELECT user_id FROM operators WHERE name='legacy-pg-admin'").fetchone()[0], first)
        finally:
            connection.close()

    def test_reject_explicit_ids(self):
        from test_pending_reject import verify_reject_ids
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_reject_ids(self, connection)
        finally:
            connection.close()

    def test_pending_delete_states(self):
        from test_pending_reject import verify_pending_delete
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_pending_delete(self, connection)
        finally:
            connection.close()

    def test_percent_literals_and_parameters(self):
        connection = postgresdb.Connection(self.test_dsn)
        try:
            row = connection.execute("SELECT '100%' AS percent, '%s' AS literal, 7 % 4 AS remainder").fetchone()
            self.assertEqual(tuple(row[k] for k in ('percent', 'literal', 'remainder')), ('100%', '%s', 3))
            row = connection.execute("SELECT ? AS value WHERE ? LIKE '%(%' AND ? NOT LIKE '%)'",
                                     ('bound%value', 'Test (BR', 'Test (BR')).fetchone()
            self.assertEqual(row['value'], 'bound%value')
            self.assertIsNone(connection.execute("SELECT ? WHERE ? LIKE '%(%' AND ? NOT LIKE '%)'",
                                                ('closed', 'Test (BR)', 'Test (BR)')).fetchone())
            row = connection.execute("SELECT 'it''s 100%?' AS literal, ? AS value", ('%?',)).fetchone()
            self.assertEqual(row['literal'], "it's 100%?")
            self.assertEqual(row['value'], '%?')
        finally:
            connection.close()

    def test_full_audit_with_unclosed_port_name(self):
        from unittest.mock import patch
        connection = postgresdb.Connection(self.test_dsn)
        try:
            connection.execute("INSERT INTO ports(name, name_en, active, source, locode_ref) VALUES(?,?,?,?,?)",
                               ('AUDIT test (BR', 'AUDIT test (BR', 0, 'test', ''))
            connection.execute("INSERT INTO ports(name, name_en, active, source, locode_ref) VALUES(?,?,?,?,?)",
                               ('AUDIT test (BR)', 'AUDIT test (BR)', 0, 'test', ''))
            connection.commit()
        finally:
            connection.close()
        manualdb.invalidate_master()
        try:
            with patch.object(manual_server, 'conn', side_effect=lambda: postgresdb.Connection(self.test_dsn)):
                report = manual_server.act_audit({})
            self.assertIn('master_health', report)
            issue = next(i for i in report['master_health']['issues'] if i['code'] == '规范名括号不闭合')
            self.assertIn('AUDIT test (BR', issue['samples'])
            self.assertNotIn('AUDIT test (BR)', issue['samples'])
        finally:
            manualdb.invalidate_master()

    def test_read_validate_stage_approve_and_duplicate_guard(self):
        connection = postgresdb.Connection(self.test_dsn)
        try:
            master = manualdb.get_master(connection)
            self.assertGreater(len(master.ports), 1000)
            record = {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "航线": "南美东岸",
                      "柜型": "20GP", "海运费USD": "200", "有效期起": "2026-09-28",
                      "有效期止": "2026-10-28", "消息原文": "PostgreSQL integration test"}
            self.assertTrue(manualdb.validate_record(connection, master, "rates", record)["ok"])
            ids = manualdb.stage_pending(connection, master, "rates", [record])
            self.assertEqual(len(ids), 1)
            self.assertEqual(manualdb.list_pending(connection)["total"], 1)
            pending_entry_id = manualdb.list_pending(connection)["rows"][0]["entry_id"]
            approved = manualdb.approve_pending(connection, master, ids[0], reviewed_by="test")
            self.assertTrue(approved["ok"], approved)
            released = manualdb.query(connection, master, "rates", {})
            self.assertEqual(released["total"], 1)
            self.assertEqual(approved["entry_id"], pending_entry_id)
            self.assertEqual(released["rows"][0]["entry_id"], pending_entry_id)
            self.assertFalse(manualdb.approve_pending(connection, master, ids[0], reviewed_by="test")["ok"])
        finally:
            connection.close()

    def test_manual_write_generates_unique_entry_ids(self):
        connection = postgresdb.Connection(self.test_dsn)
        try:
            master = manualdb.get_master(connection)
            record = {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "航线": "南美东岸",
                      "柜型": "20GP", "海运费USD": "200", "有效期起": "2026-09-28",
                      "有效期止": "2026-10-28", "消息原文": "manual entry id regression"}
            result = manualdb.push_with_review(connection, master, "rates", [record])
            self.assertFalse(result["rejected"], result)
            self.assertEqual(result["accepted"], 1)
            first = manualdb.query(connection, master, "rates", {})["rows"][0]["entry_id"]
            self.assertRegex(first, r"^frt_[0-9a-f]{32}$")
            result = manualdb.push_with_review(connection, master, "rates", [dict(
                record, **{"目的港": "Itajai/Recife", "消息原文": "expanded manual entry id regression"})])
            self.assertFalse(result["rejected"], result)
            self.assertEqual(result["accepted"], 2)
            rows = manualdb.query(connection, master, "rates", {})["rows"]
            self.assertEqual(len({r["entry_id"] for r in rows}), len(rows))
        finally:
            connection.close()

    def test_sales_advanced_filter_boundaries(self):
        from test_sales_filters import verify_sales_filters
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_sales_filters(self, connection, manualdb.get_master(connection))
        finally:
            connection.close()

    def test_port_route_membership_flow(self):
        from test_port_routes import verify_port_route_flow
        connection = postgresdb.Connection(self.test_dsn)
        try:
            manualdb.invalidate_master()
            verify_port_route_flow(self, connection)
        finally:
            connection.close()
            manualdb.invalidate_master()

    def test_read_only_sales_filters_and_metadata(self):
        connection = postgresdb.Connection(self.test_dsn)
        try:
            master = manualdb.get_master(connection)
            self.assertEqual(manualdb.query(connection, master, "rates", {"valid": "2026-09-28"})["total"], 0)
            self.assertEqual(manualdb.query_grouped(connection, master, "rates", {})["total"], 0)
            self.assertIn("ports", manualdb.master_payload(connection))
            self.assertIn("route", manualdb.options(connection)["rates"])
            self.assertIn("freight_items", manualdb.export_all(connection))
        finally:
            connection.close()

    def test_oversized_source_never_splits_across_pages(self):
        from test_pending_source_pages import verify_whole_group_pages
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_whole_group_pages(self, connection)
        finally:
            connection.close()

    def test_source_view_filters_and_pages_whole_messages(self):
        connection = postgresdb.Connection(self.test_dsn)
        try:
            master = manualdb.get_master(connection)
            base = {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "航线": "南美东岸",
                    "柜型": "20GP", "海运费USD": "200", "有效期起": "2026-09-28",
                    "有效期止": "2026-10-28", "消息原文": "source view regression"}
            records = [dict(base), dict(base, **{"消息ID": "", "目的港": "Itajai"}),
                       dict(base, **{"消息ID": "123", "目的港": "Recife"}),
                       dict(base, **{"消息ID": "mixed", "目的港": "Suape"}),
                       dict(base, **{"消息ID": "mixed", "目的港": "INVALID-TEST-PORT"})]
            ids = []
            for record in records:
                ids.extend(manualdb.stage_pending(connection, master, "rates", [record]))
            before = [dict(r) for r in connection.execute(
                "SELECT id,check_status,review_status FROM pending_records ORDER BY id")]
            failed = manualdb.list_pending_sources(connection, check="failed", limit=1)
            self.assertEqual(failed["total"], 1)
            self.assertEqual(failed["record_total"], 2)
            self.assertEqual({r["id"] for r in failed["rows"]}, set(ids[-2:]))
            self.assertEqual({r["check_status"] for r in failed["rows"]}, {"passed", "failed"})
            self.assertEqual(manualdb.list_pending(connection, check="failed")["total"], 1)
            passed = manualdb.list_pending_sources(connection, check="passed")
            self.assertEqual(passed["total"], 3)
            self.assertEqual(passed["record_total"], 3)
            self.assertTrue(all(r["check_status"] == "passed" for r in passed["rows"]))
            first = manualdb.list_pending_sources(connection, limit=1)
            second = manualdb.list_pending_sources(connection, limit=1, offset=1)
            self.assertEqual(first["total"], 4)
            self.assertEqual(len(first["rows"]), 2)
            self.assertFalse({r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]})
            identity = failed["rows"][0]["entry_id"]
            by_id = manualdb.list_pending_sources(connection, entry_id=identity)
            self.assertEqual({r["id"] for r in by_id["rows"]}, set(ids[-2:]))
            self.assertEqual(manualdb.list_pending_sources(connection, message_id="msg//mixed==")["total"], 1)
            self.assertEqual(manualdb.list_pending_sources(connection, message_id="missing")["rows"], [])
            self.assertEqual([dict(r) for r in connection.execute(
                "SELECT id,check_status,review_status FROM pending_records ORDER BY id")], before)
        finally:
            connection.close()

    def test_bulk_approval_and_group_filters(self):
        connection = postgresdb.Connection(self.test_dsn)
        try:
            master = manualdb.get_master(connection)
            base = {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "航线": "南美东岸",
                    "柜型": "20GP", "海运费USD": "200", "有效期起": "2026-09-28",
                    "有效期止": "2026-10-28", "消息原文": "bulk approval regression"}
            records = [dict(base), dict(base, **{"消息ID": "", "目的港": "Itajai", "海运费USD": "201"}),
                       dict(base, **{"消息ID": 123, "目的港": "Recife", "海运费USD": "202"}),
                       dict(base, **{"消息ID": "mixed", "目的港": "Suape", "海运费USD": "203"}),
                       dict(base, **{"消息ID": "mixed", "目的港": "INVALID-TEST-PORT"})]
            ids = []
            for record in records:
                ids.extend(manualdb.stage_pending(connection, master, "rates", [record]))
            self.assertEqual(len(ids), len(records))
            passed = manualdb.list_pending(connection, check="passed", group_aware=True)
            self.assertEqual(passed["total"], 3, [dict(r) for r in connection.execute(
                "SELECT id,check_status,errors FROM pending_records")])
            failed = manualdb.list_pending(connection, check="failed", group_aware=True)
            self.assertEqual(failed["total"], 2)
            pending_entry_ids = {r["entry_id"] for r in manualdb.list_pending(
                connection, check="passed", whole_passed=True)["rows"]}
            result = manualdb.approve_all_pending(connection, master, reviewed_by="test")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["approved"], 3)
            self.assertEqual(result["held_by_group"], 1)
            self.assertEqual(result["remaining_passed"], 0)
            released = manualdb.query(connection, master, "rates", {})
            self.assertEqual(released["total"], 3)
            self.assertEqual({r["entry_id"] for r in released["rows"]}, pending_entry_ids)
            self.assertEqual(manualdb.approve_all_pending(connection, master)["approved"], 0)
            statuses = {r["id"]: (r["review_status"], r["check_status"]) for r in
                        connection.execute("SELECT id, review_status, check_status FROM pending_records")}
            self.assertEqual(statuses[ids[-2]], ("awaiting", "passed"))
            self.assertEqual(statuses[ids[-1]], ("awaiting", "failed"))
        finally:
            connection.close()

    def test_whole_source_bulk_gate_postgres(self):
        from test_bulk_whole_groups import verify_group_gate
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_group_gate(self, connection)
        finally:
            connection.close()

    def test_atomic_bulk_recheck_postgres(self):
        from test_bulk_whole_groups import verify_atomic_recheck
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_atomic_recheck(self, connection)
        finally:
            connection.close()

    def test_bulk_ownership_gate_postgres(self):
        from test_bulk_whole_groups import verify_ownership_gate
        connection = postgresdb.Connection(self.test_dsn)
        try:
            verify_ownership_gate(self, connection)
        finally:
            connection.close()

    def test_http_whole_group_count_and_release_postgres(self):
        from test_bulk_whole_groups import verify_http_bulk
        verify_http_bulk(self, lambda: postgresdb.Connection(self.test_dsn))

    def test_http_agent_pending_login_approval_and_sales_read(self):
        password = secrets.token_urlsafe(24)
        connection = postgresdb.Connection(self.test_dsn)
        try:
            manualdb.save_operator(connection, {
                "name": "postgres-test-admin", "role": "admin", "active": 1,
                "password_hash": manual_server.password_hash(password),
            })
        finally:
            connection.close()
        with tempfile.TemporaryDirectory(prefix="freight-pg-http-") as directory:
            env_file = Path(directory) / "postgres.env"
            env_file.write_text("FREIGHT_POSTGRES_DSN=" + self.test_dsn, encoding="ascii")
            original = dict(manual_server.CFG)
            manual_server.CFG.update(backend="postgres", postgres_env_file=str(env_file),
                                     token="frtm_" + secrets.token_hex(16))
            server = manual_server.FreightHTTPServer(("127.0.0.1", 0), manual_server.Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            base = "http://127.0.0.1:%d" % server.server_port

            def call(path, payload=None, headers=None):
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
                request = urllib.request.Request(base + path, data=data,
                                                 headers=headers or {"x-push-token": manual_server.CFG["token"]})
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.load(response), response.headers

            try:
                self.assertTrue(call("/api/bootstrap")[0]["ok"])
                self.assertEqual(call("/api/stats")[0]["ratesTotal"], 0)
                record = {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "航线": "南美东岸",
                          "柜型": "20GP", "海运费USD": "200", "有效期起": "2026-09-28",
                          "有效期止": "2026-10-28", "消息ID": "msg//http-id-group==", "消息原文": "PostgreSQL HTTP integration test"}
                self.assertEqual(call("/api/validate", {"kind": "rates", "records": [record]})[0]["good"], 1)
                staged, _ = call("/api/pending", {"kind": "rates", "origin": "agent", "records": [record]},
                                 {"x-push-token": manual_server.CFG["token"]})
                self.assertEqual(staged["staged"], 1)
                self.assertTrue(staged['coverage']['ok'])
                self.assertEqual(staged['coverage']['expected_unique'],1)
                by_group, _ = call("/api/pending?message_id=http-id-group&status=all",
                                   headers={"x-push-token": manual_server.CFG["token"]})
                self.assertEqual(by_group["total"], 1)
                source_page, _ = call("/api/pending?view=source&limit=1&id_query=http-id-group",
                                      headers={"x-push-token": manual_server.CFG["token"]})
                self.assertEqual(source_page["pagination_unit"], "groups")
                self.assertEqual(source_page["total"], 1)
                self.assertEqual(source_page["record_total"], 1)
                self.assertEqual(len(source_page["rows"]), 1)
                for value in ("http-id-group", "msg%2F%2Fhttp-id-group%3D%3D", staged["entry_ids"][0]):
                    unified, _ = call("/api/pending?id_query=" + value,
                                      headers={"x-push-token": manual_server.CFG["token"]})
                    self.assertEqual(unified["total"], 1)
                by_entry, _ = call("/api/pending?entry_id=" + staged["entry_ids"][0],
                                   headers={"x-push-token": manual_server.CFG["token"]})
                self.assertEqual(by_entry["total"], 1)
                self.assertEqual(call("/api/stats")[0]["ratesTotal"], 0)
                login, headers = call("/api/auth/login", {"name": "postgres-test-admin", "password": password})
                self.assertTrue(login["ok"])
                cookie = headers["Set-Cookie"].split(";", 1)[0]
                unreviewed_stats = call('/api/group-stats', headers={'Cookie':cookie})[0]['groups']
                self.assertTrue(all(g['clean_rate'] is None for g in unreviewed_stats))
                self.assertEqual(sum(g['awaiting'] for g in unreviewed_stats),1)
                audit, _ = call("/api/audit", headers={"Cookie": cookie})
                self.assertIn("master_health", audit)
                self.assertNotIn("error", audit)
                self.assertEqual(call("/api/system/health", headers={"Cookie": cookie})[0]["data"]["database"]["engine"],
                                 "postgresql")
                approval, _ = call("/api/pending/approve", {"id": staged["ids"][0]}, {"Cookie": cookie})
                self.assertTrue(approval["ok"], approval)
                reviewed_stats = call('/api/group-stats', headers={'Cookie':cookie})[0]['groups']
                self.assertEqual(sum(g['clean'] for g in reviewed_stats),1)
                self.assertEqual(sum(g['reviewed_total'] for g in reviewed_stats),1)
                self.assertEqual(call("/api/rates?pod=Santos")[0]["total"], 1)
                self.assertEqual(call("/api/rates?message_id=http-id-group")[0]["total"], 1)
                self.assertEqual(call("/api/rates?id_query=http-id-group")[0]["total"], 1)
                self.assertEqual(call("/api/rates?id_query=" + staged["entry_ids"][0])[0]["total"], 1)
                self.assertEqual(call("/api/rates?entry_id=" + staged["entry_ids"][0])[0]["total"], 1)
                bulk_records = [dict(record, **{"消息ID": "http-bulk-a", "海运费USD": "210",
                                               "消息原文": "HTTP bulk first quote"}),
                                dict(record, **{"消息ID": "http-bulk-b", "海运费USD": "220",
                                               "目的港": "Itajai",
                                               "消息原文": "HTTP bulk second quote"})]
                self.assertEqual(call("/api/validate", {"kind": "rates", "records": bulk_records})[0]["good"], 2)
                bulk_records.append(dict(bulk_records[0], **{'目的港':'INVALID-BULK-PORT'}))
                self.assertEqual(call("/api/pending", {"kind": "rates", "records": bulk_records},
                                      {"x-push-token": manual_server.CFG["token"]})[0]["staged"], 3)
                bulk, _ = call("/api/pending/approve_all", {"kind": ""}, {"Cookie": cookie})
                self.assertTrue(bulk["ok"], bulk)
                self.assertEqual(bulk["approved"], 1)
                remaining=call('/api/pending?message_id=http-bulk-a&status=awaiting')[0]['rows']
                self.assertEqual(len(remaining),2)
                failed_row=next(row for row in remaining if row['check_status']=='failed')
                deleted=call('/api/pending/delete', {'id':failed_row['id']}, {'Cookie':cookie})[0]
                self.assertTrue(deleted['ok'])
                ready=call('/api/pending/approve_all', {'kind':''}, {'Cookie':cookie})[0]
                self.assertEqual(ready['approved'],1)
                self.assertEqual(call('/api/pending?message_id=http-bulk-a&status=awaiting')[0]['total'],0)
                protected=call('/api/pending/delete', {'id':staged['ids'][0]}, {'Cookie':cookie})[0]
                self.assertFalse(protected['ok'], 'ordinary UI deletion cannot delete an approved row')
                self.assertEqual(call("/api/rates")[0]["total"], 3)
                self.assertEqual(call("/api/pending/approve_all", {"kind": ""},
                                      {"Cookie": cookie})[0]["approved"], 0)
                reject_records = [dict(record, **{"消息ID": "reject-source", "目的港": port,
                                                 "海运费USD": str(300 + index),
                                                 "消息原文": "HTTP reject regression"})
                                  for index, port in enumerate(("Santos", "Itajai"))]
                pending, _ = call("/api/pending", {"kind": "rates", "records": reject_records},
                                  {"x-push-token": manual_server.CFG["token"]})
                self.assertEqual(pending["staged"], 2)
                rejected, _ = call("/api/pending/reject_group",
                                   {"ids": pending["ids"], "kind": "rates", "note": "目的港需核对"},
                                   {"Cookie": cookie})
                self.assertTrue(rejected["ok"], rejected)
                self.assertEqual(rejected["rejected"], pending["ids"])
                self.assertEqual(call("/api/rates")[0]["total"], 3)
                # Default Agent submission must not delete another identified source,
                # including multiple same-product sources in one request.
                scope_a = dict(record, **{'来源群':'HTTP scope', '消息ID':'scope-a'})
                scope_b = dict(scope_a, **{'消息ID':'scope-b'})
                scope_c = dict(scope_a, **{'消息ID':'scope-c'})
                self.assertEqual(call('/api/pending', {'table':'rates','records':[scope_a]})[0]['staged'],1)
                self.assertEqual(call('/api/pending', {'table':'rates','records':[scope_b,scope_c]})[0]['staged'],2)
                for mid in ('scope-a','scope-b','scope-c'):
                    self.assertEqual(call('/api/pending?message_id='+mid)[0]['total'],1)
                duplicate=call('/api/pending', {'table':'rates','records':[scope_c,dict(scope_c)]})[0]
                self.assertTrue(duplicate['coverage']['ok'])
                self.assertEqual(duplicate['coverage']['exact_duplicates_merged'],1)
                conflicting=call('/api/pending', {'table':'rates','records':[scope_c,dict(scope_c,海运费USD='999')]})[0]
                self.assertFalse(conflicting['coverage']['ok'])
                self.assertEqual(len(conflicting['coverage']['missing']),1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
                manual_server.CFG.clear()
                manual_server.CFG.update(original)


if __name__ == "__main__":
    unittest.main()
