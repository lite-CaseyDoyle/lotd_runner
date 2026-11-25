#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

capture_dir = pathlib.Path(os.environ["MOCK_CAPTURE_DIR"])
capture_dir.mkdir(parents=True, exist_ok=True)
request = json.load(sys.stdin)
count = len(list(capture_dir.glob("request-*.json"))) + 1
(capture_dir / "request-{}.json".format(count)).write_text(json.dumps(request))

scenario = os.environ.get("MOCK_SCENARIO", "success")
if scenario == "transport_hang":
    (capture_dir / "grpcurl.pid").write_text(str(os.getpid()))
    time.sleep(30)
    sys.exit(0)
operation = request.get("type")
payload = (request.get("payload") or [{}])[0]
if scenario == "container_action":
    assert operation == "RT_ACTION"
    assert payload["@type"] == "type.googleapis.com/lotd.EquipmentPackPayloadList"
    equipment = payload["equipment_pack"]
    assert isinstance(equipment, list) and len(equipment) == 1 and equipment[0]["id"] == 1
    actions = equipment[0]["actions"]
    assert len(actions) == 1
    action, content = next(iter(actions.items()))
    assert action in ("dump_container_to_file", "write_file_to_container")
    selected = content["request"]
    assert type(selected["instance_number"]) is int
    assert "/" not in selected["calibration_file_path"] and "\\" not in selected["calibration_file_path"]
    expected_keys = {"container_type", "instance_number", "calibration_file_path"}
    if action == "write_file_to_container":
        expected_keys.add("validate_after_write")
        assert selected["validate_after_write"] is True
    assert set(selected) == expected_keys
    if os.environ.get("MOCK_CONTAINER_INTERRUPT"):
        time.sleep(30)
    if os.environ.get("MOCK_CONTAINER_FAILURE"):
        print("Failed to dial target host: context deadline exceeded", file=sys.stderr)
        sys.exit(1)
    if "MOCK_CONTAINER_RESPONSE" in os.environ:
        print(os.environ["MOCK_CONTAINER_RESPONSE"])
    elif action == "dump_container_to_file":
        print(json.dumps({"cid": "123"}))
    else:
        print(json.dumps({"cid": "123", "response_payload": [{"code": "RC_OK", "payload": [{
            "@type": "type.googleapis.com/lotd.EquipmentPackPayloadList", "equipment_pack": [{"id": 1,
            "actions": {action: {"response": {"success": os.environ.get("MOCK_CONTAINER_SUCCESS", "true") == "true"}}}}]}]}]}))
    sys.exit(0)
if scenario == "reset_manager":
    assert operation == "RT_GET"
    assert payload == {"@type": "type.googleapis.com/lotd.ResetManagerPayloadList",
                       "reset_manager": {"id": 1, "state": {}}}
    if os.environ.get("MOCK_RESET_INTERRUPT"):
        time.sleep(30)
    if os.environ.get("MOCK_RESET_TIMEOUT"):
        print("Failed to dial target host: context deadline exceeded", file=sys.stderr)
        sys.exit(1)
    print(os.environ.get("MOCK_RESPONSE") or json.dumps({"cid": "123", "response_payload": [
        {"code": "RC_OK", "payload": [{"@type": "type.googleapis.com/lotd.ResetManagerPayloadList",
         "reset_manager": [{"id": 1, "state": {"last_reset_reason": {"value": "RR_POWER_ON"}}}]}]}]}))
    sys.exit(0)
if scenario in ("mirror_lds_details", "r320_selected_mirrors"):
    assert operation == "RT_GET"
    if "MOCK_RESPONSE" in os.environ:
        print(os.environ["MOCK_RESPONSE"])
        sys.exit(0)
    if "lds_card" in payload:
        entity = payload["lds_card"]
        key = "lds_card"
        section = "inventory" if "inventory" in entity else "state"
        assert entity == {"id": 1, section: {}}
        value = ({"entity_description": "LDS Card", "max_num_north_lds_ocs_ids": 3,
                  "max_num_south_lds_ocs_ids": 3} if section == "inventory" else
                 {"missing": False, "temperature": 2775, "ps_failure": False,
                  "laser_failure": False, "eeprom_failure": False, "flash_failure": True,
                  "laser_power_oor": False})
        body = {key: [{"id": 1, section: value}]}
    else:
        module = payload["mems_module"]
        assert module["id"] == 1
        entity = module["mirrors"]
        section = "state" if "state" in entity else "config"
        assert entity[section] == {} or (scenario == "r320_selected_mirrors" and
                                        entity[section] in ({"x_tilt_voltage": 0}, {"y_tilt_voltage": 0}))
        port = entity["id"]
        if os.environ.get("MOCK_MIRROR_ERROR_ID") == str(port):
            print(json.dumps({"response_payload": [{"code": "RC_OPERATION_FAILED"}]}))
            sys.exit(0)
        value = ({"status": {"value": "MS_USABLE"}, "x_tilt_voltage": 143224,
                  "y_tilt_voltage": 120364, "x_tilt_actual_voltage": 143216,
                  "y_tilt_actual_voltage": 120354, "il_delta": 1} if section == "state" else
                 {"x_tilt_voltage": 143224, "y_tilt_voltage": 120364, "connection": 629})
        if scenario == "r320_selected_mirrors":
            value = ({"status": {"value": "MS_USABLE"}, "x_tilt_voltage": 0,
                      "y_tilt_voltage": 0, "x_tilt_actual_voltage": 302,
                      "y_tilt_actual_voltage": 394, "il_delta": 0} if section == "state" else
                     {"x_tilt_voltage": 0, "y_tilt_voltage": 0, "connection": 0})
        body = {"mems_module": [{"id": 1, "mirrors": [{"id": port, section: value}]}]}
    result = {"cid": "123", "response_payload": [{"code": "RC_OK", "payload": [body]}]}
    if "lds_card" in payload and section == "inventory":
        result["response_payload"].append({"code": "RC_OPERATION_FAILED", "payload": [{"lds_card": [
            {"id": 1, "inventory": {"entity_info": {"serial_no": "", "hardware_rev": "",
             "manufacturing_site": "", "manufacturing_date": "", "clei_code": "", "marketing_pn": ""}}}]}]})
    print(json.dumps(result))
    sys.exit(0)
if scenario == "hvdac_details":
    assert operation == "RT_GET"
    module = payload.get("memsModule", payload.get("mems_module"))
    assert module["id"] == 1
    card = module["dac_cards"]
    card_id = card["id"]
    if os.environ.get("MOCK_HVDAC_TIMEOUT_ID") == str(card_id):
        print("Failed to dial target host: context deadline exceeded", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("MOCK_HVDAC_INTERRUPT_ID") == str(card_id):
        time.sleep(30)
    if "MOCK_RESPONSE" in os.environ:
        print(os.environ["MOCK_RESPONSE"])
        sys.exit(0)
    if os.environ.get("MOCK_HVDAC_ERROR_ID") == str(card_id):
        print(json.dumps({"response_payload": [{"code": "RC_OPERATION_FAILED"}]}))
        sys.exit(0)
    state = {"missing": False, "power_supply_failure": False, "channel_oor_failure": False,
        "fpga_failure": False, "eeprom_memory_failure": False, "loss_of_internal_comms": False,
        "voltage_1v0_failure": False, "voltage_12v_failure": False, "voltage_1v8_failure": False,
        "voltage_185v_failure": False, "voltage_3v3_failure": False, "voltage_5v_failure": False,
        "channel_oor_degrade": False, "loss_of_dac_comms": False}
    if "state" in card:
        assert card["state"] == {}
        cards = [{"id": i, "state": state} for i in range(1, (11 if os.environ.get("MOCK_MODEL") == "R320" else 10)+1)]
    elif "inventory" in card:
        assert card["inventory"] == {}
        cards = [{"id": card_id, "inventory": {"entity_description": f"High Voltage Card {card_id}",
            "entity_info": {"serial_no": "DACS260200150", "hardware_rev": "1", "manufacturing_site": "Sanmina",
                            "manufacturing_date": "01-11-2026 00:00:00", "marketing_pn": "OCS-DAC"}}}]
    else:
        chip = card["dac_chips"][0]
        channel = chip["channels"][0]
        assert channel["config"] == {} and channel["state"] == {}
        cards = [{"id": card_id, "dac_chips": [{"id": chip["id"], "channels": [{"id": channel["id"],
            "config": {"dac_count": 0, "target_voltage": 215}, "state": {"actual_voltage": 215, "oor_failure": False}}]}]}]
    result = {"cid": "123", "response_payload": [{"code": "RC_OK", "payload": [{
        "@type": "type.googleapis.com/lotd.MemsModulePayloadList", "mems_module": [{"id": 1, "dac_cards": cards}]}]}]}
    if "inventory" in card:
        result["response_payload"].append({"code": "RC_MISSING_VALUE", "payload": [{"mems_module": [{"id": 1,
            "dac_cards": [{"id": card_id, "inventory": {"entity_info": {"clei_code": ""}}}]}]}]})
    print(json.dumps(result))
    sys.exit(0)
if scenario in ("ps_reads", "r320_ps_reads"):
    assert operation == "RT_GET"
    ps_id = payload["ps_module"]["id"]
    assert set(payload["ps_module"]) == {"id"} and ps_id in (1, 2)
    if os.environ.get("MOCK_PS_TIMEOUT_ID") == str(ps_id):
        print("Failed to dial target host: context deadline exceeded", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("MOCK_PS_INTERRUPT_ID") == str(ps_id):
        time.sleep(30)
    if "MOCK_RESPONSE" in os.environ:
        print(os.environ["MOCK_RESPONSE"])
        sys.exit(0)
    if os.environ.get("MOCK_PS_ERROR_ID") == str(ps_id):
        print(json.dumps({"response_payload": [{"code": "RC_OPERATION_FAILED"}]}))
        sys.exit(0)
    state = {"missing": False, "involt_oor": False, "outvolt_oor": False, "outcurr_oor": False,
        "fan_fail": False, "ps_error": False, "status_word": 130, "status_vout": 0, "status_iout": 0,
        "status_vin": 0, "status_cml": 128, "status_fans_1_2": 0, "read_vin": 11650, "read_iin": 89,
        "read_vout": 1201, "read_iout": 687, "read_temperature1": 2400, "read_fan_speed": 9504,
        "read_pin": 10300, "calculated_pout": 8244, "peak_pout": 9672, "peak_vout": 1203, "peak_iout": 806}
    absent_state = {key: False if isinstance(value, bool) else 0 for key,value in state.items()}
    absent_state["missing"] = True
    modules = [{"id": 1, "state": state,
        "sensor": [{"id": "temp", "state": {"value": 2400, "oor_high": False, "oor_low": False, "peak_value": 2400},
                    "config": {"high_threshold": {"threshold": 12500, "hysteresis": 0},
                               "low_threshold": {"threshold": -500, "hysteresis": 500}},
                    "inventory": {"entity_description": "PS Temperature Sensor 1"}}],
        "inventory": {"entity_description": "PS Controller 1", "entity_info": {"serial_no": "SU2445RF0193",
            "hardware_rev": "1101-0601-0000", "marketing_pn": "D1U54P-W-650-12-HB4C", "marketing_name": "Murata-PS"}}},
        {"id": 2, "state": absent_state,
         "sensor": [{"id": "temp", "inventory": {"entity_description": "PS Temperature Sensor 2"}}],
         "inventory": {"entity_description": "PS Controller 2"}}]
    result = {"cid": "123", "response_payload": [
        {"code": "RC_OK", "payload": [{"@type": "type.googleapis.com/lotd.PsControlPayloadList", "ps_module": modules}]},
        {"code": "RC_FRU_MISSING", "payload": [{"@type": "type.googleapis.com/lotd.PsControlPayloadList",
            "ps_module": [{"id": 2, "sensor": [{"id": "temp", "state": {
                "value": 0, "oor_high": False, "oor_low": False, "peak_value": 0}}]}]}]}]}
    if scenario == "r320_ps_reads":
        for module, temperature in zip(modules, (3600, 3500)):
            module["state"] = {**state, "missing": False}
            module["state"].pop("read_temperature1")
            module["sensor"] = [{"id": "temp", "state": {"value": temperature,
                "oor_high": False, "oor_low": False, "peak_value": temperature + 200},
                "config": {"high_threshold": {"threshold": 12000, "hysteresis": 0},
                           "low_threshold": {"threshold": -500, "hysteresis": 500}},
                "inventory": {"entity_description": f"PS Temperature Sensor {module['id']}"}}]
        result["response_payload"] = result["response_payload"][:1]
    print(json.dumps(result))
    sys.exit(0)
entity_id = None
led_id = 1
cards = {}
cv_card = {}
led_component = None
led_fru = None

mems = payload.get("memsModule", {})
mirror_module = payload.get("mems_module", {})
mirror = mirror_module.get("mirrors", {}) if isinstance(mirror_module, dict) else {}
mirror_id = mirror.get("id") if isinstance(mirror, dict) else None
mirror_config = mirror.get("config", {}) if isinstance(mirror, dict) else {}
mirror_field = next(iter(mirror_config), None) if isinstance(mirror_config, dict) else None
connection_managers = payload.get("connection_manager", [])
if isinstance(connection_managers, dict):
    connection_managers = [connection_managers]
connection_manager = connection_managers[0] if connection_managers else {}
connection_ports = connection_manager.get("port_array", []) if isinstance(connection_manager, dict) else []
if isinstance(connection_ports, dict):
    connection_ports = [connection_ports]
connection_port = connection_ports[0] if connection_ports else {}
connection_id = connection_port.get("id") if isinstance(connection_port, dict) else None
connection_config = connection_port.get("config", {}) if isinstance(connection_port, dict) else {}
if isinstance(mems, dict):
    cv_card = mems.get("common_voltage_card", {})
    cards = mems.get("dac_cards", {})
    if isinstance(cards, dict):
        entity_id = cards.get("id")
        led = cards.get("led", {})
        if isinstance(led, dict):
            led_id = led.get("id", led_id)
        if isinstance(led, list) and led:
            led_id = led[0].get("id", led_id)
    if isinstance(cv_card, dict) and "led" in cv_card:
        led_component = "cv"
        led_fru = cv_card
    elif isinstance(cards, dict) and "led" in cards:
        led_component = "hvdac"
        led_fru = cards
if "equipment_pack" in payload:
    led_component = "equipment_pack"
    led_fru = payload["equipment_pack"]
elif "fans_module" in payload:
    led_component = "fans_module"
    led_fru = payload["fans_module"]
elif "lds_card" in payload:
    led_component = "lds_card"
    led_fru = payload["lds_card"]
cv_channel_id = None
if isinstance(cv_card, dict):
    channels = cv_card.get("channels", [])
    if isinstance(channels, list) and channels:
        cv_channel_id = channels[0].get("id")

if scenario == "fan_reads":
    assert operation == "RT_GET"
    if "MOCK_RESPONSE" in os.environ:
        print(os.environ["MOCK_RESPONSE"])
        sys.exit(0)
    requested = payload["fans_module"]
    fan_id = requested["id"]
    if str(fan_id) == os.environ.get("MOCK_FAN_TIMEOUT_ID"):
        print('Failed to dial target host "mock-device:3217": context deadline exceeded', file=sys.stderr)
        sys.exit(1)
    if str(fan_id) == os.environ.get("MOCK_FAN_INTERRUPT_ID"):
        time.sleep(30)
    if str(fan_id) in os.environ.get("MOCK_FAN_ERROR_IDS", "").split(","):
        print(json.dumps({"response_payload": [{"code": "RC_OPERATION_FAILED"}]}))
        sys.exit(0)
    section = next((key for key in requested if key != "id"), None)
    serials = ("FANS255101509", "FANS255101416", "FANS255101441")
    speeds = (17045, 16574, 16759)
    fans, missing = [], []
    for i in (1,2,3):
        if i != fan_id and not os.environ.get("MOCK_FAN_RETURN_ALL"):
            continue
        inventory = {"entity_description": f"Fan Controller {i}", "entity_info": {
            "serial_no": serials[i-1], "hardware_rev": "1" if i == 3 else "0", "manufacturing_site": "Thailand",
            "manufacturing_date": ("01-06-2026 00:00:00", "01-05-2026 00:00:00", "12-23-2025 00:00:00")[i-1]}}
        if i != 3:
            inventory["entity_info"]["marketing_pn"] = "OCS-FAN"
        fan = {"id": i, "state": {"presence": True, "oper_status": "openconfig-platform-types:ACTIVE",
            "is_replaceable": True, "pwm": 65, "speed": speeds[i-1], "temperature": 4200,
            "missing": False, "pwr_error": False, "fan_error": False, "bus_error": False, "eeprom_failure": False},
            "config": {"pwm_target": 40, "speed_low": {"threshold": 5000}, "manual_pwm_target": 40,
                       "control_mode": {"value": "FCM_AUTO"}},
            "inventory": inventory,
            "sensor": [{"id": "temp", "state": {"value": 4200, "oor_high": False, "oor_low": False},
                        "config": {"high_threshold": {"threshold": 5500, "hysteresis": 500},
                                   "low_threshold": {"threshold": 500, "hysteresis": 500}},
                        "inventory": {"entity_description": f"Fan {i} Temperature Sensor"}}],
            "led": [{"id": 1, "config": {"control": {"value": "LC_ON"}},
                     "inventory": {"entity_description": f"Fan {i} Blue LED"}},
                    {"id": 2, "config": {"control": {"value": "LC_OFF"}},
                     "inventory": {"entity_description": f"Fan {i} Amber LED"}}]}
        if section:
            result = json.loads(os.environ["MOCK_SECTION_RESULT"]) if "MOCK_SECTION_RESULT" in os.environ else fan[section]
            fan = {"id": i, section: result}
        fans.append(fan)
        if section in (None, "inventory"):
            fields = {"clei_code": "", "marketing_name": ""}
            if i == 3:
                fields["marketing_pn"] = ""
            missing.append({"id": i, "inventory": {"entity_info": fields}})
    responses = [{"code": "RC_OK", "payload": [{"@type": "type.googleapis.com/lotd.FanControlPayloadList", "fans_module": fans}]}]
    if missing:
        responses.append({"code": "RC_MISSING_VALUE", "payload": [{"fans_module": missing}]})
    print(json.dumps({"cid": "123", "response_payload": responses}))
    sys.exit(0)

if scenario == "fan_control_set":
    fan = payload["fans_module"]
    fan_id = fan["id"]
    assert 1 <= fan_id <= 3
    phase_id = operation + ":" + str(fan_id)
    if os.environ.get("MOCK_FAN_CONTROL_INTERRUPT") == phase_id:
        time.sleep(30)
    if os.environ.get("MOCK_FAN_CONTROL_TIMEOUT") == phase_id:
        print("Failed to dial target host: context deadline exceeded", file=sys.stderr)
        sys.exit(1)
    override = os.environ.get("MOCK_FAN_CONTROL_" + ("SET" if operation == "RT_SET" else "GET") + "_RESPONSE")
    if operation == "RT_SET":
        config = fan["config"]
        assert set(config) <= {"manual_pwm_target", "control_mode"}
        (capture_dir / f"fan-control-{fan_id}.json").write_text(json.dumps(config))
        result = {"cid": "123"}
        if str(fan_id) == os.environ.get("MOCK_FAN_CONTROL_ERROR_ID"):
            result = {"response_payload": [{"code": "RC_OPERATION_FAILED"}]}
    else:
        assert fan["config"] == {}
        config = {"pwm_target": 40, "manual_pwm_target": 40, "control_mode": {"value": "FCM_AUTO"}}
        config.update(json.loads((capture_dir / f"fan-control-{fan_id}.json").read_text()))
        result = {"response_payload": [{"code": "RC_OK", "payload": [{"fans_module": [{"id": fan_id, "config": config}]}]}]}
    print(override if override is not None else json.dumps(result))
    sys.exit(0)

if scenario in ("cv_control_set", "cv_control_set_cid"):
    card = payload["mems_module"]["common_voltage_card"]
    assert payload["mems_module"]["id"] == 1 and card["id"] == 1
    control = card["cv_control"][0]
    assert control["id"] == 1
    if os.environ.get("MOCK_CONTROL_INTERRUPT") == operation:
        time.sleep(30)
    if os.environ.get("MOCK_CONTROL_FAILURE") == operation:
        print("Failed to dial target host: context deadline exceeded", file=sys.stderr)
        sys.exit(1)
    override = os.environ.get("MOCK_CONTROL_" + ("SET" if operation == "RT_SET" else "GET") + "_RESPONSE")
    if operation == "RT_SET":
        assert len(control["config"]) == 1
        field, target = next(iter(control["config"].items()))
        assert field in ("dead_band", "logging", "kinit_coeff", "ki_coeff", "filter_coeff", "loop_interval", "num_readings")
        assert type(target) is (bool if field == "logging" else int)
        (capture_dir / "cv-control-target.json").write_text(json.dumps(control["config"]))
        result = {"cid": "123"} if scenario == "cv_control_set_cid" else {"response_payload": [{"code": "RC_OK"}]}
    else:
        assert control["config"] == {}
        config = {"kinit_coeff": 1000, "ki_coeff": 25, "dead_band": 0,
                  "filter_coeff": 900, "logging": False, "loop_interval": 30,
                  "num_readings": 5, "enable": True}
        config.update(json.loads((capture_dir / "cv-control-target.json").read_text()))
        result = {"response_payload": [{"code": "RC_OK", "payload": [{"mems_module": [{
            "id": 1, "common_voltage_card": [{"id": 1, "cv_control": [{"id": 1, "config": config}]}]}]}]}]}
    print(override if override is not None else json.dumps(result))
    sys.exit(0)

if scenario == "read_details":
    assert operation == "RT_GET"
    if "MOCK_RESPONSE" in os.environ:
        print(os.environ["MOCK_RESPONSE"])
        sys.exit(0)
    module = payload.get("mems_module", payload.get("memsModule", {}))
    module = module[0] if isinstance(module, list) else module
    key = "equipment_pack" if "equipment_pack" in payload else next((name for name in ("common_voltage_card", "dac_cards") if name in module), "mems_module")
    entity = payload[key] if key == "equipment_pack" else (module if key == "mems_module" else module[key])
    section = next(name for name in entity if name != "id")
    side_ids = ("A-mems-temp", "B-mems-temp") if os.environ.get("MOCK_MODEL") == "R300-L" else ("north-mems-temp", "south-mems-temp")
    def sensor(sensor_id, number, failed=False):
        entry = {"id": sensor_id,
                 "state": {"value": number, "failed": failed, "oor_high": False, "oor_high_degrade": False},
                 "config": {"high_threshold": {"threshold": 3000 if sensor_id == "humidity" else 6000,
                                               "hysteresis": 200 if sensor_id == "humidity" else 250},
                            "high_degrade_threshold": {"threshold": 2500 if sensor_id == "humidity" else 5500,
                                                       "hysteresis": 200 if sensor_id == "humidity" else 250}},
                 "inventory": {"entity_description": sensor_id + " sensor"}}
        if sensor_id != "humidity":
            entry["state"].update(oor_low=failed, oor_low_degrade=failed)
            entry["config"].update(low_threshold={"threshold": 500, "hysteresis": 250},
                                   low_degrade_threshold={"threshold": 1000, "hysteresis": 250})
        return entry
    sensors = [sensor("ob-temp", -2500, True), sensor(side_ids[0], -2500, True),
               sensor(side_ids[1], -2500, True), sensor("humidity", 500)] if key == "mems_module" else [sensor("temp", 2400)]
    if os.environ.get("MOCK_MODEL") == "R300-L" and key == "mems_module":
        for entry, number in zip(sensors, (2286, 2100, 2100, 500)):
            entry["state"].update(value=number, failed=False)
            if entry["id"] != "humidity":
                entry["state"].update(oor_low=False, oor_low_degrade=False)
    voltages = [34980,35159,35267,89206,91758,92877,93257,92896,86639,87364,88169,87386,90051,90558,91382,89953,113518,11936,4989,3316,633,-3583,-72879]
    state = {"loss_of_comms": False, "module_failure": False, "correction_temperature": 2195,
             "flash_memory_failure": False, "relative_humidity": 4064, "relative_humidity_failed": False} if key == "mems_module" else {
             "missing": False, "channel_oor_failure": True, "voltage_3v3": 3316,
             "voltage_neg3v3": -3583, "channel_oor_degrade": True, "voltages_adjusted": True}
    defaults = {"state": state, "sensor": sensors,
                "cv_control": [{"id": 1, "config": {"kinit_coeff": 1000, "ki_coeff": 25,
                    "dead_band": 0, "filter_coeff": 900, "logging": False,
                    "loop_interval": 30, "num_readings": 5, "enable": True}}],
                "cv_voltages": [{"id": i, "state": {"cv_voltage": number}} for i,number in enumerate(voltages,1)],
                "inventory": {"entity_description": "MEMs Module"} if key == "mems_module" else {"entity_description": "Common Voltage Card", "entity_info": {
                    "serial_no": "CVMS260400006", "hardware_rev": "0", "manufacturing_site": "Thailand",
                    "manufacturing_date": "01-24-2026 00:00:00", "marketing_pn": "OCS-CV"}}}
    result = json.loads(os.environ["MOCK_SECTION_RESULT"]) if "MOCK_SECTION_RESULT" in os.environ else defaults[section]
    def response_item(value):
        returned = {"id": entity["id"], section: value}
        item = {key: [returned]}
        return item if key in ("equipment_pack", "mems_module") else {"mems_module": [{"id": 1, **item}]}
    responses = [{"code": os.environ.get("MOCK_CODE", "RC_OK"), "payload": [response_item(result)]}]
    if os.environ.get("MOCK_SPLIT"):
        responses.append({"code": "RC_MISSING_VALUE", "payload": [response_item({"entity_info": {"clei_code": ""}})]})
    print(json.dumps({"cid": "123", "response_payload": responses}))
    sys.exit(0)

if scenario in ("fru_data", "identity_sensor"):
    assert operation == "RT_GET"
    entity = cv_card if cv_card else (cards if cards else led_fru)
    assert isinstance(entity, dict)
    key = "common_voltage_card" if cv_card else ("dac_cards" if cards else led_component)
    section = next(key for key in entity if key != "id")
    defaults = {"state": {"missing": False}, "inventory": {"entity_info": {"serial_number": "SIM-1"}},
                "mac_addresses": [{"id": 2, "inventory": {"entity_description": "eth1"}, "state": {"mac_address": "00:11:22:33:44:56"}}, {"id": 1, "inventory": {"entity_description": "eth0"}, "state": {"mac_address": "00:11:22:33:44:55"}}],
                "load_version": [{"id": 1, "inventory": {"entity_description": "Active Image"}, "state": {"version": "SONiC-OS-active-build"}}, {"id": 2, "inventory": {"entity_description": "Inactive Image"}, "state": {"version": "SONiC-OS-inactive-build"}}],
                "sensor": [{"id": "temp", "state": {"value": 3100}}]}
    result = json.loads(os.environ["MOCK_SECTION_RESULT"]) if "MOCK_SECTION_RESULT" in os.environ else defaults[section]
    result_key = section
    if os.environ.get("MOCK_CAMEL"):
        key = key.split("_")[0] + "".join(x.title() for x in key.split("_")[1:])
        result_key = section.split("_")[0] + "".join(x.title() for x in section.split("_")[1:])
    returned = {"id": entity["id"], result_key: result}
    item = {key: [returned]}
    if cv_card or cards:
        item = {"memsModule": [{"id": 1, **item}]}
    print(json.dumps({"response_payload": [{"code": os.environ.get("MOCK_CODE", "RC_OK"), "payload": [item]}]}))
    sys.exit(0)

if scenario.startswith("dac_count_"):
    if operation == "RT_SET":
        print(json.dumps({"cid": "123"}))
        sys.exit(0)
    chip = cards["dac_chips"][0]
    channel = chip["channels"][0]
    value = 100
    if scenario.startswith("dac_count_set"):
        previous = json.loads((capture_dir / "request-1.json").read_text())
        value = previous["payload"][0]["memsModule"]["dac_cards"]["dac_chips"][0]["channels"][0]["config"]["dac_count"]
        if scenario.endswith("mismatch"):
            value += 1
    print(json.dumps({"code": "RC_OK", "memsModule": {"id": 1, "dac_cards": {
        "id": cards["id"], "dac_chips": [{"id": chip["id"], "channels": [
            {"id": channel["id"], "config": {"dac_count": value}}]}]}}}))
    sys.exit(0)

if scenario.startswith("mirror_"):
    if operation == "RT_SET":
        print(json.dumps({"cid": "123"}))
        sys.exit(0)
    target = 77.25
    if scenario in ("mirror_set_success", "mirror_set_mismatch"):
        set_request = json.loads((capture_dir / "request-1.json").read_text())
        set_mirror = set_request["payload"][0]["mems_module"]["mirrors"]
        set_field, target = next(iter(set_mirror["config"].items()))
        if scenario == "mirror_set_mismatch":
            target += 1
    response = {
        "cid": "123",
        "response_payload": [{
            "code": "RC_OK",
            "payload": [{
                "@type": "type.googleapis.com/lotd.MemsModulePayloadList",
                "mems_module": {"id": 1, "mirrors": {
                    "id": mirror_id,
                    "config": {mirror_field: target},
                }},
            }],
        }],
    }
    print(json.dumps(response))
    sys.exit(0)

if scenario == "dial_failure":
    print('Failed to dial target host "mock-device:3217": context deadline exceeded', file=sys.stderr)
    sys.exit(1)

if scenario.startswith("connection_"):
    if operation == "RT_SET":
        print(json.dumps({"cid": "123"}))
        sys.exit(0)
    peer = 10001 if scenario == "connection_get" or (scenario == "connection_get_range" and connection_id == 2) else (1 if connection_id == 321 else 0)
    blocking = False
    if scenario.startswith("connection_set_"):
        set_request = None
        for index in range(count - 1, 0, -1):
            candidate = json.loads((capture_dir / "request-{}.json".format(index)).read_text())
            candidate_manager = candidate["payload"][0]["connection_manager"][0]
            candidate_port = candidate_manager["port_array"][0]
            if candidate.get("type") == "RT_SET" and candidate_port["id"] == connection_id:
                set_request = candidate_port["config"]
                break
        field, value = next(iter(set_request.items()))
        if field == "peer_port_number":
            peer = 0 if value == -1 else value
            if scenario == "connection_set_peer_mismatch":
                peer += 1
        else:
            blocking = value
            if scenario == "connection_set_blocking_mismatch":
                blocking = not blocking
    else:
        if scenario == "connection_get":
            blocking = False
    response = {
        "cid": "123",
        "response_payload": [{
            "code": "RC_OK",
            "payload": [{
                "@type": "type.googleapis.com/lotd.ConnectionManagerPayloadList",
                "connection_manager": [{
                    "id": 1,
                    "port_array": [{
                        "id": connection_id,
                        "config": {
                            "peer_port_number": peer,
                            "optical_blocking_enable": blocking,
                        },
                    }],
                }],
            }],
        }],
    }
    print(json.dumps(response))
    sys.exit(0)

if scenario == "interrupt_get":
    time.sleep(30)

if scenario == "interrupt_set" and operation == "RT_SET" and cv_channel_id == 2:
    time.sleep(30)

if scenario == "malformed_json":
    print("this is not JSON")
    sys.exit(0)

if scenario in ("led_both", "led_both_set"):
    assert isinstance(led_fru, dict)
    if operation == "RT_SET":
        led_set = led_fru.get("led", {})
        assert led_set.get("id") in (1, 2)
        assert led_set.get("config", {}).get("control", {}).get("value") == "LC_ON"
        print(json.dumps({"cid": "123"}))
        sys.exit(0)
    if scenario == "led_both":
        assert led_fru.get("led") == {}
        led_values = {1: "LC_ON", 2: "LC_OFF"}
    else:
        led_values = {1: "LC_ON", 2: "LC_ON"}
    led_list = [
        {"id": led_id, "config": {"control": {"value": value}}}
        for led_id, value in led_values.items()
    ]
    if led_component == "equipment_pack":
        item = {"@type": "type.googleapis.com/lotd.EquipmentPackPayloadList",
                "equipment_pack": [{"id": led_fru["id"], "led": led_list}]}
    elif led_component == "hvdac":
        item = {"@type": "type.googleapis.com/lotd.MemsModulePayloadList",
                "memsModule": {"id": 1, "dac_cards": [{"id": led_fru["id"], "led": led_list}]}}
    elif led_component == "cv":
        item = {"@type": "type.googleapis.com/lotd.MemsModulePayloadList",
                "memsModule": {"id": 1, "common_voltage_card": [{"id": led_fru["id"], "led": led_list}]}}
    elif led_component == "fans_module":
        item = {"@type": "type.googleapis.com/lotd.FanControlPayloadList",
                "fans_module": [{"id": led_fru["id"], "led": led_list}]}
    else:
        item = {"@type": "type.googleapis.com/lotd.LdsCardPayloadList",
                "lds_card": [{"id": led_fru["id"], "led": led_list}]}
    print(json.dumps({"cid": "123", "response_payload": [{"code": "RC_OK", "payload": [item]}]}))
    sys.exit(0)

if scenario in ("cv_channel_get", "cv_channel_get_range", "cv_per_channel_error"):
    channel = cv_card["channels"][0]
    if scenario == "cv_per_channel_error" and operation == "RT_GET" and channel["id"] == 2:
        print(json.dumps({"code": "RC_OPERATION_FAILED"}))
        sys.exit(0)
    response = {
        "cid": "123",
        "response_payload": [{
            "code": "RC_OK",
            "payload": [{
                "@type": "type.googleapis.com/lotd.MemsModulePayloadList",
                "memsModule": {"id": 1, "common_voltage_card": {
                    "id": 1,
                    "channels": [{
                        "id": channel["id"],
                        "config": {"target_voltage": 7.5},
                        "state": {"actual_voltage": 6.5},
                    }],
                }},
            }],
        }],
    }
    print(json.dumps(response))
    sys.exit(0)

if scenario in ("cv_channel_set_success", "cv_channel_set_mismatch", "interrupt_set"):
    if operation == "RT_SET":
        print(json.dumps({"cid": "123"}))
        sys.exit(0)
    set_request = json.loads((capture_dir / "request-1.json").read_text())
    set_cv = set_request["payload"][0]["memsModule"]["common_voltage_card"]
    set_channel = set_cv["channels"][0]
    target = set_channel["config"]["target_voltage"]
    if scenario == "cv_channel_set_mismatch":
        target += 1
    channel = cv_card["channels"][0]
    response = {
        "cid": "123",
        "response_payload": [{
            "code": "RC_OK",
            "payload": [{
                "@type": "type.googleapis.com/lotd.MemsModulePayloadList",
                "memsModule": {"id": 1, "common_voltage_card": {
                    "id": 1,
                    "channels": [{
                        "id": channel["id"],
                        "config": {"target_voltage": target},
                    }],
                }},
            }],
        }],
    }
    print(json.dumps(response))
    sys.exit(0)

if scenario in ("channel_set_success", "channel_set_mismatch"):
    if operation == "RT_SET":
        print(json.dumps({"cid": "123"}))
        sys.exit(0)
    set_request = json.loads((capture_dir / "request-1.json").read_text())
    set_cards = set_request["payload"][0]["memsModule"]["dac_cards"]
    set_channel = set_cards["dac_chips"][0]["channels"][0]
    target = set_channel["config"]["target_voltage"]
    if scenario == "channel_set_mismatch":
        target += 1
    chips = cards.get("dac_chips", [])
    chip = chips[0]
    channel = chip["channels"][0]
    channel_data = {
        "id": channel["id"],
        "config": {"target_voltage": target},
        "code": "RC_OPERATION_FAILED",
    }
    if "state" in channel:
        channel_data["state"] = {"actual_voltage": 120, "oor_failure": False}
    response = {
        "cid": "123",
        "response_payload": [{
            "code": "RC_OK",
            "payload": [{
                "@type": "type.googleapis.com/lotd.MemsModulePayloadList",
                "memsModule": {"id": 1, "dac_cards": {
                    "id": cards["id"],
                    "dac_chips": [{"id": chip["id"], "channels": [channel_data]}],
                }},
            }],
        }],
    }
    print(json.dumps(response))
    sys.exit(0)

if scenario == "channel_success":
    chips = cards.get("dac_chips", [])
    chip = chips[0]
    channel = chip["channels"][0]
    print(json.dumps({
        "code": "RC_OK",
        "memsModule": {"id": 1, "dac_cards": {
            "id": cards["id"],
            "dac_chips": [{"id": chip["id"], "channels": [{
                "id": channel["id"],
                "config": {"target_voltage": 123},
                "state": {"actual_voltage": 120, "oor_failure": False},
            }]}],
        }},
    }))
    sys.exit(0)

if scenario == "r320_humidity":
    assert operation == "RT_GET"
    assert payload == {"@type": "type.googleapis.com/lotd.MemsModulePayloadList",
                       "mems_module": {"id": 1, "sensor": [{"id": "humidity", "state": {}}]}}
    print(os.environ.get("MOCK_RESPONSE") or json.dumps({"cid": "123", "response_payload": [
        {"code": "RC_OK", "payload": [{"@type": "type.googleapis.com/lotd.MemsModulePayloadList",
         "mems_module": [{"id": 1, "sensor": [{"id": "humidity", "state": {
             "value": 0, "oor_high": False, "oor_high_degrade": False, "failed": False}}]}]}]}]}))
    sys.exit(0)

if scenario == "r320_temperatures":
    assert operation == "RT_GET"
    key = "equipment_pack" if "equipment_pack" in payload else "fans_module"
    entity = payload[key]
    assert set(entity) == {"id", "sensor"}
    sensor_id = entity["sensor"][0]["id"]
    assert entity["sensor"] == [{"id": sensor_id, "state": {"value": 0}}]
    assert sensor_id in (("cpu-temp", "inlet-temp", "outlet-temp") if key == "equipment_pack" else ("temp",))
    if "MOCK_RESPONSE" in os.environ:
        print(os.environ["MOCK_RESPONSE"])
        sys.exit(0)
    if os.environ.get("MOCK_SENSOR_TIMEOUT_ID") == str(entity["id"]):
        print("Failed to dial target host: context deadline exceeded", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("MOCK_SENSOR_FAILURE_ID") == str(entity["id"]):
        print(json.dumps({"response_payload": [{"code": "RC_OPERATION_FAILED"}]}))
        sys.exit(0)
    def temperature_sensor(name, value, thresholds, description, low=True, failed=False):
        state = {"value": value, "oor_high": False, "oor_high_degrade": False}
        if low:
            state.update(oor_low=False, oor_low_degrade=False)
        if failed:
            state["failed"] = False
        names = ("high_threshold", "high_degrade_threshold", "low_threshold", "low_degrade_threshold")
        return {"id": name, "state": state,
                "config": {name: {"threshold": threshold, "hysteresis": 500} for name, threshold in zip(names, thresholds)},
                "inventory": {"entity_description": description}}
    if key == "equipment_pack":
        assert entity["id"] == 1
        sensors = [temperature_sensor("cpu-temp", 4300, (8500, 7500, -2000, -1000), "CPU Temperature Sensor"),
                   temperature_sensor("inlet-temp", 3250, (5500, 4500, 500, 1500), "Inlet Temperature Sensor", failed=True),
                   temperature_sensor("outlet-temp", 3300, (7500, 6500), "Outlet Temperature Sensor", low=False, failed=True)]
    else:
        assert entity["id"] in (1, 2, 3)
        sensors = [{"id": "temp", "state": {"value": 4100, "oor_high": False, "oor_low": False},
                    "config": {"high_threshold": {"threshold": 5500, "hysteresis": 500},
                               "low_threshold": {"threshold": 500, "hysteresis": 500}},
                    "inventory": {"entity_description": f"Fan {entity['id']} Temperature Sensor"}}]
    print(json.dumps({"cid": "123", "response_payload": [{"code": "RC_OK", "payload": [
        {key: [{"id": entity["id"], "sensor": sensors}]}]}]}))
    sys.exit(0)

if scenario == "r320_hvdac_temperature":
    assert operation == "RT_GET" and cards["id"] == 5
    assert cards["sensor"] == [{"id": "temp", "state": {"value": 0}}]
    print(json.dumps({"cid": "123", "response_payload": [{"code": "RC_OK", "payload": [{
        "@type": "type.googleapis.com/lotd.MemsModulePayloadList", "mems_module": [{"id": 1,
            "dac_cards": [{"id": 5, "sensor": [{"id": "temp", "state": {"value": 2725}}]}]}]}]}]}))
    sys.exit(0)

if scenario in ("temperature_sensor", "temperature_fallback"):
    if "sensor" in cards:
        if scenario == "temperature_fallback":
            print(json.dumps({"code": "RC_OPERATION_FAILED"}))
        else:
            print(json.dumps({
                "code": "RC_OK",
                "memsModule": {"id": 1, "dac_cards": {"id": cards["id"],
                    "sensor": [{"id": "temp", "state": {"value": 3100}}]}},
            }))
    else:
        print(json.dumps({"code": "RC_OK", "memsModule": {"id": 1, "dac_cards": {
            "id": cards["id"], "state": {"temperature": 3150}}}}))
    sys.exit(0)

if scenario == "set_app_error" and operation == "RT_SET":
    print(json.dumps({"code": "RC_OPERATION_FAILED"}))
    sys.exit(0)

if scenario == "per_id_error" and operation == "RT_GET" and entity_id == 1:
    print(json.dumps({"code": "RC_OPERATION_FAILED"}))
    sys.exit(0)

if operation == "RT_SET":
    print(json.dumps({"cid": "123"}))
else:
    value = os.environ.get("MOCK_VALUE", "LC_ON")
    leds = [{"id": led_id, "config": {"control": {"value": value}}}]
    key = {"cv": "common_voltage_card", "hvdac": "dac_cards"}.get(led_component, led_component)
    item = {key: [{"id": led_fru["id"], "led": leds}]}
    if key in ("common_voltage_card", "dac_cards"):
        item = {"mems_module": [{"id": 1, **item}]}
    print(json.dumps({"cid": "123", "response_payload": [{"code": "RC_OK", "payload": [item]}]}))
