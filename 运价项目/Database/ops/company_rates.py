"""External IO orchestration for optional company-rate searches."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.company_rates_contracts import (
    COMPANY_GATEWAY_URL,
    COMPANY_RATE_CACHE_SECONDS,
    COMPANY_RATE_HTTP_TIMEOUT_SECONDS,
    COMPANY_RATE_KINDS,
    COMPANY_RATE_MAX_PORT_PAIRS,
    COMPANY_RATE_MAX_WORKERS,
    COMPANY_RATE_MAX_DETAIL_QUERIES,
    COMPANY_RATE_MAX_PAGES,
    COMPANY_RATE_DAY_MILLISECONDS,
    COMPANY_MAPPING_CACHE_SECONDS,
    COMPANY_MAPPING_CACHE_MAX_ENTRIES,
    COMPANY_REALTIME_BUDGET_SECONDS,
    COMPANY_REALTIME_CACHE_SECONDS,
    COMPANY_REALTIME_POLL_SECONDS,
)
from ops.company_rate_store import create_task, find_fresh, get_task, save_result, update_task
from ops import company_mappings
from services.company_rates import (
    build_carrier_match_request,
    build_city_ports_request,
    build_company_rate_request,
    build_port_match_request,
    build_realtime_create_request,
    build_realtime_poll_request,
    normalize_gateway_response,
    port_pairs,
    select_exact_carrier,
    select_exact_city,
    select_exact_port,
    local_mapping_profile,
)


def _signature(query):
    encoded = json.dumps(query, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CompanyRateRuntime:
    def __init__(self, connect, environ=None, gateway_url=COMPANY_GATEWAY_URL, executor=None,
                 now=None, sleep=None, credential_provider=None, master_provider=None):
        self.connect = connect
        self.environ = os.environ if environ is None else environ
        self.gateway_url = gateway_url
        self.executor = executor or ThreadPoolExecutor(max_workers=COMPANY_RATE_MAX_WORKERS,
                                                       thread_name_prefix="company-rates")
        self.now = now or (lambda: int(time.time()))
        self.sleep = sleep or time.sleep
        self.credential_provider = credential_provider
        self.master_provider = master_provider
        self._mapping_cache = OrderedDict()
        self._mapping_lock = Lock()

    def _cached_mapping(self, key):
        with self._mapping_lock:
            saved = self._mapping_cache.get(key)
            if saved and saved[0] > self.now():
                self._mapping_cache.move_to_end(key)
                return dict(saved[1])
            self._mapping_cache.pop(key, None)
        return None

    def _save_mapping(self, key, result):
        profile = json.loads(key[2])
        connection = self.connect()
        try:
            company_mappings.candidate(connection,key[0],key[1],profile['canonical'],result,profile.get('country',''))
        finally:
            connection.close()
        with self._mapping_lock:
            self._mapping_cache[key] = (self.now() + COMPANY_MAPPING_CACHE_SECONDS, dict(result))
            self._mapping_cache.move_to_end(key)
            while len(self._mapping_cache) > COMPANY_MAPPING_CACHE_MAX_ENTRIES:
                self._mapping_cache.popitem(last=False)
        return dict(result)

    def _confirmed_mapping(self, user_id, kind, profile):
        connection = self.connect()
        try:
            row = company_mappings.read(connection,user_id,kind,profile['canonical'])
        finally:
            connection.close()
        if not row or row['status'] == 'pending':
            return None
        if row['status'] == 'disabled':
            raise ValueError('公司接口映射已停用：' + profile['canonical'])
        return {'id':row['company_id'],'code':row['company_code'],'enName':row['company_name'],
                'name':row['company_name'],'match_text':row['company_code'] or row['company_name'],
                'type':11 if kind == 'port' else None}

    def mapping_list(self):
        _, business_id = self._credentials()
        connection = self.connect()
        try:
            rows = [dict(r) for r in connection.execute('SELECT * FROM company_mappings WHERE business_user_id=? ORDER BY kind,local_name',(business_id,))]
            master = self.master_provider(connection) if self.master_provider else {'ports':[],'carriers':[]}
            return {'rows':rows,'choices':{'port':[r['name'] for r in master['ports']], 'carrier':[r['code'] for r in master['carriers']]}}
        finally:
            connection.close()

    def mapping_save(self, body):
        _, business_id = self._credentials()
        connection = self.connect()
        try:
            master = self.master_provider(connection) if self.master_provider else {'ports':[],'carriers':[]}
            result = company_mappings.save(connection,business_id,body,master,body['_actor_id'])
        finally:
            connection.close()
        with self._mapping_lock:
            self._mapping_cache.clear()
        return result

    def mapping_probe(self, body):
        kind = body.get('kind')
        if kind not in ('port','carrier'):
            raise ValueError('请选择港口或船司')
        name = str(body.get('local_name') or '').strip()
        _, business_id = self._credentials()
        connection = self.connect()
        try:
            master = self.master_provider(connection) if self.master_provider else {'ports':[],'carriers':[]}
            rows = master['ports' if kind == 'port' else 'carriers']
            profile = local_mapping_profile(name,kind,rows)
            primary = 'name' if kind == 'port' else 'code'
            if not any(r.get(primary) == profile['canonical'] for r in rows):
                raise ValueError('请选择启用的本地主数据')
        finally:
            connection.close()
        result = (self._port if kind == 'port' else self._carrier)(business_id,name,profile)
        return {'kind':kind,'local_name':profile['canonical'],'company_id':result['id'],
                'company_code':result.get('code') or result.get('match_text') or '',
                'company_name':result.get('enName') or result.get('name') or result.get('match_text') or ''}

    def _credentials(self):
        stored = self.credential_provider() if self.credential_provider else None
        if stored:
            return str(stored["token"]), int(stored["user_id"])
        token = str(self.environ.get("YQN_DINGTALK_TOKEN") or "").strip()
        user_id = str(self.environ.get("YQN_USER_ID") or "").strip()
        if not token or not user_id:
            raise ValueError("公司查价服务尚未配置凭据")
        try:
            user_id = int(user_id)
        except ValueError as error:
            raise ValueError("公司查价业务用户ID格式不正确") from error
        return token, user_id

    def _post(self, payload):
        token, _user_id = self._credentials()
        request = Request(self.gateway_url, data=json.dumps(payload).encode("utf-8"), method="POST", headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with urlopen(request, timeout=COMPANY_RATE_HTTP_TIMEOUT_SECONDS) as response:
                return normalize_gateway_response(json.loads(response.read().decode("utf-8")))
        except HTTPError as error:
            raise ValueError("公司接口返回HTTP " + str(error.code)) from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ValueError("公司接口暂时不可用") from error

    def _port(self, user_id, name, profile=None):
        profile = profile or local_mapping_profile(name, "port", [])
        confirmed = self._confirmed_mapping(user_id,'port',profile)
        if confirmed:
            return confirmed
        cache_key = (user_id, "port", json.dumps(profile, sort_keys=True))
        cached = self._cached_mapping(cache_key)
        if cached:
            return cached
        last_error = None
        for variant in profile["variants"]:
            data = self._post(build_port_match_request(user_id, variant))
            if not isinstance(data, list):
                raise ValueError("公司港口响应格式不正确")
            try:
                result = select_exact_port(variant, data, profile["codes"], profile["country"], profile.get('identities',()))
                return self._save_mapping(cache_key, result)
            except ValueError as error:
                last_error = error
                try:
                    city = select_exact_city(variant, data)
                except ValueError:
                    continue
                expanded = self._post(build_city_ports_request(user_id, city["id"]))
                if not isinstance(expanded, dict) or not isinstance(expanded.get("content"), list):
                    raise ValueError("公司城市港口响应格式不正确")
                ports = expanded["content"]
                for port_variant in profile["variants"]:
                    try:
                        result = select_exact_port(port_variant, ports, profile["codes"], profile["country"], profile.get('identities',()))
                        return self._save_mapping(cache_key, result)
                    except ValueError:
                        continue  # Only an identified child port may stand for a local port.
                last_error = ValueError("城市下的具体港口需要确认：" + str(name))
        raise last_error or ValueError("无法精确匹配港口：" + str(name))

    def _carrier(self, user_id, name, profile=None):
        profile = profile or local_mapping_profile(name, "carrier", [])
        confirmed = self._confirmed_mapping(user_id,'carrier',profile)
        if confirmed:
            return confirmed
        cache_key = (user_id, "carrier", json.dumps(profile, sort_keys=True))
        cached = self._cached_mapping(cache_key)
        if cached:
            return cached
        last_error = None
        for variant in profile["variants"]:
            data = self._post(build_carrier_match_request(user_id, variant))
            candidate = data.get("result") if isinstance(data, dict) else None
            try:
                result = select_exact_carrier(variant, candidate, profile["variants"])
                return self._save_mapping(cache_key, result)
            except ValueError as error:
                last_error = error
        raise last_error or ValueError("无法精确匹配船司：" + str(name))

    def create(self, user_id, kind, query):
        if kind not in COMPANY_RATE_KINDS or not isinstance(query, dict):
            raise ValueError("公司查价请求格式不正确")
        pol = query.get("pol") or []
        pod = query.get("pod") or []
        carriers = query.get("carriers") or []
        if not isinstance(pol, list) or not isinstance(pod, list) or not pol or not pod:
            raise ValueError("起运港和目的港不能为空")
        if not isinstance(carriers, list):
            raise ValueError("船司列表格式不正确")
        if len(pol) * len(pod) > COMPANY_RATE_MAX_PORT_PAIRS:
            raise ValueError("一次最多20个港口组合")
        now_epoch = self.now()
        connection = self.connect()
        try:
            try:
                _, business_id = self._credentials()
            except ValueError:
                business_id = None  # Missing credentials are reported by the asynchronous task.
            decisions = [(r['kind'],r['local_name'],r['revision']) for r in connection.execute(
                "SELECT kind,local_name,revision FROM company_mappings WHERE business_user_id=? AND status<>'pending' ORDER BY kind,local_name",
                (business_id,))]
            # Old results must not be offered under another identity or changed mapping decisions.
            signature = _signature({'query':query,'business_id':business_id,'mappings':decisions,'port_name_verification':True})
            cached = find_fresh(connection, user_id, kind, signature, now_epoch)
            task_id = "crt_" + uuid.uuid4().hex
            create_task(connection, task_id, user_id, kind, signature, query, now_epoch)
        finally:
            connection.close()
        self.executor.submit(self._run, task_id, kind, query)
        return {"task_id": task_id, "cached": cached}

    def get(self, user_id, task_id):
        connection = self.connect()
        try:
            return get_task(connection, task_id, user_id, self.now())
        finally:
            connection.close()

    def _run(self, task_id, kind, query):
        connection = self.connect()
        try:
            update_task(connection, task_id, "running", self.now())
            _token, business_user_id = self._credentials()
            master = self.master_provider(connection) if self.master_provider else {"ports": [], "carriers": []}
            mappings = []
            failures = 0
            def resolve_ports(names, side):
                resolved = []
                nonlocal failures
                for name in dict.fromkeys(names):
                    try:
                        profile = local_mapping_profile(name, "port", master["ports"])
                        port = self._port(business_user_id, name, profile)
                        resolved.append(port)
                        mappings.append({"kind": side, "input": name, "id": port["id"], "code": port.get("code"), "name": port.get("enName") or port.get("name")})
                    except ValueError as error:
                        stamp = self.now()
                        save_result(connection, task_id, side + ":" + str(name), [], stamp, stamp, str(error))
                        failures += 1
                return resolved
            origins = resolve_ports(query.get("pol") or [], "起运港")
            destinations = resolve_ports(query.get("pod") or [], "目的港")
            carriers = []
            for name in query.get("carriers") or []:
                profile = local_mapping_profile(name, "carrier", master["carriers"])
                carrier = self._carrier(business_user_id, name, profile)
                carriers.append(carrier)
                mappings.append({"kind": "船司", "input": name, "id": carrier["id"], "code": carrier.get("code") or carrier.get("match_text")})
            connection.execute("UPDATE company_rate_tasks SET query_json=? WHERE task_id=?",
                               (json.dumps({**query, "resolved_mappings": mappings}, ensure_ascii=False), task_id))
            connection.commit()
            pairs = port_pairs([row["id"] for row in origins], [row["id"] for row in destinations])
            jobs = {}
            with ThreadPoolExecutor(max_workers=COMPANY_RATE_MAX_WORKERS) as pool:
                for pol_id, pod_id in pairs:
                    operation = self._company_pair if kind == "company" else self._realtime_pair
                    jobs[pool.submit(operation, business_user_id, pol_id, pod_id,
                                     [row["id"] for row in carriers], query)] = (pol_id, pod_id)
                successes = 0
                ttl = COMPANY_RATE_CACHE_SECONDS if kind == "company" else COMPANY_REALTIME_CACHE_SECONDS
                for future in as_completed(jobs):
                    pol_id, pod_id = jobs[future]
                    stamp = self.now()
                    try:
                        items = future.result()
                        save_result(connection, task_id, f"{pol_id}:{pod_id}", items, stamp, stamp + ttl)
                        successes += 1
                        if any(item.get('_company_missing_products') or item.get('_company_associated_unpriced_products')
                               or item.get('_realtime_missing_carriers') for item in items):
                            failures += 1
                    except Exception as error:  # pair failures are isolated and persisted
                        save_result(connection, task_id, f"{pol_id}:{pod_id}", [], stamp, stamp, str(error))
                        failures += 1
            status = "complete" if successes and not failures else "partial" if successes else "failed"
            update_task(connection, task_id, status, self.now(), None if successes else "公司查价没有成功的港口组合")
        except Exception as error:
            update_task(connection, task_id, "failed", self.now(), str(error))
        finally:
            connection.close()

    def _company_pair(self, user_id, pol_id, pod_id, carrier_ids, query):
        def fetch_rows(valid_from, valid_to):
            payload = build_company_rate_request(user_id, pol_id, pod_id, carrier_ids, valid_from, valid_to)
            rows = []
            for page in range(COMPANY_RATE_MAX_PAGES):
                if page:
                    payload['body']['model']['page'] = page
                data = self._post(payload)
                if not isinstance(data, dict) or not isinstance(data.get('content'), list):
                    raise ValueError('公司运价响应格式不正确')
                rows.extend(data['content'])
                if data.get('isLast') is not False:
                    return rows
            raise ValueError('公司运价分页未完成')

        rows = fetch_rows(query.get('valid_from'), query.get('valid_to'))
        if query.get('valid_to') is not None:
            return rows
        # The list endpoint collapses schedules; retrieve referenced products using their source dates.
        expected = {str(x['product_id']) for row in rows for x in row.get('schedule_list') or []
                    if isinstance(x, dict) and x.get('product_id') is not None}
        present = {str(row.get('product_id')) for row in rows}
        dates = dict.fromkeys(note['validFrom'] for row in rows for note in row.get('comments_list') or []
                              if isinstance(note, dict) and str(note.get('productId')) in expected - present
                              and isinstance(note.get('validFrom'), (int, float)) and note['validFrom'] > 0)
        seen = {str(row.get('id') or row.get('product_id')) for row in rows}
        associated_unpriced = set()
        for index, date in enumerate(dates):
            if index >= COMPANY_RATE_MAX_DETAIL_QUERIES or not expected - present:
                break
            try:
                details = fetch_rows(date, date + COMPANY_RATE_DAY_MILLISECONDS - 1)
            except ValueError:
                continue  # Preserve known prices; uncovered products remain explicit.
            for item in details:
                product = str(item.get('product_id'))
                identity = str(item.get('id') or item.get('product_id'))
                if (query.get('valid_from') is not None
                        and isinstance(item.get('valid_to'), (int, float))
                        and item['valid_to'] < query['valid_from']):
                    continue  # A date probe must not reintroduce expired quotes.
                if any(isinstance(price, dict) and price.get('show_price') not in (None, '')
                       for price in item.get('product_price') or []):
                    related = {str(value.get('product_id')) for value in item.get('schedule_list') or []
                               if isinstance(value, dict) and value.get('product_id') is not None}
                    related.update(str(note.get('productId')) for note in item.get('comments_list') or []
                                   if isinstance(note, dict) and note.get('productId') is not None)
                    associated_unpriced.update((related & expected) - present)
                if product in expected and identity not in seen:
                    rows.append(item)
                    seen.add(identity)
                    present.add(product)
        unresolved_associations = associated_unpriced - present
        missing_products = expected - present - unresolved_associations
        if rows and unresolved_associations:
            rows[0] = {**rows[0], '_company_associated_unpriced_products': sorted(unresolved_associations)}
        if rows and missing_products:
            rows[0] = {**rows[0], '_company_missing_products': sorted(missing_products)}
        return rows

    def _realtime_pair(self, user_id, pol_id, pod_id, carrier_ids, query):
        if not carrier_ids:
            raise ValueError("实时价格须选择船司")
        container_id = query.get("container_id")
        created = self._post(build_realtime_create_request(
            user_id, pol_id, pod_id, carrier_ids, [container_id], query.get("valid_from"),
            query.get("valid_to"), query.get("weight")
        ))
        tasks = created.get("task") if isinstance(created, dict) else None
        if not isinstance(tasks, list):
            raise ValueError("公司实时任务创建响应格式不正确")
        if not tasks:
            raise ValueError("公司接口未创建实时查价任务，尚未取得报价；这不代表该航线无运价。请核对公司业务用户ID及实时查价权限")
        deadline = self.now() + COMPANY_REALTIME_BUDGET_SECONDS
        expected = {str(value) for value in carrier_ids}
        received = {}
        while self.now() <= deadline:
            data = self._post(build_realtime_poll_request(user_id, tasks, container_id))
            rows = data.get("shipOwnerFreightChargeVO") if isinstance(data, dict) else None
            if isinstance(rows, list):
                for row in rows:
                    identity = str((row.get('carrier') or {}).get('carrierId'))
                    if identity in expected and row.get('status') in (0, 1):
                        received[identity] = row
                if expected <= received.keys():
                    return list(received.values())
            self.sleep(COMPANY_REALTIME_POLL_SECONDS)
        if received:
            result = list(received.values())
            result[0] = {**result[0], '_realtime_missing_carriers': sorted(expected - received.keys())}
            return result
        raise ValueError("船公司实时价格查询未在预算内完成")

