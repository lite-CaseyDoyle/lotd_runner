#!/usr/bin/env python3
"""Portable parser/CLI tests and POSIX mock-launcher integration tests, all offline."""

from __future__ import annotations

import json
import os
import pathlib
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

try:
    import pty
except ImportError:
    pty = None

ROOT = pathlib.Path(__file__).resolve().parent
CLI = ROOT / "lotd.py"
MOCK = ROOT / "mock_grpcurl.py"


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class LotdParserTests(unittest.TestCase):
    """Portable parser/runner-plan tests; no subprocesses, mock launcher or PTYs."""

    def test_audit_legacy_parsers_scope_successful_owners_and_fragments(self):
        from lotd_client import Client, LotdError
        client = Client("offline.invalid:3217", False)
        def reply(body, code="RC_OK"):
            return {"response_payload": [{"code": code, "payload": [body]}]}
        def module(body, module_id=1):
            return {"mems_module": [{"id": module_id, **body}]}
        mirror = {"mirrors": [{"id": 1, "config": {"x_tilt_voltage": 10}}]}
        cv = {"common_voltage_card": [{"id": 1, "channels": [{"id": 1,
              "config": {"target_voltage": 10}, "state": {"actual_voltage": 9}}]}]}
        led = {"id": 1, "config": {"control": {"value": "LC_ON"}}}
        cases = [(module(mirror), "mirror_set", ("1", "x", "10")),
                 (module(cv), "cv_channel_set", ("1", "10")),
                 ({"fans_module": [{"id": 1, "led": [led]}]}, "led", ("1", "fan", "1")),
                 (module({"dac_cards": [{"id": 1, "sensor": [{"id": "temp", "state": {"value": 2400}}]}]}),
                  "temperature_sensor", ("1",))]
        for body, parser, args in cases:
            self.assertIsNotNone(client.parse(json.dumps(reply(body)), parser, *args))
            wrong = json.loads(json.dumps(body))
            owner = next(iter(wrong))
            wrong[owner][0]["id"] = 2
            for raw in (reply(wrong), reply(body, "RC_MISSING_VALUE"),
                        reply({owner: [body[owner][0], body[owner][0]]})):
                with self.assertRaises(LotdError, msg=parser):
                    client.parse(json.dumps(raw), parser, *args)
        mixed = reply({"fans_module": [{"id": 1, "led": [led]}]})
        missing = reply({"fans_module": [{"id": 1, "led": [
            {"id": 2, "config": {"control": {"value": "LC_OFF"}}}]}]}, "RC_MISSING_VALUE")
        mixed["response_payload"] += missing["response_payload"]
        with self.assertRaises(LotdError):
            client.parse(json.dumps(mixed), "led_all", "1", "fan", "1")
        # A valid entity alongside unrelated module data must still select module 1.
        both = module(mirror)
        both["mems_module"].append({"id": 2, "mirrors": [{"id": 1, "config": {"x_tilt_voltage": 999}}]})
        self.assertEqual(client.parse(json.dumps(reply(both)), "mirror_set", "1", "x", "10"),
                         {"x_tilt_voltage": 10})
        # Return codes cannot identify the model: R320 also returned LDS failures.
        for parser in ("led", "led_all"):
            for code in ("RC_OPERATION_FAILED", "RC_FRU_MISSING", "RC_UNKNOWN_FIELD"):
                raw = json.dumps(reply({"lds_card": [{"id": 1}]}, code))
                with self.assertRaisesRegex(LotdError, "^LOTD returned a non-OK response code: " + code + "$"):
                    client.parse(raw, parser, "1", "lds", "1")

    def test_audit_numeric_types_reject_truncation_booleans_and_strings(self):
        from lotd_client import Client, LotdError
        client = Client("offline.invalid:3217", False)
        def response(body):
            return json.dumps({"response_payload": [{"code": "RC_OK", "payload": [body]}]})
        for value in (1.9, True, "1", None, -1):
            raw = response({"connection_manager": [{"id": 1, "port_array": [{"id": 1,
                "config": {"peer_port_number": value, "optical_blocking_enable": False}}]}]})
            for parser, args in (("connection_peer_set", ("1", "1")), ("connection", ("1",))):
                with self.assertRaises(LotdError):
                    client.parse(raw, parser, *args)
        for value in (False, "10", None, float("inf")):
            for field in ("target_voltage", "actual_voltage"):
                channel = {"id": 1, "config": {"target_voltage": 10}, "state": {"actual_voltage": 9}}
                channel["config" if field == "target_voltage" else "state"][field] = value
                raw = response({"mems_module": [{"id": 1, "common_voltage_card": [{"id": 1, "channels": [channel]}]}]})
                with self.assertRaises(LotdError):
                    client.parse(raw, "cv_channel", "1")
            raw = response({"mems_module": [{"id": 1, "mirrors": [{"id": 1, "config": {"x_tilt_voltage": value}}]}]})
            with self.assertRaises(LotdError):
                client.parse(raw, "mirror", "1", "x")
        raw = response({"connection_manager": [{"id": 1, "port_array": [{"id": 1,
            "config": {"peer_port_number": 1, "optical_blocking_enable": "false"}}]}]})
        with self.assertRaises(LotdError):
            client.parse(raw, "connection", "1")

        # JSON exponent overflow must stay raw text, so error reports remain valid JSON.
        client._exchange = {"operation": "RT_GET", "response": None}
        overflow = '{"response_payload":[{"code":"RC_OK","value":1e400}]}'
        with self.assertRaises(LotdError):
            client.parse(overflow, "cv_channel", "1")
        self.assertIsNone(client._exchange["response"])
        self.assertEqual(client._exchange["response_text"], overflow)

    def test_audit_legacy_and_new_write_acknowledgements_are_consistent(self):
        from lotd_client import Client, LotdError
        for parser in ("status", "write_status"):
            for raw, accepted in (({"cid": "123"}, None), ({"code": "RC_OK"}, True),
                                   ({"cid": "123", "response_payload": [{"code": "RC_OK"}]}, True)):
                client = Client("offline.invalid:3217", False)
                client._exchange = {"operation": "RT_SET", "error": None}
                self.assertIs(client.parse(json.dumps(raw), parser), accepted)
                self.assertIs(client._exchange["accepted"], accepted)
            for raw in ({}, [], {"cid": "other"}, {"cid": "other", "response_payload": [{"code": "RC_OK"}]},
                        {"response_payload": [{}]}, {"response_payload": [{"code": "RC_MISSING_VALUE"}]},
                        {"response_payload": [{"code": "RC_OK"}, {"code": "RC_OPERATION_FAILED"}]}):
                with self.assertRaises(LotdError):
                    Client("offline.invalid:3217", False).parse(json.dumps(raw), parser)

    def test_container_import_parser_scopes_casing_codes_and_boolean_result(self):
        from lotd_client import Client, LotdError
        client = Client("unit.example:3217", False)
        def response(entities, code="RC_OK"):
            return {"cid": "123", "response_payload": [{"code": code, "payload": [{
                "@type": "type.googleapis.com/lotd.EquipmentPackPayloadList", "equipment_pack": entities}]}]}
        def entity(success, entity_id=1):
            return {"id": entity_id, "actions": {"write_file_to_container": {"response": {"success": success}}}}
        for success in (True, False):
            value = client.parse(json.dumps(response([entity(success)])), "container_action", "write_file_to_container")
            self.assertIs(value["success"], success)
        camel = {"cid": "123", "responsePayload": [{"code": "RC_OK", "payload": [{"equipmentPack": [
            {"id": 2, "actions": {"writeFileToContainer": {"response": {"success": False}}}},
            {"id": 1, "actions": {"writeFileToContainer": {"response": {"success": True, "future": 0}}}}]}]}]}
        self.assertTrue(client.parse(json.dumps(camel), "container_action", "write_file_to_container")["success"])
        invalid = [{"cid": "123"}, {}, {"cid": "wrong"}, response([]), response([entity(True, 2)]),
                   response([entity(True), entity(True)]), response([entity(True)], "RC_OPERATION_FAILED"),
                   response([entity(True)], "RC_MISSING_VALUE"), response([{"id": 1, "actions": {}}])]
        for success in (None, 1, "true", {"value": True}):
            invalid.append(response([entity(success)]))
        missing = entity(True); missing["actions"]["write_file_to_container"]["response"] = {}
        invalid.append(response([missing]))
        wrong_cid = response([entity(True)]); wrong_cid["cid"] = "wrong"; invalid.append(wrong_cid)
        conflict = response([entity(True)])
        conflict["response_payload"] += response([entity(False)])["response_payload"]
        invalid.append(conflict)
        mixed = response([entity(True)])
        mixed["response_payload"].append({"code": "RC_OPERATION_FAILED"}); invalid.append(mixed)
        for data in invalid:
            with self.assertRaises(LotdError):
                client.parse(json.dumps(data), "container_action", "write_file_to_container")
        with self.assertRaises(LotdError):
            client.parse("not JSON", "container_action", "write_file_to_container")

    def test_hvdac_channel_parser_returns_native_values_and_verifies_selected_field(self):
        from lotd_client import Client, LotdError
        client = Client("offline.invalid:3217", False)
        channel = {"id": 7, "config": {"target_voltage": 0, "dac_count": -2},
                   "state": {"actual_voltage": -1.25, "oor_failure": False}}
        def raw():
            return json.dumps({"response_payload": [{"code": "RC_OK", "payload": [
                {"mems_module": [{"id": 1, "dac_cards": [{"id": 2,
                    "dac_chips": [{"id": 3, "channels": [channel]}]}]}]}]}]})
        client._exchange = {"operation": "RT_GET"}
        values = client.parse(raw(), "channel", "2", "3", "7")
        self.assertEqual(values, {"target_voltage": 0, "actual_voltage": -1.25,
                                  "oor_failure": False, "dac_count": -2})
        for field, expected_type in (("target_voltage", int), ("actual_voltage", float),
                                     ("oor_failure", bool), ("dac_count", int)):
            self.assertIs(type(values[field]), expected_type)
        self.assertEqual(client._exchange["data"], values)
        del channel["config"]["dac_count"]
        self.assertNotIn("dac_count", client.parse(raw(), "channel", "2", "3", "7"))
        # Readback requires only the field being verified, never actual telemetry.
        del channel["state"]
        for parser, field, value, expected in (("channel", "target_voltage", 8.25, "8.250"),
                                                ("dac_count", "dac_count", -2, "-2")):
            channel["config"] = {field: value}
            values = client.parse(raw(), parser + "_set", "2", "3", "7", expected)
            self.assertEqual(values, {field: value})
            self.assertEqual(client._exchange["data"], values)
            with self.assertRaises(LotdError):
                client.parse(raw(), parser + "_set", "2", "3", "7", "99")
        for invalid in (True, 1.5, "2"):
            channel["config"] = {"dac_count": invalid}
            with self.assertRaisesRegex(LotdError, "integer DAC count"):
                client.parse(raw(), "dac_count", "2", "3", "7")

    def test_identity_parser_scopes_entity_and_rejects_duplicates(self):
        from lotd_client import Client, LotdError
        client = Client("unused:3217", False)
        response = {"code": "RC_OK", "equipmentPack": [
            {"id": 2, "sensor": [{"id": "temp", "state": {"value": 123}}]},
            {"id": 1, "sensor": [{"id": "temp", "state": {"value": 456}}]}]}
        args = ("identity_sensor", "equipment_pack", "2", "sensor", "temp")
        self.assertEqual(client.parse(json.dumps(response), *args), 123)
        response["equipmentPack"].append(response["equipmentPack"][0])
        with self.assertRaises(LotdError):
            client.parse(json.dumps(response), *args)
        with self.assertRaises(LotdError):
            client.parse(json.dumps({"equipmentPack": {"id": 1}}), *args)

    def test_fragment_merging_missing_only_and_conflicts(self):
        from lotd_client import Client, LotdError
        def entry(code, info):
            return {"code": code, "payload": [{"mems_module": [{"id": 1, "common_voltage_card": [
                {"id": 1, "inventory": {"entity_info": info}}]}]}]}
        args = ("fru_section", "common_voltage_card", "1", "inventory", "cv")
        response = {"response_payload": [entry("RC_OK", {"serial_no": "CV-1"}),
                                         entry("RC_OK", {"hardware_rev": "3"}),
                                         entry("RC_MISSING_VALUE", {"clei_code": ""})]}
        client = Client("unused:3217", False)
        value = client.parse(json.dumps(response), *args)
        self.assertEqual(value, {"entity_info": {"serial_no": "CV-1", "hardware_rev": "3"}})
        self.assertIn("RC_MISSING_VALUE", client.response_notes[0])
        with self.assertRaises(LotdError):
            client.parse(json.dumps(entry("RC_MISSING_VALUE", {"serial_no": ""})), *args)
        for entries in ([entry("RC_MISSING_VALUE", {"serial_no": ""})],
                        [entry("RC_OK", {"serial_no": "one"}), entry("RC_OK", {"serial_no": "two"})],
                        [entry("RC_OK", {"serial_no": "one"}), entry("RC_UNKNOWN_FIELD", {})]):
            with self.assertRaises(LotdError):
                client.parse(json.dumps({"response_payload": entries}), *args)

    def test_fragment_merging_distinguishes_booleans_from_numbers_recursively(self):
        from lotd_client import Client, LotdError
        client = Client("offline.invalid:3217", False)
        def response(values):
            return json.dumps({"response_payload": [{"code": "RC_OK", "payload": [
                {"fans_module": [{"id": 1, "config": {"future_field": value}}]}]}
                for value in values]})
        shapes = (lambda value: value, lambda value: {"nested": value},
                  lambda value: [value], lambda value: [{"nested": [value]}])
        for boolean, number in ((False, 0), (True, 1), (False, 0.0), (True, 1.0)):
            for shape in shapes:
                for left, right in ((boolean, number), (number, boolean)):
                    with self.subTest(left=left, right=right, shape=shape(left)):
                        with self.assertRaisesRegex(LotdError, "Conflicting successful response fragments"):
                            client.parse(response([shape(left), shape(right)]), "read_section", "fans_module", "1", "config")
        # Compatible JSON numeric representations and identical nested values still merge.
        for left, right in ((0, 0.0), (1.0, 1), (False, False), (True, True),
                            ([{"value": 0, "failed": False}], [{"value": 0.0, "failed": False}])):
            with self.subTest(compatible=(left, right)):
                result = client.parse(response([left, right]), "read_section", "fans_module", "1", "config")
                self.assertEqual(json.dumps(result["future_field"]), json.dumps(left))
        # A numeric duplicate must not be hidden behind a valid action success boolean.
        for values in ((True, 1), (1, True)):
            raw = json.dumps({"cid": "123", "response_payload": [{"code": "RC_OK", "payload": [
                {"equipment_pack": [{"id": 1, "actions": {"write_file_to_container": {
                    "response": {"success": value}}}}]}]} for value in values]})
            with self.assertRaisesRegex(LotdError, "Conflicting successful response fragments"):
                client.parse(raw, "container_action", "write_file_to_container")

    def test_ps_parser_scopes_codes_casing_fragments_and_missing_notes(self):
        from lotd_client import Client, LotdError
        def entry(code, modules):
            return {"code": code, "payload": [{"psModule": modules}]}
        def response(entries):
            return json.dumps({"responsePayload": entries})
        client = Client("unit.example:3217", False)
        good = entry("RC_OK", [{"id": "1", "state": {"missing": False, "readTemperature1": 0},
            "inventory": {"entityDescription": "PS", "entityInfo": {"serialNo": "SERIAL"}}, "newField": {"raw": -3}},
            {"id": 2, "state": {"missing": True}}])
        raw = response([good, entry("RC_OK", [{"id": "1", "inventory": {"entityInfo": {"hardwareRev": "A"}}}]),
            entry("RC_MISSING_VALUE", [{"id": 1, "inventory": {"entityInfo": {"cleiCode": ""}}}]),
            entry("RC_FRU_MISSING", [{"id": 2, "sensor": [{"id": "temp", "state": {"value": 0}}]}])])
        value = client.parse(raw, "ps_entity", "1")
        self.assertEqual(value["inventory"]["entityInfo"], {"serialNo": "SERIAL", "hardwareRev": "A"})
        self.assertEqual(value["newField"], {"raw": -3})
        self.assertTrue(any("cleiCode" in note for note in client.response_notes))
        for entries in ([good, entry("RC_FRU_MISSING", [{"id": 1}])],
                        [good, {"code": "RC_FRU_MISSING"}],
                        [good, entry("RC_UNKNOWN_FIELD", [{"id": 2}])],
                        [entry("RC_FRU_MISSING", [{"id": 2, "state": {"missing": True}}])],
                        [entry("RC_OK", [{"id": 1, "state": {"missing": "false"}}])],
                        [entry("RC_OK", [{"id": 1, "state": {"missing": False}}] * 2)],
                        [good, entry("RC_OK", [{"id": 1, "state": {"missing": True}}])]):
            with self.assertRaises(LotdError):
                client.parse(response(entries), "ps_entity", "1")

    def test_cv_channel_parser_returns_native_values_for_reads_and_verified_readback(self):
        from lotd_client import Client, LotdError
        client = Client("offline.invalid:3217", False)
        for target, actual in ((0, -1.25), (8.25, 0)):
            channel = {"id": 1, "config": {"targetVoltage": target},
                       "state": {"actualVoltage": actual}}
            def raw():
                return json.dumps({"response_payload": [{"code": "RC_OK", "payload": [
                    {"memsModule": [{"id": 1, "commonVoltageCard": [
                        {"id": 1, "channels": [channel]}]}]}]}]})
            client._exchange = {"operation": "RT_GET"}
            result = client.parse(raw(), "cv_channel", "1")
            self.assertEqual(result, {"target_voltage": target, "actual_voltage": actual})
            self.assertIs(type(result["target_voltage"]), type(target))
            self.assertIs(type(result["actual_voltage"]), type(actual))
            self.assertEqual(client._exchange["data"], result)
            # Target-only readback verifies the write without requiring telemetry.
            del channel["state"]
            result = client.parse(raw(), "cv_channel_set", "1", str(target))
            self.assertEqual(result, {"target_voltage": target})
            self.assertEqual(client._exchange["data"], result)
            with self.assertRaises(LotdError):
                client.parse(raw(), "cv_channel_set", "1", str(target + 1))

    def test_mirror_target_parser_returns_native_axis_values_and_exact_verification(self):
        from lotd_client import Client, LotdError
        client = Client("offline.invalid:3217", False)
        for axis in ("x", "y"):
            field = axis + "_tilt_voltage"
            config = {}
            def raw():
                return json.dumps({"responsePayload": [{"code": "RC_OK", "payload": [
                    {"memsModule": [{"id": 1, "mirrors": [{"id": 2, "config": config}]}]}]}]})
            for value in (0, -2.5, 8.25):
                config.clear()
                config[axis + "TiltVoltage"] = value
                # Only the selected axis is required; no state/actual value is needed.
                client._exchange = {"operation": "RT_GET"}
                result = client.parse(raw(), "mirror", "2", axis)
                self.assertEqual(result, {field: value})
                self.assertIs(type(result[field]), type(value))
                self.assertEqual(client._exchange["data"], result)
                requested = f"{value:.3f}" if isinstance(value, float) else str(value)
                result = client.parse(raw(), "mirror_set", "2", axis, requested)
                self.assertEqual(result, {field: value})
                self.assertEqual(client._exchange["data"], result)
                with self.assertRaisesRegex(LotdError, "Mirror target verification failed"):
                    client.parse(raw(), "mirror_set", "2", axis, "99")
            for invalid in (True, "8.25", None):
                config[axis + "TiltVoltage"] = invalid
                with self.assertRaises(LotdError):
                    client.parse(raw(), "mirror", "2", axis)

    def test_connection_parser_returns_native_values_and_verifies_selected_field(self):
        from lotd_client import Client, LotdError
        client = Client("offline.invalid:3217", False)
        config = {}
        def raw():
            return json.dumps({"responsePayload": [{"code": "RC_OK", "payload": [
                {"connectionManager": [{"id": 1, "portArray": [{"id": 2, "config": config}]}]}]}]})
        for peer, blocking in ((0, False), (10001, True)):
            config = {"peerPortNumber": peer, "opticalBlockingEnable": blocking}
            expected = {"peer_port_number": peer, "optical_blocking_enable": blocking}
            for parser, args in (("connection", ()), ("connection_peer_set", (str(peer),)),
                                 ("connection_block_set", (str(blocking).lower(),))):
                client._exchange = {"operation": "RT_GET"}
                result = client.parse(raw(), parser, "2", *args)
                self.assertEqual(result, expected)
                self.assertIs(type(result["peer_port_number"]), int)
                self.assertIs(type(result["optical_blocking_enable"]), bool)
                self.assertEqual(client._exchange["data"], result)
        # A focused readback may contain only the field being verified.
        for parser, key, field, value, wanted, mismatch in (
                ("connection_peer_set", "peerPortNumber", "peer_port_number", 0, "0", "10001"),
                ("connection_block_set", "opticalBlockingEnable", "optical_blocking_enable", False, "false", "true")):
            config = {key: value}
            result = client.parse(raw(), parser, "2", wanted)
            self.assertEqual(result, {field: value})
            self.assertEqual(client._exchange["data"], result)
            with self.assertRaisesRegex(LotdError, "verification failed"):
                client.parse(raw(), parser, "2", mismatch)


    def test_live_runner_hvdac_card_selection_plans_validation_and_set_scope(self):
        import io
        from contextlib import redirect_stderr
        from live_test_lotd import checks_for, parse_args, set_checks_for
        for selection, first in (("5,8", "5"), ("8,5", "8"), ("11", "11")):
            args = parse_args(["offline.invalid", "--model", "R320", "--skip-lds", "--hvdac-cards", selection])
            checks = checks_for(args.model, args.skip_lds, hvdac_cards=args.hvdac_cards)
            self.assertEqual(len(checks), 63)
            self.assertTrue(all(c.arguments[0] == "get" and c.arguments[1] != "lds" for c in checks))
            for check in checks:
                if check.arguments[1] == "hvdac":
                    self.assertEqual(check.arguments[2], selection if check.arguments[3] == "led" else first)
            outputs = {"Selected HVDAC power and fault LEDs": f"entity ID {first}\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_OFF",
                       "HVDAC channel target and state": "Target voltage: 215",
                       "HVDAC DAC count": "DAC count: 0"}
            setters, _ = set_checks_for("R320", True, outputs, hvdac_cards=selection)
            hvdac = [c for c in setters if c.arguments[1] == "hvdac"]
            self.assertEqual(len(hvdac), 2)  # R320 numeric writes excluded; LEDs retained.
            self.assertTrue(all(c.arguments[2] == first for c in hvdac))
            missing, _ = set_checks_for("R320", True, {}, hvdac_cards=selection)
            self.assertFalse(any(c.arguments[1] == "hvdac" for c in missing))
        for model, invalid in (("R320", ""), ("R320", "5,5"), ("R320", "0"), ("R320", "12"),
                               ("R320", "5-8"), ("R320", "all"), ("R320", "05,8"), ("R320", "5, 8"),
                               ("R300G", "11"), ("R300L", "11")):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                parse_args(["offline.invalid", "--model", model, "--hvdac-cards", invalid])
            self.assertEqual(error.exception.code, 2)


    def test_live_runner_mirror_selection_preserves_defaults_models_and_get_scope(self):
        import io
        from contextlib import redirect_stderr
        from unittest.mock import patch
        import live_test_lotd as runner
        default = runner.checks_for("R320", True, hvdac_cards="5,8")
        def per_mirror(check):
            return check.arguments[1] in ("mirror", "mems") and check.arguments[2][0].isdigit()
        self.assertEqual([c.arguments[2] for c in default if per_mirror(c)], ["1", "321", "1-3", "1-3", "1-3"])
        for selection in ("500", "500,501", "501,500", "640"):
            args = runner.parse_args(["offline.invalid", "--model", "R320", "--skip-lds", "--hvdac-cards", "5,8", "--mirror-ports", selection])
            checks = runner.checks_for(args.model, args.skip_lds, hvdac_cards=args.hvdac_cards, mirror_ports=args.mirror_ports)
            self.assertEqual(len(checks), 63)
            self.assertEqual(sum("--json" in c.arguments for c in checks), 6)
            self.assertEqual([c for c in checks if not per_mirror(c)], [c for c in default if not per_mirror(c)])
            scoped = [c for c in checks if per_mirror(c)]
            self.assertEqual([c.arguments[2] for c in scoped], [selection.split(",")[0]] * 2 + [selection] * 3)
            self.assertTrue(all(c.arguments[0] == "get" for c in checks))
            for port in selection.split(","):
                self.assertIn(f"Mirror port {port} state:", scoped[2].expected_text)
                self.assertIn(f"Mirror port {port} configuration:", scoped[4].expected_text)
            setters, skipped = runner.set_checks_for("R320", True, {scoped[0].name: "X target voltage: 0"})
            self.assertFalse(any(c.arguments[1] in ("mirror", "mems") for c in setters))
            self.assertTrue(any("mirror SET checks: excluded" in note for note in skipped))
        for model, valid in (("R300G", "501,640"), ("R300L", "500,620"), ("R300", "501,620")):
            runner.parse_args(["offline.invalid", "--model", model, "--mirror-ports", valid])
        for model, invalid in (("R320", ""), ("R320", "0"), ("R320", "641"), ("R320", "10001"),
                               ("R320", "0500"), ("R320", "500,500"), ("R320", "500-501"),
                               ("R320", "all"), ("R320", "500, 501"), ("R320", "500,"),
                               ("R300G", "500"), ("R300L", "301"), ("R300", "640")):
            with patch.object(runner, "_run") as request, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                runner.main(["offline.invalid", "--model", model, "--mirror-ports", invalid])
            self.assertEqual(error.exception.code, 2)
            request.assert_not_called()
        with patch.object(runner, "checks_for", side_effect=RuntimeError("offline plan boundary")) as plan:
            with self.assertRaisesRegex(RuntimeError, "offline plan boundary"):
                runner.main(["offline.invalid", "--model", "R320", "--mirror-ports", "500"])
            self.assertEqual(plan.call_args.kwargs["mirror_ports"], "500")


    def test_r320_runner_excludes_coupled_hvdac_writes_with_valid_baselines(self):
        from live_test_lotd import set_checks_for, checks_for
        outputs = {"HVDAC channel target and state": "Target voltage: 209\nActual voltage: 208\n",
                   "HVDAC DAC count": "DAC count: 0\n",
                   "CV channels 1-3": "1 | 90000 | 90000 | Read OK\n"}
        for model in ("R320", "r320"):
            for cards in (None, "5,8", "8,5"):
                first = cards.split(",")[0] if cards else "1"
                led_name = "Selected HVDAC power and fault LEDs" if cards else "All HVDAC power and fault LEDs"
                captured = {**outputs, led_name: f"entity ID {first}\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_OFF\n"}
                checks, skipped = set_checks_for(model, True, captured, hvdac_cards=cards)
                hvdac = [c for c in checks if c.arguments[1] == "hvdac"]
                self.assertEqual(len(hvdac), 2)
                self.assertTrue(all(c.arguments[2:4] == (first, "led") for c in hvdac))
                self.assertTrue(any(c.arguments[1] == "cv" for c in checks))
                self.assertTrue(any("coupled baseline preservation is unconfirmed" in s for s in skipped))
        for model in ("R300", "R300G", "R300L"):
            checks, _ = set_checks_for(model, True, outputs)
            self.assertEqual([c.arguments[3] for c in checks if c.arguments[1] == "hvdac"], ["channel", "dac-count"])
        gets = [c for c in checks_for("R320", True, hvdac_cards="5,8") if c.arguments[1] == "hvdac"]
        self.assertEqual(len(gets), 8)
        self.assertTrue(all(c.arguments[0] == "get" for c in gets))

    def test_sensor_sections_indent_children_and_labels_preserve_raw_input(self):
        from lotd import format_sensor_details, format_fields
        sensor = {"id": "temp", "state": {"value": 0, "failed": True},
                  "config": {"high_threshold": {"threshold": 8500, "hysteresis": 500}},
                  "inventory": {"entity_description": "Temperature sensor"}}
        original = json.loads(json.dumps(sensor))
        text = format_sensor_details([sensor])
        self.assertIn("Sensor temp:\n  State:\n    Value: 0\n    Failed: true", text)
        self.assertIn("\n  Configuration:\n    High threshold:\n      Threshold: 8500", text)
        self.assertIn("\n  Inventory:\n    Description: Temperature sensor", text)
        self.assertEqual(sensor, original)
        labels = format_fields({"kiCoeffPhase1": 400, "num_loop_phase1": 10, "status_fans_1_2": 0, "future_field": False})
        self.assertIn("Ki coefficient phase 1: 400", labels)
        self.assertIn("Number of loops phase 1: 10", labels)
        self.assertIn("Status fans 1/2: 0", labels)
        self.assertIn("Future field: false", labels)


class LotdPortableCLITests(unittest.TestCase):
    """Exercise main, transport bookkeeping and parsers without launching a process."""

    @staticmethod
    def reply(body, code="RC_OK"):
        return {"cid": "123", "response_payload": [{"code": code, "payload": [body]}]}

    def invoke(self, arguments, replies=(), *, installed=True):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from unittest.mock import patch
        import lotd
        from lotd_client import REQUEST_TIMEOUT_SECONDS

        requests = []
        responses = iter(replies)
        def transport(command, **kwargs):
            self.assertEqual(command, ["grpcurl", "-plaintext", "-d", "@",
                                       "offline.invalid:3217", "lotd.Operations.RequestOp"])
            self.assertTrue(kwargs["text"])
            self.assertTrue(kwargs["capture_output"])
            self.assertEqual(kwargs["timeout"], REQUEST_TIMEOUT_SECONDS)
            request = json.loads(kwargs["input"])
            self.assertEqual(request["lotd_version"], "LV_1")
            self.assertEqual(request["cid"], "123")
            requests.append(request)
            response = next(responses)  # An unexpected extra request fails the test.
            if isinstance(response, BaseException):
                raise response
            return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["offline.invalid", *arguments, "--json"]
        with patch("lotd_client.shutil.which", return_value="offline-mock" if installed else None), \
             patch("lotd_client.subprocess.run", side_effect=transport) as run, \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = lotd.main(argv)
        self.assertEqual(run.call_count, len(requests))
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["exit_code"], code)
        self.assertEqual(report["command"], argv[:-1])
        if report["endpoint"] is not None:
            self.assertEqual(report["endpoint"], "offline.invalid:3217")
        return code, report, stderr.getvalue(), requests

    def test_reset_and_model_aliases_keep_json_stdout_clean(self):
        response = self.reply({"reset_manager": [{"id": 1, "state": {
            "last_reset_reason": {"value": "RR_POWER_ON"}}}]})
        for model, canonical in (("R300L", "R300-L"), ("r300g", "R300-G"), ("R320", "R320")):
            with self.subTest(model=model):
                code, report, human, requests = self.invoke(
                    ["get", "reset", "last-reset-reason", "--model", model], [response])
                self.assertEqual(code, 0, human)
                self.assertEqual(report["model"], canonical)
                self.assertEqual(report["results"][0]["data"], {"last_reset_reason": "RR_POWER_ON"})
                self.assertEqual(report["results"][0]["response"], response)
                self.assertEqual(report["summary"]["successful_ids"], ["1"])
                self.assertFalse(report["results"][0]["verified"])
                self.assertEqual(report["errors"], [])
                self.assertIn("Last reset reason: RR_POWER_ON", human)
                self.assertEqual(requests[0]["payload"][0], {
                    "@type": "type.googleapis.com/lotd.ResetManagerPayloadList",
                    "reset_manager": {"id": 1, "state": {}}})

    def test_firmware_views_select_versions_and_hide_response_json(self):
        for load, load_id in (("active", 1), ("inactive", 2)):
            with self.subTest(load=load):
                version = "SONiC-OS-" + load + "-build"
                response = self.reply({"equipment_pack": [{"id": 1, "load_version": [
                    {"id": load_id, "inventory": {"entity_description": "Image"},
                     "state": {"version": version}}]}]})
                code, report, human, requests = self.invoke(["get", "shelf", "firmware-version", load], [response])
                self.assertEqual(code, 0, human)
                self.assertIn(f"{load.capitalize()} firmware load version: {version}", human)
                self.assertNotIn('"inventory"', human)
                self.assertEqual(report["results"][0]["response"], response)
                self.assertEqual(requests[0]["payload"][0]["equipment_pack"],
                                 {"id": 1, "load_version": {"id": load_id}})

    def test_absent_supply_keeps_batch_failure_and_no_zero_measurement(self):
        replies = [self.reply({"ps_module": [{"id": 1, "state": {
            "missing": False, "read_temperature1": 2400}}]}),
            self.reply({"ps_module": [{"id": 2, "state": {
                "missing": True, "read_temperature1": 0}}]})]
        code, report, human, requests = self.invoke(["get", "psu", "all", "temperature"], replies)
        self.assertEqual(code, 1, human)
        self.assertEqual(report["summary"]["successful_ids"], ["1"])
        self.assertEqual(report["summary"]["failed_ids"], ["2"])
        self.assertEqual(report["summary"]["verified_ids"], [])
        self.assertEqual(report["results"][0]["data"], {"read_temperature1": 2400})
        self.assertEqual(report["results"][1]["data"], {"missing": True})
        self.assertIn("Temperature measurement is unavailable (supply missing)", human)
        self.assertEqual([r["payload"][0]["ps_module"] for r in requests], [{"id": 1}, {"id": 2}])

    def test_cid_only_write_requires_matching_selected_field_readback(self):
        for actual, expected_code in ((90000, 0), (89999, 1)):
            with self.subTest(readback=actual):
                readback = self.reply({"mems_module": [{"id": 1, "common_voltage_card": [
                    {"id": 1, "channels": [{"id": 1, "config": {"target_voltage": actual}}]}]}]})
                code, report, human, requests = self.invoke(
                    ["set", "cv", "channel", "1", "90000", "--model", "R320"],
                    [{"cid": "123"}, readback])
                self.assertEqual(code, expected_code, human)
                self.assertEqual([r["type"] for r in requests], ["RT_SET", "RT_GET"])
                self.assertIsNone(report["results"][0]["accepted"])
                self.assertFalse(report["results"][0]["verified"])
                self.assertEqual(report["summary"]["verified_ids"], [] if code else ["1"])
                self.assertEqual(report["results"][1]["verified"], code == 0)
                self.assertEqual(bool(report["errors"]), bool(code))
                self.assertIn("no status code", human)
                channel = requests[1]["payload"][0]["memsModule"]["common_voltage_card"]["channels"][0]
                self.assertEqual(channel, {"id": 1, "config": {"target_voltage": 0}})

    def test_timeouts_stop_batches_and_leave_writes_unverified(self):
        from lotd_client import REQUEST_TIMEOUT_SECONDS
        for arguments, writing in ((["get", "fan", "all", "speed"], False),
                                   (["set", "cv", "channel", "1", "90000"], True)):
            with self.subTest(arguments=arguments):
                code, report, human, requests = self.invoke(arguments, [
                    subprocess.TimeoutExpired("grpcurl", REQUEST_TIMEOUT_SECONDS)])
                self.assertEqual(code, 1, human)
                self.assertEqual(len(requests), 1)
                self.assertEqual(report["summary"]["failed_ids"], ["1"])
                self.assertEqual(report["summary"]["not_attempted_ids"], [] if writing else ["2", "3"])
                self.assertEqual(report["summary"]["verified_ids"], [])
                self.assertEqual(report["results"][0]["error"]["kind"], "transport")
                self.assertIsNone(report["results"][0]["accepted"])
                self.assertFalse(report["results"][0]["verified"])
                self.assertEqual("completion is unknown" in human, writing)

    def test_conflicting_boolean_numeric_fragments_fail_with_raw_json_preserved(self):
        response = self.reply({"fans_module": [{"id": 1, "config": {"pwm_target": 0}}]})
        response["response_payload"] += self.reply({"fans_module": [
            {"id": 1, "config": {"pwm_target": False}}]})["response_payload"]
        code, report, human, requests = self.invoke(["get", "fan", "1", "config"], [response])
        self.assertEqual(code, 1, human)
        self.assertEqual(len(requests), 1)
        self.assertEqual(report["summary"]["failed_ids"], ["1"])
        self.assertEqual(report["summary"]["successful_ids"], [])
        self.assertEqual(report["summary"]["verified_ids"], [])
        self.assertEqual(report["results"][0]["response"], response)
        self.assertIsNone(report["results"][0]["data"])
        self.assertEqual(report["results"][0]["error"]["kind"], "response")
        self.assertIn("Conflicting successful response fragments", human)
        self.assertTrue(report["errors"])

    def test_usage_and_dependency_errors_do_not_launch_transport(self):
        for arguments in (["set", "fan", "1", "auto"],
                          ["get", "shelf", "temp", "--model", "unsupported"]):
            with self.subTest(arguments=arguments):
                code, report, human, requests = self.invoke(arguments)
                self.assertEqual(code, 2, human)
                self.assertEqual(requests, [])
                self.assertEqual(report["results"], [])
                self.assertEqual(report["errors"][0]["kind"], "usage")
        code, report, human, requests = self.invoke(["get", "reset", "state"], installed=False)
        self.assertEqual(code, 1, human)
        self.assertEqual(requests, [])
        self.assertEqual(report["results"][0]["error"]["kind"], "dependency")


@unittest.skipUnless(
    os.name == "posix" and pty is not None,
    "CLI integration tests require POSIX PTYs and the mock launcher; run in WSL/Linux",
)
class LotdCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not CLI.is_file():
            raise RuntimeError("lotd.py must be in the same directory as test_lotd.py")
        result = subprocess.run([sys.executable, str(CLI), "help", "fan"],
                                text=True, capture_output=True, timeout=5)
        if result.returncode != 0 or "<power|fault|both|all>" not in result.stdout:
            raise RuntimeError(
                "This test suite requires the current lotd.py with the LED 'all' alias. "
                "Replace the local lotd.py with the current copy, then run the tests again."
            )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lotd-tests-")
        self.root = pathlib.Path(self.temp.name)
        self.capture = self.root / "capture"
        self.mock_bin = self.root / "mock-bin"
        self.mock_bin.mkdir()
        mock_launcher = self.mock_bin / "grpcurl"
        mock_launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import runpy\n"
            f"runpy.run_path({str(MOCK)!r}, run_name='__main__')\n"
        )
        mock_launcher.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(self.mock_bin) + os.pathsep + self.env.get("PATH", ""),
            "MOCK_CAPTURE_DIR": str(self.capture),
            "MOCK_VALUE": "LC_OFF",
        })
        self.env.pop("NO_COLOR", None)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def reset_capture(self) -> None:
        self.capture.mkdir(exist_ok=True)
        for path in self.capture.glob("request-*.json"):
            path.unlink()

    def run_cli(self, scenario: str, *args: str, stdin=None, env=None) -> subprocess.CompletedProcess[str]:
        self.reset_capture()
        run_env = self.env.copy()
        run_env["MOCK_SCENARIO"] = scenario
        if env:
            run_env.update(env)
        return subprocess.run([sys.executable, str(CLI), *args], input=stdin,
                              text=True, capture_output=True, env=run_env, timeout=15)

    def run_tty(self, scenario: str, args: tuple[str, ...], answer: str | None = None,
                interrupt_at: str | None = None, request_count: int | None = None,
                env=None) -> tuple[int, str]:
        self.reset_capture()
        run_env = self.env.copy()
        run_env["MOCK_SCENARIO"] = scenario
        if env:
            run_env.update(env)
        master, slave = pty.openpty()
        proc = subprocess.Popen([sys.executable, str(CLI), *args], stdin=slave,
                                stdout=slave, stderr=slave, env=run_env,
                                close_fds=True, start_new_session=True)
        os.close(slave)
        output = bytearray()
        sent_answer = answer is None
        sent_interrupt = False
        deadline = time.monotonic() + 12
        try:
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    os.killpg(proc.pid, signal.SIGKILL)
                    self.fail("timed out waiting for interactive command")
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        chunk = b""
                    output.extend(chunk)
                text = output.decode(errors="replace")
                if not sent_answer and "[y/N]" in text:
                    os.write(master, ((answer or "") + "\n").encode())
                    sent_answer = True
                if interrupt_at and not sent_interrupt and interrupt_at in text:
                    count = len(list(self.capture.glob("request-*.json")))
                    if request_count is None or count >= request_count:
                        os.killpg(proc.pid, signal.SIGINT)
                        sent_interrupt = True
            while select.select([master], [], [], 0)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            return proc.wait(), output.decode(errors="replace")
        finally:
            os.close(master)

    def requests(self) -> list[dict]:
        files = sorted(self.capture.glob("request-*.json"),
                       key=lambda path: int(path.stem.split("-")[-1]))
        return [json.loads(path.read_text()) for path in files]

    def assert_has(self, text: str, output: str) -> None:
        self.assertIn(text, output)

    def test_help_documents_all_alias_and_led_commands(self):
        result = subprocess.run([sys.executable, str(CLI), "help", "fan"], text=True,
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn("get fan <ids> led [<power|fault|both|all>]", result.stdout)
        self.assertIn("Use both or all (or omit the LED name on GET) to read both LEDs.", result.stdout)

    def test_audit_runner_interrupt_reapply_requires_verified_readback(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        import live_test_lotd as runner
        check = runner.Check("baseline", ("get", "shelf", "state"), ("baseline",))
        setter = runner.SetCheck("write", ("set", "shelf", "led", "power", "on"), "LC_ON")
        for restored, expected in (("Cancelled; no changes sent.", False), ("", False),
                                   ("Verified: LC_ON\nOutcome:        Completed successfully", True)):
            responses = [subprocess.CompletedProcess([], 0, "baseline", ""), KeyboardInterrupt(),
                         subprocess.CompletedProcess([], 0, restored, "")]
            out = io.StringIO()
            with patch.object(runner, "checks_for", return_value=[check]), \
                 patch.object(runner, "set_checks_for", return_value=([setter], [])), \
                 patch.object(runner, "_run", side_effect=responses), \
                 patch.object(runner.sys.stdin, "isatty", return_value=True), \
                 patch("builtins.input", return_value="y"), redirect_stdout(out):
                self.assertEqual(runner.main(["offline.invalid", "--include-set"]), 130)
            self.assertEqual("Baseline value reapplied." in out.getvalue(), expected)

    def test_audit_request_timeout_marks_unknown_writes_and_never_retries(self):
        from unittest.mock import patch
        from lotd_client import Client, JsonReport, LotdError, REQUEST_TIMEOUT_SECONDS
        for operation in ("RT_GET", "RT_SET", "RT_ACTION"):
            report = JsonReport([])
            client = Client("offline.invalid:3217", False, report)
            client.current_id = "1"
            with patch("lotd_client.shutil.which", return_value="mock-grpcurl"), \
                 patch("lotd_client.subprocess.run", side_effect=subprocess.TimeoutExpired("grpcurl", REQUEST_TIMEOUT_SECONDS)) as run:
                with self.assertRaises(LotdError) as error:
                    client.request(operation, "EquipmentPackPayloadList", {"equipment_pack": {"id": 1}})
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs["timeout"], REQUEST_TIMEOUT_SECONDS)
            self.assertEqual("completion is unknown" in str(error.exception), operation != "RT_GET")
            self.assertEqual(report.results[0]["error"]["kind"], "transport")
            self.assertIsNone(report.results[0]["accepted"])

    def test_audit_timeout_kills_and_reaps_mock_grpcurl_process(self):
        from unittest.mock import patch
        from lotd_client import Client, LotdError
        env = {**self.env, "MOCK_SCENARIO": "transport_hang"}
        with patch.dict(os.environ, env), patch("lotd_client.REQUEST_TIMEOUT_SECONDS", 1):
            with self.assertRaises(LotdError):
                Client("offline.invalid:3217", False).request("RT_GET", "EquipmentPackPayloadList", {"equipment_pack": {"id": 1}})
        pid = int((self.capture / "grpcurl.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(len(self.requests()), 1)

    def test_audit_timeout_stops_batch_and_temperature_fallback(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from unittest.mock import patch
        import lotd
        from lotd_client import LotdError
        for args in (("get", "fan", "all", "speed"), ("get", "hvdac", "1", "temperature")):
            out, err = io.StringIO(), io.StringIO()
            with patch("lotd.Client.request", side_effect=LotdError("context deadline exceeded")) as request, \
                 redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(lotd.main(["offline.invalid", *args]), 1)
            self.assertEqual(request.call_count, 1)
            if args[1] == "fan":
                self.assertIn("Not attempted (2): 2,3", out.getvalue()+err.getvalue())

    def test_r320_ps_temperature_aliases_batch_json_and_legacy_models(self):
        for component in ("ps", "psu"):
            for alias in ("temp", "temperature"):
                result, report = self.json_cli("r320_ps_reads", "unit.example", "get", component, "1", alias, "--model", "R320")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(report["results"][0]["data"], {"temperature": 3600})
                self.assertEqual(report["errors"], [])
                self.assertEqual(len(self.requests()), 1)
                self.assertEqual(self.requests()[0]["payload"][0]["ps_module"], {"id": 1})
                self.assertNotIn("read_temperature1", report["results"][0]["response"]["response_payload"][0]["payload"][0]["ps_module"][0]["state"])
        result = self.run_cli("r320_ps_reads", "unit.example", "get", "ps", "all", "temperature", "--model", "R320")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"1\s*\|\s*3600\s*\|\s*Read OK")
        self.assertRegex(result.stdout, r"2\s*\|\s*3500\s*\|\s*Read OK")
        result, report = self.json_cli("r320_ps_reads", "unit.example", "get", "ps", "1", "state", "--model", "R320")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("temperature", report["results"][0]["data"])
        self.assertNotIn("read_temperature1", report["results"][0]["data"])
        for model in ("R300", "R300G", "R300L"):
            result, report = self.json_cli("ps_reads", "unit.example", "get", "ps", "1", "temp", "--model", model)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(report["results"][0]["data"], {"read_temperature1": 2400})

    def test_r320_ps_temperature_scopes_sensors_missing_and_failed_data_without_retry(self):
        def response(sensors, owner=1, missing=False, code="RC_OK"):
            return {"response_payload": [{"code": code, "payload": [{"ps_module": [
                {"id": owner, "state": {"missing": missing, "read_temperature1": 9999}, "sensor": sensors}]}]}]}
        valid = {"id": "temp", "state": {"value": 0, "failed": True}}
        result, report = self.json_cli("r320_ps_reads", "unit.example", "get", "ps", "1", "temp", "--model", "R320",
                                      env={"MOCK_RESPONSE": json.dumps(response([valid]))})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["results"][0]["data"], {"temperature": 0})
        malformed = [response([valid], owner=2), response([valid], missing=True), response([]),
                     response([{**valid, "id": "other-temp"}]), response([valid, valid]),
                     response([{"id": "temp"}]), response([valid], code="RC_OPERATION_FAILED")]
        malformed += [response([{"id": "temp", "state": {"value": v}}]) for v in (True, "3600", None)]
        mixed = response([valid]); mixed["response_payload"].append({"code": "RC_OPERATION_FAILED"})
        malformed.append(mixed)
        for raw in malformed:
            result, report = self.json_cli("r320_ps_reads", "unit.example", "get", "ps", "1", "temp", "--model", "R320",
                                          env={"MOCK_RESPONSE": json.dumps(raw)})
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(report["summary"]["failed_ids"], ["1"])
            self.assertTrue(report["errors"])
            self.assertEqual(len(self.requests()), 1)

    def test_live_runner_selected_mirrors_match_cli_and_keep_unavailable_errors(self):
        from live_test_lotd import checks_for, json_report_error
        for selection in ("500", "500,501", "501,500"):
            checks = checks_for("R320", True, hvdac_cards="5,8", mirror_ports=selection)
            checks = [c for c in checks if c.arguments[1] in ("mirror", "mems") and c.arguments[2][0].isdigit()]
            self.assertEqual(len(checks), 5)
            for check in checks:
                result = self.run_cli("r320_selected_mirrors", "unit.example", *check.arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                for marker in check.expected_text:
                    self.assertIn(marker, result.stdout + result.stderr)
                requests = self.requests()
                expected = [int(p) for p in check.arguments[2].split(",")]
                self.assertEqual([r["payload"][0]["mems_module"]["mirrors"]["id"] for r in requests], expected)
                self.assertTrue(all(r["type"] == "RT_GET" for r in requests))
                if "--json" in check.arguments:
                    self.assertIsNone(json_report_error(result.stdout))
            result, report = self.json_cli("r320_selected_mirrors", "unit.example", "get", "mirror", selection, "state", "--model", "R320")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(report["results"][0]["data"], {"status": {"value": "MS_USABLE"},
                             "x_tilt_voltage": 0, "y_tilt_voltage": 0, "x_tilt_actual_voltage": 302,
                             "y_tilt_actual_voltage": 394, "il_delta": 0})
        result, report = self.json_cli("r320_selected_mirrors", "unit.example", "get", "mirror", "500,1", "state", "--model", "R320",
                                       env={"MOCK_MIRROR_ERROR_ID": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["summary"]["successful_ids"], ["500"])
        self.assertEqual(report["summary"]["failed_ids"], ["1"])
        self.assertEqual(len(self.requests()), 2)
        self.assertIn("RC_OPERATION_FAILED", report["errors"][0]["message"])

    def test_r320_humidity_sensor_source_flags_and_other_model_state_reads(self):
        from live_test_lotd import checks_for
        for component in ("mems", "mirror"):
            result, report = self.json_cli("r320_humidity", "unit.example", "get", component, "humidity", "--model", "R320")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(self.requests()), 1)
            self.assertEqual(report["results"][0]["data"],
                             {"id": "humidity", "value": 0, "oor_high": False, "oor_high_degrade": False, "failed": False})
            self.assertEqual(report["summary"]["successful_ids"], ["1"])
            self.assertEqual(report["errors"], [])
            self.assertIn("Humidity: 0", result.stderr)
            self.assertIn("Failed: false", result.stderr)
            check = next(check for check in checks_for("R320", True) if check.arguments[:3] == ("get", "mems", "humidity"))
            self.assertIn(check.expected_text[0], result.stderr)
        result = self.run_cli("r320_humidity", "unit.example", "get", "mems", "humidity", "--model", "R320")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(marker in result.stdout for marker in check.expected_text))
        for model in ("R300", "R300-L", "R300-G"):
            result, report = self.json_cli("read_details", "unit.example", "get", "mems", "humidity", "--model", model)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.requests()[0]["payload"][0]["mems_module"], {"id": 1, "state": {}})
            self.assertEqual(report["results"][0]["data"], {"relative_humidity": 4064, "relative_humidity_failed": False})
        result = self.run_cli("r320_humidity", "help", "mems")
        self.assertIn("On R320, get mems humidity", result.stdout)

    def test_r320_humidity_rejects_failed_fragments_wrong_ids_and_bad_values_without_retry(self):
        def response(sensors, owner=1, code="RC_OK"):
            return {"response_payload": [{"code": code, "payload": [{"mems_module": [{"id": owner, "sensor": sensors}]}]}]}
        valid = {"id": "humidity", "state": {"value": 0, "failed": True, "oor_high": True}}
        result, report = self.json_cli("r320_humidity", "unit.example", "get", "mems", "humidity", "--model", "R320",
                                       env={"MOCK_RESPONSE": json.dumps(response([valid]))})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["results"][0]["data"]["failed"])
        malformed = [response([valid], owner=2), response([{**valid, "id": "unrelated"}]),
                     response([valid, valid]), response([{"id": "humidity"}]),
                     response([valid], code="RC_OPERATION_FAILED"), response([valid], code="RC_MISSING_VALUE")]
        malformed += [response([{"id": "humidity", "state": {"value": value}}]) for value in (True, "0", None)]
        mixed = response([valid]); mixed["response_payload"].append({"code": "RC_OPERATION_FAILED"})
        malformed.append(mixed)
        for raw in malformed:
            result, report = self.json_cli("r320_humidity", "unit.example", "get", "mems", "humidity", "--model", "R320",
                                           env={"MOCK_RESPONSE": json.dumps(raw)})
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(report["summary"]["failed_ids"], ["1"])
            self.assertTrue(report["errors"])
            self.assertEqual(len(self.requests()), 1)

    def test_r320_shelf_temperature_aliases_use_exact_sensors_and_stable_json(self):
        cases = (("temp", "cpu-temp", "temperature", 4300), ("temperature", "cpu-temp", "temperature", 4300),
                 ("inlet-temp", "inlet-temp", "inlet_temperature", 3250),
                 ("inlet-temperature", "inlet-temp", "inlet_temperature", 3250),
                 ("outlet-temp", "outlet-temp", "outlet_temperature", 3300),
                 ("outlet-temperature", "outlet-temp", "outlet_temperature", 3300))
        for command, sensor_id, field, value in cases:
            result, report = self.json_cli("r320_temperatures", "unit.example", "get", "shelf", command, "--model", "R320")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(report["results"][0]["data"], {field: value})
            self.assertEqual(report["summary"]["successful_ids"], ["1"])
            self.assertEqual(len(self.requests()), 1)
            entity = self.requests()[0]["payload"][0]["equipment_pack"]
            self.assertEqual(entity, {"id": 1, "sensor": [{"id": sensor_id, "state": {"value": 0}}]})
            self.assertIn(str(value), result.stderr)
        for topic, marker in (("shelf", "cpu-temp"), ("temperature", "inlet-temp"), ("fan", "state.value")):
            result = self.run_cli("r320_temperatures", "help", topic)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(marker, result.stdout)

    def test_r320_fan_temperature_sensors_aliases_batches_and_transport_failures(self):
        for command in ("temp", "temperature"):
            result, report = self.json_cli("r320_temperatures", "unit.example", "get", "fan", "1", command, "--model", "R320")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(report["results"][0]["data"], {"temperature": 4100})
            self.assertEqual(self.requests()[0]["payload"][0]["fans_module"],
                             {"id": 1, "sensor": [{"id": "temp", "state": {"value": 0}}]})
        result = self.run_cli("r320_temperatures", "unit.example", "get", "fan", "all", "temperature", "--model", "R320")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fan readings:", result.stdout)
        self.assertRegex(result.stdout, r"3\s*\|\s*4100\s*\|\s*Read OK")
        for env, successful, failed, pending, count in (
                ({"MOCK_SENSOR_FAILURE_ID": "2"}, ["1", "3"], ["2"], [], 3),
                ({"MOCK_SENSOR_TIMEOUT_ID": "2"}, ["1"], ["2"], ["3"], 2)):
            result, report = self.json_cli("r320_temperatures", "unit.example", "get", "fan", "all", "temp", "--model", "R320", env=env)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(report["summary"]["successful_ids"], successful)
            self.assertEqual(report["summary"]["failed_ids"], failed)
            self.assertEqual(report["summary"]["not_attempted_ids"], pending)
            self.assertEqual(len(self.requests()), count)

    def test_r320_temperature_reads_reject_wrong_owners_sensors_and_failed_fragments(self):
        for key, component, sensor_id in (("equipment_pack", "shelf", "cpu-temp"), ("fans_module", "fan", "temp")):
            args = ("get", "shelf", "temp") if component == "shelf" else ("get", "fan", "1", "temp")
            def response(sensors, owner=1, code="RC_OK"):
                return {"response_payload": [{"code": code, "payload": [{key: [{"id": owner, "sensor": sensors}]}]}]}
            valid = {"id": sensor_id, "state": {"value": 0, "failed": True}}
            # Zero and fault flags remain real returned data; no health judgment.
            result, report = self.json_cli("r320_temperatures", "unit.example", *args, "--model", "R320",
                                           env={"MOCK_RESPONSE": json.dumps(response([valid]))})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(report["results"][0]["data"], {"temperature": 0})
            malformed = [response([valid], owner=2), response([{**valid, "id": "unrelated-temp"}]),
                         response([valid, valid]), response([{"id": sensor_id}]),
                         response([valid], code="RC_OPERATION_FAILED"), response([valid], code="RC_MISSING_VALUE")]
            malformed += [response([{"id": sensor_id, "state": {"value": value}}]) for value in (True, "4300", None)]
            mixed = response([valid]); mixed["response_payload"].append({"code": "RC_OPERATION_FAILED"})
            malformed.append(mixed)
            for raw in malformed:
                result, report = self.json_cli("r320_temperatures", "unit.example", *args, "--model", "R320",
                                               env={"MOCK_RESPONSE": json.dumps(raw)})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(report["summary"]["failed_ids"], ["1"])
                self.assertTrue(report["errors"])
                self.assertEqual(len(self.requests()), 1)

    def test_live_runner_selected_hvdac_checks_match_mock_cli_and_r320_temperature(self):
        from live_test_lotd import checks_for
        outputs = {}
        scenarios = {"sensors": "read_details", "led": "led_both", "presence": "fru_data",
                     "state": "hvdac_details", "inventory": "hvdac_details", "channel": "channel_success",
                     "dac-count": "dac_count_get", "temperature": "r320_hvdac_temperature"}
        for check in checks_for("R320", True, hvdac_cards="5,8"):
            if check.arguments[1] != "hvdac":
                continue
            result = self.run_cli(scenarios[check.arguments[3]], "unit.example", *check.arguments,
                                  env={"MOCK_MODEL": "R320"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for marker in check.expected_text:
                self.assertIn(marker, result.stdout + result.stderr)
            requests = self.requests()
            self.assertTrue(all(r["type"] == "RT_GET" for r in requests))
            actual_ids = [r["payload"][0].get("memsModule", r["payload"][0].get("mems_module"))["dac_cards"]["id"] for r in requests]
            self.assertEqual(actual_ids, [5, 8] if check.arguments[3] == "led" else [5])
            outputs[check.name] = result.stdout
        self.assertEqual(len(outputs), 8)
        self.assertIn("Temperature: 2725", outputs["HVDAC temperature"])
        result, report = self.json_cli("r320_hvdac_temperature", "unit.example", "get", "hvdac", "5", "temp", "--model", "R320")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["data"], 2725)
        # R320 has no legacy state.temperature field; retain the sensor error.
        result, report = self.json_cli("temperature_fallback", "unit.example", "get", "hvdac", "5", "temp", "--model", "R320")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(report["results"]), 1)
        self.assertIn("RC_OPERATION_FAILED", report["errors"][0]["message"])
        self.assertIn("sensor path", result.stderr)

    def test_live_runner_confirmed_views_match_mock_cli_output(self):
        from live_test_lotd import checks_for
        selected = {"Mirror ports 1-3 state", "MEMS ports 1-3 configuration", "LDS state",
                    "HVDAC card 1 inventory", "CV inventory", "Reset Manager state", "Reset alias last reason"}
        seen = set()
        for check in checks_for("R300", False):
            if check.name not in selected:
                continue
            scenario = ("reset_manager" if check.arguments[1] in ("reset", "reset-manager") else
                        "hvdac_details" if check.arguments[1] == "hvdac" else
                        "fru_data" if check.arguments[1] == "cv" else "mirror_lds_details")
            result = self.run_cli(scenario, "unit.example", *check.arguments)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            for text in check.expected_text:
                self.assertIn(text, result.stdout+result.stderr, check.name)
            seen.add(check.name)
        self.assertEqual(seen, selected)
        for model in ("R300", "R300-G", "R300-L", "R320"):
            checks = checks_for(model, False)
            self.assertTrue(all(check.arguments[0] == "get" for check in checks))
            self.assertFalse(any(check.arguments[:3] == ("get", "lds", "inventory") for check in checks))
        self.assertFalse(any("lds" in check.arguments for check in checks_for("R300", True, include_lds_inventory=True)))
        self.assertFalse(any("lds" in check.arguments for check in checks_for("R300-L", False, include_lds_inventory=True)))

    def test_live_runner_expanded_get_views_and_json_match_mock_cli(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from live_test_lotd import checks_for, json_report_error, main
        selected = {"MEMS module inventory": "read_details", "All power supply overviews": "ps_reads",
                    "MEMS inventory JSON": "read_details", "All power supply states JSON": "ps_reads",
                    "All fan speeds JSON": "fan_reads", "CV control JSON": "read_details",
                    "Mirror configuration JSON": "mirror_lds_details", "Reset reason JSON": "reset_manager"}
        checks = [check for check in checks_for("R300", False) if check.name in selected]
        self.assertEqual({check.name for check in checks}, set(selected))
        responses = []
        for check in checks:
            result = self.run_cli(selected[check.name], "unit.example", *check.arguments)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            for expected in check.expected_text:
                self.assertIn(expected, result.stdout+result.stderr, check.name)
            if "--json" in check.arguments:
                self.assertIsNone(json_report_error(result.stdout), check.name)
            self.assertTrue(all(request["type"] == "RT_GET" for request in self.requests()))
            responses.append(result)
        out = io.StringIO()
        with patch("live_test_lotd.checks_for", return_value=checks), \
             patch("live_test_lotd._run", side_effect=responses), redirect_stdout(out):
            status = main(["unit.example"])
        self.assertEqual(status, 0, out.getvalue())
        self.assertIn("GET smoke checks: 8 passed, 0 failed, 0 not run.", out.getvalue())

    def test_live_runner_json_rejects_malformed_and_inconsistent_reports(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from live_test_lotd import checks_for, json_report_error, main
        check = next(check for check in checks_for("R300", False) if check.name == "MEMS inventory JSON")
        result = self.run_cli("read_details", "unit.example", *check.arguments)
        valid = json.loads(result.stdout)
        bad_texts = ["human text\n"+result.stdout, result.stdout+result.stdout, "null", "[]", "{", "NaN"]
        for key, value in (("schema_version", 2), ("exit_code", True), ("exit_code", 1),
                           ("cancelled", True), ("errors", [{}]), ("summary", {}),
                           ("results", []), ("results", [{}])):
            bad_texts.append(json.dumps({**valid, key: value}))
        for key, value in (("failed_ids", ["1"]), ("not_attempted_ids", ["1"]),
                           ("verified_ids", ["1"]), ("successful_ids", [1]), ("successful_ids", ["2"])):
            bad_texts.append(json.dumps({**valid, "summary": {**valid["summary"], key: value}}))
        for key, value in (("phase", "RT_ACTION"), ("operation", "RT_SET"), ("item_status", "failed"),
                           ("error", {}), ("accepted", True), ("verified", True), ("data", "display text")):
            bad_texts.append(json.dumps({**valid, "results": [{**valid["results"][0], key: value}]}))
        for bad in bad_texts:
            self.assertIsNotNone(json_report_error(bad), bad)
        broken = subprocess.CompletedProcess([], 0, "unexpected text\n"+result.stdout, result.stderr)
        out = io.StringIO()
        with patch("live_test_lotd.checks_for", return_value=[check]), \
             patch("live_test_lotd._run", return_value=broken), redirect_stdout(out):
            self.assertEqual(main(["unit.example"]), 1)
        self.assertIn("stdout must contain exactly one valid JSON document", out.getvalue())
        self.assertIn("0 passed, 1 failed", out.getvalue())

    def test_live_runner_optional_lds_inventory_keeps_real_failure(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from live_test_lotd import checks_for, main
        check = next(check for check in checks_for("R300", False, include_lds_inventory=True)
                     if check.name == "LDS inventory")
        response = self.run_cli("mirror_lds_details", "unit.example", *check.arguments)
        stdout = io.StringIO()
        with patch("live_test_lotd.checks_for", return_value=[check]) as plan, \
             patch("live_test_lotd._run", return_value=response), redirect_stdout(stdout):
            status = main(["unit.example", "--include-lds-inventory"])
        self.assertEqual(status, 1)
        self.assertIn("CHECK: FAIL (lotd.py exited 1)", stdout.getvalue())
        self.assertIn("RC_OPERATION_FAILED", stdout.getvalue())
        plan.assert_called_once_with("R300", False, include_lds_inventory=True, hvdac_cards=None, mirror_ports=None)

    def test_consistent_headings_help_and_summary_counts(self):
        cases = (("fan_reads", ("fan", "1-2", "config"), "Fan 1 configuration:\n  PWM target:", "Fan 2 configuration:"),
                 ("fru_data", ("shelf", "inventory"), "Shelf inventory:\n  ", ""),
                 ("hvdac_details", ("hvdac", "1", "inventory"), "HVDAC card 1 inventory:\n  Description:", ""),
                 ("mirror_lds_details", ("mirror", "1", "state"), "Mirror port 1 state:\n  Status:", "Returned state: 1 port"),
                 ("hvdac_details", ("hvdac", "1", "state"), "HVDAC card 1 state:\n  Missing:", "Returned state: 1 card"))
        for scenario, args, heading, other in cases:
            result = self.run_cli(scenario, "unit.example", "get", *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(heading, result.stdout)
            self.assertIn(other, result.stdout)
        help_result = self.run_cli("success", "--help")
        for command in ("<state|config|x-tilt|y-tilt>", "get cv control", "set cv control",
                        "set fan <ids> control-mode", "<presence|inventory|state>"):
            self.assertIn(command, help_result.stdout)
        self.assertEqual(self.requests(), [])

        for topic, markers in (("shelf", ("get shelf state", "get shelf inventory")),
                               ("inventory", ("get fan <ids>", "get ps <ids>", "get mems inventory")),
                               ("ps", ("On R320", "sensor temp/state.value"))):
            result = self.run_cli("success", "help", topic)
            self.assertEqual(result.returncode, 0, result.stderr)
            for marker in markers:
                self.assertIn(marker, result.stdout)
            self.assertEqual(self.requests(), [])

    def test_container_import_payload_validation_and_reported_success(self):
        from lotd import CONTAINER_TYPES
        for container_type in CONTAINER_TYPES:
            code, output = self.run_tty("container_action",
                ("unit.example", "import", "container", container_type, "1", "unit-inventory.cal", "--json"), "y")
            self.assertEqual(code, 0, output)
            report = self.json_from_tty(output)
            self.assertEqual(report["summary"]["successful_ids"], ["1"])
            self.assertEqual(report["summary"]["verified_ids"], [])
            self.assertEqual(report["errors"], [])
            self.assertIsNone(report["results"][0]["accepted"])
            self.assertFalse(report["results"][0]["verified"])
            self.assertEqual(report["results"][0]["data"], {"container_type": container_type,
                "instance_number": 1, "filename": "unit-inventory.cal", "unit_path": "/sftpmnt/unit-inventory.cal",
                "independently_verified": False, "success": True, "validation_requested": True})
            self.assertEqual(len(self.requests()), 1)
            self.assertEqual(self.requests()[0], {"lotd_version": "LV_1", "cid": "123", "type": "RT_ACTION",
                "payload": [{"@type": "type.googleapis.com/lotd.EquipmentPackPayloadList", "equipment_pack": [
                {"id": 1, "actions": {"write_file_to_container": {"request": {
                    "container_type": {"value": container_type}, "instance_number": 1,
                    "calibration_file_path": "unit-inventory.cal", "validate_after_write": True}}}}]}]})
        code, output = self.run_tty("container_action",
            ("unit.example", "import", "container", "CC_CARD_INVENTORY", "1", "unit.cal"), "y",
            env={"MOCK_CONTAINER_SUCCESS": "false"})
        self.assertEqual(code, 1, output)
        self.assertIn("success=false", output)
        self.assertNotIn("Completed successfully", output)
        self.assertEqual(len(self.requests()), 1)

    def test_container_dump_submission_remains_unverified(self):
        code, output = self.run_tty("container_action",
            ("unit.example", "dump", "container", "CC_CARD_INVENTORY", "7", "new-unit-file.cal", "--json"), "y")
        self.assertEqual(code, 0, output)
        self.assertIn("file creation is unverified", output)
        self.assertNotIn("Completed successfully", output)
        report = self.json_from_tty(output)
        self.assertEqual(report["summary"]["successful_ids"], [])
        self.assertEqual(report["summary"]["verified_ids"], [])
        self.assertEqual(report["summary"]["failed_ids"], [])
        result = report["results"][0]
        self.assertEqual(result["id"], "7")
        self.assertEqual(result["item_status"], "incomplete")
        self.assertTrue(result["data"]["submitted"])
        self.assertFalse(result["data"]["file_creation_verified"])
        self.assertFalse(result["data"]["independently_verified"])
        self.assertIsNone(result["accepted"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(self.requests()), 1)
        action = self.requests()[0]["payload"][0]["equipment_pack"][0]["actions"]
        self.assertEqual(action, {"dump_container_to_file": {"request": {"container_type": {"value": "CC_CARD_INVENTORY"},
                         "instance_number": 7, "calibration_file_path": "new-unit-file.cal"}}})

    def test_container_input_guards_and_confirmation_send_no_requests(self):
        prefix = ("unit.example", "import", "container")
        for target, instance, filename in (("CC_UNKNOWN", "1", "a.cal"), ("CC_SSL_CERTS", "1", "a.cal"),
                ("CC_FAN_INVENTORY", "1", "a.cal"), ("CC_CARD_INVENTORY", "0", "a.cal"),
                ("CC_CARD_INVENTORY", "4294967296", "a.cal"), ("CC_CARD_INVENTORY", "1-3", "a.cal"),
                ("CC_CARD_INVENTORY", "all", "a.cal"), ("CC_CARD_INVENTORY", "01", "a.cal"),
                *(("CC_CARD_INVENTORY", "1", name) for name in ("/sftpmnt/a.cal", "a/b.cal", "a\\b.cal", "..",
                    "../a.cal", ".hidden", "C:a.cal", "a b.cal", "a\n.cal", "é.cal", "a"*256))):
            r, report = self.json_cli("container_action", *prefix, target, instance, filename)
            self.assertEqual(r.returncode, 2, r.stdout+r.stderr)
            self.assertEqual(report["results"], [])
            self.assertEqual(self.requests(), [])
        for mode in ("dump", "import"):
            args = ("unit.example", mode, "container", "CC_CARD_INVENTORY", "1", "a.cal", "--json")
            r = self.run_cli("container_action", *args)
            self.assertEqual(r.returncode, 2)
            self.assertIn("interactive terminal", r.stderr)
            self.assertEqual(self.requests(), [])
            code, output = self.run_tty("container_action", args, "n")
            self.assertEqual(code, 0, output)
            self.assertTrue(self.json_from_tty(output)["cancelled"])
            self.assertEqual(self.requests(), [])
            r = self.run_cli("container_action", "unit.example", mode, "container", "CC_LDS_INVENTORY", "1", "a.cal", "--model", "R300-L")
            self.assertEqual(r.returncode, 2)
            self.assertIn("R300-L has no LDS", r.stderr)
            self.assertEqual(self.requests(), [])
        for args in (("get", "container", "CC_CARD_INVENTORY", "1", "a.cal"),
                     ("dump", "fan", "1", "a.cal"), ("dump", "container", "CC_CARD_INVENTORY", "1")):
            r = self.run_cli("container_action", "unit.example", *args)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(self.requests(), [])

    def test_container_transport_errors_rejections_and_interrupt_never_retry(self):
        for mode in ("dump", "import"):
            args = ("unit.example", mode, "container", "CC_CARD_INVENTORY", "1", "a.cal", "--json")
            for override in ("", '{"cid":"123","response_payload":[{"code":"RC_OPERATION_FAILED"}]}', '{}'):
                code, output = self.run_tty("container_action", args, "y", env={"MOCK_CONTAINER_RESPONSE": override})
                self.assertEqual(code, 1, output)
                report = self.json_from_tty(output)
                self.assertEqual(report["summary"]["verified_ids"], [])
                self.assertEqual(report["summary"]["failed_ids"], ["1"])
                self.assertEqual(len(self.requests()), 1)
            code, output = self.run_tty("container_action", args, "y", env={"MOCK_CONTAINER_FAILURE": "1"})
            self.assertEqual(code, 1, output)
            self.assertEqual(len(self.requests()), 1)
            code, output = self.run_tty("container_action", args, "y", interrupt_at="Submitting container",
                                       request_count=1, env={"MOCK_CONTAINER_INTERRUPT": "1"})
            self.assertEqual(code, 130, output)
            report = self.json_from_tty(output)
            self.assertEqual(report["summary"]["successful_ids"], [])
            self.assertEqual(report["results"][0]["item_status"], "incomplete")
            self.assertIsNone(report["results"][0]["accepted"])
            self.assertEqual(len(self.requests()), 1)
        r = self.run_cli("container_action", "help", "container")
        self.assertEqual(r.returncode, 0)
        self.assertIn("file creation remains unverified", r.stdout)
        self.assertIn("CC_HVDAC_CALIBRATION", r.stdout)
        self.assertEqual(self.requests(), [])

    def test_reset_alias_matches_manager_and_remains_read_only(self):
        for command in ("state", "last-reset-reason"):
            canonical, expected = self.json_cli("reset_manager", "unit.example", "get", "reset-manager", command)
            request = self.requests()[0]
            alias, actual = self.json_cli("reset_manager", "unit.example", "get", "reset", command)
            self.assertEqual(alias.returncode, canonical.returncode)
            self.assertEqual(actual["results"], expected["results"])
            self.assertEqual(actual["summary"], expected["summary"])
            self.assertEqual(actual["command"], ["unit.example", "get", "reset", command])
            self.assertEqual(self.requests(), [request])
        canonical = self.run_cli("reset_manager", "help", "reset-manager")
        alias = self.run_cli("reset_manager", "help", "reset")
        self.assertEqual(alias.returncode, 0)
        self.assertEqual(alias.stdout, canonical.stdout)
        self.assertEqual(self.requests(), [])
        for args in (("set", "reset", "state"), ("get", "reset", "now")):
            result = self.run_cli("reset_manager", "unit.example", *args)
            self.assertEqual(result.returncode, 2)
            self.assertIn("read-only", result.stderr)
            self.assertEqual(self.requests(), [])

    def test_reset_manager_reads_payload_human_and_json(self):
        for command in ("state", "last-reset-reason"):
            r, data = self.json_cli("reset_manager", "unit.example", "get", "reset-manager", command)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("RR_POWER_ON", r.stderr)
            self.assertEqual(data["summary"]["successful_ids"], ["1"])
            self.assertEqual(data["summary"]["verified_ids"], [])
            self.assertEqual(data["results"][0]["data"], {"last_reset_reason":
                {"value": "RR_POWER_ON"} if command == "state" else "RR_POWER_ON"})
            self.assertEqual(self.requests()[0]["payload"], [{
                "@type": "type.googleapis.com/lotd.ResetManagerPayloadList",
                "reset_manager": {"id": 1, "state": {}}}])
            r = self.run_cli("reset_manager", "unit.example", "get", "reset-manager", command)
            self.assertEqual(r.returncode, 0)
            self.assertIn("Reset Manager 1 state:\n  Last reset reason:\n    Value: RR_POWER_ON" if command == "state"
                          else "Last reset reason: RR_POWER_ON", r.stdout)

    def test_reset_manager_scoping_casing_and_response_validation(self):
        def reply(entities, code="RC_OK"):
            return {"response_payload": [{"code": code, "payload": [{"reset_manager": entities}]}]}
        raw = json.dumps({"responsePayload": [{"code": "RC_OK", "payload": [{"resetManager": [
            {"id": 2, "state": {"lastResetReason": {"value": "WRONG"}}},
            {"id": 1, "state": {"lastResetReason": {"value": "RR_FUTURE"}, "futureFlag": False, "count": 0}}]}]}]})
        for command in ("state", "last-reset-reason"):
            r, data = self.json_cli("reset_manager", "unit.example", "get", "reset-manager", command,
                                   env={"MOCK_RESPONSE": raw})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("RR_FUTURE", r.stderr)
            self.assertNotIn("WRONG", r.stderr)
            if command == "state":
                self.assertIs(data["results"][0]["data"]["futureFlag"], False)
        invalid = [reply([]), reply([{"id": 2, "state": {"value": 1}}]),
                   reply([{"id": 1, "state": {}}]), reply([{"id": 1, "state": []}]),
                   reply([{"id": 1, "state": {"value": 1}}]*2),
                   reply([{"id": 1, "state": {"value": 1}}], "RC_OPERATION_FAILED"),
                   reply([{"id": 1, "state": {"value": 1}}], "RC_MISSING_VALUE")]
        conflicting = reply([{"id": 1, "state": {"value": 1}}])
        conflicting["response_payload"] += reply([{"id": 1, "state": {"value": 2}}])["response_payload"]
        invalid.append(conflicting)
        for response in invalid:
            r, data = self.json_cli("reset_manager", "unit.example", "get", "reset-manager", "state",
                                   env={"MOCK_RESPONSE": json.dumps(response)})
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertEqual(data["summary"]["failed_ids"], ["1"])
            self.assertTrue(data["errors"])
        for reason in (None, "RR_POWER_ON", {}, {"value": None}, {"value": False}, {"value": " "}):
            r = self.run_cli("reset_manager", "unit.example", "get", "reset-manager", "last-reset-reason",
                             env={"MOCK_RESPONSE": json.dumps(reply([{"id": 1, "state": {"last_reset_reason": reason}}]))})
            self.assertEqual(r.returncode, 1, r.stdout+r.stderr)

    def test_reset_manager_read_only_validation_transport_and_interrupt(self):
        for args in (("set", "reset-manager", "state"), ("get", "reset-manager", "reset"),
                     ("get", "reset-manager", "state", "2"), ("get", "reset-manager", "1", "state")):
            r, data = self.json_cli("reset_manager", "unit.example", *args)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("read-only", r.stderr)
            self.assertEqual(data["results"], [])
            self.assertEqual(self.requests(), [])
        r = self.run_cli("reset_manager", "help", "reset-manager")
        self.assertEqual(r.returncode, 0)
        self.assertIn("get reset-manager last-reset-reason", r.stdout)
        self.assertEqual(self.requests(), [])
        r, data = self.json_cli("reset_manager", "unit.example", "get", "reset-manager", "state",
                               env={"MOCK_RESET_TIMEOUT": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(data["summary"]["failed_ids"], ["1"])
        code, output = self.run_tty("reset_manager", ("unit.example", "get", "reset-manager", "state", "--json"),
                                   interrupt_at="Reading Reset Manager state", request_count=1, env={"MOCK_RESET_INTERRUPT": "1"})
        self.assertEqual(code, 130, output)
        report = self.json_from_tty(output)
        self.assertEqual(report["summary"]["successful_ids"], [])
        self.assertEqual(report["results"][0]["item_status"], "incomplete")

    def test_live_smoke_suite_is_read_only_and_skips_absent_lds(self):
        from live_test_lotd import checks_for

        r300 = checks_for("R300", False)
        self.assertTrue(r300)
        self.assertTrue(all(check.arguments[0] == "get" for check in r300))
        self.assertTrue(any("lds" in check.arguments for check in r300))
        self.assertTrue(any(check.arguments[1:6] == ("hvdac", "1", "dac-count", "1", "1") for check in r300))
        self.assertTrue(any(check.arguments[1:] == ("shelf", "state", "--model", "R300") for check in r300))
        self.assertTrue(any(check.arguments[1:] == ("hvdac", "1", "inventory", "--model", "R300") for check in r300))
        r300l = checks_for("R300-L", False)
        self.assertFalse(any("lds" in check.arguments for check in r300l))
        self.assertTrue(all(check.arguments[0] == "get" for check in r300l))

    def test_live_set_plan_uses_captured_values_and_skips_active_connection(self):
        from live_test_lotd import checks_for, set_checks_for

        outputs = {
            "Shelf power and fault LEDs": "Reading power and fault LEDs on shelf (entity ID 1)...\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_OFF\n",
            "Midplane power and fault LEDs": "Reading power and fault LEDs on midplane (entity ID 2)...\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_ON\n",
            "CV power and fault LEDs": "Reading power and fault LEDs on cv (entity ID 1)...\nPower LED (ID 1): LC_OFF\nFault LED (ID 2): LC_OFF\n",
            "All HVDAC power and fault LEDs": "[1/10] Reading power and fault LEDs on hvdac (entity ID 1)...\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_OFF\n",
            "All fan power and fault LEDs": "[1/3] Reading power and fault LEDs on fan (entity ID 1)...\nPower LED (ID 1): LC_OFF\nFault LED (ID 2): LC_ON\n",
            "LDS power and fault LEDs": "Reading power and fault LEDs on lds (entity ID 1)...\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_OFF\n",
            "HVDAC channel target and state": "Target voltage: 10\nActual voltage: 20\n",
            "HVDAC DAC count": "DAC count: 100\n",
            "CV channels 1-3": "[1/3] Reading CV channel 1 at unit...\nTarget voltage: 199\nActual voltage: 200\n",
            "Mirror 1 X tilt": "X target voltage: 1000\n",
            "Mirror 321 Y tilt": "Y target voltage: 0\n",
            "Connection Manager ports 1-3": "[3/3] Reading Connection Manager state for port 3 at unit...\nPeer port number: 10001\nOptical blocking enable: false\n",
        }
        self.assertTrue(all(check.arguments[0] == "get" for check in checks_for("R300", False)))
        planned, skipped = set_checks_for("R300", False, outputs)
        by_name = {check.name: check for check in planned}
        self.assertEqual(by_name["shelf power LED remains LC_ON"].arguments,
                         ("set", "shelf", "led", "power", "on", "--model", "R300"))
        self.assertEqual(by_name["HVDAC channel target remains unchanged"].arguments[6], "10")
        self.assertEqual(by_name["HVDAC DAC count remains unchanged"].arguments,
                         ("set", "hvdac", "1", "dac-count", "1", "1", "100", "--model", "R300"))
        self.assertEqual(by_name["CV channel 1 target remains unchanged"].arguments[4], "199")
        self.assertFalse(any("mirror" in check.arguments for check in planned))
        self.assertTrue(any("mirror SET checks" in reason for reason in skipped))
        self.assertFalse(any("connection" in check.arguments and "peer" in check.arguments for check in planned))
        self.assertTrue(any("active connection" in reason for reason in skipped))
        self.assertTrue(all(check.arguments[0] == "set" for check in planned))

    def test_live_set_plan_skips_leds_without_baselines_and_sets_disconnect_only_when_safe(self):
        from live_test_lotd import set_checks_for

        outputs = {
            "Shelf power and fault LEDs": "Reading LEDs (entity ID 1)\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_OFF\n",
            "Connection Manager ports 1-3": "[3/3] Reading Connection Manager state for port 3 at unit...\nPeer port number: 0\nOptical blocking enable: true\n",
        }
        planned, skipped = set_checks_for("R300-L", False, outputs)
        self.assertTrue(any(check.arguments[:5] == ("set", "connection", "3", "peer", "disconnect") for check in planned))
        self.assertTrue(any(check.arguments[:5] == ("set", "connection", "3", "blocking", "true") for check in planned))
        self.assertFalse(any("lds" in check.arguments for check in planned))
        self.assertTrue(any("hvdac power LED" in reason for reason in skipped))

    def test_live_set_runner_stops_when_baseline_reapply_fails(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        import live_test_lotd

        checks = live_test_lotd.checks_for("R300", False)
        fixtures = {
            "Shelf power and fault LEDs": "Reading LEDs (entity ID 1)\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_OFF\n",
            "Midplane power and fault LEDs": "Reading LEDs (entity ID 2)\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_ON\n",
            "CV power and fault LEDs": "Reading LEDs (entity ID 1)\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_ON\n",
            "LDS power and fault LEDs": "Reading LEDs (entity ID 1)\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_OFF\n",
            "All HVDAC power and fault LEDs": "[1/10] Reading LEDs (entity ID 1)\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_OFF\n",
            "All fan power and fault LEDs": "[1/3] Reading LEDs (entity ID 1)\nPower LED (ID 1): LC_ON\nFault LED (ID 2): LC_OFF\n",
            "HVDAC channel target and state": "Target voltage: 10\nActual voltage: 20\nOut-of-range failure: false\n",
            "CV channels 1-3": "[1/3] Reading CV channel 1 at unit\nTarget voltage: 20\nActual voltage: 30\n",
            "Mirror 1 X tilt": "X target voltage: 1000\n",
            "Mirror 321 Y tilt": "Y target voltage: 0\n",
            "Connection Manager ports 1-3": "[3/3] Reading Connection Manager state for port 3 at unit\nPeer port number: 0\nOptical blocking enable: false\n",
        }
        calls = []

        def fake_run(script, device_ip, arguments):
            calls.append(arguments)
            if len(calls) <= len(checks):
                check = checks[len(calls) - 1]
                output = fixtures.get(check.name, "") + "\n".join(check.expected_text)
                return subprocess.CompletedProcess(arguments, 0, output, "")
            return subprocess.CompletedProcess(arguments, 1, "", "LOTD returned RC_OPERATION_FAILED")

        class TTY:
            @staticmethod
            def isatty():
                return True

        stdout = io.StringIO()
        with patch.object(live_test_lotd, "_run", side_effect=fake_run), \
             patch.object(live_test_lotd.sys, "stdin", TTY()), \
             patch("builtins.input", return_value="y"), redirect_stdout(stdout):
            code = live_test_lotd.main(["10.13.197.52", "--include-set"])

        self.assertEqual(code, 1)
        self.assertEqual(len(calls), len(checks) + 2)  # failed SET plus failed reapply
        self.assertIn("Stopping SET checks because the baseline could not be verified or reapplied.", stdout.getvalue())
        self.assertIn("15 not run", stdout.getvalue())

    def test_batch_led_get_progress_values_and_payload(self):
        r = self.run_cli("success", "10.13.197.52", "get", "hvdac", "1-3", "led", "power")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout + r.stderr
        self.assertIn("[1/3]", out); self.assertIn("[3/3]", out)
        self.assertIn("Successful IDs (3): 1,2,3", out)
        self.assertIn("Returned LED values (ID: value): 1: LC_OFF, 2: LC_OFF, 3: LC_OFF", out)
        self.assertEqual(len(self.requests()), 3)
        self.assertEqual(self.requests()[0]["payload"][0]["memsModule"]["dac_cards"]["id"], 1)

    def test_individual_error_continues_and_summary_orders_results(self):
        r = self.run_cli("per_id_error", "10.13.197.52", "get", "hvdac", "1-3", "led", "power")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1)
        self.assertIn("Successful IDs (2): 2,3", out); self.assertIn("Failed IDs (1): 1", out)
        summary=out.split("LOTD SUMMARY")[-1]
        self.assertLess(summary.index("Successful IDs"),summary.index("Failed IDs"))
        self.assertLess(summary.index("Failed IDs"),summary.index("Returned LED values"))
        self.assertEqual(len(self.requests()),3)

    def test_endpoint_timeout_stops_batch_and_suggests_container_check(self):
        r=self.run_cli("dial_failure","10.13.197.52","get","hvdac","1-3","led","power")
        out=r.stdout+r.stderr
        self.assertEqual(r.returncode,1); self.assertIn("[HINT] LOTD timed out",out)
        self.assertIn("Not attempted (2): 2,3",out); self.assertEqual(len(self.requests()),1)

    def test_address_validation_and_hostname(self):
        bad=self.run_cli("success","10.13.197.999","get","hvdac","1","led","power")
        self.assertEqual(bad.returncode,2); self.assertIn("valid IPv4 address or hostname",bad.stderr)
        self.assertEqual(len(self.requests()),0)
        good=self.run_cli("success","lotd.example.test","get","hvdac","1","led","power")
        self.assertEqual(good.returncode,0); self.assertIn("Completed successfully",good.stdout)
        self.assertIn("lotd.example.test:3217",good.stdout)

    def test_both_led_get_and_all_alias_for_fixed_and_selected_fru(self):
        cases=[("led_both","get","shelf","led"),
               ("led_both","get","hvdac","2","led","both"),
               ("led_both","get","hvdac","2","led","all")]
        for scenario,*args in cases:
            with self.subTest(args=args):
                self.capture.mkdir(exist_ok=True)
                r=self.run_cli(scenario,"10.13.197.52",*args)
                out=r.stdout+r.stderr
                self.assertEqual(r.returncode,0,out)
                self.assertIn("Power LED (ID 1): LC_ON",out)
                self.assertIn("Fault LED (ID 2): LC_OFF",out)

    def test_both_led_set_and_all_alias(self):
        for selector in ("both","all"):
            with self.subTest(selector=selector):
                self.capture.mkdir(exist_ok=True)
                code,out=self.run_tty("led_both_set",("10.13.197.52","set","shelf","led",selector,"on"),"y")
                self.assertEqual(code,0,out); self.assertIn("Proceed with these 2 LED changes?",out)
                self.assertIn("Verified: both LEDs are LC_ON.",out)
                req=self.requests(); self.assertEqual(len(req),3)
                self.assertEqual([x["payload"][0]["equipment_pack"]["led"]["id"] for x in req[:2]],[1,2])
                self.assertEqual(req[2]["type"],"RT_GET")

    def test_both_led_set_all_fru_families(self):
        cases=[("midplane",), ("cv",), ("lds",), ("hvdac","1"), ("fan","1")]
        for parts in cases:
            with self.subTest(component=parts[0]):
                self.capture.mkdir(exist_ok=True)
                args=("10.13.197.52","set",*parts,"led","both","on")
                code,out=self.run_tty("led_both_set",args,"y")
                self.assertEqual(code,0,out); self.assertIn("Verified: both LEDs are LC_ON.",out)
                self.assertEqual(len(self.requests()),3)

    def test_led_single_set_payload_readback_and_application_failure(self):
        r=self.run_cli("success","10.13.197.52","set","hvdac","1","led","fault","off")
        out=r.stdout+r.stderr
        self.assertEqual(r.returncode,0,out); self.assertIn("Verified: LED is LC_OFF.",out)
        led=self.requests()[0]["payload"][0]["memsModule"]["dac_cards"]["led"]
        self.assertEqual(led,{"id":2,"config":{"control":{"value":"LC_OFF"}}})
        self.assertIn("Write response has no status code; verifying readback...", r.stdout)
        self.assertIn("Verification: readback matched for successful IDs", r.stdout)
        self.assertNotIn('"cid"', r.stdout)
        self.capture.mkdir(exist_ok=True)
        bad=self.run_cli("set_app_error","10.13.197.52","set","hvdac","1","led","power","on")
        self.assertEqual(bad.returncode,1); self.assertIn("RC_OPERATION_FAILED",bad.stderr)
        self.assertNotIn("Reading back configured value",bad.stdout+bad.stderr)
        self.assertEqual(len(self.requests()),1)
        self.assertNotIn("Write accepted", bad.stdout)

    def test_malformed_json_fails_cleanly(self):
        r=self.run_cli("malformed_json","10.13.197.52","get","hvdac","1","led","power")
        self.assertEqual(r.returncode,1); self.assertIn("LOTD returned invalid JSON:",r.stderr)
        self.assertNotIn("Traceback",r.stdout+r.stderr)

    def test_hvdac_channel_get_and_connection_failure(self):
        r=self.run_cli("channel_success","10.13.197.52","get","hvdac","1","channel","2","7")
        out=r.stdout+r.stderr; self.assertEqual(r.returncode,0,out)
        for text in ("Target voltage: 123","Actual voltage: 120","Out-of-range failure: false",
                     "Returned values: target=123, actual=120, out-of-range failure=false"):
            self.assertIn(text,out)
        req=self.requests()[0]["payload"][0]["memsModule"]["dac_cards"]["dac_chips"][0]["channels"][0]
        self.assertEqual(req["id"],7)
        self.capture.mkdir(exist_ok=True)
        failed=self.run_cli("dial_failure","10.13.197.52","get","hvdac","1","channel","1","7")
        self.assertEqual(failed.returncode,1); self.assertIn("HVDAC channel request failed:",failed.stderr)
        self.assertIn("LOTD SUMMARY",failed.stderr)

    def test_hvdac_temperature_sensor_and_legacy_fallback(self):
        r=self.run_cli("temperature_sensor","10.13.197.52","get","hvdac","1","temp")
        self.assertEqual(r.returncode,0); self.assertIn("Temperature: 3100",r.stdout)
        self.assertEqual(self.requests()[0]["payload"][0]["memsModule"]["dac_cards"]["sensor"][0]["id"],"temp")
        self.capture.mkdir(exist_ok=True)
        f=self.run_cli("temperature_fallback","10.13.197.52","get","hvdac","1","temperature")
        self.assertEqual(f.returncode,0); self.assertIn("Temperature: 3150",f.stdout)
        self.assertIn("Read path: legacy fallback",f.stdout); self.assertEqual(len(self.requests()),2)
        self.assertEqual(self.requests()[1]["payload"][0]["memsModule"]["dac_cards"]["state"]["temperature"],0)

    def test_hvdac_channel_set_verifies_target_and_rejects_mismatch(self):
        r=self.run_cli("channel_set_success","10.13.197.52","set","hvdac","1","channel","2","7","123.5")
        self.assertEqual(r.returncode,0,r.stdout+r.stderr); self.assertIn("Verified: target voltage matches 123.5",r.stdout)
        req=self.requests(); channel=req[0]["payload"][0]["memsModule"]["dac_cards"]["dac_chips"][0]["channels"][0]
        self.assertEqual(channel["config"]["target_voltage"],123.5)
        self.assertNotIn("state",req[1]["payload"][0]["memsModule"]["dac_cards"]["dac_chips"][0]["channels"][0])
        self.capture.mkdir(exist_ok=True)
        bad=self.run_cli("channel_set_mismatch","10.13.197.52","set","hvdac","1","channel","2","7","123.5")
        self.assertEqual(bad.returncode,1); self.assertIn("Target voltage verification failed: requested 123.5, read back 124.5",bad.stderr)
        self.assertEqual(len(self.requests()),2)
        self.assertIn("Write response has no status code", bad.stdout)
        self.assertNotIn("Verification: readback matched", bad.stdout+bad.stderr)
        self.assertNotIn('"cid"', bad.stdout)

    def test_hvdac_dac_count_get_set_and_validation(self):
        get=self.run_cli("dac_count_get","10.13.197.52","get","hvdac","1","dac-count","2","7")
        self.assertEqual(get.returncode,0,get.stdout+get.stderr)
        self.assertIn("DAC count: 100",get.stdout)
        self.assertIn("Returned DAC count: 100",get.stdout)
        requested=self.requests()[0]
        channel=requested["payload"][0]["memsModule"]["dac_cards"]["dac_chips"][0]["channels"][0]
        self.assertEqual(channel,{"id":7,"config":{"dac_count":0}})

        self.capture.mkdir(exist_ok=True)
        set_ok=self.run_cli("dac_count_set_success","10.13.197.52","set","hvdac","1","dac-count","2","7","123")
        self.assertEqual(set_ok.returncode,0,set_ok.stdout+set_ok.stderr)
        self.assertIn("Verified: DAC count matches 123",set_ok.stdout)
        requests=self.requests()
        set_channel=requests[0]["payload"][0]["memsModule"]["dac_cards"]["dac_chips"][0]["channels"][0]
        self.assertEqual(set_channel["config"],{"dac_count":123})
        readback=requests[1]["payload"][0]["memsModule"]["dac_cards"]["dac_chips"][0]["channels"][0]
        self.assertEqual(readback["config"],{"dac_count":0})
        self.assertNotIn("state",readback)

        self.capture.mkdir(exist_ok=True)
        mismatch=self.run_cli("dac_count_set_mismatch","10.13.197.52","set","hvdac","1","dac-count","2","7","123")
        self.assertEqual(mismatch.returncode,1)
        self.assertIn("DAC count verification failed: requested 123, read back 124",mismatch.stderr)

        invalid=self.run_cli("success","10.13.197.52","set","hvdac","1","dac-count","2","7","1.5")
        self.assertEqual(invalid.returncode,2)
        self.assertIn("DAC count must be an integer",invalid.stderr)
        self.assertEqual(self.requests(),[])

    def test_hvdac_help_documents_dac_count_separately_from_voltage(self):
        result=subprocess.run([sys.executable,str(CLI),"help","hvdac"],text=True,
                              capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn("get hvdac <card-id> dac-count <chip-id> <channel-id>",result.stdout)
        self.assertIn("DAC count is the integer DAC code, not a voltage.",result.stdout)

    def test_identity_reads_and_load_selection(self):
        for component, entity_id in (("shelf", 1),):
            for camel in ("", "1"):
                r = self.run_cli("identity_sensor", "unit.example", "get", component, "mac-addresses",
                                 env={"MOCK_CAMEL": camel})
                self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
                self.assertIn("00:11:22:33:44:55", r.stdout)
                self.assertEqual(self.requests()[0]["payload"][0]["equipment_pack"],
                                 {"id": entity_id, "mac_addresses": {}})
                for load, load_id in (("active", 1), ("inactive", 2)):
                    r = self.run_cli("identity_sensor", "unit.example", "get", component,
                                     "firmware-version", load, env={"MOCK_CAMEL": camel})
                    self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
                    self.assertIn(f"{load.capitalize()} firmware load version: SONiC-OS-{load}-build", r.stdout)
                    self.assertIn(f"Returned {load} firmware load version: SONiC-OS-{load}-build", r.stdout)
                    self.assertNotIn('"inventory"', r.stdout)
                    self.assertNotIn('"version"', r.stdout)
                    other = "inactive" if load == "active" else "active"
                    self.assertNotIn(f"SONiC-OS-{other}-build", r.stdout)
                    self.assertEqual(self.requests()[0]["payload"][0]["equipment_pack"],
                                     {"id": entity_id, "load_version": {"id": load_id}})
                    self.assertTrue(all(req["type"] == "RT_GET" for req in self.requests()))

    def test_cv_midplane_and_shelf_temperatures(self):
        for component in ("cv", "midplane"):
            for alias in ("temp", "temperature"):
                r = self.run_cli("identity_sensor", "unit.example", "get", component, alias,
                                 "--model", "R320", env={"MOCK_CAMEL": "1"})
                self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
                self.assertIn("Temperature: 3100", r.stdout)
                payload = self.requests()[0]["payload"][0]
                entity = payload["memsModule"]["common_voltage_card"] if component == "cv" else payload["equipment_pack"]
                self.assertEqual(entity, {"id": 1 if component == "cv" else 2,
                                         "sensor": [{"id": "temp", "state": {"value": 0}}]})
        for command, expected in (("temperature", -2500), ("inlet-temperature", 0), ("inlet-temp", 0), ("outlet-temperature", 3000), ("outlet-temp", 3000)):
            r = self.run_cli("identity_sensor", "unit.example", "get", "shelf", command,
                             env={"MOCK_SECTION_RESULT": '{"temperature":-2500,"inletTemperature":0,"outlet_temperature":3000}'})
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            self.assertIn(f": {expected}", r.stdout)
            self.assertEqual(self.requests()[0]["payload"][0]["equipment_pack"], {"id": 1, "state": {}})

    def test_identity_sensor_missing_ambiguous_and_error_responses(self):
        cases = [("shelf", ("mac-addresses",), '{}'),
                 ("shelf", ("firmware-version", "active"), '[{"id":2,"version":"other"}]'),
                 ("shelf", ("firmware-version", "inactive"), '[{"id":2},{"id":2}]'),
                 ("shelf", ("firmware-version", "active"), '{"id":1}'),
                 ("cv", ("temperature",), '[{"id":"other","state":{"value":3000}}]'),
                 ("cv", ("temperature",), '[{"id":"temp","state":{"value":true}}]'),
                 ("midplane", ("temperature",), '[{"id":"temp","state":{}}]'),
                 ("shelf", ("temperature",), '{"inlet_temperature":3000}')]
        for comp, args, result in cases:
            r = self.run_cli("identity_sensor", "unit.example", "get", comp, *args,
                             env={"MOCK_SECTION_RESULT": result})
            self.assertEqual(r.returncode, 1, r.stdout+r.stderr)
            self.assertNotIn("Completed successfully", r.stdout+r.stderr)
        r = self.run_cli("identity_sensor", "unit.example", "get", "cv", "temperature",
                         env={"MOCK_CODE": "RC_OPERATION_FAILED"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.requests()), 1)
        # A singleton object and string-valued protobuf ID also work.
        r = self.run_cli("identity_sensor", "unit.example", "get", "shelf", "firmware-version", "active",
                         env={"MOCK_SECTION_RESULT": '{"id":"1","state":{"version":"build"}}'})
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)

    def test_firmware_requires_version_and_non_led_errors_are_relevant(self):
        for entry in ({"id": 1, "inventory": {"entity_description": "Active Image"}},
                      {"id": 1, "state": {"version": ""}},
                      {"id": 1, "state": {"version": 123}},
                      {"id": 1, "state": {"version": None}}):
            r = self.run_cli("identity_sensor", "unit.example", "get", "shelf", "firmware-version", "active",
                             env={"MOCK_SECTION_RESULT": json.dumps(entry)})
            self.assertEqual(r.returncode, 1, r.stdout+r.stderr)
            self.assertIn("state.version", r.stderr)
        for comp in ("shelf", "midplane", "cv"):
            r = self.run_cli("identity_sensor", "unit.example", "get", comp, "unknown-temperature")
            self.assertEqual(r.returncode, 2)
            self.assertIn(f"Unrecognized {comp} GET command", r.stderr)
            self.assertIn(f"help {comp}", r.stderr)
            self.assertNotIn("led [", r.stderr)
            self.assertEqual(self.requests(), [])
        for args in (("mac-addresses",), ("firmware-version", "active"), ("firmware-version", "inactive")):
            r = self.run_cli("identity_sensor", "unit.example", "get", "midplane", *args)
            self.assertEqual(r.returncode, 2)
            self.assertIn("supported for shelf only", r.stderr)
            self.assertEqual(self.requests(), [])

    def test_identity_sensor_validation_and_help(self):
        for args in (("set", "shelf", "mac-addresses"), ("get", "shelf", "firmware-version"),
                     ("get", "shelf", "firmware-version", "both"),
                     ("get", "cv", "temperature", "extra"), ("get", "cv", "inlet-temperature"),
                     ("get", "cv", "mac-addresses")):
            r = self.run_cli("identity_sensor", "unit.example", *args)
            self.assertEqual(r.returncode, 2, r.stdout+r.stderr)
            self.assertEqual(self.requests(), [])
        for topic, expected in (("identity", "firmware-version <active|inactive>"),
                                ("temp", "get cv <temp|temperature>"),
                                ("shelf", "mac-addresses"), ("cv", "temperature")):
            r = self.run_cli("identity_sensor", "help", topic)
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            self.assertIn(expected, r.stdout)

    def test_mems_module_inventory_human_json_alias_and_future_fields(self):
        for component in ("mems", "mirror"):
            r = self.run_cli("read_details", "unit.example", "get", component, "inventory")
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            self.assertIn("MEMS module 1 inventory:\n  Description: MEMs Module", r.stdout)
            self.assertNotIn("Serial number:", r.stdout)
            request = self.requests()[0]
            self.assertEqual(request["type"], "RT_GET")
            self.assertEqual(request["payload"][0]["mems_module"], {"id": 1, "inventory": {}})
            r, data = self.json_cli("read_details", "unit.example", "get", component, "inventory")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(data["results"][0]["data"], {"entity_description": "MEMs Module"})
            self.assertEqual(data["summary"]["successful_ids"], ["1"])
        inventory = {"entityDescription": "MEMs Module", "future_flag": False, "future_count": 0}
        r, data = self.json_cli("read_details", "unit.example", "get", "mems", "inventory",
                               env={"MOCK_SECTION_RESULT": json.dumps(inventory), "MOCK_SPLIT": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(data["results"][0]["data"], inventory)
        self.assertIn("Future flag: false", r.stderr)
        self.assertIn("Future count: 0", r.stderr)
        self.assertIn("RC_MISSING_VALUE", " ".join(data["summary"]["notes"]))
        for topic in ("mems", "mirror", "sensors"):
            r = self.run_cli("read_details", "help", topic)
            self.assertIn("get mems inventory", r.stdout)

    def test_mems_module_inventory_rejects_bad_scope_shapes_and_writes(self):
        for tail in (("set", "mems", "inventory"), ("get", "mems", "inventory", "extra"),
                     ("get", "mems", "1", "inventory")):
            r = self.run_cli("read_details", "unit.example", *tail)
            self.assertEqual(r.returncode, 2, r.stdout+r.stderr)
            self.assertEqual(self.requests(), [])
        for inventory in ({}, [], [{"entity_description": "wrong shape"}], "invalid"):
            r = self.run_cli("read_details", "unit.example", "get", "mems", "inventory",
                             env={"MOCK_SECTION_RESULT": json.dumps(inventory)})
            self.assertEqual(r.returncode, 1, r.stdout+r.stderr)
        for modules in ([{"id": 2, "inventory": {"entity_description": "other module"}}],
                        [{"id": 1, "state": {"value": 1}}],
                        [{"id": 1, "inventory": {"entity_description": "one"}},
                         {"id": 1, "inventory": {"entity_description": "two"}}]):
            raw = {"response_payload": [{"code": "RC_OK", "payload": [{"mems_module": modules}]}]}
            r, data = self.json_cli("read_details", "unit.example", "get", "mems", "inventory",
                                   env={"MOCK_RESPONSE": json.dumps(raw)})
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertEqual(data["summary"]["failed_ids"], ["1"])
        r = self.run_cli("read_details", "unit.example", "get", "mems", "inventory",
                         env={"MOCK_CODE": "RC_OPERATION_FAILED"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("RC_OPERATION_FAILED", r.stderr)

    def test_mems_environmental_state_values_and_distinct_humidity(self):
        for command, expected in (("state", "Correction temperature: 2195"),
                                  ("correction-temp", "Correction temperature: 2195"),
                                  ("correction-temperature", "Correction temperature: 2195"),
                                  ("humidity", "Relative humidity: 4064")):
            r = self.run_cli("read_details", "unit.example", "get", "mems", command)
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            self.assertIn(expected, r.stdout)
            self.assertEqual(self.requests()[0]["payload"][0]["mems_module"], {"id": 1, "state": {}})
        self.assertIn("Relative humidity failed: false", r.stdout)
        r = self.run_cli("read_details", "unit.example", "get", "mems", "sensors", "humidity")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn("Value: 500", r.stdout)
        self.assertNotIn("4064", r.stdout)
        self.assertNotIn("Out-of-range low:", r.stdout)

    def test_mems_sensor_layouts_and_raw_alarm_details(self):
        for model, ids in (("R300-G", ("north-mems-temp", "south-mems-temp")),
                           ("R300-L", ("A-mems-temp", "B-mems-temp"))):
            r = self.run_cli("read_details", "unit.example", "get", "mems", "sensors", "--model", model,
                             env={"MOCK_MODEL": model})
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            self.assertEqual(self.requests()[0]["payload"][0]["mems_module"], {"id": 1, "sensor": {}})
            for sensor_id in (*ids, "ob-temp", "humidity"):
                self.assertIn("Sensor " + sensor_id, r.stdout)
            self.assertIn("    Configuration:\n      High threshold:\n        Threshold: 6000", r.stdout)
            self.assertIn("Hysteresis: 250", r.stdout)
            self.assertIn("Description:", r.stdout)
            if model == "R300-G":
                self.assertIn("Value: -2500", r.stdout)
                self.assertIn("Failed: true", r.stdout)
                self.assertIn("Out-of-range low: true", r.stdout)
            self.assertIn("Outcome:        Completed successfully", r.stdout)
        r = self.run_cli("read_details", "unit.example", "get", "mems", "temp", "north-mems-temp")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn("Temperature: -2500", r.stdout)
        self.assertIn("Failed: true", r.stdout)
        r = self.run_cli("read_details", "unit.example", "get", "mems", "temperature", "north-mems-temp",
                         env={"MOCK_MODEL": "R300-L"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("was not returned", r.stderr)
        self.assertIn("A-mems-temp", r.stderr)

    def test_full_card_sensor_details_and_hvdac_model_bounds(self):
        for args, expected in ((("midplane", "sensors"), {"equipment_pack": {"id": 2, "sensor": {}}}),
                               (("cv", "sensors", "temp"), {"mems_module": {"id": 1, "common_voltage_card": {"id": 1, "sensor": {}}}}),
                               (("hvdac", "11", "sensors", "temp", "--model", "R320"), {"mems_module": {"id": 1, "dac_cards": {"id": 11, "sensor": {}}}})):
            r = self.run_cli("read_details", "unit.example", "get", *args)
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            self.assertIn("Sensor temp", r.stdout)
            self.assertIn("Value: 2400", r.stdout)
            self.assertIn("Threshold: 6000", r.stdout)
            body = self.requests()[0]["payload"][0].copy()
            body.pop("@type")
            self.assertEqual(body, expected)
        for args in (("hvdac", "11", "sensors"), ("hvdac", "1-2", "sensors")):
            r = self.run_cli("read_details", "unit.example", "get", *args)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(self.requests(), [])

    def test_cv_control_read_payload_display_and_typed_json(self):
        r = self.run_cli("read_details", "unit.example", "get", "cv", "control")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        for line in ("CV control 1:\n  Configuration:\n    Kinit coefficient: 1000",
                     "    Ki coefficient: 25", "    Dead band: 0", "    Filter coefficient: 900",
                     "    Logging: false", "    Loop interval: 30", "    Number of readings: 5", "    Enabled: true"):
            self.assertIn(line, r.stdout)
        request = self.requests()[0]
        self.assertEqual(request["type"], "RT_GET")
        self.assertEqual(request["payload"], [{"@type": "type.googleapis.com/lotd.MemsModulePayloadList",
            "mems_module": {"id": 1, "common_voltage_card": {"id": 1,
                "cv_control": [{"id": 1, "config": {}}]}}}])
        r, data = self.json_cli("read_details", "unit.example", "get", "cv", "control")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(data["results"][0]["data"], {"id": 1, "config": {
            "kinit_coeff": 1000, "ki_coeff": 25, "dead_band": 0, "filter_coeff": 900,
            "logging": False, "loop_interval": 30, "num_readings": 5, "enable": True}})
        self.assertEqual(data["summary"]["successful_ids"], ["1"])
        self.assertEqual(data["summary"]["verified_ids"], [])
        self.assertEqual(data["errors"], [])

    def test_cv_control_scoping_camel_case_unknown_fields_and_missing_notes(self):
        controls = [{"id": 2, "config": {"kiCoeff": 999}},
                    {"id": "1", "config": {"kiCoeff": 25, "deadBand": 0, "logging": False,
                                             "enable": True, "newField": {"raw": -7}}}]
        response = {"responsePayload": [{"code": "RC_OK", "payload": [{"memsModule": [
            {"id": 2, "commonVoltageCard": [{"id": 1, "cvControl": [{"id": 1, "config": {"kiCoeff": 999}}]}]},
            {"id": 1, "commonVoltageCard": [
                {"id": 2, "cvControl": [{"id": 1, "config": {"kiCoeff": 999}}]},
                {"id": 1, "cvControl": controls}]}]}]},
            {"code": "RC_MISSING_VALUE", "payload": [{"memsModule": [{"id": 1,
                "commonVoltageCard": [{"id": 1, "cvControl": [{"id": 1, "config": {"numReadings": ""}}]}]}]}]}]}
        env = {"MOCK_RESPONSE": json.dumps(response)}
        r, data = self.json_cli("read_details", "unit.example", "get", "cv", "control", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(data["results"][0]["data"], {"id": 1, "config": controls[1]["config"]})
        self.assertTrue(any("numReadings" in note for note in data["summary"]["notes"]))
        r = self.run_cli("read_details", "unit.example", "get", "cv", "control", env=env)
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn("Ki coefficient: 25", r.stdout)
        self.assertIn("    New field:\n      Raw: -7", r.stdout)
        self.assertNotIn("999", r.stdout)

    def test_cv_control_rejects_invalid_responses_and_transport_failures(self):
        for value in ([], {}, [{"id": 2, "config": {"enable": True}}],
                      [{"id": 1, "config": {} }], [{"id": 1, "config": []}],
                      [{"id": 1, "config": {"enable": True}}] * 2,
                      [{"id": 1, "config": {"ki_coeff": True}}],
                      [{"id": 1, "config": {"loopInterval": None}}],
                      [{"id": 1, "config": {"logging": "false"}}]):
            with self.subTest(value=value):
                r, data = self.json_cli("read_details", "unit.example", "get", "cv", "control",
                                       env={"MOCK_SECTION_RESULT": json.dumps(value)})
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertEqual(data["summary"]["failed_ids"], ["1"])
                self.assertTrue(data["errors"])
                self.assertEqual(data["results"][0]["item_status"], "failed")
        for code in ("RC_UNKNOWN_FIELD", "RC_MISSING_VALUE"):
            r = self.run_cli("read_details", "unit.example", "get", "cv", "control", env={"MOCK_CODE": code})
            self.assertEqual(r.returncode, 1, r.stdout+r.stderr)
            self.assertNotIn("Completed successfully", r.stdout)
        r, data = self.json_cli("dial_failure", "unit.example", "get", "cv", "control")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(data["results"][0]["error"]["kind"], "transport")

    def test_cv_control_validation_and_help_documents_fixed_selection(self):
        for args in (("set", "cv", "control"), ("set", "cv", "control", "enable", "false"),
                     ("get", "cv", "control", "2"), ("get", "cv", "control", "all")):
            r = self.run_cli("read_details", "unit.example", *args)
            self.assertEqual(r.returncode, 2, r.stdout+r.stderr)
            self.assertEqual(self.requests(), [])
        for topic in ("cv", "sensors"):
            r = self.run_cli("read_details", "help", topic)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("get cv control", r.stdout)
            self.assertIn("control-loop ID 1", r.stdout)
            self.assertIn("set cv control dead-band <nonnegative-integer>", r.stdout)
            self.assertIn("set cv control logging", r.stdout)
            self.assertIn("interactive confirmation", r.stdout)

    def test_cv_control_set_verified_fields_payloads_and_json(self):
        for selector, argument, field, target in (("dead-band", "0", "dead_band", 0),
                ("dead-band", "12", "dead_band", 12), ("logging", "on", "logging", True),
                ("logging", "off", "logging", False)):
            with self.subTest(selector=selector, argument=argument):
                code, output = self.run_tty("cv_control_set",
                    ("unit.example", "set", "cv", "control", selector, argument, "--json"), "y")
                self.assertEqual(code, 0, output)
                data = self.json_from_tty(output)
                self.assertEqual(data["summary"]["verified_ids"], ["1"])
                self.assertEqual(data["errors"], [])
                self.assertTrue(data["results"][0]["accepted"])
                self.assertFalse(data["results"][0]["verified"])
                self.assertTrue(data["results"][1]["verified"])
                self.assertIsNone(data["results"][1]["accepted"])
                actual = data["results"][1]["data"]["config"][field]
                self.assertEqual(actual, target)
                self.assertIs(type(actual), type(target))
                requests = self.requests()
                self.assertEqual([r["type"] for r in requests], ["RT_SET", "RT_GET"])
                expected = {"@type": "type.googleapis.com/lotd.MemsModulePayloadList",
                    "mems_module": {"id": 1, "common_voltage_card": {"id": 1,
                        "cv_control": [{"id": 1, "config": {field: target}}]}}}
                self.assertEqual(requests[0]["payload"], [expected])
                expected["mems_module"]["common_voltage_card"]["cv_control"][0]["config"] = {}
                self.assertEqual(requests[1]["payload"], [expected])
        code, output = self.run_tty("cv_control_set",
            ("unit.example", "set", "cv", "control", "logging", "off"), "y")
        self.assertEqual(code, 0, output)
        self.assertIn("Logging: false", output)
        self.assertIn("Verification: readback matched", output)

    def test_cv_control_numeric_tuning_writes_confirm_and_verify_one_field(self):
        for selector, value in (("kinit-coeff", 1000), ("ki-coeff", 25), ("filter-coeff", 900),
                                ("loop-interval", 30), ("num-readings", 5)):
            field = selector.replace("-", "_")
            code, output = self.run_tty("cv_control_set_cid",
                ("unit.example", "set", "cv", "control", selector, str(value), "--json"), "y")
            self.assertEqual(code, 0, output)
            report = self.json_from_tty(output)
            self.assertEqual(report["summary"]["verified_ids"], ["1"])
            self.assertIsNone(report["results"][0]["accepted"])
            self.assertEqual(report["results"][1]["data"]["config"][field], value)
            requests = self.requests()
            self.assertEqual([req["type"] for req in requests], ["RT_SET", "RT_GET"])
            for req, config in zip(requests, ({field: value}, {})):
                self.assertEqual(req["payload"][0]["mems_module"], {"id": 1,
                    "common_voltage_card": {"id": 1, "cv_control": [{"id": 1, "config": config}]}})
            r = self.run_cli("cv_control_set", "unit.example", "set", "cv", "control", selector, str(value))
            self.assertEqual(r.returncode, 2)
            self.assertEqual(self.requests(), [])
            code, output = self.run_tty("cv_control_set", ("unit.example", "set", "cv", "control", selector, str(value)), "n")
            self.assertEqual(code, 0, output)
            self.assertEqual(self.requests(), [])

    def test_cv_control_numeric_tuning_validation_and_readback_errors(self):
        for selector in ("kinit-coeff", "ki-coeff", "filter-coeff", "loop-interval", "num-readings"):
            for invalid in ("1.5", "NaN", "Infinity", "1e3", "true", "01", "+1"):
                r = self.run_cli("cv_control_set", "unit.example", "set", "cv", "control", selector, invalid)
                self.assertEqual(r.returncode, 2, r.stdout+r.stderr)
                self.assertEqual(self.requests(), [])
        for config in ({"ki_coeff": 26}, {"ki_coeff": True}, {"ki_coeff": "25"}, {"logging": False}):
            raw = json.dumps({"response_payload": [{"code": "RC_OK", "payload": [{"mems_module": [{"id": 1,
                "common_voltage_card": [{"id": 1, "cv_control": [{"id": 1, "config": config}]}]}]}]}]})
            code, output = self.run_tty("cv_control_set_cid",
                ("unit.example", "set", "cv", "control", "ki-coeff", "25", "--json"), "y",
                env={"MOCK_CONTROL_GET_RESPONSE": raw})
            self.assertEqual(code, 1, output)
            self.assertEqual(self.json_from_tty(output)["summary"]["verified_ids"], [])
        code, output = self.run_tty("cv_control_set_cid",
            ("unit.example", "set", "cv", "control", "ki-coeff", "-1", "--json"), "y")
        self.assertEqual(code, 0, output)  # No undocumented firmware range imposed locally.
        self.assertEqual(self.requests()[0]["payload"][0]["mems_module"]["common_voltage_card"]
                         ["cv_control"][0]["config"], {"ki_coeff": -1})

    def test_cv_control_set_validation_refusal_and_cancellation(self):
        for suffix in ((), ("dead-band",), ("dead-band", "-1"), ("dead-band", "1.5"),
                       ("dead-band", "01"), ("dead-band", "NaN"), ("logging", "true"),
                       ("enable", "on"), ("ki-coeff", "1.5"), ("logging", "off", "extra")):
            r, data = self.json_cli("cv_control_set", "unit.example", "set", "cv", "control", *suffix)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(self.requests(), [])
            self.assertEqual(data["results"], [])
        r, data = self.json_cli("cv_control_set", "unit.example", "set", "cv", "control", "logging", "on")
        self.assertEqual(r.returncode, 2)
        self.assertIn("CV control set requires an interactive terminal", r.stderr)
        self.assertEqual(self.requests(), [])
        code, output = self.run_tty("cv_control_set",
            ("unit.example", "set", "cv", "control", "dead-band", "0", "--json"), "n")
        self.assertEqual(code, 0, output)
        data = self.json_from_tty(output)
        self.assertTrue(data["cancelled"])
        self.assertEqual(data["results"], [])
        self.assertEqual(self.requests(), [])

    def test_cv_control_set_rejections_and_unconfirmed_acknowledgements(self):
        for response, acceptance in (({"response_payload": [{"code": "RC_OPERATION_FAILED"}]}, False),
                ({"response_payload": [{"code": "RC_MISSING_VALUE"}]}, None),
                ({"response_payload": [{"code": "RC_OK"}, {"code": "RC_MISSING_VALUE"}]}, None),
                ({"response_payload": [{"code": "RC_OK"}, {}]}, None),
                ({"cid": "wrong"}, None), ({"cid": "123", "response_payload": []}, None),
                ({}, None), ("invalid JSON", None)):
            raw = response if isinstance(response, str) else json.dumps(response)
            code, output = self.run_tty("cv_control_set",
                ("unit.example", "set", "cv", "control", "logging", "off", "--json"), "y",
                env={"MOCK_CONTROL_SET_RESPONSE": raw})
            self.assertEqual(code, 1, output)
            data = self.json_from_tty(output)
            self.assertIs(data["results"][0]["accepted"], acceptance)
            self.assertEqual(data["summary"]["verified_ids"], [])
            self.assertTrue(data["errors"])
            self.assertEqual(len(self.requests()), 1)
        for phase in ("RT_SET", "RT_GET"):
            code, output = self.run_tty("cv_control_set",
                ("unit.example", "set", "cv", "control", "logging", "off", "--json"), "y",
                env={"MOCK_CONTROL_FAILURE": phase})
            self.assertEqual(code, 1, output)
            data = self.json_from_tty(output)
            self.assertEqual(data["results"][-1]["error"]["kind"], "transport")
            self.assertEqual(data["summary"]["verified_ids"], [])
            self.assertIs(data["results"][0]["accepted"], True if phase == "RT_GET" else None)
            self.assertEqual(len(self.requests()), 2 if phase == "RT_GET" else 1)

    def test_cv_control_set_readback_failure_and_field_specific_verification(self):
        def response(controls):
            return json.dumps({"response_payload": [{"code": "RC_OK", "payload": [{"memsModule": [{
                "id": 1, "commonVoltageCard": [{"id": 1, "cvControl": controls}]}]}]}]})
        for config in ({"dead_band": 7}, {"logging": False}, {"dead_band": True}, {"dead_band": "5"}):
            code, output = self.run_tty("cv_control_set",
                ("unit.example", "set", "cv", "control", "dead-band", "5", "--json"), "y",
                env={"MOCK_CONTROL_GET_RESPONSE": response([{"id": 1, "config": config}])})
            self.assertEqual(code, 1, output)
            data = self.json_from_tty(output)
            self.assertTrue(data["results"][0]["accepted"])
            self.assertFalse(data["results"][1]["verified"])
            self.assertEqual(data["summary"]["failed_ids"], ["1"])
            self.assertEqual(data["summary"]["verified_ids"], [])
            self.assertIn("readback mismatch", data["errors"][0]["message"])
        code, output = self.run_tty("cv_control_set",
            ("unit.example", "set", "cv", "control", "dead-band", "5", "--json"), "y",
            env={"MOCK_CONTROL_GET_RESPONSE": response([{"id": 2, "config": {"deadBand": 99}},
                {"id": 1, "config": {"deadBand": 5, "kiCoeff": None}}])})
        self.assertEqual(code, 0, output)
        self.assertEqual(self.json_from_tty(output)["summary"]["verified_ids"], ["1"])
        for raw in (response([{ "id": 2, "config": {"deadBand": 5}}]), "invalid JSON",
                    json.dumps({"response_payload": [{"code": "RC_UNKNOWN_FIELD"}]})):
            code, output = self.run_tty("cv_control_set",
                ("unit.example", "set", "cv", "control", "dead-band", "5", "--json"), "y",
                env={"MOCK_CONTROL_GET_RESPONSE": raw})
            self.assertEqual(code, 1, output)
            data = self.json_from_tty(output)
            self.assertTrue(data["results"][0]["accepted"])
            self.assertEqual(data["summary"]["verified_ids"], [])

    def test_cv_control_cid_only_response_requires_matching_readback(self):
        for selector, target in (("dead-band", "0"), ("logging", "off")):
            code, output = self.run_tty("cv_control_set_cid",
                ("unit.example", "set", "cv", "control", selector, target, "--json"), "y")
            self.assertEqual(code, 0, output)
            data = self.json_from_tty(output)
            self.assertEqual(data["results"][0]["response"], {"cid": "123"})
            self.assertIsNone(data["results"][0]["accepted"])
            self.assertEqual(data["summary"]["verified_ids"], ["1"])
            self.assertTrue(data["results"][1]["verified"])
            self.assertEqual(data["errors"], [])
            self.assertEqual(len(self.requests()), 2)
            self.assertNotIn("Write accepted", output)
        code, output = self.run_tty("cv_control_set_cid",
            ("unit.example", "set", "cv", "control", "dead-band", "0"), "y")
        self.assertEqual(code, 0, output)
        self.assertIn("Verification: readback matched", output)
        mismatch = {"response_payload": [{"code": "RC_OK", "payload": [{"mems_module": [{
            "id": 1, "common_voltage_card": [{"id": 1, "cv_control": [{"id": 1,
                "config": {"logging": True}}]}]}]}]}]}
        for env in ({"MOCK_CONTROL_GET_RESPONSE": json.dumps(mismatch)},
                    {"MOCK_CONTROL_FAILURE": "RT_GET"}):
            code, output = self.run_tty("cv_control_set_cid",
                ("unit.example", "set", "cv", "control", "logging", "off", "--json"), "y", env=env)
            self.assertEqual(code, 1, output)
            data = self.json_from_tty(output)
            self.assertIsNone(data["results"][0]["accepted"])
            self.assertEqual(data["summary"]["verified_ids"], [])
            self.assertTrue(data["errors"])
            self.assertFalse(data["results"][1]["verified"])
        code, output = self.run_tty("cv_control_set_cid",
            ("unit.example", "set", "cv", "control", "logging", "off", "--json"), "y",
            interrupt_at="Write response has no status code", request_count=2,
            env={"MOCK_CONTROL_INTERRUPT": "RT_GET"})
        self.assertEqual(code, 130, output)
        data = self.json_from_tty(output)
        self.assertIsNone(data["results"][0]["accepted"])
        self.assertEqual(data["summary"]["verified_ids"], [])
        self.assertIn("CID-only write response", data["errors"][0]["message"])
        self.assertNotIn("RT_SET was accepted", output)

    def test_cv_control_set_interruptions_preserve_unknown_completion(self):
        args = ("unit.example", "set", "cv", "control", "logging", "off", "--json")
        for phase, marker, count in (("RT_SET", "Writing CV control", 1),
                                     ("RT_GET", "Write accepted; verifying", 2)):
            code, output = self.run_tty("cv_control_set", args, "y", interrupt_at=marker,
                                       request_count=count, env={"MOCK_CONTROL_INTERRUPT": phase})
            self.assertEqual(code, 130, output)
            data = self.json_from_tty(output)
            self.assertEqual(len(self.requests()), count)
            self.assertEqual(data["summary"]["verified_ids"], [])
            self.assertEqual(data["results"][-1]["item_status"], "incomplete")
            self.assertIsNone(data["results"][-1]["accepted"])
            self.assertIn("unknown" if phase == "RT_SET" else "verification is incomplete", output)
            if phase == "RT_GET":
                self.assertTrue(data["results"][0]["accepted"])
        code, output = self.run_tty("cv_control_set", args, interrupt_at="[y/N]")
        self.assertEqual(code, 130, output)
        self.assertEqual(self.json_from_tty(output)["results"], [])
        self.assertEqual(self.requests(), [])

    def test_cv_regulator_voltages_and_card_state(self):
        r = self.run_cli("read_details", "unit.example", "get", "cv", "voltages")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn("Reading cv regulator voltages at unit.example:3217...", r.stdout)
        self.assertNotIn("Reading cv cv", r.stdout)
        self.assertIn("Returned CV regulator voltages: 23", r.stdout)
        self.assertRegex(r.stdout, r"23\s*\|\s*-72879")
        self.assertRegex(r.stdout, r"18\s*\|\s*11936")
        self.assertEqual(self.requests()[0]["payload"][0]["mems_module"],
                         {"id": 1, "common_voltage_card": {"id": 1, "cv_voltages": {}}})
        r = self.run_cli("read_details", "unit.example", "get", "cv", "state")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn("Channel out-of-range failure: true", r.stdout)
        self.assertIn("Voltage neg3v3: -3583", r.stdout)
        self.assertIn("Missing: false", r.stdout)
        self.assertEqual(self.requests()[0]["payload"][0]["mems_module"]["common_voltage_card"]["state"], {})

    def test_inventory_split_missing_value_and_readable_identity(self):
        r = self.run_cli("read_details", "unit.example", "get", "cv", "inventory", env={"MOCK_SPLIT": "1"})
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn("Serial number: CVMS260400006", r.stdout)
        self.assertIn("Hardware revision: 0", r.stdout)
        self.assertIn("Manufacturing site: Thailand", r.stdout)
        self.assertIn("RC_MISSING_VALUE", r.stdout)
        self.assertIn("inventory.entity_info.clei_code", r.stdout)
        self.assertNotIn("CLEI code:", r.stdout)
        r = self.run_cli("identity_sensor", "unit.example", "get", "shelf", "mac-addresses")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn("eth0: 00:11:22:33:44:55", r.stdout)
        self.assertIn("eth1: 00:11:22:33:44:56", r.stdout)
        self.assertNotIn('"mac_address"', r.stdout)

    def test_new_read_validation_missing_fields_and_transport_errors(self):
        for args in (("set", "mems", "state"), ("set", "cv", "voltages"), ("set", "midplane", "sensors"),
                     ("get", "mems", "temp"), ("get", "mems", "state", "extra"),
                     ("get", "cv", "voltages", "1-3"), ("get", "cv", "sensors", "temp", "extra")):
            r = self.run_cli("read_details", "unit.example", *args)
            self.assertEqual(r.returncode, 2, r.stdout+r.stderr)
            self.assertEqual(self.requests(), [])
        for args, result in ((("mems", "state"), '{}'),
                             (("mems", "humidity"), '{"relative_humidity":true}'),
                             (("mems", "sensors"), '[{"id":"temp","state":{}}]'),
                             (("cv", "voltages"), '[{"id":1,"state":{"cv_voltage":null}}]'),
                             (("cv", "voltages"), '[{"id":1,"state":{"cv_voltage":1}},{"id":1,"state":{"cv_voltage":2}}]')):
            r = self.run_cli("read_details", "unit.example", "get", *args, env={"MOCK_SECTION_RESULT": result})
            self.assertEqual(r.returncode, 1, r.stdout+r.stderr)
        r = self.run_cli("read_details", "unit.example", "get", "mems", "state", env={"MOCK_CODE": "RC_OPERATION_FAILED"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("RC_OPERATION_FAILED", r.stderr)
        r = self.run_cli("dial_failure", "unit.example", "get", "cv", "voltages")
        self.assertEqual(r.returncode, 1)
        self.assertIn("3217", r.stderr)
        self.assertEqual(len(self.requests()), 1)

    def test_new_read_camel_case_and_module_entity_scoping(self):
        from lotd_client import Client, LotdError
        client = Client("unused:3217", False)
        data = {"responsePayload": [{"code": "RC_OK", "payload": [{"memsModule": [
            {"id": 2, "commonVoltageCard": [{"id": 1, "cvVoltages": [{"id": 1, "state": {"cvVoltage": 999}}]}]},
            {"id": 1, "commonVoltageCard": [{"id": 1, "cvVoltages": [{"id": 1, "state": {"cvVoltage": -3583}}]}]}]}]}]}
        self.assertEqual(client.parse(json.dumps(data), "read_section", "common_voltage_card", "1", "cv_voltages"),
                         [{"id": 1, "state": {"cvVoltage": -3583}}])
        data["responsePayload"][0]["payload"][0]["memsModule"].pop()
        with self.assertRaises(LotdError):
            client.parse(json.dumps(data), "read_section", "common_voltage_card", "1", "cv_voltages")
        from live_test_lotd import checks_for
        for check in checks_for("R300", False):
            self.assertFalse(check.arguments[1:3] in (("midplane", "mac-addresses"), ("midplane", "firmware-version")))
        r = self.run_cli("read_details", "help", "sensors")
        self.assertEqual(r.returncode, 0)
        self.assertIn("cv_voltages", r.stdout)
        self.assertIn("A-mems-temp", r.stdout)

    def test_fan_speed_selectors_payloads_and_complete_values(self):
        speeds = {1: 17045, 2: 16574, 3: 16759}
        for selector, ids in (("all", [1,2,3]), ("2-3", [2,3]), ("3,1-2,1", [3,1,2])):
            r = self.run_cli("fan_reads", "unit.example", "get", "fan", selector, "speed",
                             env={"MOCK_FAN_RETURN_ALL": "1"})
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            self.assertIn(f"Successful IDs ({len(ids)}): " + ",".join(map(str,ids)), r.stdout)
            self.assertIn("Failed IDs: none", r.stdout)
            for fan_id in ids:
                self.assertRegex(r.stdout, rf"{fan_id}\s*\|\s*{speeds[fan_id]}\s*\|\s*Read OK")
            requests = self.requests()
            self.assertEqual(len(requests), len(ids))
            for req, fan_id in zip(requests, ids):
                self.assertEqual(req["type"], "RT_GET")
                self.assertEqual(req["payload"], [{"@type": "type.googleapis.com/lotd.FanControlPayloadList",
                                                  "fans_module": {"id": fan_id, "state": {}}}])

    def test_fan_state_presence_and_temperature_raw_values(self):
        r = self.run_cli("fan_reads", "unit.example", "get", "fan", "1", "state")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        for text in ("Presence: true", "Missing: false", "PWM: 65", "Speed: 17045",
                     "Temperature: 4200", "Fan error: false", "Operational status: openconfig-platform-types:ACTIVE"):
            self.assertIn(text, r.stdout)
        r = self.run_cli("fan_reads", "unit.example", "get", "fan", "2", "presence")
        self.assertEqual(r.returncode, 0)
        self.assertIn("presence=true, missing=false", r.stdout)
        for result, expected in (({"presence": False}, "Presence: false"), ({"missing": True}, "Missing: true")):
            r = self.run_cli("fan_reads", "unit.example", "get", "fan", "1", "presence",
                             env={"MOCK_SECTION_RESULT": json.dumps(result)})
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            self.assertIn(expected, r.stdout)
            self.assertNotIn("Missing:" if "presence" in result else "Presence:", r.stdout)
        for alias in ("temp", "temperature"):
            r = self.run_cli("fan_reads", "unit.example", "get", "fan", "1", alias,
                             env={"MOCK_SECTION_RESULT": '{"temperature":-2500}'})
            self.assertEqual(r.returncode, 0)
            self.assertIn("Temperature: -2500", r.stdout)

    def test_fan_inventory_overview_and_scoped_missing_notes(self):
        r = self.run_cli("fan_reads", "unit.example", "get", "fan", "3", "inventory",
                         env={"MOCK_FAN_RETURN_ALL": "1"})
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn("Serial number: FANS255101441", r.stdout)
        self.assertIn("Hardware revision: 1", r.stdout)
        self.assertNotIn("Marketing part number:", r.stdout)
        self.assertNotIn("CLEI code:", r.stdout)
        self.assertIn("RC_MISSING_VALUE (fans_module ID 3)", r.stdout)
        self.assertNotIn("RC_MISSING_VALUE (fans_module ID 1)", r.stdout)
        self.assertNotIn("FANS255101509", r.stdout)
        self.assertEqual(self.requests()[0]["payload"][0]["fans_module"], {"id": 3, "inventory": {}})
        r = self.run_cli("fan_reads", "unit.example", "get", "fan", "1", "overview")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn("  PWM: 65", r.stdout)
        self.assertIn("  Configuration:\n    PWM target: 40", r.stdout)
        self.assertIn("FCM_AUTO", r.stdout)
        self.assertIn("  State:\n    Presence: true", r.stdout)
        self.assertIn("  LEDs:\n    ID 1:", r.stdout)
        self.assertIn("Description: Fan 1 Blue LED", r.stdout)
        self.assertIn("  Sensors:\n    ID temp:\n      State:\n        Value: 4200", r.stdout)
        self.assertIn("Low threshold:\n          Threshold: 500", r.stdout)
        self.assertNotIn("State /", r.stdout)
        self.assertNotIn('"config"', r.stdout)
        self.assertNotIn('[{', r.stdout)
        self.assertIn("FANS255101509", r.stdout)
        self.assertEqual(self.requests()[0]["payload"][0]["fans_module"], {"id": 1})

    def test_fan_sensor_details_selects_exact_sensor_and_retains_flags(self):
        for args in (("sensors",), ("sensors", "temp")):
            r = self.run_cli("fan_reads", "unit.example", "get", "fan", "all", *args)
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            for text in ("Value: 4200", "Out-of-range high: false", "Out-of-range low: false", "Threshold: 5500", "Hysteresis: 500",
                         "Fan 3 Temperature Sensor", "Successful IDs (3): 1,2,3"):
                self.assertIn(text, r.stdout)
            self.assertNotIn("Failed: false", r.stdout)
            self.assertTrue(all(req["payload"][0]["fans_module"]["sensor"] == {} for req in self.requests()))
        r = self.run_cli("fan_reads", "unit.example", "get", "fan", "1", "sensors", "unknown")
        self.assertEqual(r.returncode, 1)
        self.assertIn("available IDs: temp", r.stderr)

    def test_fan_read_validation_and_malformed_missing_fields(self):
        for args in (("get", "fan", "0", "state"), ("get", "fan", "4", "speed"),
                     ("get", "fan", "1", "speed", "extra"), ("get", "fan", "1", "sensors", "temp", "extra"),
                     ("set", "fan", "all", "state"), ("set", "fan", "1", "speed", "100"),
                     ("set", "fan", "1", "overview")):
            r = self.run_cli("fan_reads", "unit.example", *args)
            self.assertEqual(r.returncode, 2, r.stdout+r.stderr)
            self.assertEqual(self.requests(), [])
        for command, result in (("speed", {"speed": True}), ("temperature", {"temperature": None}),
                                ("presence", {"presence": "true"}), ("presence", {"fan_error": False}),
                                ("state", []), ("inventory", ["invalid"]),
                                ("sensors", [{"id": "temp", "state": {}}])):
            r = self.run_cli("fan_reads", "unit.example", "get", "fan", "1", command,
                             env={"MOCK_SECTION_RESULT": json.dumps(result)})
            self.assertEqual(r.returncode, 1, r.stdout+r.stderr)
            self.assertNotIn("Completed successfully", r.stdout+r.stderr)
        r = self.run_cli("malformed_json", "unit.example", "get", "fan", "1", "state")
        self.assertEqual(r.returncode, 1)
        self.assertIn("invalid JSON", r.stderr)
        r = self.run_cli("fan_reads", "help", "fan")
        self.assertEqual(r.returncode, 0)
        self.assertIn("overview|state|config|control-mode|inventory|presence|speed|pwm|temp|temperature", r.stdout)
        self.assertIn("set fan <ids> pwm <0-100>", r.stdout)
        self.assertIn("set fan <ids> control-mode auto", r.stdout)
        self.assertIn("set fan <ids> control-mode manual <0-100>", r.stdout)

    def test_fan_batch_failures_stop_timeout_and_interrupt(self):
        r = self.run_cli("fan_reads", "unit.example", "get", "fan", "all", "speed", env={"MOCK_FAN_ERROR_IDS": "2"})
        output = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.requests()), 3)
        self.assertIn("Successful IDs (2): 1,3", output)
        self.assertIn("Failed IDs (1): 2", output)
        self.assertLess(output.index("Successful IDs (2)"), output.index("Failed IDs (1)"))
        self.assertLess(output.index("Failed IDs (1)"), output.index("Returned speed"))
        r = self.run_cli("fan_reads", "unit.example", "get", "fan", "all", "state", env={"MOCK_FAN_TIMEOUT_ID": "2"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.requests()), 2)
        self.assertIn("Stopped after endpoint connection failure", r.stderr)
        self.assertIn("Not attempted (1): 3", r.stderr)
        self.assertIn("[HINT] LOTD timed out", r.stderr)
        code, output = self.run_tty("fan_reads", ("unit.example", "get", "fan", "all", "speed"),
                                    interrupt_at="Reading fan 2 speed", request_count=2,
                                    env={"MOCK_FAN_INTERRUPT_ID": "2"})
        self.assertEqual(code, 130, output)
        self.assertIn("Successful IDs (1): 1", output)
        self.assertIn("Interrupted during fan 2 RT_GET", output)
        self.assertIn("Not attempted (1): 3", output)
        self.assertEqual(len(self.requests()), 2)

    def test_ps_present_reads_payload_and_typed_json(self):
        for command, text in (("overview", "Description: PS Controller 1"),
                              ("state", "Input voltage reading: 11650"),
                              ("inventory", "Serial number: SU2445RF0193"),
                              ("presence", "Missing: false"), ("temperature", "Temperature: 2400")):
            r = self.run_cli("ps_reads", "unit.example", "get", "ps", "1", command)
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            self.assertIn(text, r.stdout)
            self.assertNotIn("Power supply 2", r.stdout)
            self.assertNotIn("RC_FRU_MISSING", r.stdout+r.stderr)
            self.assertEqual(self.requests()[0]["payload"], [{"@type": "type.googleapis.com/lotd.PsControlPayloadList", "ps_module": {"id": 1}}])
            self.assertEqual(self.requests()[0]["type"], "RT_GET")
        r, data = self.json_cli("ps_reads", "unit.example", "get", "psu", "1", "state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(data["results"][0]["data"]["status_word"], 130)
        self.assertEqual(data["results"][0]["data"]["read_temperature1"], 2400)
        self.assertIs(data["results"][0]["data"]["missing"], False)
        self.assertEqual(data["summary"]["successful_ids"], ["1"])
        self.assertNotIn("notes", data["summary"])
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["results"][0]["response"]["response_payload"][1]["code"], "RC_FRU_MISSING")

    def test_ps_absent_supply_presence_inventory_and_no_fake_measurements(self):
        r, data = self.json_cli("ps_reads", "unit.example", "get", "ps", "all", "presence")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(data["summary"]["successful_ids"], ["1", "2"])
        self.assertEqual([item["data"] for item in data["results"]], [{"missing": False}, {"missing": True}])
        self.assertTrue(any("Power supply 2 is missing" in note for note in data["summary"]["notes"]))
        r, data = self.json_cli("ps_reads", "unit.example", "get", "ps", "2", "inventory")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(data["results"][0]["data"], {"entity_description": "PS Controller 2"})
        r, data = self.json_cli("ps_reads", "unit.example", "get", "ps", "2", "overview")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("state", data["results"][0]["data"]["sensor"][0])
        r = self.run_cli("ps_reads", "unit.example", "get", "ps", "2", "state")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn("telemetry unavailable. Raw returned fields follow", r.stdout)
        for command in ("temp", "sensors"):
            r, data = self.json_cli("ps_reads", "unit.example", "get", "ps", "2", command)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertEqual(data["results"][0]["data"], {"missing": True})
            self.assertIn("measurement is unavailable", data["errors"][0]["message"])
            self.assertEqual(data["summary"]["failed_ids"], ["2"])
            self.assertNotIn("Temperature: 0", r.stdout+r.stderr)

    def test_ps_temperature_sensor_details_and_batch_unavailable_row(self):
        for command in ("temp", "temperature"):
            r, data = self.json_cli("ps_reads", "unit.example", "get", "ps", "1", command)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(data["results"][0]["data"], {"read_temperature1": 2400})
        r = self.run_cli("ps_reads", "unit.example", "get", "ps", "1", "sensors", "temp")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        for text in ("Sensor temp", "Value: 2400", "Peak value: 2400", "Threshold: -500", "Hysteresis: 500"):
            self.assertIn(text, r.stdout)
        r = self.run_cli("ps_reads", "unit.example", "get", "ps", "1", "sensors", "other")
        self.assertEqual(r.returncode, 1)
        self.assertIn("available IDs: temp", r.stderr)
        r = self.run_cli("ps_reads", "unit.example", "get", "ps", "all", "temperature")
        self.assertEqual(r.returncode, 1, r.stdout+r.stderr)
        self.assertRegex(r.stdout, r"1\s*\|\s*2400\s*\|\s*Read OK")
        self.assertRegex(r.stdout, r"2\s*\|\s*-\s*\|\s*Unavailable \(missing\)")
        self.assertIn("Successful IDs (1): 1", r.stderr)
        self.assertIn("Failed IDs (1): 2", r.stderr)

    def test_ps_validation_read_only_selectors_and_help(self):
        for args in (("set", "ps", "1", "state"), ("set", "psu", "1", "led", "on"),
                     ("get", "ps", "0", "state"), ("get", "ps", "3", "state"),
                     ("get", "ps", "1", "state", "extra"), ("get", "ps", "inventory"),
                     ("get", "ps", "1", "sensor"), ("get", "ps", "1", "sensors", "temp", "extra")):
            r, data = self.json_cli("ps_reads", "unit.example", *args)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(self.requests(), [])
            self.assertEqual(data["results"], [])
        for selector, expected in (("2,1", [2, 1]), ("1-2", [1, 2]), ("all", [1, 2])):
            r = self.run_cli("ps_reads", "unit.example", "get", "ps", selector, "presence")
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            self.assertEqual([item["payload"][0]["ps_module"]["id"] for item in self.requests()], expected)
        for topic in ("ps", "psu"):
            r = self.run_cli("ps_reads", "help", topic)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("ps and psu are aliases", r.stdout)
            self.assertIn("RC_FRU_MISSING", r.stdout)
            self.assertIn("writes and local Unix-socket transport are not implemented", r.stdout)

    def test_ps_batch_errors_timeout_and_interruptions(self):
        r, data = self.json_cli("ps_reads", "unit.example", "get", "ps", "all", "presence", env={"MOCK_PS_ERROR_ID": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(data["summary"]["successful_ids"], ["2"])
        self.assertEqual(data["summary"]["failed_ids"], ["1"])
        self.assertEqual(len(self.requests()), 2)
        r = self.run_cli("ps_reads", "unit.example", "get", "ps", "all", "temperature", env={"MOCK_PS_TIMEOUT_ID": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.requests()), 1)
        self.assertIn("Not attempted (1): 2", r.stderr)
        self.assertRegex(r.stdout, r"2\s*\|\s*-\s*\|\s*Not attempted")
        code, output = self.run_tty("ps_reads", ("unit.example", "get", "ps", "all", "presence", "--json"),
            interrupt_at="Reading power supply 2 presence", request_count=2, env={"MOCK_PS_INTERRUPT_ID": "2"})
        self.assertEqual(code, 130, output)
        data = self.json_from_tty(output)
        self.assertEqual(data["summary"]["successful_ids"], ["1"])
        self.assertEqual(data["results"][-1]["item_status"], "incomplete")

    def test_ps_malformed_responses_and_missing_fru_not_globally_ignored(self):
        from lotd_client import Client, LotdError
        client = Client("unit.example:3217", False)
        for mode, args in (("read_entity", ("fans_module", "1")), ("status", ())):
            with self.assertRaisesRegex(LotdError, "RC_FRU_MISSING"):
                client.parse('{"response_payload":[{"code":"RC_FRU_MISSING"}]}', mode, *args)
        for raw in ('invalid JSON', '{}', '{"response_payload":42}',
                    '{"response_payload":[{"code":"RC_OK","payload":[{"ps_module":{"id":1,"state":{}}}]}]}'):
            r, data = self.json_cli("ps_reads", "unit.example", "get", "ps", "1", "state", env={"MOCK_RESPONSE": raw})
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertTrue(data["errors"])
            self.assertNotEqual(data["errors"][0]["kind"], "internal")
        for state in ({"missing": False}, {"missing": False, "read_temperature1": True}):
            raw = json.dumps({"response_payload": [{"code": "RC_OK", "payload": [{"ps_module": {"id": 1, "state": state}}]}]})
            r = self.run_cli("ps_reads", "unit.example", "get", "ps", "1", "temp", env={"MOCK_RESPONSE": raw})
            self.assertEqual(r.returncode, 1, r.stdout+r.stderr)

    def test_unrecognized_commands_do_not_report_unrelated_led_syntax(self):
        for mode in ("get", "set"):
            for comp, args in (("fan", ("1", "auto")), ("hvdac", ("1", "bogus")),
                               ("cv", ("bogus",)), ("shelf", ("bogus",)),
                               ("midplane", ("bogus",)), ("lds", ("bogus",))):
                r, data = self.json_cli("success", "unit.example", mode, comp, *args)
                self.assertEqual(r.returncode, 2, r.stderr)
                message = data["errors"][0]["message"]
                self.assertIn(f"Unrecognized {comp} {mode.upper()} command", message)
                self.assertNotIn("LED", message)
                self.assertEqual(self.requests(), [])
                if comp != "fan":
                    self.assertNotIn("fan", message)
                else:
                    self.assertIn(f"{mode} fan 1 control-mode", message)
        for comp, args in (("fan", ("1", "led")), ("hvdac", ("1", "led")),
                           ("shelf", ("led",)), ("midplane", ("led",)), ("cv", ("led",))):
            r = self.run_cli("success", "unit.example", "set", comp, *args)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn(f"{comp} LED SET syntax", r.stderr)
            self.assertEqual(self.requests(), [])

    def test_fan_control_reads_configuration_mode_and_actual_pwm(self):
        for command, expected in (("config", "Manual PWM target: 40"),
                                  ("control-mode", "Control mode: FCM_AUTO"), ("pwm", "PWM: 65")):
            r = self.run_cli("fan_reads", "unit.example", "get", "fan", "1", command)
            self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
            self.assertIn(expected, r.stdout)
            section = "state" if command == "pwm" else "config"
            self.assertEqual(self.requests()[0]["payload"][0]["fans_module"], {"id": 1, section: {}})
        r, data = self.json_cli("fan_reads", "unit.example", "get", "fan", "all", "pwm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([r["data"] for r in data["results"]], [{"pwm": 65}] * 3)

    def test_fan_control_set_manual_and_auto_payloads_verification(self):
        for suffix, expected in ((("pwm", "50"), {"manual_pwm_target": 50, "control_mode": {"value": "FCM_MANUAL"}}),
                (("control-mode", "manual", "40"), {"manual_pwm_target": 40, "control_mode": {"value": "FCM_MANUAL"}}),
                (("control-mode", "auto"), {"control_mode": {"value": "FCM_AUTO"}})):
            code, output = self.run_tty("fan_control_set", ("unit.example", "set", "fan", "1", *suffix, "--json"), "y")
            self.assertEqual(code, 0, output)
            data = self.json_from_tty(output)
            self.assertEqual(data["summary"]["verified_ids"], ["1"])
            self.assertIsNone(data["results"][0]["accepted"])
            self.assertTrue(data["results"][1]["verified"])
            self.assertEqual(data["errors"], [])
            requests = self.requests()
            self.assertEqual([r["type"] for r in requests], ["RT_SET", "RT_GET"])
            self.assertEqual(requests[0]["payload"], [{"@type": "type.googleapis.com/lotd.FanControlPayloadList",
                "fans_module": {"id": 1, "config": expected}}])
            self.assertEqual(requests[1]["payload"][0]["fans_module"], {"id": 1, "config": {}})
        code, output = self.run_tty("fan_control_set", ("unit.example", "set", "fan", "1-3", "pwm", "50"), "y",
            env={"MOCK_FAN_CONTROL_SET_RESPONSE": '{"response_payload":[{"code":"RC_OK"}]}'})
        self.assertEqual(code, 0, output)
        self.assertIn("Successful IDs (3): 1,2,3", output)
        self.assertIn("Verification: readback matched", output)
        self.assertEqual(len(self.requests()), 6)

    def test_fan_control_set_validation_confirmation_and_no_requests(self):
        for suffix in (("pwm", "-1"), ("pwm", "101"), ("pwm", "1.5"), ("pwm", "true"),
                       ("pwm",), ("control-mode", "manual"), ("control-mode", "other"),
                       ("control-mode", "auto", "40")):
            r, data = self.json_cli("fan_control_set", "unit.example", "set", "fan", "1", *suffix)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(self.requests(), [])
        for selector in ("0", "4", "1-4"):
            r = self.run_cli("fan_control_set", "unit.example", "set", "fan", selector, "pwm", "50")
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(self.requests(), [])
        r = self.run_cli("fan_control_set", "unit.example", "set", "fan", "1", "pwm", "50", stdin="y\n")
        self.assertEqual(r.returncode, 2)
        self.assertIn("fan control set requires an interactive terminal", r.stderr)
        self.assertEqual(self.requests(), [])
        code, output = self.run_tty("fan_control_set", ("unit.example", "set", "fan", "all", "pwm", "50", "--json"), "n")
        self.assertEqual(code, 0, output)
        self.assertTrue(self.json_from_tty(output)["cancelled"])
        self.assertEqual(self.requests(), [])

    def test_fan_control_set_rejection_continues_and_timeout_stops(self):
        args = ("unit.example", "set", "fan", "all", "control-mode", "auto", "--json")
        code, output = self.run_tty("fan_control_set", args, "y", env={"MOCK_FAN_CONTROL_ERROR_ID": "2"})
        self.assertEqual(code, 1, output)
        data = self.json_from_tty(output)
        self.assertEqual(data["summary"]["verified_ids"], ["1", "3"])
        self.assertEqual(data["summary"]["failed_ids"], ["2"])
        self.assertFalse(data["results"][2]["accepted"])
        self.assertEqual(len(self.requests()), 5)
        for phase in ("RT_SET", "RT_GET"):
            code, output = self.run_tty("fan_control_set", args, "y", env={"MOCK_FAN_CONTROL_TIMEOUT": phase + ":2"})
            self.assertEqual(code, 1, output)
            data = self.json_from_tty(output)
            self.assertEqual(data["summary"]["successful_ids"], ["1"])
            self.assertEqual(data["summary"]["failed_ids"], ["2"])
            self.assertEqual(data["summary"]["not_attempted_ids"], ["3"])
            self.assertEqual(len(self.requests()), 3 if phase == "RT_SET" else 4)

    def test_fan_control_set_checks_changed_fields_and_scopes_readback(self):
        def response(entries, code="RC_OK"):
            return json.dumps({"responsePayload": [{"code": code, "payload": [{"fansModule": entries}]}]})
        args = ("unit.example", "set", "fan", "1", "pwm", "50", "--json")
        for entries in ([{"id": 1, "config": {"control_mode": {"value": "FCM_AUTO"}, "manual_pwm_target": 50}}],
                [{"id": 1, "config": {"control_mode": {"value": "FCM_MANUAL"}, "manual_pwm_target": 40}}],
                [{"id": 1, "config": {"control_mode": {"value": "FCM_MANUAL"}, "manual_pwm_target": "50"}}],
                [{"id": 2, "config": {"control_mode": {"value": "FCM_MANUAL"}, "manual_pwm_target": 50}}],
                [{"id": 1, "config": {"control_mode": {"value": "FCM_MANUAL"}, "manual_pwm_target": 50}}] * 2):
            code, output = self.run_tty("fan_control_set", args, "y", env={"MOCK_FAN_CONTROL_GET_RESPONSE": response(entries)})
            self.assertEqual(code, 1, output)
            data = self.json_from_tty(output)
            self.assertEqual(data["summary"]["verified_ids"], [])
            self.assertFalse(data["results"][1]["verified"])
        entries = [{"id": 2, "config": {"controlMode": {"value": "FCM_AUTO"}}},
                   {"id": "1", "config": {"controlMode": {"value": "FCM_MANUAL"}, "manualPwmTarget": 50}}]
        code, output = self.run_tty("fan_control_set", args, "y", env={"MOCK_FAN_CONTROL_GET_RESPONSE": response(entries)})
        self.assertEqual(code, 0, output)
        for raw in ('{}', '{"cid":"wrong"}', '{"response_payload":[{"code":"RC_MISSING_VALUE"}]}', 'invalid JSON'):
            code, output = self.run_tty("fan_control_set", args, "y", env={"MOCK_FAN_CONTROL_SET_RESPONSE": raw})
            self.assertEqual(code, 1, output)
            self.assertEqual(len(self.requests()), 1)

    def test_fan_control_set_interruptions_keep_verified_and_pending_ids(self):
        args = ("unit.example", "set", "fan", "all", "pwm", "50", "--json")
        for phase, marker, count in (("RT_SET", "Writing fan 2 control", 3),
                                     ("RT_GET", "Writing fan 2 control", 4)):
            code, output = self.run_tty("fan_control_set", args, "y", interrupt_at=marker,
                request_count=count, env={"MOCK_FAN_CONTROL_INTERRUPT": phase + ":2"})
            self.assertEqual(code, 130, output)
            data = self.json_from_tty(output)
            self.assertEqual(data["summary"]["verified_ids"], ["1"])
            self.assertEqual(data["summary"]["not_attempted_ids"], ["3"])
            self.assertEqual(data["results"][-1]["item_status"], "incomplete")
            self.assertIsNone(data["results"][-1]["accepted"])
            self.assertEqual(len(self.requests()), count)

    def test_fan_response_casing_ids_duplicates_and_missing_only(self):
        def raw_response(entries, code="RC_OK"):
            return json.dumps({"responsePayload": [{"code": code, "payload": [{"fansModule": entries}]}]})
        for entries, code in (([{"id": "1", "state": {"speed": 0}}, {"id": 2, "state": {"speed": 999}}], 0),
                              ([{"id": 2, "state": {"speed": 999}}], 1),
                              ([{"id": 1, "state": {"speed": 1}}, {"id": 1, "state": {"speed": 2}}], 1)):
            r = self.run_cli("fan_reads", "unit.example", "get", "fan", "1", "speed", env={"MOCK_RESPONSE": raw_response(entries)})
            self.assertEqual(r.returncode, code, r.stdout+r.stderr)
            if code == 0:
                self.assertIn("Speed: 0", r.stdout)
                self.assertNotIn("999", r.stdout)
        r = self.run_cli("fan_reads", "unit.example", "get", "fan", "1", "inventory",
                         env={"MOCK_RESPONSE": raw_response([{"id": 1, "inventory": {"entity_info": {"clei_code": ""}}}], "RC_MISSING_VALUE")})
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Completed successfully", r.stdout+r.stderr)

    def json_cli(self, scenario, *args, env=None):
        result = self.run_cli(scenario, *args, "--json", env=env)
        data = json.loads(result.stdout)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["exit_code"], result.returncode)
        self.assertEqual(set(data["summary"]) - {"notes"},
                         {"successful_ids", "failed_ids", "not_attempted_ids", "verified_ids"})
        if "notes" in data["summary"]:
            self.assertTrue(data["summary"]["notes"])
        self.assertNotIn("\x1b[", result.stdout)
        return result, data

    def json_from_tty(self, output):
        documents = [line for line in strip_ansi(output).splitlines() if line.startswith('{"schema_version":')]
        self.assertEqual(len(documents), 1, output)
        return json.loads(documents[0])

    def test_numeric_tables_keep_failure_and_pending_rows(self):
        r = self.run_cli("fan_reads", "unit.example", "get", "fan", "all", "speed", env={"MOCK_FAN_TIMEOUT_ID": "2"})
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout, r"1\s*\|\s*17045\s*\|\s*Read OK")
        self.assertRegex(r.stdout, r"2\s*\|\s*-\s*\|\s*Failed")
        self.assertRegex(r.stdout, r"3\s*\|\s*-\s*\|\s*Not attempted")
        r = self.run_cli("cv_per_channel_error", "unit.example", "get", "cv", "channel", "1-3")
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout, r"2\s*\|\s*-\s*\|\s*-\s*\|\s*Failed")
        self.assertRegex(r.stdout, r"3\s*\|\s*7.5\s*\|\s*6.5\s*\|\s*Read OK")
        self.assertEqual(len(self.requests()), 3)

    def test_live_cv_set_baseline_accepts_tables_and_skips_failed_rows(self):
        from live_test_lotd import set_checks_for
        from lotd import format_table
        output = format_table(["Channel ID", "Target voltage", "Actual voltage", "Read result"],
                              [[1, "199.25", "200", "Read OK"], [2, "-", "-", "Failed"]])
        checks, _ = set_checks_for("R300", True, {"CV channels 1-3": output})
        cv = [check for check in checks if check.arguments[1:3] == ("cv", "channel")]
        self.assertEqual(len(cv), 1)
        self.assertEqual(cv[0].baseline, "199.25")
        checks, _ = set_checks_for("R300", True, {"CV channels 1-3": output.replace("Read OK", "Failed")})
        self.assertFalse(any(check.arguments[1:3] == ("cv", "channel") for check in checks))

    def test_json_batch_reads_are_typed_and_stdout_is_one_document(self):
        result, data = self.json_cli("fan_reads", "unit.example", "get", "fan", "all", "speed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data["summary"]["successful_ids"], ["1", "2", "3"])
        self.assertEqual(data["summary"]["failed_ids"], [])
        self.assertEqual(data["errors"], [])
        self.assertEqual([record["data"]["speed"] for record in data["results"]], [17045, 16574, 16759])
        self.assertEqual(data["endpoint"], "unit.example:3217")
        self.assertEqual(data["model"], "R300")
        self.assertIn("Reading fan", result.stderr)
        self.assertTrue(all(record["item_status"] == "successful" for record in data["results"]))
        self.assertEqual(len(self.requests()), 3)

    def test_json_existing_families_export_native_data_not_display_text(self):
        cases = [("channel_success", ("hvdac", "1", "channel", "2", "7"), {"target_voltage":123,"actual_voltage":120,"oor_failure":False}),
                 ("dac_count_get", ("hvdac", "1", "dac-count", "2", "7"), {"dac_count":100}),
                 ("cv_channel_get", ("cv", "channel", "1"), {"target_voltage":7.5,"actual_voltage":6.5}),
                 ("mirror_get", ("mems", "1", "x-tilt"), {"x_tilt_voltage":77.25}),
                 ("identity_sensor", ("shelf", "firmware-version", "active"), "SONiC-OS-active-build")]
        for scenario, args, expected in cases:
            result, data = self.json_cli(scenario, "unit.example", "get", *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(data["results"][-1]["data"], expected)
        result, data = self.json_cli("read_details", "unit.example", "get", "cv", "voltages")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(data["results"][0]["data"]), 23)
        self.assertEqual(data["results"][0]["data"][-1]["state"]["cv_voltage"], -72879)
        result, data = self.json_cli("read_details", "unit.example", "get", "mems", "sensors", "A-mems-temp", "--model", "R300-L",
                                     env={"MOCK_MODEL":"R300-L"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data["model"], "R300-L")
        self.assertEqual([item["id"] for item in data["results"][0]["data"]], ["A-mems-temp"])

    def test_json_usage_errors_have_no_requests(self):
        for args in ((), ("invalid..host", "get", "fan", "1", "speed"),
                     ("unit.example", "get", "fan", "4", "speed"),
                     ("unit.example", "get", "cv", "state", "--model", "bogus"),
                     ("unit.example", "set", "cv", "channel", "1", "9" * 400 + ".0"), ("help",)):
            result, data = self.json_cli("fan_reads", *args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertTrue(data["errors"])
            self.assertEqual(data["results"], [])
            self.assertEqual(self.requests(), [])

    def test_json_partial_timeout_and_post_parse_validation_failures(self):
        result, data = self.json_cli("fan_reads", "unit.example", "get", "fan", "all", "speed", env={"MOCK_FAN_ERROR_IDS":"2"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(data["summary"]["successful_ids"], ["1","3"])
        self.assertEqual(data["summary"]["failed_ids"], ["2"])
        self.assertEqual(data["results"][1]["item_status"], "failed")
        self.assertIn("RC_OPERATION_FAILED", data["errors"][0]["message"])
        result, data = self.json_cli("fan_reads", "unit.example", "get", "fan", "all", "speed", env={"MOCK_FAN_TIMEOUT_ID":"2"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(data["summary"]["not_attempted_ids"], ["3"])
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["results"][-1]["error"]["kind"], "transport")
        result, data = self.json_cli("fan_reads", "unit.example", "get", "fan", "1", "speed", env={"MOCK_SECTION_RESULT":'{"speed":true}'})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(data["results"][0]["error"]["kind"], "result")
        self.assertEqual(data["summary"]["successful_ids"], [])

    def test_json_compact_summary_preserves_command_error_diagnostics(self):
        from lotd_client import JsonReport, id_summary
        report = JsonReport(["unit.example", "get", "fan", "1", "speed"])
        report.record_summary("Completed with 1 failure", "get fan 1 speed",
                              id_summary([], ["1"]) + ["Required speed is absent"], [])
        data = report.finish(1)
        self.assertEqual(data["summary"], {"successful_ids": [], "failed_ids": ["1"],
                                           "not_attempted_ids": [], "verified_ids": []})
        self.assertEqual(data["errors"], [{"kind": "command", "message": "Completed with 1 failure",
                                           "details": ["Required speed is absent"]}])

    def test_json_inventory_notes_exclude_missing_placeholders_from_parsed_data(self):
        result, data = self.json_cli("fan_reads", "unit.example", "get", "fan", "3", "inventory")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data["errors"], [])
        self.assertNotIn("marketing_pn", data["results"][0]["data"]["entity_info"])
        self.assertTrue(any("marketing_pn" in note for note in data["summary"]["notes"]))
        self.assertEqual(data["results"][0]["response"]["response_payload"][1]["code"], "RC_MISSING_VALUE")

    def test_json_write_acceptance_is_separate_from_readback_verification(self):
        for scenario, expected_code in (("channel_set_success",0), ("channel_set_mismatch",1)):
            result, data = self.json_cli(scenario, "unit.example", "set", "hvdac", "1", "channel", "2", "7", "123.5")
            self.assertEqual(result.returncode, expected_code, result.stderr)
            self.assertIsNone(data["results"][0]["accepted"])
            self.assertEqual(data["summary"]["verified_ids"], ["7"] if expected_code == 0 else [])
            self.assertEqual(data["results"][1]["verified"], expected_code == 0)
            self.assertEqual(len(self.requests()), 2)
        result, data = self.json_cli("set_app_error", "unit.example", "set", "hvdac", "1", "led", "power", "on")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(data["results"][0]["accepted"])
        self.assertEqual(len(self.requests()), 1)
        result, data = self.json_cli("malformed_json", "unit.example", "set", "hvdac", "1", "led", "power", "on")
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(data["results"][0]["accepted"])
        self.assertEqual(data["summary"]["verified_ids"], [])

    def test_json_confirmation_remains_interactive_and_cancellation_is_explicit(self):
        args = ("unit.example", "set", "cv", "channel", "1-2", "5", "--json")
        result, data = self.json_cli("cv_channel_set_success", *args[:-1])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.requests(), [])
        code, output = self.run_tty("cv_channel_set_success", args, "n")
        self.assertEqual(code, 0, output)
        data = self.json_from_tty(output)
        self.assertTrue(data["cancelled"])
        self.assertEqual(data["summary"], {"successful_ids": [], "failed_ids": [],
                                           "not_attempted_ids": [], "verified_ids": []})
        self.assertEqual(self.requests(), [])
        code, output = self.run_tty("cv_channel_set_success", args, "y")
        self.assertEqual(code, 0, output)
        data = self.json_from_tty(output)
        self.assertFalse(data["cancelled"])
        self.assertEqual(data["summary"]["verified_ids"], ["1","2"])
        self.assertEqual(len(self.requests()), 4)

    def test_json_interruptions_preserve_completed_ids_and_unknown_writes(self):
        code, output = self.run_tty("fan_reads", ("unit.example","get","fan","all","speed","--json"),
                                    interrupt_at="Reading fan 2 speed", request_count=2, env={"MOCK_FAN_INTERRUPT_ID":"2"})
        self.assertEqual(code, 130, output)
        data = self.json_from_tty(output)
        self.assertEqual(data["exit_code"], 130)
        self.assertEqual(data["summary"]["successful_ids"], ["1"])
        self.assertEqual(data["summary"]["not_attempted_ids"], ["3"])
        code, output = self.run_tty("interrupt_set", ("unit.example","set","cv","channel","1-3","5","--json"), "y",
                                    interrupt_at="[2/3] Setting CV channel 2", request_count=3)
        self.assertEqual(code, 130, output)
        data = self.json_from_tty(output)
        self.assertEqual(data["summary"]["verified_ids"], ["1"])
        self.assertIsNone(data["results"][-1]["accepted"])
        self.assertEqual(data["results"][-1]["operation"], "RT_SET")
        self.assertEqual(data["results"][-1]["item_status"], "incomplete")

    def test_json_dependencies_malformed_responses_and_recovered_fallbacks(self):
        result, data = self.json_cli("fan_reads", "unit.example", "get", "fan", "1", "speed", env={"PATH":str(self.root)})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(data["results"][0]["error"]["kind"], "dependency")
        self.assertEqual(self.requests(), [])
        result, data = self.json_cli("malformed_json", "unit.example", "get", "fan", "1", "state")
        self.assertEqual(result.returncode, 1)
        self.assertIn("response_text", data["results"][0])
        result, data = self.json_cli("fan_reads", "unit.example", "get", "fan", "1", "speed",
                                     env={"MOCK_RESPONSE": '{"code":"RC_OK","fans_module":{"id":1,"state":{"speed":NaN}}}'})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Non-finite JSON token", data["results"][0]["error"]["message"])
        self.assertIsNone(data["results"][0]["response"])
        result, data = self.json_cli("temperature_fallback", "unit.example", "get", "hvdac", "1", "temp")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data["errors"], [])
        self.assertEqual(len(data["results"]), 2)
        self.assertIsNotNone(data["results"][0]["error"])
        self.assertEqual(data["results"][1]["data"], 3150)

    def test_shelf_and_midplane_state_inventory_reads(self):
        state=self.run_cli("fru_data","10.13.197.52","get","shelf","state",
                           env={"MOCK_SECTION_RESULT":'{"cpu_sensor_0":{"temperature":3200}}'})
        self.assertEqual(state.returncode,0,state.stdout+state.stderr)
        self.assertIn("Shelf state:\n  Cpu sensor 0:\n    Temperature: 3200",state.stdout)
        self.assertNotIn('"temperature"',state.stdout)
        self.assertIn("Returned state fields: cpu_sensor_0",state.stdout)
        request=self.requests()[0]
        self.assertEqual(request["payload"][0]["equipment_pack"],{"id":1,"state":{}})

        self.capture.mkdir(exist_ok=True)
        inventory=self.run_cli("fru_data","10.13.197.52","get","midplane","inventory",
                               env={"MOCK_SECTION_RESULT":'{"serial_number":"MP-001"}'})
        self.assertEqual(inventory.returncode,0,inventory.stdout+inventory.stderr)
        self.assertIn("Serial number: MP-001",inventory.stdout)
        self.assertEqual(self.requests()[0]["payload"][0]["equipment_pack"],{"id":2,"inventory":{}})

    def test_hvdac_full_state_fields_and_models(self):
        r = self.run_cli("hvdac_details", "unit.example", "get", "hvdac", "1", "state")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        for text in ("HVDAC card 1 state:", "  FPGA failure: false", "  Voltage 185v failure: false",
                     "  Loss of internal communications: false", "  Loss of DAC communications: false",
                     "  Channel out-of-range degrade: false"):
            self.assertIn(text, r.stdout)
        self.assertEqual(self.requests()[0]["payload"][0]["memsModule"], {"id": 1, "dac_cards": {"id": 1, "state": {}}})
        r, data = self.json_cli("hvdac_details", "unit.example", "get", "hvdac", "all", "state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(data["summary"]["successful_ids"], [str(i) for i in range(1,11)])
        self.assertEqual(len(data["results"]), 10)
        self.assertIs(data["results"][0]["data"]["fpga_failure"], False)
        r = self.run_cli("hvdac_details", "unit.example", "get", "hvdac", "11", "state", "--model", "R320", env={"MOCK_MODEL": "R320"})
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        for args in (("get", "hvdac", "11", "state"), ("get", "hvdac", "1", "state", "extra"),
                     ("set", "hvdac", "1", "state")):
            r = self.run_cli("hvdac_details", "unit.example", *args)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(self.requests(), [])

    def test_hvdac_state_scoping_unknowns_and_invalid_responses(self):
        def response(cards):
            return json.dumps({"responsePayload": [{"code": "RC_OK", "payload": [{"memsModule": [
                {"id": 2, "dacCards": [{"id": 1, "state": {"missing": False, "wrongModule": 999}}]},
                {"id": 1, "dacCards": cards}]}]}]})
        raw = response([{"id": 2, "state": {"missing": False, "wrongCard": 999}},
                        {"id": "1", "state": {"missing": False, "fpgaFailure": True, "futureField": 17}}])
        r, data = self.json_cli("hvdac_details", "unit.example", "get", "hvdac", "1", "state", env={"MOCK_RESPONSE": raw})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(data["results"][0]["data"], {"missing": False, "fpgaFailure": True, "futureField": 17})
        for cards in ([{"id": 2, "state": {"missing": False}}],
                      [{"id": 1, "state": {"missing": False}}] * 2,
                      [{"id": 1, "state": {"missing": "false"}}]):
            r = self.run_cli("hvdac_details", "unit.example", "get", "hvdac", "1", "state", env={"MOCK_RESPONSE": response(cards)})
            self.assertEqual(r.returncode, 1, r.stdout+r.stderr)

    def test_hvdac_state_batches_failures_timeout_and_interrupt(self):
        r, data = self.json_cli("hvdac_details", "unit.example", "get", "hvdac", "1-3", "state", env={"MOCK_HVDAC_ERROR_ID": "2"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(data["summary"]["successful_ids"], ["1", "3"])
        self.assertEqual(data["summary"]["failed_ids"], ["2"])
        r, data = self.json_cli("hvdac_details", "unit.example", "get", "hvdac", "1-3", "state", env={"MOCK_HVDAC_TIMEOUT_ID": "2"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(data["summary"]["not_attempted_ids"], ["3"])
        self.assertEqual(len(self.requests()), 2)
        code, output = self.run_tty("hvdac_details", ("unit.example", "get", "hvdac", "1-3", "state", "--json"),
            interrupt_at="Reading state for HVDAC card ID 2", request_count=2, env={"MOCK_HVDAC_INTERRUPT_ID": "2"})
        self.assertEqual(code, 130, output)
        self.assertEqual(self.json_from_tty(output)["summary"]["successful_ids"], ["1"])
        self.assertEqual(self.json_from_tty(output)["summary"]["not_attempted_ids"], ["3"])

    def test_hvdac_inventory_description_and_combined_channel_dac_count(self):
        r, data = self.json_cli("hvdac_details", "unit.example", "get", "hvdac", "1", "inventory")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(data["results"][0]["data"]["entity_description"], "High Voltage Card 1")
        self.assertNotIn("clei_code", data["results"][0]["data"]["entity_info"])
        self.assertTrue(any("clei_code" in note for note in data["summary"]["notes"]))
        r = self.run_cli("hvdac_details", "unit.example", "get", "hvdac", "1", "channel", "1", "1")
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn("DAC count: 0", r.stdout)
        self.assertIn("Target voltage: 215", r.stdout)
        channel = self.requests()[0]["payload"][0]["memsModule"]["dac_cards"]["dac_chips"][0]["channels"][0]
        self.assertEqual(channel, {"id": 1, "config": {}, "state": {}})
        r, data = self.json_cli("hvdac_details", "unit.example", "get", "hvdac", "1", "channel", "1", "1")
        self.assertEqual(data["results"][0]["data"], {"target_voltage": 215, "actual_voltage": 215, "oor_failure": False, "dac_count": 0})
        from lotd_client import Client, LotdError
        client = Client("unit.example:3217", False)
        raw = {"responsePayload": [{"code": "RC_OK", "payload": [{"memsModule": [
            {"id": 2, "dacCards": [{"id": 1, "dacChips": [{"id": 1, "channels": [{"id": 1, "config": {"targetVoltage": 999}}]}]}]},
            {"id": 1, "dacCards": [{"id": 1, "dacChips": [{"id": 1, "channels": [{"id": 1,
                "config": {"targetVoltage": 215, "dacCount": 0}, "state": {"actualVoltage": 215, "oorFailure": False}}]}]}]}]}]}]}
        values = client.parse(json.dumps(raw), "channel", "1", "1", "1")
        self.assertEqual(values, {"target_voltage": 215, "actual_voltage": 215,
                                  "oor_failure": False, "dac_count": 0})
        raw["responsePayload"][0]["payload"][0]["memsModule"][1]["dacCards"][0]["dacChips"][0]["channels"][0]["config"]["dacCount"] = True
        with self.assertRaisesRegex(LotdError, "integer DAC count"):
            client.parse(json.dumps(raw), "channel", "1", "1", "1")

    def test_ps_missing_measurement_message_is_concise_and_json_notes_remain(self):
        r = self.run_cli("ps_reads", "unit.example", "get", "ps", "2", "temperature")
        self.assertEqual(r.returncode, 1)
        self.assertEqual((r.stdout+r.stderr).count("measurement is unavailable"), 1)
        self.assertNotIn("Raw state placeholders", r.stdout+r.stderr)
        r, data = self.json_cli("ps_reads", "unit.example", "get", "ps", "2", "temperature")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(any("Power supply 2 is missing" in note for note in data["summary"]["notes"]))
        self.assertIn("measurement is unavailable", data["errors"][0]["message"])
        self.assertEqual(data["results"][0]["data"], {"missing": True})
        r = self.run_cli("ps_reads", "unit.example", "get", "ps", "2", "inventory")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.count("Power supply 2 is missing"), 1)

    def test_hvdac_inventory_batch_and_card_presence(self):
        batch=self.run_cli("fru_data","10.13.197.52","get","hvdac","1-2","inventory",
                           env={"MOCK_SECTION_RESULT":'{"entity_info":{"serial_number":"SIM-1"}}'})
        self.assertEqual(batch.returncode,0,batch.stdout+batch.stderr)
        self.assertIn("Successful IDs (2): 1,2",batch.stdout)
        requests=self.requests()
        self.assertEqual(len(requests),2)
        for request,card_id in zip(requests,(1,2)):
            card=request["payload"][0]["memsModule"]["dac_cards"]
            self.assertEqual(card,{"id":card_id,"inventory":{}})

        self.capture.mkdir(exist_ok=True)
        presence=self.run_cli("fru_data","10.13.197.52","get","hvdac","1","presence")
        self.assertEqual(presence.returncode,0,presence.stdout+presence.stderr)
        self.assertIn("Missing: false",presence.stdout)
        card=self.requests()[0]["payload"][0]["memsModule"]["dac_cards"]
        self.assertEqual(card,{"id":1,"state":{"missing":False}})

    def test_mirror_full_state_and_config_views(self):
        for component, section in (("mirror", "state"), ("mems", "config")):
            r, data = self.json_cli("mirror_lds_details", "unit.example", "get", component, "1-2", section)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(data["summary"]["successful_ids"], ["1", "2"])
            self.assertEqual(len(data["results"]), 2)
            value = data["results"][0]["data"]
            self.assertEqual(value["x_tilt_voltage"], 143224)
            self.assertEqual(value["il_delta"] if section == "state" else value["connection"],
                             1 if section == "state" else 629)
            if section == "state":
                self.assertEqual(value["status"], {"value": "MS_USABLE"})
                self.assertEqual(value["x_tilt_actual_voltage"], 143216)
            self.assertIn("Mirror port 1", r.stderr)
            self.assertIn("Mirror port 2", r.stderr)
            self.assertEqual(self.requests()[0]["payload"][0]["mems_module"],
                             {"id": 1, "mirrors": {"id": 1, section: {}}})
        r, data = self.json_cli("mirror_lds_details", "unit.example", "get", "mirror", "1-3", "state",
                                env={"MOCK_MIRROR_ERROR_ID": "2"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(data["summary"]["successful_ids"], ["1", "3"])
        self.assertEqual(data["summary"]["failed_ids"], ["2"])

    def test_mirror_section_scoping_validation_and_unknown_fields(self):
        def reply(modules):
            return json.dumps({"response_payload": [{"code": "RC_OK", "payload": [{"mems_module": modules}]}]})
        for section in ("state", "config"):
            raw = reply([{"id": 2, "mirrors": [{"id": 1, section: {"wrong": 999}}]},
                         {"id": 1, "mirrors": [{"id": 2, section: {"wrong": 888}},
                          {"id": 1, section: {"future_flag": False, "future_value": 0}}]}])
            r, data = self.json_cli("mirror_lds_details", "unit.example", "get", "mirror", "1", section,
                                   env={"MOCK_RESPONSE": raw})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(data["results"][0]["data"], {"future_flag": False, "future_value": 0})
            for modules in ([{"id": 2, "mirrors": [{"id": 1, section: {"value": 1}}]}],
                            [{"id": 1, "mirrors": [{"id": 1, section: {}}]}],
                            [{"id": 1, "mirrors": [{"id": 1, section: {"value": 1}},
                                                   {"id": 1, section: {"value": 1}}]}]):
                r = self.run_cli("mirror_lds_details", "unit.example", "get", "mirror", "1", section,
                                 env={"MOCK_RESPONSE": reply(modules)})
                self.assertEqual(r.returncode, 1, r.stdout+r.stderr)
        for tail in (("1", "inventory"), ("1", "sensors"), ("160", "state", "--model", "R300-G")):
            r = self.run_cli("mirror_lds_details", "unit.example", "get", "mirror", *tail)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(self.requests(), [])

    def test_lds_state_and_mixed_inventory_failure(self):
        r, data = self.json_cli("mirror_lds_details", "unit.example", "get", "lds", "state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(data["results"][0]["data"]["flash_failure"])
        self.assertFalse(data["results"][0]["data"]["laser_failure"])
        self.assertIn("Flash failure: true", r.stderr)
        self.assertEqual(self.requests()[0]["payload"][0]["lds_card"], {"id": 1, "state": {}})
        r, data = self.json_cli("mirror_lds_details", "unit.example", "get", "lds", "inventory")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(data["summary"]["failed_ids"], ["1"])
        self.assertIn("RC_OPERATION_FAILED", data["errors"][0]["message"])
        self.assertEqual(data["results"][0]["response"]["response_payload"][0]["payload"][0]
                         ["lds_card"][0]["inventory"]["max_num_north_lds_ocs_ids"], 3)
        raw = json.dumps({"response_payload": [{"code": "RC_OK", "payload": [{"lds_card": [
            {"id": 1, "inventory": {"entity_description": "LDS Card", "max_num_north_lds_ocs_ids": 3}}]}]}]})
        r = self.run_cli("mirror_lds_details", "unit.example", "get", "lds", "inventory", env={"MOCK_RESPONSE": raw})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LDS card 1 inventory:", r.stdout)
        for section in ("state", "inventory", "presence"):
            r = self.run_cli("mirror_lds_details", "unit.example", "get", "lds", section, "--model", "R300-L")
            self.assertEqual(r.returncode, 2)
            self.assertEqual(self.requests(), [])

    def test_hvdac_state_summary_is_compact(self):
        r = self.run_cli("hvdac_details", "unit.example", "get", "hvdac", "all", "state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Returned state: 10 cards", r.stdout)
        self.assertNotIn("Returned state (ID: value)", r.stdout)
        self.assertEqual(r.stdout.count("Loss of DAC communications: false"), 10)

    def test_cv_and_lds_presence_inventory_reads(self):
        for component,operation,expected in (("cv","presence",{"id":1,"state":{"missing":False}}),
                                              ("cv","inventory",{"id":1,"inventory":{}}),
                                              ("lds","presence",{"id":1,"state":{"missing":False}})):
            with self.subTest(component=component,operation=operation):
                r=self.run_cli("fru_data","10.13.197.52","get",component,operation)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
                self.assertEqual(self.requests()[0]["payload"][0].get("memsModule",{}).get("common_voltage_card")
                                 or self.requests()[0]["payload"][0].get("lds_card"),expected)

    def test_cv_get_single_and_complete_batch_tables(self):
        single=self.run_cli("cv_channel_get","10.13.197.52","get","cv","channel","1")
        self.assertEqual(single.returncode,0); self.assertIn("Target voltage: 7.5",single.stdout)
        self.assertIn("Actual voltage: 6.5",single.stdout)
        self.assertIn("Returned values (channel: target, actual): 1: 7.5, 6.5",single.stdout)
        self.capture.mkdir(exist_ok=True)
        batch=self.run_cli("cv_channel_get_range","10.13.197.52","get","cv","channel","1-3")
        self.assertEqual(batch.returncode,0); self.assertIn("[3/3]",batch.stdout)
        self.assertIn("Successful IDs (3): 1,2,3",batch.stdout)
        self.assertEqual(len(self.requests()),3)
        self.capture.mkdir(exist_ok=True)
        big=self.run_cli("cv_channel_get_range","10.13.197.52","get","cv","channel","1-10")
        self.assertEqual(big.returncode,0); self.assertNotIn("... and",big.stdout); self.assertRegex(big.stdout,r"10\s*\|\s*7.5\s*\|\s*6.5\s*\|\s*Read OK")
        self.assertIn("Successful IDs (10): 1,2,3,4,5,6,7,8,9,10",big.stdout)

    def test_cv_get_batch_error_timeout_and_interrupt(self):
        r=self.run_cli("cv_per_channel_error","10.13.197.52","get","cv","channel","1-3")
        out=r.stdout+r.stderr; self.assertEqual(r.returncode,1)
        self.assertIn("Failed IDs (1): 2",out); self.assertIn("Successful IDs (2): 1,3",out)
        self.capture.mkdir(exist_ok=True)
        d=self.run_cli("dial_failure","10.13.197.52","get","cv","channel","1-3")
        self.assertEqual(d.returncode,1); self.assertIn("Not attempted (2): 2,3",d.stderr)
        self.capture.mkdir(exist_ok=True)
        code,out=self.run_tty("interrupt_get",("10.13.197.52","get","cv","channel","1-3"),
                              interrupt_at="[1/3] Reading CV channel 1",request_count=1)
        plain=strip_ansi(out)
        self.assertEqual(code,130,out); self.assertIn("Interrupted (Ctrl+C)",plain)
        self.assertIn("Not attempted (2): 2,3",plain)

    def test_cv_set_single_range_mismatch_cancel_interrupt(self):
        single=self.run_cli("cv_channel_set_success","10.13.197.52","set","cv","channel","1","8.25")
        self.assertEqual(single.returncode,0,single.stderr); self.assertIn("Verified: target voltage matches 8.25",single.stdout)
        self.assertEqual(self.requests()[0]["payload"][0]["memsModule"]["common_voltage_card"]["channels"][0]["config"]["target_voltage"],8.25)
        self.capture.mkdir(exist_ok=True)
        mismatch=self.run_cli("cv_channel_set_mismatch","10.13.197.52","set","cv","channel","1","8.25")
        self.assertEqual(mismatch.returncode,1); self.assertIn("requested 8.25, read back 9.25",mismatch.stderr)
        self.capture.mkdir(exist_ok=True)
        batch_args=("10.13.197.52","set","cv","channel","1-3","8.25")
        code,out=self.run_tty("cv_channel_set_success",batch_args,"y")
        self.assertEqual(code,0,out); self.assertIn("Successful IDs (3): 1,2,3",out)
        self.assertEqual(len(self.requests()),6)
        self.capture.mkdir(exist_ok=True)
        code,out=self.run_tty("cv_channel_set_success",batch_args,"n")
        self.assertEqual(code,0,out); self.assertIn("Cancelled; no changes sent.",out)
        self.assertEqual(len(self.requests()),0)
        self.capture.mkdir(exist_ok=True)
        code,out=self.run_tty("interrupt_set",batch_args,"y",interrupt_at="[2/3] Setting CV channel 2",request_count=3)
        self.assertEqual(code,130,out); self.assertIn("Successful IDs (1): 1",out)
        self.assertIn("whether LOTD applied the target is unknown",out); self.assertIn("Not attempted (1): 3",out)

    def test_cv_model_limits_and_validation(self):
        r=self.run_cli("cv_channel_get","10.13.197.52","get","cv","channel","89","--model","R320")
        self.assertEqual(r.returncode,0,r.stderr)
        self.capture.mkdir(exist_ok=True)
        bad=self.run_cli("success","10.13.197.52","get","cv","channel","18")
        self.assertEqual(bad.returncode,2); self.assertIn("outside the valid range 1-17",bad.stderr)
        self.assertEqual(len(self.requests()),0)

    def test_mirror_get_set_and_batches(self):
        r=self.run_cli("mirror_get","10.13.197.52","get","mirror","321","x-tilt")
        self.assertEqual(r.returncode,0,r.stderr); self.assertIn("X target voltage: 77.25",r.stdout)
        req=self.requests()[0]["payload"][0]["mems_module"]["mirrors"]
        self.assertEqual(req["id"],321); self.assertEqual(req["config"],{"x_tilt_voltage":0})
        self.capture.mkdir(exist_ok=True)
        s=self.run_cli("mirror_set_success","10.13.197.52","set","mirror","1","y-tilt","1000")
        self.assertEqual(s.returncode,0,s.stderr); self.assertIn("Verified: Y target voltage matches 1000",s.stdout)
        self.assertEqual(self.requests()[0]["payload"][0]["mems_module"]["mirrors"]["config"],{"y_tilt_voltage":1000})
        self.capture.mkdir(exist_ok=True)
        mismatch=self.run_cli("mirror_set_mismatch","10.13.197.52","set","mirror","1","x","1000")
        self.assertEqual(mismatch.returncode,1); self.assertIn("Mirror target verification failed",mismatch.stderr)
        self.capture.mkdir(exist_ok=True)
        batch=self.run_cli("mirror_get","10.13.197.52","get","mirror","1-2","y-tilt","--model","R320")
        self.assertEqual(batch.returncode,0); self.assertIn("Successful IDs (2): 1,2",batch.stdout)
        self.capture.mkdir(exist_ok=True)
        code,out=self.run_tty("mirror_set_success",("10.13.197.52","set","mirror","1-2","x-tilt","5"),"y")
        self.assertEqual(code,0,out); self.assertIn("Successful IDs (2): 1,2",out)

    def test_mems_alias_matches_mirror_get_set_and_help(self):
        mirror = self.run_cli("mirror_get", "10.13.197.52", "get", "mirror", "1", "x-tilt", "--model", "R300")
        self.assertEqual(mirror.returncode, 0, mirror.stderr)
        mirror_request = self.requests()[0]

        mems = self.run_cli("mirror_get", "10.13.197.52", "get", "mems", "1", "x-tilt", "--model", "R300")
        self.assertEqual(mems.returncode, 0, mems.stderr)
        self.assertEqual(self.requests()[0]["payload"], mirror_request["payload"])
        self.assertIn("Operation:      get mirror X target voltage", mems.stdout)

        mirror_set = self.run_cli("mirror_set_success", "10.13.197.52", "set", "mirror", "1", "x-tilt", "1000")
        self.assertEqual(mirror_set.returncode, 0, mirror_set.stderr)
        mirror_set_request = self.requests()[0]
        mems_set = self.run_cli("mirror_set_success", "10.13.197.52", "set", "mems", "1", "x-tilt", "1000")
        self.assertEqual(mems_set.returncode, 0, mems_set.stderr)
        self.assertEqual(self.requests()[0]["payload"], mirror_set_request["payload"])

        help_result = subprocess.run([sys.executable, str(CLI), "help", "mems"], text=True,
                                     capture_output=True, timeout=5)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("also accepts the keyword mems", help_result.stdout)

    def test_model_aliases_preserve_layout_guards_and_canonical_json(self):
        for alias, canonical, port in (("R300G", "R300-G", "310"), ("r300g", "R300-G", "310"),
                                       ("R300L", "R300-L", "150"), ("r300l", "R300-L", "150")):
            r, data = self.json_cli("mirror_get", "unit.example", "get", "mirror", port, "x-tilt", "--model", alias)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(data["model"], canonical)
            self.assertEqual(data["command"][-1], alias)
            aliased_request = self.requests()[0]
            r = self.run_cli("mirror_get", "unit.example", "get", "mirror", port, "x-tilt", "--model", canonical)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.requests()[0], aliased_request)
        for alias, port in (("R300G", "150"), ("R300L", "310")):
            r = self.run_cli("success", "unit.example", "get", "mirror", port, "x-tilt", "--model", alias)
            self.assertEqual(r.returncode, 2, r.stdout+r.stderr)
            self.assertEqual(self.requests(), [])
        r = self.run_cli("success", "unit.example", "get", "lds", "state", "--model", "R300L")
        self.assertEqual(r.returncode, 2)
        self.assertIn("R300-L has no LDS", r.stderr)
        self.assertEqual(self.requests(), [])
        r = self.run_cli("success", "--help")
        self.assertIn("R300G and R300L are aliases", r.stdout)

    def test_live_runner_model_aliases_normalize_before_plan_and_skips(self):
        import io
        from contextlib import redirect_stderr
        from live_test_lotd import checks_for, parse_args
        for alias, canonical in (("R300G", "R300-G"), ("r300g", "R300-G"),
                                  ("R300L", "R300-L"), ("r300l", "R300-L")):
            args = parse_args(["unit.example", "--model", alias])
            self.assertEqual(args.model, canonical)
            checks = checks_for(args.model, False, include_lds_inventory=True)
            self.assertEqual(checks, checks_for(canonical, False, include_lds_inventory=True))
            self.assertTrue(all(check.arguments[-1] == canonical for check in checks))
            self.assertEqual(any("lds" in check.arguments for check in checks), canonical != "R300-L")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            parse_args(["unit.example", "--model", "R300X"])
        self.assertEqual(error.exception.code, 2)

    def test_mirror_model_ranges_and_all(self):
        invalid=[("149","R300-G","not valid for R300G"),("469","R300-G","not valid for R300G"),
                 ("301","R300-L","not valid for R300L"),("310",None,"not valid on all R300 variants"),
                 ("641","R320","outside the internal LOTD port ID range")]
        for port,model,message in invalid:
            with self.subTest(port=port,model=model):
                args=["10.13.197.52","get","mirror",port,"x-tilt"]
                if model: args += ["--model",model]
                r=self.run_cli("success",*args)
                self.assertEqual(r.returncode,2); self.assertIn(message,r.stderr)
                self.assertEqual(len(self.requests()),0)
        self.capture.mkdir(exist_ok=True)
        all_generic=self.run_cli("success","10.13.197.52","get","mirror","all","x-tilt")
        self.assertEqual(all_generic.returncode,2); self.assertIn("requires --model",all_generic.stderr)
        self.capture.mkdir(exist_ok=True)
        all_model=self.run_cli("dial_failure","10.13.197.52","get","mirror","all","x-tilt","--model","R300-G")
        self.assertEqual(all_model.returncode,1); self.assertIn("Not attempted (591):",all_model.stderr)

    def test_preflight_error_terminal_color_and_no_color(self):
        cmd=("10.13.197.52","get","mirror","160","x-tilt","--model","R300-G")
        code,colored=self.run_tty("success",cmd)
        self.assertEqual(code,2); self.assertIn("\x1b[1m\x1b[31mError:",colored)
        self.assertIn("\x1b[36mRun\x1b[0m",colored)
        code,plain=self.run_tty("success",cmd,env={"NO_COLOR":"1"})
        self.assertEqual(code,2); self.assertNotIn("\x1b[",plain)
        self.assertIn("Error: mirror port ID 160 is not valid for R300G",plain)

    def test_connection_get_values_and_payload(self):
        r=self.run_cli("connection_get","10.13.197.52","get","connection","1")
        self.assertEqual(r.returncode,0,r.stderr)
        for text in ("Peer port number: 10001","Optical blocking enable: false",
                     "Peer mappings (port->peer): 1->10001","Optical blocking (port=value): 1=false"):
            self.assertIn(text,r.stdout)
        port=self.requests()[0]["payload"][0]["connection_manager"][0]["port_array"][0]
        self.assertEqual(port,{"id":1,"config":{}})
        self.capture.mkdir(exist_ok=True)
        south=self.run_cli("connection_get_south","10.13.197.52","get","connection","321","--model","R300-G")
        self.assertEqual(south.returncode,0); self.assertIn("Peer port number: 1",south.stdout)
        self.capture.mkdir(exist_ok=True)
        batch=self.run_cli("connection_get_range","10.13.197.52","get","connection","1-3")
        self.assertEqual(batch.returncode,0); self.assertIn("1->0, 2->10001, 3->0",batch.stdout)

    def test_connection_peer_blocking_and_disconnect_set(self):
        r=self.run_cli("connection_set_peer_success","10.13.197.52","set","connection","1","peer","10001")
        self.assertEqual(r.returncode,0,r.stderr); self.assertIn("Verified: peer port number matches 10001",r.stdout)
        port=self.requests()[0]["payload"][0]["connection_manager"][0]["port_array"][0]
        self.assertEqual(port["config"],{"peer_port_number":10001})
        self.capture.mkdir(exist_ok=True)
        disc=self.run_cli("connection_set_peer_success","10.13.197.52","set","connection","1","peer","disconnect")
        self.assertEqual(disc.returncode,0); self.assertIn("Verified: peer port number matches 0",disc.stdout)
        self.assertEqual(self.requests()[0]["payload"][0]["connection_manager"][0]["port_array"][0]["config"],{"peer_port_number":-1})
        self.capture.mkdir(exist_ok=True)
        block=self.run_cli("connection_set_blocking_success","10.13.197.52","set","connection","1","blocking","on")
        self.assertEqual(block.returncode,0); self.assertIn("Verified: optical blocking enable matches true",block.stdout)
        self.assertEqual(self.requests()[0]["payload"][0]["connection_manager"][0]["port_array"][0]["config"],{"optical_blocking_enable":True})
        self.capture.mkdir(exist_ok=True)
        code,out=self.run_tty("connection_set_blocking_success",("10.13.197.52","set","connection","1-2","blocking","off"),"y")
        self.assertEqual(code,0,out); self.assertIn("Successful IDs (2): 1,2",out)

    def test_connection_direction_target_and_internal_id_validation(self):
        cases=[(("321","peer","1","--model","R300-G"),"north/A-side"),
               (("1","peer","1"),"valid south/B-side"),
               (("10001","peer","1"),"10001-style numbers belong in peer targets")]
        for suffix,message in cases:
            self.capture.mkdir(exist_ok=True)
            r=self.run_cli("success","10.13.197.52","set","connection",*suffix)
            self.assertEqual(r.returncode,2); self.assertIn(message,r.stderr)
            self.assertEqual(len(self.requests()),0)

    def test_connection_batch_stops_on_endpoint_timeout(self):
        r=self.run_cli("dial_failure","10.13.197.52","get","connection","1-3")
        self.assertEqual(r.returncode,1); self.assertIn("Not attempted (2): 2,3",r.stderr)
        self.assertEqual(len(self.requests()),1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
