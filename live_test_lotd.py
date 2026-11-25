#!/usr/bin/env python3
"""Run opt-in live LOTD smoke checks against a reachable unit.

GET checks are read-only. Optional SET checks repeat captured values only and
require an explicit interactive confirmation.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass

from lotd_client import canonical_model
from lotd import mirror_valid


@dataclass(frozen=True)
class Check:
    name: str
    arguments: tuple[str, ...]
    expected_text: tuple[str, ...]


@dataclass(frozen=True)
class SetCheck:
    name: str
    arguments: tuple[str, ...]
    baseline: str


def _hvdac_scope(model: str, cards: str | None) -> tuple[str, str, str]:
    """Return representative card, LED selector and output key without discovery."""
    if cards is None:
        return "1", "all", "All HVDAC power and fault LEDs"
    if not re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)*", cards):
        raise ValueError("--hvdac-cards requires comma-separated card IDs, for example 5,8")
    ids = cards.split(",")
    maximum = 11 if canonical_model(model) == "R320" else 10
    if any(len(value) > 2 or int(value) > maximum for value in ids):
        raise ValueError(f"HVDAC card IDs must be 1-{maximum} for {model}")
    if len(ids) != len(set(ids)):
        raise ValueError("--hvdac-cards must not contain duplicate IDs")
    return ids[0], cards, "Selected HVDAC power and fault LEDs"


def _mirror_scope(model: str, ports: str | None) -> tuple[str, str, str, list[str]]:
    if ports is None:
        return "1", "321", "1-3", ["1", "2", "3"]
    if not re.fullmatch(r"[1-9][0-9]{0,2}(?:,[1-9][0-9]{0,2})*", ports):
        raise ValueError("--mirror-ports requires comma-separated mirror IDs, for example 500 or 500,501")
    ids = ports.split(",")
    if len(ids) != len(set(ids)):
        raise ValueError("--mirror-ports must not contain duplicate IDs")
    layout = {"R300": "generic", "R300-G": "r300g", "R300-L": "r300l", "R320": "r320"}[canonical_model(model)]
    if any(not mirror_valid(int(value), layout) for value in ids):
        raise ValueError(f"--mirror-ports contains an invalid internal mirror ID for {canonical_model(model)}")
    return ids[0], ids[0], ports, ids


def checks_for(model: str, skip_lds: bool, *, include_lds_inventory: bool = False,
               hvdac_cards: str | None = None, mirror_ports: str | None = None) -> list[Check]:
    card, led_selector, led_name = _hvdac_scope(model, hvdac_cards)
    mirror_x, mirror_y, mirror_selector, mirrors = _mirror_scope(model, mirror_ports)
    mirror_label = f"{'port' if len(mirrors) == 1 else 'ports'} {mirror_selector}"
    mirror_success = f"Successful IDs ({len(mirrors)}): {','.join(mirrors)}"
    model_args = ("--model", model)

    def command(name: str, args: tuple[str, ...], expected: tuple[str, ...]) -> Check:
        return Check(name, args + model_args, expected)

    result = [
        command("Shelf power and fault LEDs", ("get", "shelf", "led"),
                ("Power LED (ID 1):", "Fault LED (ID 2):", "Outcome:        Completed successfully")),
        command("Midplane power and fault LEDs", ("get", "midplane", "led", "all"),
                ("Power LED (ID 1):", "Fault LED (ID 2):", "Outcome:        Completed successfully")),
        command("CV power and fault LEDs", ("get", "cv", "led"),
                ("Power LED (ID 1):", "Fault LED (ID 2):", "Outcome:        Completed successfully")),
        command("Shelf controller state", ("get", "shelf", "state"),
                ("Returned state fields:", "Outcome:        Completed successfully")),
        command("Shelf controller inventory", ("get", "shelf", "inventory"),
                ("Returned inventory fields:", "Outcome:        Completed successfully")),
        command("Reset Manager state", ("get", "reset-manager", "state"),
                ("Reset Manager 1 state:", "Last reset reason:", "Outcome:        Completed successfully")),
        command("Reset alias last reason", ("get", "reset", "last-reset-reason"),
                ("Last reset reason:", "Outcome:        Completed successfully")),
        command("Midplane state", ("get", "midplane", "state"),
                ("Returned state fields:", "Outcome:        Completed successfully")),
        command("Midplane inventory", ("get", "midplane", "inventory"),
                ("Returned inventory fields:", "Outcome:        Completed successfully")),
        *[command(f"{component} MAC addresses", ("get", component, "mac-addresses"),
                  ("MAC addresses:", "Outcome:        Completed successfully"))
          for component in ("shelf",)],
        *[command(f"{component} {load} firmware", ("get", component, "firmware-version", load),
                  (f"{load.capitalize()} firmware load version:", "Outcome:        Completed successfully"))
          for component in ("shelf",) for load in ("active", "inactive")],
        *[command(f"{component} temperature", ("get", component, "temperature"),
                  ("Temperature:", "Outcome:        Completed successfully"))
          for component in ("shelf", "midplane", "cv")],
        command("Shelf inlet temperature", ("get", "shelf", "inlet-temperature"),
                ("Inlet temperature:", "Outcome:        Completed successfully")),
        command("Shelf outlet temperature", ("get", "shelf", "outlet-temperature"),
                ("Outlet temperature:", "Outcome:        Completed successfully")),
        command("MEMS module state", ("get", "mems", "state"),
                ("Returned state fields:", "Outcome:        Completed successfully")),
        command("MEMS module inventory", ("get", "mems", "inventory"),
                ("MEMS module 1 inventory:", "Outcome:        Completed successfully")),
        command("MEMS sensors", ("get", "mems", "sensors"),
                ("Returned sensor IDs:", "Outcome:        Completed successfully")),
        command("MEMS correction temperature", ("get", "mems", "correction-temperature"),
                ("Correction temperature:", "Outcome:        Completed successfully")),
        command("MEMS sensor humidity" if model == "R320" else "MEMS relative humidity", ("get", "mems", "humidity"),
                ("Humidity:" if model == "R320" else "Relative humidity:", "Outcome:        Completed successfully")),
        command("Midplane sensor details", ("get", "midplane", "sensors"),
                ("Returned sensor IDs:", "Outcome:        Completed successfully")),
        command("CV sensor details", ("get", "cv", "sensors"),
                ("Returned sensor IDs:", "Outcome:        Completed successfully")),
        command("CV card state", ("get", "cv", "state"),
                ("Returned state fields:", "Outcome:        Completed successfully")),
        command("CV control configuration", ("get", "cv", "control"),
                ("CV control 1:", "  Configuration:", "Returned CV control configuration fields:",
                 "Outcome:        Completed successfully")),
        command("CV regulator voltages", ("get", "cv", "voltages"),
                ("CV regulator voltages:", "Regulator ID", "CV voltage", "Outcome:        Completed successfully")),
        command(f"HVDAC card {card} sensor details", ("get", "hvdac", card, "sensors"),
                ("Returned sensor IDs:", "Outcome:        Completed successfully")),
        command(led_name, ("get", "hvdac", led_selector, "led", "all"),
                ("Successful IDs (", "LED values (ID: power, fault):", "Outcome:        Completed successfully")),
        command(f"HVDAC card {card} presence", ("get", "hvdac", card, "presence"),
                ("Missing:", "Outcome:        Completed successfully")),
        command(f"HVDAC card {card} state", ("get", "hvdac", card, "state"),
                (f"HVDAC card {card} state:", "Missing:", "Outcome:        Completed successfully")),
        command(f"HVDAC card {card} inventory", ("get", "hvdac", card, "inventory"),
                (f"HVDAC card {card} inventory:", "Description:", "Outcome:        Completed successfully")),
        command("CV presence", ("get", "cv", "presence"),
                ("Missing:", "Outcome:        Completed successfully")),
        command("CV inventory", ("get", "cv", "inventory"),
                ("CV card 1 inventory:", "Returned inventory (ID: value):", "Outcome:        Completed successfully")),
        command("Power supply presence", ("get", "ps", "all", "presence"),
                ("Power supply 1 presence:", "Power supply 2 presence:", "Successful IDs (2): 1,2",
                 "Outcome:        Completed successfully")),
        command("Power supply 1 overview", ("get", "ps", "1", "overview"),
                ("Power supply 1 overview:", "Outcome:        Completed successfully")),
        command("All power supply overviews", ("get", "ps", "all", "overview"),
                ("Power supply 1 overview:", "Power supply 2 overview:",
                 "Successful IDs (2): 1,2", "Outcome:        Completed successfully")),
        command("Fan 1 overview", ("get", "fan", "1", "overview"),
                ("State:", "  Presence:", "Returned overview", "Outcome:        Completed successfully")),
        command("Fan 1 state", ("get", "fan", "1", "state"),
                ("Speed:", "Returned state", "Outcome:        Completed successfully")),
        command("Fan 1 configuration", ("get", "fan", "1", "config"),
                ("Control mode:", "Returned config", "Outcome:        Completed successfully")),
        command("Fan 1 control mode", ("get", "fan", "1", "control-mode"),
                ("Control mode:", "Outcome:        Completed successfully")),
        command("All fan actual PWM", ("get", "fan", "all", "pwm"),
                ("Fan ID", "PWM", "Successful IDs (3): 1,2,3", "Outcome:        Completed successfully")),
        command("All fan speeds", ("get", "fan", "all", "speed"),
                ("Successful IDs (3): 1,2,3", "Returned speed", "Outcome:        Completed successfully")),
        command("All fan presence", ("get", "fan", "all", "presence"),
                ("Successful IDs (3): 1,2,3", "Returned presence", "Outcome:        Completed successfully")),
        command("Fan 1 inventory", ("get", "fan", "1", "inventory"),
                ("Returned inventory", "Outcome:        Completed successfully")),
        command("Fan 1 temperature", ("get", "fan", "1", "temperature"),
                ("Temperature:", "Outcome:        Completed successfully")),
        command("Fan 1 sensors", ("get", "fan", "1", "sensors"),
                ("Sensor temp", "Returned sensors", "Outcome:        Completed successfully")),
        command("All fan power and fault LEDs", ("get", "fan", "all", "led", "all"),
                ("Successful IDs (3): 1,2,3", "LED values (ID: power, fault):", "Outcome:        Completed successfully")),
        command("HVDAC channel target and state", ("get", "hvdac", card, "channel", "1", "1"),
                ("Target voltage:", "Actual voltage:", "Out-of-range failure:", "Outcome:        Completed successfully")),
        command("HVDAC DAC count", ("get", "hvdac", card, "dac-count", "1", "1"),
                ("DAC count:", "Returned DAC count:", "Outcome:        Completed successfully")),
        command("HVDAC temperature", ("get", "hvdac", card, "temperature"),
                ("Temperature:", "Outcome:        Completed successfully")),
        command("CV channels 1-3", ("get", "cv", "channel", "1-3"),
                ("Successful IDs (3): 1,2,3", "Channel ID", "Target voltage", "Actual voltage", "Outcome:        Completed successfully")),
        command(f"Mirror {mirror_x} X tilt", ("get", "mirror", mirror_x, "x-tilt"),
                ("X target voltage:", f"Successful IDs (1): {mirror_x}", "Outcome:        Completed successfully")),
        command(f"Mirror {mirror_y} Y tilt", ("get", "mirror", mirror_y, "y-tilt"),
                ("Y target voltage:", f"Successful IDs (1): {mirror_y}", "Outcome:        Completed successfully")),
        command(f"Mirror {mirror_label} state", ("get", "mirror", mirror_selector, "state"),
                (*[f"Mirror port {port} state:" for port in mirrors],
                 mirror_success, "Outcome:        Completed successfully")),
        command(f"MEMS {mirror_label} configuration", ("get", "mems", mirror_selector, "config"),
                (*[f"Mirror port {port} configuration:" for port in mirrors],
                 mirror_success, "Outcome:        Completed successfully")),
        command("Connection Manager ports 1-3", ("get", "connection", "1-3"),
                ("Successful IDs (3): 1,2,3", "Peer mappings (port->peer):", "Optical blocking (port=value):", "Outcome:        Completed successfully")),
        command("MEMS inventory JSON", ("get", "mems", "inventory", "--json"),
                ("MEMS module 1 inventory:",)),
        command("All power supply states JSON", ("get", "ps", "all", "state", "--json"),
                ("Power supply 1 state:", "Power supply 2 state:")),
        command("All fan speeds JSON", ("get", "fan", "all", "speed", "--json"),
                ("Reading fan 1 speed", "Reading fan 2 speed", "Reading fan 3 speed")),
        command("CV control JSON", ("get", "cv", "control", "--json"),
                ("CV control 1:", "  Configuration:")),
        command("Mirror configuration JSON", ("get", "mems", mirror_selector, "config", "--json"),
                tuple(f"Mirror port {port} configuration:" for port in mirrors)),
        command("Reset reason JSON", ("get", "reset", "last-reset-reason", "--json"),
                ("Last reset reason:",)),
    ]
    if not skip_lds and model != "R300-L":
        result.insert(3, command("LDS presence", ("get", "lds", "presence"),
                                 ("Missing:", "Outcome:        Completed successfully")))
        result.insert(4, command("LDS power and fault LEDs", ("get", "lds", "led", "all"),
                                 ("Power LED (ID 1):", "Fault LED (ID 2):", "Outcome:        Completed successfully")))
        result.insert(5, command("LDS state", ("get", "lds", "state"),
                                 ("LDS card 1 state:", "Missing:", "Outcome:        Completed successfully")))
        if include_lds_inventory:
            result.insert(6, command("LDS inventory", ("get", "lds", "inventory"),
                                     ("LDS card 1 inventory:", "Outcome:        Completed successfully")))
    return result


def _number(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1) if match else None


def led_values_by_id(output: str) -> dict[int, dict[str, str]]:
    """Extract per-FRU LED values from the readable per-item GET output."""
    found: dict[int, dict[str, str]] = {}
    current_id: int | None = None
    for line in output.splitlines():
        marker = re.search(r"entity ID (\d+)", line)
        if marker:
            current_id = int(marker.group(1))
        value = re.search(r"(Power|Fault) LED \(ID [12]\):\s*(LC_(?:ON|OFF))", line)
        if current_id is not None and value:
            found.setdefault(current_id, {})[value.group(1).lower()] = value.group(2)
    return found


def _set_check(name: str, args: tuple[str, ...], baseline: str) -> SetCheck:
    return SetCheck(name, args, baseline)


def set_checks_for(model: str, skip_lds: bool, outputs: dict[str, str], *,
                   hvdac_cards: str | None = None) -> tuple[list[SetCheck], list[str]]:
    """Build same-value setters only from successful GET output snapshots."""
    card, _, led_name = _hvdac_scope(model, hvdac_cards)
    checks: list[SetCheck] = []
    skipped: list[str] = []
    components = [
        ("shelf", "Shelf power and fault LEDs", 1),
        ("midplane", "Midplane power and fault LEDs", 2),
        ("cv", "CV power and fault LEDs", 1),
        ("hvdac", led_name, int(card)),
        ("fan", "All fan power and fault LEDs", 1),
    ]
    if not skip_lds and model != "R300-L":
        components.append(("lds", "LDS power and fault LEDs", 1))
    for component, source_name, entity_id in components:
        values = led_values_by_id(outputs.get(source_name, "")).get(entity_id, {})
        selector = (str(entity_id),) if component in ("hvdac", "fan") else ()
        for led_name in ("power", "fault"):
            state = values.get(led_name)
            if state is None:
                skipped.append(f"{component} {led_name} LED: no successful baseline GET value")
                continue
            state_arg = "on" if state == "LC_ON" else "off"
            checks.append(_set_check(
                f"{component} {led_name} LED remains {state}",
                ("set", component, *selector, "led", led_name, state_arg, "--model", model),
                state,
            ))

    if canonical_model(model) == "R320":
        skipped.append("R320 HVDAC voltage/DAC-count SET checks: excluded after a same-count write changed the reported voltage target; coupled baseline preservation is unconfirmed")
    else:
        hvdac_target = _number(r"^Target voltage:\s*(-?\d+(?:\.\d+)?)$", outputs.get("HVDAC channel target and state", ""))
        if hvdac_target is not None:
            checks.append(_set_check("HVDAC channel target remains unchanged",
                                     ("set", "hvdac", card, "channel", "1", "1", hvdac_target, "--model", model), hvdac_target))
        else:
            skipped.append("HVDAC channel target: no successful baseline GET value")

        hvdac_dac_count = _number(r"^DAC count:\s*(\d+)$", outputs.get("HVDAC DAC count", ""))
        if hvdac_dac_count is not None:
            checks.append(_set_check("HVDAC DAC count remains unchanged",
                                     ("set", "hvdac", card, "dac-count", "1", "1", hvdac_dac_count, "--model", model), hvdac_dac_count))
        else:
            skipped.append("HVDAC DAC count: no successful baseline GET value")

    cv_output = outputs.get("CV channels 1-3", "")
    cv_match = re.search(r"^\s*1\s*\|\s*(-?\d+(?:\.\d+)?)\s*\|\s*-?\d+(?:\.\d+)?\s*\|\s*Read OK\s*$", cv_output, re.MULTILINE)
    if cv_match is None:
        cv_match = re.search(r"\[1/3\] Reading CV channel 1[^\n]*\nTarget voltage:\s*(-?\d+(?:\.\d+)?)", cv_output)
    if cv_match:
        target = cv_match.group(1)
        checks.append(_set_check("CV channel 1 target remains unchanged",
                                 ("set", "cv", "channel", "1", target, "--model", model), target))
    else:
        skipped.append("CV channel 1 target: no successful baseline GET value")

    if any(re.fullmatch(r"Mirror [0-9]+ [XY] tilt", name) for name in outputs):
        skipped.append("mirror SET checks: excluded pending confirmation of accepted target ranges and port-side behavior")

    connection_output = outputs.get("Connection Manager ports 1-3", "")
    port_three = re.search(r"Reading Connection Manager state for port 3[^\n]*\nPeer port number:\s*(\d+)\nOptical blocking enable:\s*(true|false)", connection_output, re.IGNORECASE)
    if port_three:
        peer, blocking = port_three.group(1), port_three.group(2).lower()
        if peer == "0":
            checks.append(_set_check("Connection port 3 remains disconnected",
                                     ("set", "connection", "3", "peer", "disconnect", "--model", model), "0"))
        else:
            skipped.append(f"connection port 3 peer: currently mapped to {peer}; skipped to avoid changing an active connection")
        checks.append(_set_check("Connection port 3 blocking state remains unchanged",
                                 ("set", "connection", "3", "blocking", blocking, "--model", model), blocking))
    else:
        skipped.append("connection port 3: no successful baseline GET values")

    return checks, skipped


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live LOTD GET smoke checks, with optional guarded same-value SET checks.")
    parser.add_argument("device_ip", help="unit IPv4 address or hostname")
    parser.add_argument("--model", type=canonical_model, choices=("R300", "R300-G", "R300-L", "R320"), default="R300",
                        help="unit model, case-insensitive; R300G/R300L alias R300-G/R300-L (default: R300)")
    parser.add_argument("--skip-lds", action="store_true", help="skip LDS checks if this unit has no LDS card")
    parser.add_argument("--hvdac-cards", metavar="IDS",
                        help="comma-separated installed cards, e.g. 5,8; LEDs use all listed IDs, other HVDAC checks use the first (default: card 1, all LEDs)")
    parser.add_argument("--mirror-ports", metavar="IDS",
                        help="comma-separated available internal mirror IDs, e.g. 500; state/config checks use all listed IDs, X/Y tilt use the first (default: 1-3 state/config, X=1, Y=321); GET checks only")
    parser.add_argument("--include-lds-inventory", action="store_true",
                        help="include LDS inventory diagnostic; known RC_OPERATION_FAILED replies count as failures")
    parser.add_argument("--include-set", action="store_true",
                        help="after GETs, offer same-value SET/readback checks; excludes R320 HVDAC voltage/DAC-count writes; requires an interactive terminal and confirmation")
    parser.add_argument("--lotd-script", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().with_name("lotd.py"),
                        help="path to lotd.py (default: beside this runner)")
    args = parser.parse_args(argv)
    try:
        _hvdac_scope(args.model, args.hvdac_cards)
        _mirror_scope(args.model, args.mirror_ports)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def json_report_error(stdout: str) -> str | None:
    """Validate successful GET reports without assuming readings or FRU presence."""
    def invalid_constant(value):
        raise ValueError(f"non-JSON constant {value}")

    try:
        report = json.loads(stdout, parse_constant=invalid_constant)
    except ValueError:
        return "stdout must contain exactly one valid JSON document"
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        return "expected a schema_version 1 report object"
    if type(report.get("exit_code")) is not int or report["exit_code"] != 0 or report.get("cancelled") is not False:
        return "report must indicate exit_code 0 and cancelled=false"
    if report.get("errors") != []:
        return "report contains errors or lacks an errors list"
    summary, results = report.get("summary"), report.get("results")
    if not isinstance(summary, dict) or any(
        not isinstance(summary.get(key), list) or any(not isinstance(value, str) for value in summary[key])
        for key in ("successful_ids", "failed_ids", "not_attempted_ids", "verified_ids")
    ):
        return "report must contain four summary ID lists of strings"
    if any(summary[key] for key in ("failed_ids", "not_attempted_ids", "verified_ids")):
        return "successful GET report has failed, pending or verified IDs"
    if not isinstance(results, list) or not results:
        return "report must contain GET results"
    for result in results:
        if (not isinstance(result, dict) or not isinstance(result.get("id"), str)
                or result.get("phase") != "RT_GET" or result.get("operation") != "RT_GET"
                or result.get("item_status") != "successful" or result.get("error") is not None
                or result.get("accepted") is not None or result.get("verified") is not False
                or not isinstance(result.get("data"), (dict, list))):
            return "expected successful typed GET results without write acceptance or verification"
    if summary["successful_ids"] != list(dict.fromkeys(result["id"] for result in results)):
        return "summary successful IDs do not match the GET results"
    return None


def verified_set_result(result) -> bool:
    if result is None or result.returncode != 0:
        return False
    output = result.stdout + result.stderr
    return "Verified:" in output and "Outcome:        Completed successfully" in output and "Cancelled;" not in output


def _run(script: pathlib.Path, device_ip: str, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(script), device_ip, *arguments]
    print("$ " + shlex.join(command))
    return subprocess.run(command, text=True, capture_output=True, timeout=30)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    script = args.lotd_script.resolve()
    if not script.is_file():
        print(f"Error: LOTD CLI script not found: {script}", file=sys.stderr)
        return 2

    suite = checks_for(args.model, args.skip_lds, include_lds_inventory=args.include_lds_inventory,
                       hvdac_cards=args.hvdac_cards, mirror_ports=args.mirror_ports)
    passed = 0
    failed = 0
    outputs: dict[str, str] = {}
    print(f"Running {len(suite)} read-only LOTD GET checks against {args.device_ip} (model {args.model})")
    print("No SET requests will be sent during this phase.\n")
    if args.hvdac_cards is not None:
        card, selector, _ = _hvdac_scope(args.model, args.hvdac_cards)
        print(f"HVDAC LED selection: {selector}; detailed/channel checks use card {card}.\n")
    if args.mirror_ports is not None:
        first, _, selector, _ = _mirror_scope(args.model, args.mirror_ports)
        print(f"Mirror state/config selection: {selector}; X/Y tilt checks use mirror {first}. No card mapping is inferred.\n")
    if not args.include_lds_inventory and not args.skip_lds and args.model != "R300-L":
        print("SKIP: LDS inventory has a known mixed RC_OPERATION_FAILED response; use --include-lds-inventory to diagnose it.\n")

    for index, check in enumerate(suite, start=1):
        print(f"[{index}/{len(suite)}] {check.name}")
        try:
            result = _run(script, args.device_ip, check.arguments)
        except subprocess.TimeoutExpired:
            failed += 1
            print("FAIL: command exceeded the 30-second smoke-check timeout.\n")
            break
        output = result.stdout + result.stderr
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        missing = [text for text in check.expected_text if text not in output]
        if "--json" in check.arguments:
            report_error = json_report_error(result.stdout)
            if report_error:
                missing.append("valid JSON GET report: " + report_error)
        if result.returncode == 0 and not missing:
            passed += 1
            outputs[check.name] = output
            print("CHECK: PASS\n")
            continue

        failed += 1
        if result.returncode != 0:
            print(f"CHECK: FAIL (lotd.py exited {result.returncode})")
        else:
            print("CHECK: FAIL (missing expected output: " + ", ".join(repr(x) for x in missing) + ")")
        print()
        if "context deadline exceeded" in output:
            print("Stopping after endpoint timeout; check the manufacturing container and port 3217.")
            break

    print(f"GET smoke checks: {passed} passed, {failed} failed, {len(suite)-passed-failed} not run.")
    if not args.include_set:
        return 1 if failed else 0
    if not sys.stdin.isatty():
        print("SET checks skipped: --include-set requires an interactive terminal for the final confirmation.")
        return 2 if failed == 0 else 1

    set_suite, skipped = set_checks_for(args.model, args.skip_lds, outputs, hvdac_cards=args.hvdac_cards)
    print("\nSame-value SET checks: each proposed target matches a value read during the GET phase.")
    for check in set_suite:
        print(f"  {check.name}: {check.baseline}")
    for reason in skipped:
        print(f"  SKIP: {reason}")
    if not set_suite:
        print("No SET checks have a safe baseline; nothing will be written.")
        return 1 if failed else 0
    try:
        answer = input(f"Proceed with these {len(set_suite)} same-value SET checks? [y/N] ").strip().lower()
    except KeyboardInterrupt:
        print("\nSET checks cancelled; no SET requests were sent.")
        return 130
    if answer not in ("y", "yes"):
        print("SET checks cancelled; no SET requests were sent.")
        return 1 if failed else 0

    set_passed = set_failed = 0
    print("\nRunning confirmed same-value SET checks.\n")
    for index, check in enumerate(set_suite, start=1):
        print(f"[{index}/{len(set_suite)}] {check.name}")
        try:
            result = _run(script, args.device_ip, check.arguments)
        except KeyboardInterrupt:
            print("\nInterrupted during a SET. Reapplying the captured baseline value...")
            try:
                restore = _run(script, args.device_ip, check.arguments)
                print((restore.stdout + restore.stderr).strip() or f"Baseline reapply exited {restore.returncode}.")
                if verified_set_result(restore):
                    print("Baseline value reapplied.")
                else:
                    print("WARNING: baseline reapply failed; inspect the unit state.")
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                print("WARNING: baseline reapply did not complete; inspect the unit state.")
            print(f"SET checks: {set_passed} passed, {set_failed + 1} failed/interrupted, {len(set_suite)-index} not run.")
            return 130
        except subprocess.TimeoutExpired:
            result = None

        output = (result.stdout + result.stderr) if result else ""
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        valid = verified_set_result(result)
        if valid:
            set_passed += 1
            print("CHECK: PASS\n")
            continue

        set_failed += 1
        if result is None:
            print("FAIL: SET exceeded the 30-second timeout.")
        else:
            print(f"FAIL: SET/readback did not verify successfully (exit {result.returncode}).")
        print("Reapplying the captured baseline value...")
        restored = False
        try:
            restore = _run(script, args.device_ip, check.arguments)
            if restore.stdout or restore.stderr:
                print((restore.stdout + restore.stderr).strip())
            if verified_set_result(restore):
                print("Baseline value reapplied.")
                restored = True
            else:
                print("WARNING: baseline reapply failed; inspect the unit state.")
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            print("WARNING: baseline reapply did not complete; inspect the unit state.")
        print()
        if result is not None and "context deadline exceeded" in output:
            print("Stopping after endpoint timeout; check the manufacturing container and port 3217.")
            break
        if not restored:
            print("Stopping SET checks because the baseline could not be verified or reapplied.")
            break

    print(f"SET checks: {set_passed} passed, {set_failed} failed, {len(set_suite)-set_passed-set_failed} not run.")
    return 1 if failed or set_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
