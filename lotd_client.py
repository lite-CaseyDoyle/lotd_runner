"""LOTD transport, response parsing, and output helpers."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

PORT = 3217
CID = "123"
HOST = "lotd.Operations.RequestOp"
PAYLOAD_PREFIX = "type.googleapis.com/lotd."
REQUEST_TIMEOUT_SECONDS = 20


def canonical_model(value: str) -> str:
    """Normalize model spelling; callers retain their supported-model validation."""
    value = value.upper()
    return {"R300G": "R300-G", "R300L": "R300-L"}.get(value, value)


def decode_response(raw):
    def invalid_constant(value):
        raise ValueError(f"Non-finite JSON token: {value}")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"JSON number exceeds finite float range: {value}")
        return number
    return json.loads(raw, parse_constant=invalid_constant, parse_float=finite_float)


class LotdError(Exception):
    pass


class Interrupted(Exception):
    pass


class SummaryField(str):
    """A display line carrying typed summary data for machine output."""
    def __new__(cls, text, key, value):
        line = super().__new__(cls, text)
        line.key, line.value = key, value
        return line


class JsonReport:
    def __init__(self, command):
        self.command = command
        self.endpoint = None
        self.model = None
        self.cancelled = False
        self.results = []
        self.errors = []
        self.summary = {"successful_ids": [], "failed_ids": [],
                        "not_attempted_ids": [], "verified_ids": []}
        self._outcome = None
        self._details = []

    def error(self, message, kind="usage"):
        self.errors.append({"kind": kind, "message": str(message)})

    def record_summary(self, outcome, operation, details, notes):
        self._outcome = outcome
        if notes:
            self.summary["notes"] = list(notes)
        else:
            self.summary.pop("notes", None)
        for detail in details:
            if isinstance(detail, SummaryField):
                self.summary[detail.key] = list(map(str, detail.value))
            else:
                self._details.append(str(detail))

    def finish(self, exit_code):
        ids = self.summary["successful_ids"] + self.summary["failed_ids"]
        for result in self.results:
            if result["id"] is None and len(ids) == 1:
                result["id"] = ids[0]
            item_id = result["id"]
            result["item_status"] = "successful" if item_id in self.summary["successful_ids"] else "failed" if item_id in self.summary["failed_ids"] else "incomplete"
            result["verified"] = result["operation"] == "RT_GET" and result.get("error") is None and item_id in self.summary["verified_ids"]
            if exit_code and result.get("error") and result["item_status"] != "successful":
                self.errors.append({"id": item_id, **result["error"]})
        if exit_code and not self.errors:
            outcome = self._outcome or ("Interrupted (Ctrl+C)" if exit_code == 130 else "Invalid command" if exit_code == 2 else "Failed")
            self.errors.append({"kind": "command", "message": outcome, "details": self._details})
        return {"schema_version": 1, "command": self.command, "endpoint": self.endpoint,
                "model": self.model, "exit_code": exit_code, "cancelled": self.cancelled,
                "summary": self.summary, "results": self.results, "errors": self.errors}


class Client:
    def __init__(self, endpoint: str, color: bool, report=None):
        self.endpoint = endpoint
        self.color = color
        self.hint_shown = False
        self.current_id: str | None = None
        self.current_phase = ""
        self.interrupted = False
        self.response_notes: list[str] = []
        self.report = report
        self._exchange = None
        if report is not None:
            report.endpoint = endpoint

    def c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def hint(self, error: str) -> None:
        if "context deadline exceeded" in error and not self.hint_shown:
            print(self.c("1;33", f"\n[HINT] LOTD timed out at {self.endpoint}. Check that the manufacturing container is running and port {PORT} is reachable."), file=sys.stderr)
            self.hint_shown = True

    def request(self, kind: str, payload_type: str, body: dict[str, Any]) -> str:
        request = {"lotd_version": "LV_1", "type": kind, "cid": CID,
                   "payload": [{"@type": PAYLOAD_PREFIX + payload_type, **body}]}
        if self.report is not None:
            self._exchange = {"id": self.current_id, "phase": self.current_phase or kind,
                              "operation": kind, "request": request, "response": None,
                              "data": None, "accepted": None, "error": None}
            self.report.results.append(self._exchange)
        if not shutil.which("grpcurl"):
            if self._exchange is not None:
                self._exchange["error"] = {"kind": "dependency", "message": "grpcurl is not installed or not in PATH"}
            raise LotdError("grpcurl is not installed or not in PATH")
        try:
            proc = subprocess.run(["grpcurl", "-plaintext", "-d", "@", self.endpoint, HOST],
                                  input=json.dumps(request), text=True, capture_output=True,
                                  timeout=REQUEST_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            error = f"LOTD request exceeded {REQUEST_TIMEOUT_SECONDS} seconds at {self.endpoint}: context deadline exceeded"
            if kind in ("RT_SET", "RT_ACTION"):
                error += "; completion is unknown. Inspect the unit before retrying."
            if self._exchange is not None:
                self._exchange["error"] = {"kind": "transport", "message": error}
            self.hint(error)
            raise LotdError(error) from exc
        except KeyboardInterrupt as exc:
            if self._exchange is not None:
                self._exchange["error"] = {"kind": "interrupted", "message": "Interrupted during request; completion is unknown"}
            raise Interrupted from exc
        if proc.returncode:
            error = (proc.stderr or proc.stdout).strip()
            if self._exchange is not None:
                self._exchange["error"] = {"kind": "transport", "message": error}
            self.hint(error)
            raise LotdError(error)
        return proc.stdout

    def parse(self, raw: str, mode: str, *args: str) -> Any:
        if self._exchange is not None:
            self._exchange["parser"] = mode
            try:
                self._exchange["response"] = decode_response(raw)
            except ValueError:
                self._exchange["response_text"] = raw
        try:
            result = self._parse(raw, mode, *args)
        except LotdError as exc:
            if self._exchange is not None:
                self._exchange["error"] = {"kind": "response", "message": str(exc)}
                if self._exchange["operation"] == "RT_SET" and any(
                    node.get("code") not in (None, "RC_OK", "RC_MISSING_VALUE")
                    for node in walk(self._exchange.get("response"))):
                    self._exchange["accepted"] = False
            raise
        if self._exchange is not None:
            self._exchange["data"] = result
            if self._exchange["operation"] == "RT_SET":
                self._exchange["accepted"] = None if mode in ("status", "write_status") and result is None else True
                self._exchange["data"] = None
        return result

    def capture_result(self, value):
        if self._exchange is not None:
            self._exchange["data"] = value

    def record_failure(self, error):
        if self._exchange is not None and self._exchange["error"] is None:
            self._exchange["error"] = {"kind": "result", "message": str(error)}

    def _parse(self, raw: str, mode: str, *args: str) -> Any:
        try:
            data = decode_response(raw)
        except ValueError as exc:
            raise LotdError(f"LOTD returned invalid JSON: {exc}") from exc
        if not isinstance(data, (dict, list)):
            raise LotdError("LOTD response must be a JSON object or array")
        rp = (field_value(data, "response_payload") or []) if isinstance(data, dict) else []
        if isinstance(rp, dict):
            rp = [rp]
        if not isinstance(rp, list):
            raise LotdError("LOTD response_payload must be an object or array")
        codes = [str(x["code"]) for x in rp if isinstance(x, dict) and "code" in x]
        if not codes and isinstance(data, dict) and "code" in data:
            codes = [str(data["code"])]
        if mode == "container_action":
            action = args[0]
            if not isinstance(data, dict) or data.get("cid") != CID:
                raise LotdError("Container action response requires a matching CID")
            if action == "dump_container_to_file" and set(data) == {"cid"}:
                return {"submitted": True, "file_creation_verified": False}
            if not rp or any(not isinstance(entry, dict) or entry.get("code") != "RC_OK" for entry in rp):
                raise LotdError("Container action requires RC_OK for every response fragment: " + ", ".join(codes))
            entity = successful_entity(data, "equipment_pack", "1")
            actions = field_value(entity, "actions")
            value = field_value(actions, action) if isinstance(actions, dict) else None
            if not isinstance(value, dict):
                raise LotdError(f"Response did not include the requested {action} action for equipment_pack ID 1")
            if action == "dump_container_to_file":
                return {"submitted": True, "file_creation_verified": False}
            response = field_value(value, "response")
            success = field_value(response, "success") if isinstance(response, dict) else None
            if not isinstance(success, bool):
                raise LotdError("Container import response requires a boolean response.success")
            return {"success": success, "validation_requested": True}
        if mode == "ps_entity":
            entity_id = args[0]
            allowed = ("RC_OK", "RC_MISSING_VALUE", "RC_FRU_MISSING")
            if not rp or any(not isinstance(entry, dict) or entry.get("code") not in allowed for entry in rp):
                raise LotdError("Invalid power-supply response or non-OK code: " + ", ".join(codes))
            entity = successful_entity(data, "ps_module", entity_id)
            state = field_value(entity, "state")
            if not isinstance(state, dict) or not isinstance(state.get("missing"), bool):
                raise LotdError("Power-supply response requires a boolean state.missing field")
            for entry in rp:
                code = entry["code"]
                matches = matching_entities(entry, "ps_module", entity_id)
                if code == "RC_FRU_MISSING":
                    scoped = [item for node in walk(entry) for item in collection(field_value(node, "ps_module"))
                              if isinstance(item, dict) and "id" in item]
                    if not scoped:
                        raise LotdError("Unscoped RC_FRU_MISSING power-supply response")
                    if matches and not state["missing"]:
                        raise LotdError(f"Contradictory RC_FRU_MISSING for present power supply {entity_id}")
                elif code == "RC_MISSING_VALUE" and matches:
                    note = f"LOTD RC_MISSING_VALUE (ps_module ID {entity_id}): " + ", ".join(missing_paths(matches))
                    if note not in self.response_notes:
                        self.response_notes.append(note)
            if state["missing"]:
                note = f"Power supply {entity_id} is missing; telemetry is unavailable. Raw state placeholders are not measurements."
                if note not in self.response_notes:
                    self.response_notes.append(note)
            return entity
        bad = [x for x in codes if x not in ("RC_OK", "RC_MISSING_VALUE")]
        if bad:
            raise LotdError("LOTD returned a non-OK response code: " + ", ".join(bad))
        if mode in ("status", "write_status"):
            if isinstance(data, dict) and "cid" in data and data["cid"] != CID:
                raise LotdError("Write response CID does not match request")
            if isinstance(data, dict) and set(data) == {"cid"} and data["cid"] == CID:
                return None  # Matching CID-only response; verify the value with GET.
            entries = rp or ([data] if isinstance(data, dict) and "code" in data else [])
            if not entries or any(not isinstance(entry, dict) or entry.get("code") != "RC_OK" for entry in entries):
                raise LotdError("Expected a matching CID-only write response or RC_OK for every response fragment")
            return True
        if mode in ("fru_section", "identity_sensor", "read_section", "read_entity"):
            missing = [entry for entry in rp if isinstance(entry, dict) and entry.get("code") == "RC_MISSING_VALUE"]
            for entry in missing:
                matches = matching_entities(entry, args[0], args[1])
                if matches:
                    note = f"LOTD RC_MISSING_VALUE ({args[0]} ID {args[1]}): " + ", ".join(missing_paths(matches))
                    if note not in self.response_notes:
                        self.response_notes.append(note)
        if mode == "read_entity":
            entity = successful_entity(data, args[0], args[1])
            if not any(key != "id" for key in entity):
                raise LotdError("Response did not include entity details")
            return entity
        if mode == "read_section":
            entity_key, entity_id, section = args
            entity = successful_entity(data, entity_key, entity_id)
            value = field_value(entity, section)
            if not isinstance(value, (dict, list)) or not value:
                raise LotdError(f"Response did not include a nonempty {section} section")
            return value
        if mode == "fru_section":
            entity_key, entity_id, section, component = args
            value = field_value(successful_entity(data, entity_key, entity_id), section)
            if not isinstance(value, (dict, list)):
                raise LotdError(f"Response did not include a valid {component} {section} section")
            if section == "state" and component in ("cv", "hvdac", "lds"):
                if not isinstance(value, dict) or not isinstance(value.get("missing"), bool):
                    raise LotdError("Response did not include a boolean missing field")
            return value
        if mode == "identity_sensor":
            entity_key, entity_id, section, selection = args
            value = field_value(successful_entity(data, entity_key, entity_id), section)
            if selection:
                matches = [item for item in collection(value)
                           if isinstance(item, dict) and str(item.get("id")) == selection]
                if len(matches) != 1:
                    raise LotdError(f"Expected one {section} entry with ID {selection}; found {len(matches)}")
                value = matches[0]
            if section == "sensor":
                state = value.get("state") if isinstance(value, dict) else None
                value = state.get("value") if isinstance(state, dict) else None
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise LotdError("Response did not include a numeric temperature value")
            elif not isinstance(value, (dict, list)) or not value:
                raise LotdError(f"Response did not include a nonempty {section} section")
            elif section == "load_version":
                state = value.get("state") if isinstance(value, dict) else None
                version = state.get("version") if isinstance(state, dict) else None
                if not isinstance(version, str) or not version.strip():
                    raise LotdError("Response did not include a nonempty firmware state.version string")
                value = version
            return value
        if mode.startswith("led"):
            component, entity_id = args[1:3]
            entity_key = {"shelf": "equipment_pack", "midplane": "equipment_pack", "cv": "common_voltage_card",
                          "hvdac": "dac_cards", "fan": "fans_module", "lds": "lds_card"}[component]
            entity = successful_entity(data, entity_key, entity_id)
            leds = collection(field_value(entity, "led"))
            wanted_ids = ("1", "2") if mode == "led_all" else (str(args[0]),)
            values = {}
            for wanted in wanted_ids:
                matches = [led for led in leds if isinstance(led, dict) and str(led.get("id")) == wanted]
                if len(matches) != 1:
                    raise LotdError(f"Expected one LED value for ID {wanted}; found {len(matches)}")
                config = field_value(matches[0], "config")
                control = field_value(config, "control") if isinstance(config, dict) else None
                value = field_value(control, "value") if isinstance(control, dict) else None
                if not isinstance(value, str) or not value:
                    raise LotdError(f"Response did not include a valid LED control for ID {wanted}")
                values[wanted] = value
            if mode == "led_all":
                return values
            return values[str(args[0])]
        if mode.startswith("channel") or mode.startswith("dac_count"):
            card_id, chip_id, channel_id = map(str, args[:3])
            card = successful_entity(data, "dac_cards", card_id)
            matches = []
            for chip in collection(field_value(card, "dac_chips")):
                if isinstance(chip, dict) and str(chip.get("id")) == chip_id:
                    matches.extend(x for x in collection(chip.get("channels")) if isinstance(x, dict) and str(x.get("id")) == channel_id)
            if len(matches) != 1: raise LotdError(f"Expected one matching channel; found {len(matches)}")
            ch = matches[0]; cfg = ch.get("config", {}); state = ch.get("state", {})
            if not isinstance(cfg, dict): raise LotdError("Response contained an invalid channel config object")
            is_dac_count = mode.startswith("dac_count")
            field = "dac_count" if is_dac_count else "target_voltage"
            label = "DAC count" if is_dac_count else "Target voltage"
            vals = [(label, field_value(cfg, field))]
            values = {field: field_value(cfg, field)}
            if mode == "channel":
                if not isinstance(state, dict): raise LotdError("Response contained an invalid channel state object")
                vals += [("Actual voltage", field_value(state, "actual_voltage")), ("Out-of-range failure", field_value(state, "oor_failure"))]
                values.update(actual_voltage=field_value(state, "actual_voltage"), oor_failure=field_value(state, "oor_failure"))
                if "dac_count" in cfg or "dacCount" in cfg:
                    count = field_value(cfg, "dac_count")
                    if isinstance(count, bool) or not isinstance(count, int):
                        raise LotdError("Response did not include an integer DAC count")
                    vals.append(("DAC count", count))
                    values["dac_count"] = count
            for label, val in vals:
                if val is None: raise LotdError("Response did not include " + label.lower())
                if label == "Out-of-range failure":
                    if not isinstance(val, bool): raise LotdError("Response did not include boolean out-of-range failure")
                elif label != "DAC count":
                    require_number(val, label)
            if mode == "channel_set":
                verify_decimal(field_value(cfg, "target_voltage"), args[3], "Target voltage")
            if mode == "dac_count":
                value = field_value(cfg, "dac_count")
                if isinstance(value, bool) or not isinstance(value, int):
                    raise LotdError("Response did not include an integer DAC count")
            if mode == "dac_count_set":
                value = field_value(cfg, "dac_count")
                if isinstance(value, bool) or not isinstance(value, int):
                    raise LotdError("Response did not include an integer DAC count")
                expected = int(args[3])
                if value != expected:
                    raise LotdError(f"DAC count verification failed: requested {expected}, read back {value}")
            return values
        if mode.startswith("cv_channel"):
            channel_id = str(args[0])
            card = successful_entity(data, "common_voltage_card", "1")
            matches = [item for item in collection(field_value(card, "channels"))
                       if isinstance(item, dict) and str(item.get("id")) == channel_id]
            if len(matches) != 1: raise LotdError(f"Expected one matching CV channel; found {len(matches)}")
            ch = matches[0]; cfg = ch.get("config", {}); state = ch.get("state", {})
            if not isinstance(cfg, dict): raise LotdError("Response contained an invalid CV channel config object")
            target = field_value(cfg, "target_voltage")
            if target is None: raise LotdError("Response did not include target voltage")
            require_number(target, "Target voltage")
            values = {"target_voltage": target}
            if mode == "cv_channel":
                if not isinstance(state, dict): raise LotdError("Response contained an invalid CV channel state object")
                actual = field_value(state, "actual_voltage")
                require_number(actual, "actual voltage")
                values["actual_voltage"] = actual
            else:
                verify_decimal(target, args[1], "CV target voltage")
            return values
        if mode.startswith("mirror"):
            port, axis = str(args[0]), str(args[1]); field = axis + "_tilt_voltage"
            mirror = successful_entity(data, "mirrors", port)
            config = field_value(mirror, "config")
            value = field_value(config, field) if isinstance(config, dict) else None
            require_number(value, field)
            if mode == "mirror_set":
                verify_decimal(value, args[2], "Mirror target")
            return {field: value}
        if mode.startswith("connection"):
            port = str(args[0])
            manager = successful_entity(data, "connection_manager", "1")
            matches = [item for item in collection(field_value(manager, "port_array"))
                       if isinstance(item, dict) and str(item.get("id")) == port]
            if len(matches) != 1: raise LotdError("Expected one matching connection port; found " + str(len(matches)))
            cfg = matches[0].get("config", {})
            if not isinstance(cfg, dict): raise LotdError("Response contained an invalid connection config object")
            peer, block = field_value(cfg, "peer_port_number"), field_value(cfg, "optical_blocking_enable")
            if mode in ("connection", "connection_peer_set") and (type(peer) is not int or peer < 0):
                raise LotdError("Response did not include a nonnegative integer peer_port_number")
            if mode in ("connection", "connection_block_set") and not isinstance(block, bool):
                raise LotdError("Response did not include boolean optical_blocking_enable")
            values = {key: value for key, value in (("peer_port_number", peer), ("optical_blocking_enable", block)) if value is not None}
            if mode == "connection":
                if peer is None or block is None: raise LotdError("Response did not include peer_port_number and optical_blocking_enable")
                return values
            expected = args[1]
            if mode == "connection_peer_set":
                if peer is None: raise LotdError("Response did not include peer_port_number")
                actual=peer; wanted=int(expected)
                if actual != wanted: raise LotdError(f"Peer port verification failed: requested {wanted}, read back {actual}")
                return values
            if block is None: raise LotdError("Response did not include optical_blocking_enable")
            wanted = expected == "true"
            if block is not wanted: raise LotdError(f"Optical blocking verification failed: requested {str(wanted).lower()}, read back {block}")
            return values
        if mode == "temperature_sensor":
            card = successful_entity(data, "dac_cards", args[0])
            vals=[s["state"]["value"] for s in collection(field_value(card, "sensor")) if isinstance(s,dict) and str(s.get("id"))=="temp" and isinstance(s.get("state"),dict) and "value" in s["state"]]
            if len(vals)!=1: raise LotdError(f"Expected one HVDAC temperature value; found {len(vals)}")
            require_number(vals[0], "HVDAC temperature")
            return vals[0]
        if mode == "temperature_legacy":
            card = successful_entity(data, "dac_cards", args[0])
            state = field_value(card, "state")
            value = field_value(state, "temperature") if isinstance(state, dict) else None
            require_number(value, "HVDAC temperature")
            return value
        raise LotdError("Unknown response parser mode: " + mode)

    def summary(self, outcome: str, operation: str, details: list[str], status: int = 0, *, hidden_notes=()) -> None:
        if self.report is not None:
            self.report.record_summary(outcome, operation, details, self.response_notes)
            return
        fd = sys.stdout if status == 0 else sys.stderr
        title = "LOTD SUMMARY"; width = 42
        border = "+" + "-" * width + "+"
        print(file=fd)
        print(self.c("36", border), file=fd)
        print(self.c("36", "|" + title.center(width) + "|"), file=fd)
        print(self.c("36", border), file=fd)
        color = "32" if outcome == "Completed successfully" else ("33" if outcome.startswith(("Interrupted", "Submitted")) else "31")
        print(f"  {self.c('1','Outcome:'.ljust(15))} {self.c(color,outcome)}", file=fd)
        print(f"  {self.c('1','Operation:'.ljust(15))} {operation}", file=fd)
        for detail in details + [note for note in self.response_notes if note not in hidden_notes]:
            print("  " + detail.replace("\n", "\n  "), file=fd)


def id_summary(successful, failed=(), pending=(), verified=False) -> list[str]:
    details = [SummaryField(f"Successful IDs ({len(successful)}): {','.join(map(str, successful))}" if successful else "Successful IDs: none", "successful_ids", successful),
               SummaryField(f"Failed IDs ({len(failed)}): {','.join(map(str, failed))}" if failed else "Failed IDs: none", "failed_ids", failed)]
    if pending:
        details.append(SummaryField(f"Not attempted ({len(pending)}): {','.join(map(str, pending))}", "not_attempted_ids", pending))
    if verified and successful:
        details.append(SummaryField("Verification: readback matched for successful IDs", "verified_ids", successful))
    return details


def field_value(node: dict, name: str):
    camel = name.split("_")[0] + "".join(part.title() for part in name.split("_")[1:])
    return node.get(name, node.get(camel))


def matching_entities(data: Any, key: str, entity_id: str) -> list[dict]:
    if key in ("common_voltage_card", "dac_cards", "mirrors"):
        modules = [module for node in walk(data) for module in collection(field_value(node, "mems_module"))
                   if isinstance(module, dict) and str(module.get("id")) == "1"]
        return [entity for module in modules for entity in collection(field_value(module, key))
                if isinstance(entity, dict) and str(entity.get("id")) == str(entity_id)]
    return [entity for node in walk(data) for entity in collection(field_value(node, key))
            if isinstance(entity, dict) and str(entity.get("id")) == str(entity_id)]


def successful_entity(data: Any, key: str, entity_id: str) -> dict:
    """Merge successful response fragments, excluding missing-value placeholders."""
    entries = field_value(data, "response_payload") if isinstance(data, dict) else None
    if entries is not None:
        sources = [entry for entry in collection(entries) if isinstance(entry, dict) and entry.get("code") == "RC_OK"]
    else:
        sources = [] if isinstance(data, dict) and data.get("code") == "RC_MISSING_VALUE" else [data]
    result = None
    for source in sources:
        matches = matching_entities(source, key, entity_id)
        if len(matches) > 1:
            raise LotdError(f"Expected one {key} entity with ID {entity_id}; found {len(matches)}")
        if matches:
            result = merge_fields(result or {}, matches[0])
    if result is None:
        raise LotdError(f"No successful {key} entity with ID {entity_id} was returned")
    return result


def response_values_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without treating booleans as numeric equivalents."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            response_values_equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            response_values_equal(a, b) for a, b in zip(left, right))
    return left == right


def merge_fields(left: dict, right: dict) -> dict:
    result = dict(left)
    for key, value in right.items():
        if key not in result:
            result[key] = value
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_fields(result[key], value)
        elif not response_values_equal(result[key], value):
            raise LotdError(f"Conflicting successful response fragments for {key}")
    return result


def missing_paths(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, list):
        return [path for item in value for path in missing_paths(item, prefix)]
    if isinstance(value, dict):
        return [path for key, item in value.items() if key not in ("id", "@type")
                for path in missing_paths(item, f"{prefix}.{key}" if prefix else key)]
    return [prefix]


def walk(node: Any):
    if isinstance(node, dict):
        yield node
        for child in node.values(): yield from walk(child)
    elif isinstance(node, list):
        for child in node: yield from walk(child)


def collection(value: Any) -> list:
    if isinstance(value, dict): return [value]
    if isinstance(value, list): return value
    return []


def decimal_text(value: str) -> str:
    try: return str(Decimal(value))
    except InvalidOperation: return value


def verify_decimal(actual: Any, requested: str, label: str) -> None:
    require_number(actual, label)
    try: a, b = Decimal(str(actual)), Decimal(requested)
    except InvalidOperation as exc: raise LotdError(f"Could not compare requested and returned {label.lower()}") from exc
    if a != b: raise LotdError(f"{label} verification failed: requested {decimal_text(requested)}, read back {a}")


def require_number(value: Any, label: str) -> None:
    if type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
        raise LotdError(f"Response did not include numeric {label.lower()}")


def is_endpoint_failure(error) -> bool:
    return "Failed to dial" in str(error) or "context deadline exceeded" in str(error)

