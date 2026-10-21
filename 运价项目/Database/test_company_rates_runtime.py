import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import manualdb
from ops.company_rates import CompanyRateRuntime


class InlineExecutor:
    def submit(self, operation, *args):
        operation(*args)


class GatewayHandler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.__class__.calls.append((self.headers.get("Authorization"), body))
        target = body["url"]
        model = body["body"]["model"]
        if target.endswith("portAndDistrictSearch"):
            keyword = model["keyword"]
            data = [{"id": {"CNSHA": 8951, "USLAX": 6296}[keyword], "type": 11,
                     "code": keyword, "enName": {"CNSHA": "SHANGHAI", "USLAX": "LOS ANGELES"}[keyword]}]
        elif target.endswith("/match/carrier"):
            data = {"result": {"id": 4528, "match_text": model["search_text"]}}
        elif target.endswith("get_product_list"):
            data = {"content": [{"product_id": 7, "product_price": [{"price_unit": "40HQ", "currency_code": "USD", "show_price": 1200}]}]}
        elif target.endswith("createIds"):
            data = {"task": [{"carrierId": 4528, "carrierName": "COSCO", "taskId": 7001}]}
        elif target.endswith("get_online_list"):
            data = {"shipOwnerFreightChargeVO": [{"carrier": {"carrierCode": "COSCO", "carrierId": 4528}, "status": 1,
                                                    "result": [{"totalOceanPriceUsd": 1300}]}]}
        else:
            self.send_error(404)
            return
        payload = json.dumps({"success": True, "code": 200, "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class CompanyRateRuntimeTests(unittest.TestCase):
    def test_realtime_waits_for_all_requested_carriers_and_keeps_incremental_results(self):
        responses = iter([
            {'task':[{'carrierId':4550,'taskId':1},{'carrierId':4525,'taskId':1}]},
            {'shipOwnerFreightChargeVO':[{'carrier':{'carrierId':4550},'status':1,'result':[{'price':6}]}]},
            {'shipOwnerFreightChargeVO':[{'carrier':{'carrierId':4525},'status':1,'result':[{'price':11}]}]},
        ])
        self.runtime._post=lambda _:next(responses)
        rows=self.runtime._realtime_pair(99,8951,8760,[4550,4525],{'container_id':12,'weight':10000,'valid_from':1791907200000})
        self.assertEqual({4550,4525},{r['carrier']['carrierId'] for r in rows})

    def test_realtime_timeout_preserves_results_and_identifies_missing_carrier(self):
        tick=[0]
        self.runtime.now=lambda:tick[0]
        self.runtime.sleep=lambda _:tick.__setitem__(0,tick[0]+121)
        responses=iter([{'task':[{'carrierId':4550,'taskId':1}]},
            {'shipOwnerFreightChargeVO':[{'carrier':{'carrierId':4550},'status':0,'result':[]}]}])
        self.runtime._post=lambda _:next(responses)
        rows=self.runtime._realtime_pair(99,8951,8760,[4550,4525],{'container_id':12,'weight':10000,'valid_from':1791907200000})
        self.assertEqual(['4525'],rows[0]['_realtime_missing_carriers'])

    def test_empty_created_tasks_are_unavailable_not_malformed_or_no_quotes(self):
        self.runtime._post=lambda req:{'task':[]}
        with self.assertRaisesRegex(ValueError,'未创建.*不代表'):
            self.runtime._realtime_pair(99,8951,8760,[4550],{
                'container_id':12,'valid_from':1792166400000,'valid_to':1792252799999,'weight':10000})

    def test_wrong_local_port_code_cannot_override_canonical_name(self):
        from services.company_rates import local_mapping_profile
        profile=local_mapping_profile('Shanghai','port',[{'name':'Shanghai','name_en':'Shanghai',
            'name_cn':'上海港','country':'CN','locode_ref':'CNSHG','aliases':'CNSHG,CNSGH,上海'}])
        wrong={'id':8952,'code':'CNSHG','enName':'SHANHAIGUAN','cnName':'山海关港','type':11}
        right={'id':8951,'code':'CNSHA','enName':'SHANGHAI','cnName':'上海港','type':11}
        self.runtime._post=lambda req:[wrong] if req['body']['model']['keyword'] in ('CNSHG','CNSGH') else [right]
        result=self.runtime._port(99,'Shanghai',profile)
        self.assertEqual(result['id'],8951)

    def test_wrong_code_without_name_evidence_must_fail_closed(self):
        from services.company_rates import local_mapping_profile
        profile=local_mapping_profile('Shanghai','port',[{'name':'Shanghai','country':'CN','locode_ref':'CNSHG'}])
        self.runtime._post=lambda req:[{'id':8952,'code':'CNSHG','enName':'SHANHAIGUAN','type':11}]
        with self.assertRaises(ValueError):
            self.runtime._port(99,'Shanghai',profile)

    def test_grouped_products_are_retrieved_with_their_own_prices(self):
        current = {'id':'new', 'product_id':2, 'product_price':[{'show_price':5150}],
                   'schedule_list':[{'product_id':1},{'product_id':2}],
                   'comments_list':[{'productId':1,'validFrom':1791907200000}]}
        old = {'id':'old', 'product_id':1, 'product_price':[{'show_price':5350}]}
        requests = []
        def post(payload):
            model = payload['body']['model']
            requests.append(model)
            return {'content':[old if model.get('valid_from') else current]}
        self.runtime._post = post
        rows = self.runtime._company_pair(99,8951,8760,[],{})
        self.assertEqual({1,2},{row['product_id'] for row in rows})
        self.assertEqual([5150,5350],[row['product_price'][0]['show_price'] for row in rows])
        self.assertEqual(1791907200000,requests[1]['valid_from'])

    def test_schedule_reference_without_own_price_is_classified_without_copying_price(self):
        day = 1791993600000
        parent = {'id':'parent', 'product_id':2, 'valid_from':day, 'valid_to':day + 86400000,
                  'product_price':[{'price_unit':'40HQ','show_price':5150}],
                  'schedule_list':[{'product_id':1},{'product_id':2}],
                  'comments_list':[{'productId':1,'validFrom':day}]}
        self.runtime._post = lambda _payload: {'content':[parent]}

        rows = self.runtime._company_pair(99, 8951, 8760, [], {})

        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]['product_id'])
        self.assertEqual([{'price_unit':'40HQ','show_price':5150}], rows[0]['product_price'])
        self.assertEqual(['1'], rows[0]['_company_associated_unpriced_products'])
        self.assertNotIn('_company_missing_products', rows[0])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "rates.db"
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        manualdb.init_schema(connection)
        connection.close()
        GatewayHandler.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.runtime = CompanyRateRuntime(
            connect=self.connect,
            environ={"YQN_DINGTALK_TOKEN": "test-token", "YQN_USER_ID": "99"},
            gateway_url=f"http://127.0.0.1:{self.server.server_port}/dispatch",
            executor=InlineExecutor(),
            now=lambda: 1000,
            sleep=lambda _seconds: None,
        )

    def test_missing_grouped_product_keeps_known_price_and_marks_partial(self):
        self.runtime._company_pair = lambda *_args: [{
            'product_id': 2, 'product_price': [{'show_price': 5150}],
            '_company_missing_products': ['1'],
        }]
        created = self.runtime.create('usr-1', 'company', {'pol': ['CNSHA'], 'pod': ['USLAX'], 'carriers': []})
        task = self.runtime.get('usr-1', created['task_id'])
        self.assertEqual('partial', task['status'])
        self.assertEqual(5150, task['results'][0]['items'][0]['product_price'][0]['show_price'])

    def test_associated_schedule_without_independent_price_keeps_task_partial(self):
        self.runtime._company_pair = lambda *_args: [{
            'product_id': 2, 'product_price': [{'show_price': 5150}],
            '_company_associated_unpriced_products': ['1'],
        }]
        created = self.runtime.create('usr-1', 'company', {'pol': ['CNSHA'], 'pod': ['USLAX'], 'carriers': []})
        task = self.runtime.get('usr-1', created['task_id'])
        self.assertEqual('partial', task['status'])

    def test_explicit_date_filter_does_not_expand_outside_query(self):
        calls = []
        def post(payload):
            calls.append(payload)
            return {'content': [{'product_id': 2, 'schedule_list': [{'product_id': 1}],
                                 'comments_list': [{'productId': 1, 'validFrom': 1791907200000}]}]}
        self.runtime._post = post
        rows = self.runtime._company_pair(99, 8951, 8760, [], {'valid_from': 1791993600000, 'valid_to': 1792079999999})
        self.assertEqual([2], [row['product_id'] for row in rows])
        self.assertEqual(1, len(calls))

    def test_from_date_expands_active_products_without_reintroducing_expired_quotes(self):
        day = 1791907200000
        current = {'product_id': 2, 'valid_from': day + 86400000, 'valid_to': day + 604800000,
                   'product_price': [{'show_price': 5150}],
                   'schedule_list': [{'product_id': 1}, {'product_id': 2}, {'product_id': 3}],
                   'comments_list': [{'productId': 1, 'validFrom': day}, {'productId': 3, 'validFrom': day}]}
        active = {'product_id': 1, 'valid_from': day - 172800000, 'valid_to': day,
                  'product_price': [{'show_price': 5350}]}
        expired = {'product_id': 3, 'valid_to': day - 1, 'product_price': [{'show_price': 9999}]}
        calls = []
        def post(payload):
            model = payload['body']['model']
            calls.append(dict(model))
            return {'content': [active, expired] if 'valid_to' in model else [current]}
        self.runtime._post = post
        rows = self.runtime._company_pair(99, 8951, 8760, [], {'valid_from': day})
        self.assertEqual([2, 1], [row['product_id'] for row in rows])
        self.assertEqual([5150, 5350], [row['product_price'][0]['show_price'] for row in rows])
        self.assertNotIn('valid_to', calls[0])
        self.assertEqual(['3'], rows[0]['_company_missing_products'])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def test_company_and_realtime_tasks_use_gateway_identity_and_save_results(self):
        query = {"pol": ["CNSHA"], "pod": ["USLAX"], "carriers": ["COSCO"],
                 "valid_from": 1791820800000, "valid_to": 1791907199999}
        company = self.runtime.create("usr-1", "company", query)
        company_task = self.runtime.get("usr-1", company["task_id"])
        self.assertEqual(company_task["status"], "complete")
        self.assertEqual(company_task["results"][0]["items"][0]["product_id"], 7)

        realtime = self.runtime.create("usr-1", "realtime", {
            **query, "container_id": 12, "weight": 10000,
        })
        realtime_task = self.runtime.get("usr-1", realtime["task_id"])
        self.assertEqual(realtime_task["status"], "complete")
        self.assertEqual(realtime_task["results"][0]["items"][0]["result"][0]["totalOceanPriceUsd"], 1300)
        self.assertTrue(GatewayHandler.calls)
        self.assertTrue(all(auth == "Bearer test-token" for auth, _body in GatewayHandler.calls))
        self.assertTrue(all(body["body"]["header"]["xUserId"] == 99 for _auth, body in GatewayHandler.calls))

    def test_missing_secret_fails_task_without_losing_local_task_record(self):
        runtime = CompanyRateRuntime(self.connect, {}, self.runtime.gateway_url, InlineExecutor(), now=lambda: 1000)
        result = runtime.create("usr-1", "company", {"pol": ["CNSHA"], "pod": ["USLAX"], "carriers": []})
        task = runtime.get("usr-1", result["task_id"])
        self.assertEqual(task["status"], "failed")
        self.assertNotIn("test-token", task.get("error") or "")

    def test_create_rejects_missing_ports_and_non_list_carriers(self):
        with self.assertRaisesRegex(ValueError, "不能为空"):
            self.runtime.create("usr-1", "company", {"pol": [], "pod": ["USLAX"], "carriers": []})
        with self.assertRaisesRegex(ValueError, "船司列表格式"):
            self.runtime.create("usr-1", "company", {
                "pol": ["CNSHA"], "pod": ["USLAX"], "carriers": "COSCO",
            })

    def test_saved_credentials_override_environment_without_restarting_runtime(self):
        current = {"token": "first", "user_id": 10}
        runtime = CompanyRateRuntime(self.connect, {"YQN_DINGTALK_TOKEN": "env", "YQN_USER_ID": "20"},
                                     self.runtime.gateway_url, InlineExecutor(), now=lambda: 1000,
                                     credential_provider=lambda: current)
        self.assertEqual(runtime._credentials(), ("first", 10))
        current.update(token="second", user_id=11)
        self.assertEqual(runtime._credentials(), ("second", 11))

    def test_confirmed_mapping_cache_is_scoped_and_expires(self):
        self.runtime._port(99, "CNSHA")
        count = len(GatewayHandler.calls)
        self.runtime._port(99, "CNSHA")
        self.assertEqual(len(GatewayHandler.calls), count)
        self.runtime._port(100, "CNSHA")
        self.assertEqual(len(GatewayHandler.calls), count + 1)
        self.runtime.now = lambda: 100000
        self.runtime._port(99, "CNSHA")
        self.assertEqual(len(GatewayHandler.calls), count + 2)

    def test_unknown_port_does_not_discard_valid_port_pairs(self):
        original = self.runtime._port
        def lookup(user, name, profile=None):
            if name == 'UNKNOWN':
                raise ValueError('无法精确匹配港口：UNKNOWN')
            return original(user, name, profile)
        self.runtime._port = lookup
        created = self.runtime.create('usr-1', 'company', {'pol':['CNSHA','UNKNOWN'], 'pod':['USLAX'], 'carriers':[]})
        task = self.runtime.get('usr-1', created['task_id'])
        self.assertEqual(task['status'], 'partial')
        self.assertEqual(len(task['results']), 2)
        self.assertTrue(any(r['error'] for r in task['results']))
        self.assertTrue(task['query']['resolved_mappings'])

    def test_city_children_require_matching_concrete_port(self):
        responses = iter([[{'id':10,'type':4,'enName':'SANTOS'}],
                          {'content':[{'id':20,'type':11,'enName':'SANTOS','code':'BRSSZ'}]}])
        self.runtime._post = lambda payload: next(responses)
        self.assertEqual(self.runtime._port(99,'SANTOS')['id'],20)
        runtime = CompanyRateRuntime(self.connect, executor=InlineExecutor())
        responses = iter([[{'id':10,'type':4,'enName':'SANTOS'}],
                          {'content':[{'id':20,'type':4,'enName':'SANTOS'}]}])
        runtime._post = lambda payload: next(responses)
        with self.assertRaisesRegex(ValueError,'需要确认'):
            runtime._port(99,'SANTOS')


if __name__ == "__main__":
    unittest.main()
