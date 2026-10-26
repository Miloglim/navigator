import unittest

from services.company_rates import (
    build_company_rate_request,
    build_realtime_create_request,
    build_realtime_poll_request,
    build_port_match_request,
    build_carrier_match_request,
    build_city_ports_request,
    lookup_variants,
    local_mapping_profile,
    select_exact_port,
    select_exact_carrier,
    normalize_gateway_response,
    port_pairs,
)


class CompanyRatesServiceTests(unittest.TestCase):
    def test_port_pairs_expand_each_origin_and_destination_with_hard_limit(self):
        self.assertEqual(
            port_pairs([101, 102], [201, 202]),
            [(101, 201), (101, 202), (102, 201), (102, 202)],
        )
        with self.assertRaisesRegex(ValueError, "最多20个港口组合"):
            port_pairs(range(1, 6), range(101, 106))

    def test_company_rate_request_uses_single_port_ids_and_documented_field_names(self):
        request = build_company_rate_request(
            99, 101, 201, [4528], 1791820800000, 1791907199999
        )
        self.assertEqual(request["body"]["header"], {"xUserId": 99})
        self.assertEqual(request["body"]["model"], {
            "pol_id": 101,
            "pod_id": 201,
            "pol_port_type": 11,
            "pod_port_type": 11,
            "carrier_id": [4528],
            "freight_charge_sort": 2,
            "search_flag": 2,
            "valid_from": 1791820800000,
            "valid_to": 1791907199999,
        })

    def test_realtime_poll_uses_only_tasks_from_current_create_response(self):
        open_ended = build_realtime_create_request(99, 101, 201, [4550], [12], 1792166400000, None, 10000)
        self.assertNotIn('validTo', open_ended['body']['model'])
        self.assertEqual(1792166400000, open_ended['body']['model']['validFrom'])
        create = build_realtime_create_request(
            99, 101, 201, [4550], [12], 1792166400000, 1794844799999, 10000
        )
        self.assertEqual(create["body"]["model"]["containerTypes"], [12])
        poll = build_realtime_poll_request(99, [{"carrierId": 4550, "taskId": 7001}], 12)
        self.assertEqual(poll["body"]["model"]["task"], [{"carrierId": 4550, "taskId": 7001}])
        with self.assertRaisesRegex(ValueError, "本次创建"):
            build_realtime_poll_request(99, [{"carrierId": 4550}], 12)

    def test_realtime_supports_only_verified_container_types(self):
        for container_id in (1, 11, 12, 20):
            create = build_realtime_create_request(
                99, 101, 201, [4550], [container_id], 1792166400000, None, 10000
            )
            self.assertEqual(create["body"]["model"]["containerTypes"], [container_id])
            poll = build_realtime_poll_request(
                99, [{"carrierId": 4550, "taskId": 7001}], container_id
            )
            self.assertEqual(poll["body"]["model"]["containerId"], container_id)
        with self.assertRaisesRegex(ValueError, "柜型暂不支持"):
            build_realtime_create_request(99, 101, 201, [4550], [999], 1792166400000, None, 10000)
        with self.assertRaisesRegex(ValueError, "柜型暂不支持"):
            build_realtime_poll_request(99, [{"carrierId": 4550, "taskId": 7001}], 999)

    def test_gateway_failure_and_malformed_data_are_not_valid_results(self):
        with self.assertRaisesRegex(ValueError, "公司接口调用失败"):
            normalize_gateway_response({"success": False, "code": 500, "message": "failed"})
        with self.assertRaisesRegex(ValueError, "响应格式不正确"):
            normalize_gateway_response({"success": True, "code": 200})
        self.assertEqual(normalize_gateway_response({"success": True, "code": 200, "data": []}), [])

    def test_master_matching_accepts_exact_identity_and_rejects_fuzzy_guess(self):
        self.assertEqual(build_port_match_request(99, "CNSHA")["body"]["model"]["keyword"], "CNSHA")
        self.assertEqual(build_carrier_match_request(99, "COSCO")["body"]["model"]["search_text"], "COSCO")
        ports = [{"id": 8951, "code": "CNSHA", "enName": "SHANGHAI", "cnName": "上海港", "type": 11}]
        self.assertEqual(select_exact_port("CNSHA", ports)["id"], 8951)
        self.assertEqual(select_exact_port("上海港", ports)["id"], 8951)
        self.assertEqual(select_exact_carrier("COSCO", {"id": 4528, "match_text": "COSCO"})["id"], 4528)
        with self.assertRaisesRegex(ValueError, "无法精确匹配船司"):
            select_exact_carrier("中远", {"id": 4528, "match_text": "COSCO"})

    def test_company_matching_normalizes_accents_and_supported_carrier_aliases(self):
        self.assertEqual(lookup_variants("São Paulo"), ["São Paulo", "Sao Paulo"])
        self.assertEqual(lookup_variants("MSK", {"msk": "MAERSK"}), ["MSK", "MAERSK"])
        ports = [{"id": 77, "code": "BRSSZ", "enName": "SAO PAULO", "cnName": "圣保罗", "type": 11}]
        self.assertEqual(select_exact_port("São Paulo", ports)["id"], 77)
        self.assertEqual(select_exact_carrier("maersk", {"id": 4550, "match_text": "MAERSK"})["id"], 4550)
        self.assertEqual(build_city_ports_request(99, 14280)["body"]["model"], {"cityId": 14280, "types": [11]})

    def test_input_echo_and_high_score_cannot_prove_carrier_identity(self):
        with self.assertRaises(ValueError):
            select_exact_carrier("HPL", {"id": 1, "input_key": "HPL", "match_text": "HMM", "score": 100})
        with self.assertRaises(ValueError):
            select_exact_carrier("HPL", {"id": 1, "match_text": "HPL", "code": "HMM"})

    def test_port_country_and_colliding_normalized_names(self):
        rows = [{"id": 1, "code": "MXZLO", "enName": "Manzanillo", "type": 11},
                {"id": 2, "code": "PAMIT", "enName": "Manzanillo", "type": 11}]
        with self.assertRaises(ValueError):
            select_exact_port("Manzanillo", rows)
        self.assertEqual(select_exact_port("Manzanillo", rows, country="MX")["id"], 1)
        with self.assertRaises(ValueError):
            select_exact_port("Manzanillo", rows[1:], country="MX")
        profile = local_mapping_profile("Pecém", "port", [{"name": "Pecém", "country": "BR", "locode_ref": "BRPEC", "aliases": "Pecem"}])
        self.assertEqual(profile["variants"][0], "BRPEC")
        self.assertIn("Pecem", profile["variants"])

    def test_port_code_and_alias_must_confirm_the_same_company_row(self):
        local = [{"name": "Sokhna Port", "name_en": "Sokhna Port", "name_cn": "苏赫纳",
                  "country": "EG", "locode_ref": "EGSOK", "aliases": "苏赫纳,Sokhna,EGSOK"}]
        profile = local_mapping_profile("Sokhna Port", "port", local)
        company = [{"id": 29817, "code": "EGSOK", "enName": "SOKHNA", "cnName": "索科纳",
                    "countryCode": "EGYPT", "type": 11}]

        self.assertEqual(profile["canonical"], "Sokhna Port")
        self.assertIn("Sokhna", profile["identities"])
        self.assertEqual(select_exact_port("Sokhna", company, profile["codes"], profile["country"],
                                           profile["identities"])["id"], 29817)
        self.assertEqual(select_exact_port("EGSOK", company, profile["codes"], profile["country"],
                                           profile["identities"])["id"], 29817)

        wrong_code = [{"id": 8952, "code": "CNSHG", "enName": "SHANHAIGUAN", "type": 11}]
        shanghai = local_mapping_profile(
            "Shanghai", "port",
            [{"name": "Shanghai", "country": "CN", "locode_ref": "CNSHG", "aliases": "CNSHG"}],
        )
        with self.assertRaisesRegex(ValueError, "无法精确匹配港口"):
            select_exact_port("CNSHG", wrong_code, shanghai["codes"], shanghai["country"],
                              shanghai["identities"])

    def test_carrier_uses_master_aliases_without_inventing_company_codes(self):
        profile = local_mapping_profile("HPL", "carrier", [{"code": "HPL", "aliases": "HAPAG-LLOYD,HLCU"}])
        self.assertIn("HAPAG-LLOYD", profile["variants"])
        self.assertEqual(local_mapping_profile("MSK", "carrier", [])['variants'][0], "MAERSK")


if __name__ == "__main__":
    unittest.main()

