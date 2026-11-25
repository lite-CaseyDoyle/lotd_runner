#!/usr/bin/env python3
"""Command-line LOTD client. Requires Python 3 and grpcurl."""

from __future__ import annotations

import json
import math
from contextlib import redirect_stdout
import os
import re
import sys

from lotd_client import Client, Interrupted, LotdError, PORT, field_value, collection, id_summary, JsonReport, canonical_model, is_endpoint_failure, decimal_text


class UsageError(Exception):
    pass


CONTAINER_TYPES = (
    "CC_CARD_INVENTORY", "CC_MIDPLANE_INVENTORY", "CC_DEVICE_FIBER_CONFIGURATION",
    "CC_MEMS_CALIBRATION", "CC_HVDAC_INVENTORY", "CC_HVDAC_CALIBRATION",
    "CC_COMMON_VOLTAGE_INVENTORY", "CC_LDS_INVENTORY",
)


def valid_address(host: str) -> bool:
    if re.fullmatch(r"[0-9.]+", host):
        p=host.split(".")
        return len(p)==4 and all(re.fullmatch(r"0|[1-9][0-9]{0,2}",x) and int(x)<=255 for x in p)
    h=host[:-1] if host.endswith(".") else host
    if not h or len(h)>253 or ".." in h: return False
    return all(len(x)<=63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?",x) for x in h.split("."))


def expand(selector: str, maximum: int, what="ID") -> list[int]:
    if selector.lower()=="all": return list(range(1, maximum+1))
    ids=[]
    for part in selector.split(","):
        part=re.sub(r"\s", "", part)
        m=re.fullmatch(r"([0-9]+)-([0-9]+)",part)
        if m: first,last=map(int,m.groups())
        elif re.fullmatch(r"[0-9]+",part): first=last=int(part)
        else: raise UsageError(f"invalid {what} selector item: {part}")
        if first>last: raise UsageError(f"range start must not exceed range end: {part}")
        if first<1 or last>maximum: raise UsageError(f"{what} selector '{part}' is outside the valid range 1-{maximum}")
        ids.extend(x for x in range(first,last+1) if x not in ids)
    if not ids: raise UsageError(f"{what} selector is empty")
    return ids


def mirror_valid(i: int, layout: str) -> bool:
    ranges = {"r300g": [(1,148),(161,168),(181,320),(321,468),(481,488),(501,640)],
              "r300l": [(1,300),(321,620)], "r320": [(1,640)],
              "generic": [(1,148),(161,168),(181,300),(321,468),(481,488),(501,620)]}
    return any(a<=i<=b for a,b in ranges[layout])


def mirror_ids(selector: str, layout: str) -> list[int]:
    if selector == "all":
        if layout == "generic": raise UsageError("mirror port selector 'all' requires --model R300-G, R300-L, or R320")
        ranges={"r300g": [(1,148),(161,168),(181,320),(321,468),(481,488),(501,640)], "r300l": [(1,300),(321,620)], "r320": [(1,640)]}[layout]
        return [i for a,b in ranges for i in range(a,b+1)]
    out=[]
    for part in selector.split(","):
        part=re.sub(r"\s", "",part); m=re.fullmatch(r"(\d+)-(\d+)",part)
        if m: a,b=map(int,m.groups())
        elif part.isdigit(): a=b=int(part)
        else: raise UsageError("invalid mirror port selector item: " + part)
        if a>b: raise UsageError("range start must not exceed range end: " + part)
        if b-a>639: raise UsageError("mirror port range is too large: " + part)
        for i in range(a,b+1):
            if i>640: raise UsageError(f"mirror port ID {i} is outside the internal LOTD port ID range 1-640; 10001-style numbers belong in peer targets")
            if not mirror_valid(i,layout):
                if layout=="generic": raise UsageError(f"mirror port ID {i} is not valid on all R300 variants; use --model R300-G or R300-L")
                raise UsageError(f"mirror port ID {i} is not valid for {layout.upper()}")
            if i not in out: out.append(i)
    return out


def format_table(headers, rows):
    rendered = [[str(value) for value in row] for row in rows]
    widths = [max([len(header)] + [len(row[index]) for row in rendered]) for index, header in enumerate(headers)]
    def line(row):
        return " | ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
    return "\n".join([line(headers), "-+-".join("-" * width for width in widths)] + [line(row) for row in rendered])


def print_read_table(c, title, headers, rows, pending=()):
    if c.report is None and (rows or pending):
        remaining = [[item_id] + ["-"] * (len(headers) - 2) + ["Not attempted"] for item_id in pending]
        print(title + ":")
        print(format_table(headers, rows + remaining))


def summarize_items(items: list[str], limit=8) -> str:
    text=", ".join(items[:limit])
    if len(items)>limit: text += f"; ... and {len(items)-limit} more (see per-item output)"
    return text


def usage() -> None:
    print("""Usage (append --json to any device command for machine-readable stdout):
  lotd.py help [led|channel|dac-count|temp|sensors|identity|inventory|output|json|status|reset-manager|reset|container|hvdac|cv|lds|mirror|mems|connection|fan|ps|psu]
  lotd.py <device-ip> dump container <container-type> <instance> <filename>
  lotd.py <device-ip> import container <container-type> <instance> <filename>
  lotd.py <device-ip> get ps <ids> <overview|state|inventory|presence|temp|temperature|sensors> [exact-sensor-id for sensors only]
  lotd.py <device-ip> get <reset-manager|reset> <state|last-reset-reason>
  lotd.py <device-ip> get <component> led [<power|fault|both|all>]
  lotd.py <device-ip> set <component> led <power|fault|both|all> <on|off>
  lotd.py <device-ip> get <hvdac|fan> <ids> led [<power|fault|both|all>] [--model R300|R320]
  lotd.py <device-ip> set <hvdac|fan> <ids> led <power|fault|both|all> <on|off> [--model R300|R320]
  lotd.py <device-ip> get hvdac <card-id> channel <chip-id> <channel-id> [--model R320]
  lotd.py <device-ip> set hvdac <card-id> channel <chip-id> <channel-id> <target> [--model R320]
  lotd.py <device-ip> get hvdac <card-id> dac-count <chip-id> <channel-id> [--model R320]
  lotd.py <device-ip> set hvdac <card-id> dac-count <chip-id> <channel-id> <count> [--model R320]
  lotd.py <device-ip> get cv channel <ids> [--model R300|R320]
  lotd.py <device-ip> set cv channel <ids> <target> [--model R300|R320]
  lotd.py <device-ip> get <mirror|mems> <port-ids> <state|config|x-tilt|y-tilt> [--model R300-G|R300-L|R320]
  lotd.py <device-ip> set <mirror|mems> <port-ids> <x-tilt|y-tilt> <target> [--model R300-G|R300-L|R320]
  lotd.py <device-ip> get connection <port-ids> [--model R300-G|R300-L|R320]
  lotd.py <device-ip> set connection <port-ids> peer <south-port-id|disconnect> [--model R300-G|R300-L|R320]
  lotd.py <device-ip> set connection <port-ids> blocking <on|off> [--model R300-G|R300-L|R320]
  lotd.py <device-ip> get hvdac <card-id> <temp|temperature> [--model R320]
  lotd.py <device-ip> get <shelf|midplane> <state|inventory>
  lotd.py <device-ip> get shelf mac-addresses
  lotd.py <device-ip> get shelf firmware-version <active|inactive>
  lotd.py <device-ip> get <shelf|midplane|cv> <temp|temperature>
  lotd.py <device-ip> get shelf <inlet-temp|inlet-temperature|outlet-temp|outlet-temperature>
  lotd.py <device-ip> get hvdac <card-ids> <presence|inventory|state> [--model R320]
  lotd.py <device-ip> get cv <presence|inventory>
  lotd.py <device-ip> get lds <presence|state|inventory>
  lotd.py <device-ip> get mems <state|inventory|sensors|correction-temperature|humidity>
  lotd.py <device-ip> get mems temperature <exact-sensor-id>
  lotd.py <device-ip> get <cv|midplane> sensors [exact-sensor-id]
  lotd.py <device-ip> get hvdac <card-id> sensors [exact-sensor-id]
  lotd.py <device-ip> get cv <state|voltages>
  lotd.py <device-ip> get cv control
  lotd.py <device-ip> set cv control <dead-band <nonnegative-integer>|logging <on|off>>
  lotd.py <device-ip> set cv control <kinit-coeff|ki-coeff|filter-coeff|loop-interval|num-readings> <integer>
  lotd.py <device-ip> get fan <ids> <overview|state|config|control-mode|inventory|presence|speed|pwm|temp|temperature>
  lotd.py <device-ip> set fan <ids> pwm <0-100>
  lotd.py <device-ip> set fan <ids> control-mode <auto|manual <0-100>>
  lotd.py <device-ip> get fan <ids> sensors [exact-sensor-id]

Fixed-ID components:
  shelf       ID 1  Shelf controller
  midplane    ID 2  Midplane
  cv          ID 1  Common voltage card
  lds         ID 1  LDS card (not present on R300-L)

ID selectors accept one ID, comma-separated IDs, ranges, mixtures, or all:
  2  |  1,3,5  |  1-10  |  1-3,5-7,9  |  all

HVDAC cards: 1-10 on R300 (default), 1-11 on R320. Fans: 1-3.
HVDAC chips: 1-4; channels per chip: 1-32.
CV channels: 1-17 on R300, 1-89 on R320.
Mirror and Connection Manager ranges depend on model; use --model R300-G,
--model R300-L, or --model R320 when a layout-specific range is needed.
Model names are case-insensitive. R300G and R300L are aliases for R300-G and R300-L.

Multi-ID SETs preview changes and require interactive confirmation. Multi-ID
operations show progress and stop after an endpoint dial failure. Successful
GETs and SETs include a summary; failures and interrupted batches show partial
results. Use an IPv4 address or hostname. Port 3217 is fixed.
Python 3 and grpcurl are required. Keep lotd_client.py beside lotd.py.""")


def help_topic(topic: str) -> None:
    key=topic.lower()
    if key == "reset": key = "reset-manager"
    led="""LED commands:
  get <component> led [<power|fault|both|all>]
  set <component> led <power|fault|both|all> <on|off>
  get <hvdac|fan> <ids> led [<power|fault|both|all>]
  set <hvdac|fan> <ids> led <power|fault|both|all> <on|off>

LED IDs: power=1 (blue), fault=2 (amber). Fixed IDs are used for shelf,
midplane, CV, and LDS. HVDAC defaults to R300; use --model R320 when needed.
Use both or all (or omit the LED name on GET) to read both statuses per FRU.
SET with both or all applies the selected on/off state to both LEDs.
Multi-ID sets show a preview and require confirmation.
Multi-ID operations show progress and stop after an endpoint dial failure.

Examples:
  lotd.py 10.13.191.75 get midplane led power
  lotd.py 10.13.191.75 get shelf led
  lotd.py 10.13.191.75 set shelf led both on
  lotd.py 10.13.191.75 set hvdac 1-3 led power off
  lotd.py 10.13.191.75 get hvdac all led
  lotd.py 10.13.191.75 get fan all led fault"""
    channel="""HVDAC target-voltage commands:
  get hvdac <card-id> channel <chip-id> <channel-id> [--model R320]

HVDAC channel target SET:
  set hvdac <card-id> channel <chip-id> <channel-id> <target> [--model R320]

R300 has cards 1-10; R320 has cards 1-11. Each card has chips 1-4 and each
chip has channels 1-32. GET reads full config/state and reports target voltage,
actual voltage, the out-of-range failure flag, and DAC count when returned.
Missing DAC count is not replaced with zero. SET passes target_voltage to LOTD without unit
conversion; LOTD validates whether the target is supported.

Example:
  lotd.py 10.13.191.75 get hvdac 1 channel 2 7"""
    dac_count="""HVDAC DAC-count commands:
  get hvdac <card-id> dac-count <chip-id> <channel-id> [--model R320]
  set hvdac <card-id> dac-count <chip-id> <channel-id> <count> [--model R320]

DAC count is the integer DAC code, not a voltage. The script passes it to
LOTD without unit conversion; LOTD validates whether the code is supported.
R300 has cards 1-10; R320 has cards 1-11. Each card has chips 1-4 and each
chip has channels 1-32. SET reads back and verifies the configured code.
Count verification does not verify voltage-target preservation. A supplied R320
same-count write changed the reported voltage target; inspect both settings.

Examples:
  lotd.py 10.13.191.75 get hvdac 1 dac-count 1 1
  lotd.py 10.13.191.75 set hvdac 1 dac-count 1 1 100"""
    temp="""HVDAC temperature read:
  get hvdac <card-id> <temp|temperature> [--model R320]

R320 uses only the sensor path. Other models try the sensor path and fall back
to the legacy 3.x state path. It prints the raw LOTD value.

Example:
  lotd.py 10.13.191.75 get hvdac 1 temp"""
    identity = """Read-only shelf state and identity commands:
  get shelf state
  get shelf inventory
  get shelf mac-addresses
  get shelf firmware-version <active|inactive>

Shelf uses entity ID 1; midplane uses ID 2. Firmware load ID 1 is active;
load ID 2 is inactive. MACs are shown as interface/address pairs; firmware reads print only state.version.
Shelf MAC and firmware reads were confirmed on one unit. That unit returned
RC_UNKNOWN_FIELD for midplane MAC and firmware reads. Those unsupported commands
have been removed. Use get midplane inventory for midplane identity."""
    temp += """

Other read-only temperature commands:
  get cv <temp|temperature>
  get midplane <temp|temperature>
  get shelf <temp|temperature>
  get shelf <inlet-temp|inlet-temperature|outlet-temp|outlet-temperature>

CV and midplane use the named temp sensor. R320 shelf reads sensor IDs cpu-temp,
inlet-temp and outlet-temp; other models read shelf state fields. Full state reads
remain separate. Values are reported exactly as LOTD returns them, without conversion."""
    topics={
        "identity": identity, "shelf": identity + "\n\n" + temp,
        "midplane": "Midplane reads: get midplane state|inventory|temp|temperature; get midplane sensors [sensor-id].",
        "led":led,"channel":channel,"hvdac-channel":channel,"dac-count":dac_count,"dac_count":dac_count,"temp":temp,"temperature":temp,
        "inventory":"""Read-only inventory and presence commands:
  get shelf state|inventory
  get midplane state|inventory
  get hvdac <card-ids> presence|inventory|state [--model R320]
  get cv presence|inventory
  get cv <temp|temperature>
  get lds <presence|state|inventory>
  get fan <ids> <inventory|presence>
  get ps <ids> <inventory|presence>
  get mems inventory

HVDAC card selectors accept one ID, comma-separated IDs, ranges, or all.
R300 has cards 1-10; R320 has cards 1-11. Presence reads report LOTD's
state.missing field. Inventory requests the complete inventory object, including
card description. HVDAC state reads the complete card-level state: power/FPGA/
memory/communication failures, voltage rail failures, channel alarms, and unknown
returned fields. Flags and values remain raw; success is not a health verdict.

Examples:
  lotd.py 10.13.191.75 get shelf inventory
  lotd.py 10.13.191.75 get midplane state
  lotd.py 10.13.191.75 get hvdac 1-3 inventory
  lotd.py 10.13.191.75 get cv presence""",
        "status":"See help inventory, help identity, and help reset-manager.",
        "container":"""Container file actions (one target per command):
  dump container <container-type> <instance> <filename>
  import container <container-type> <instance> <filename>

Both require an interactive terminal and confirmation, including --json.
Use a bare filename of 1-255 ASCII letters/digits/dots/underscores/hyphens,
starting with a letter or digit. No directory paths. Files reside under /sftpmnt
on the unit; these commands do not transfer files to/from your computer.
Instance is an explicit integer 1-4294967295 (input guard, not a supported-instance
guarantee). There are no lists, ranges, all targets, or inferred card-slot mappings.
Use a new dump filename; overwrite behavior is unconfirmed. Dump exit 0 means
submission only: file creation remains unverified, JSON result is incomplete,
and successful_ids/verified_ids stay empty. Check the unit file separately.
Import writes the container with validate_after_write=true and requires a scoped
boolean response.success=true. It reports LOTD success, without independent
readback verification. No automatic retries, rollback, or validation bypass.
LDS inventory is unavailable on R300-L. MEMS burn-in is excluded.

Supported container types:\n  """ + "\n  ".join(CONTAINER_TYPES),
        "reset-manager":"""Read-only Reset Manager commands (fixed ID 1):
  get reset-manager state
  get reset-manager last-reset-reason

reset is an alias for reset-manager, including help reset. It only reads state.
Both read ResetManagerPayloadList / reset_manager ID 1 / state={}. State displays
all returned fields, including unknown fields. Last-reset-reason displays the raw
last_reset_reason.value enum, for example RR_POWER_ON. No enum meanings or health
judgments are inferred. SET and reset actions are not supported. JSON is supported.""",
        "hvdac":led+"\n\n"+channel+"\n\n"+dac_count+"\n\n"+temp+"\n\nSee help inventory for HVDAC presence, state, and inventory.",
        "cv":"""Common voltage (CV) channel commands:
  get cv channel <ids> [--model R300|R320]
  set cv channel <ids> <target> [--model R300|R320]
  get cv presence|inventory
  get cv control
  set cv control dead-band <nonnegative-integer>
  set cv control logging <on|off>
  set cv control <kinit-coeff|ki-coeff|filter-coeff|loop-interval|num-readings> <integer>
  get cv <temp|temperature>

R300 has CV channels 1-17; R320 has channels 1-89. GET reports configured
target and actual voltage. SET writes and verifies the target. Numeric values
are passed to LOTD without unit conversion. Select a single channel, a list,
range, mixture, or all. Multi-channel SETs require confirmation.

Examples:
  lotd.py 10.13.191.75 get cv channel 1
  lotd.py 10.13.191.75 get cv channel 1-3
  lotd.py 10.13.191.75 set cv channel 1-3 199
  lotd.py 10.13.191.75 get cv channel 89 --model R320
  lotd.py 10.13.191.75 get cv presence""",
        "mirror":"""Mirror read and tilt voltage commands (also accepts the keyword mems):
  get <mirror|mems> <port-ids> <state|config> [--model R300-G|R300-L|R320]
  get <mirror|mems> <port-ids> <x-tilt|y-tilt> [--model R300-G|R300-L|R320]
  set <mirror|mems> <port-ids> <x-tilt|y-tilt> <target> [--model R300-G|R300-L|R320]

State/config reads display complete returned sections, including status, actual
voltages, IL delta, and connection when present. No meanings or units are inferred.
Each mirror port has x_tilt_voltage and y_tilt_voltage targets. Tilt GET reports
the configured target. SET writes and verifies it. Port IDs depend on model:
R300-G accepts 1-148,161-168,181-320 and 321-468,481-488,501-640;
R300-L accepts 1-300 and 321-620; R320 accepts 1-640. Mirror requests use
IDs 1-640; 10001-style numbers belong to peer targets in Connection Manager.
Use --model with all. Multi-port SETs require confirmation.

Examples:
  lotd.py 10.13.191.75 get mirror 1-3 x-tilt --model R320
  lotd.py 10.13.191.75 get mems 1-3 x-tilt --model R320
  lotd.py 10.13.191.75 set mirror 321 y-tilt 1000 --model R300-G""",
        "mirrors":"See help mirror.","tilt":"See help mirror.",
        "lds":"LDS reads: get lds <presence|state|inventory>. Not available on R300-L.\nInventory failures, including mixed RC_OPERATION_FAILED replies, remain failures; raw fragments are available with --json. See help led for LDS LEDs.",
        "connection":"""Connection Manager commands:
  get connection <port-ids> [--model R300-G|R300-L|R320]
  set connection <port-ids> peer <south-port-id|disconnect> [--model R300-G|R300-L|R320]
  set connection <port-ids> blocking <on|off> [--model R300-G|R300-L|R320]

Local port IDs use the LOTD internal 1-640 space and model-specific ranges.
GET reports peer_port_number and optical_blocking_enable. Peer SETs start
from north/A-side ports (1-320) and target south/B-side numbers (10001-10320).
Use disconnect to clear a peer. Multi-port SETs require confirmation.

Examples:
  lotd.py 10.13.191.75 get connection 1-3
  lotd.py 10.13.191.75 set connection 1 peer 10001
  lotd.py 10.13.191.75 set connection 1 blocking on""",
        "connections":"See help connection.","cm":"See help connection.",
        "fan":"""Fan read, control, and LED commands:
  get fan <ids> <overview|state|config|control-mode|inventory|presence|speed|pwm|temp|temperature>
  get fan <ids> sensors [exact-sensor-id]
  set fan <ids> pwm <0-100>
  set fan <ids> control-mode manual <0-100>
  set fan <ids> control-mode auto

Read commands use remote TCP device:3217. Overview reads all returned fields,
including configuration, LEDs, and sensors, using indented section headings
and ID-labeled list entries; it does not write settings. State
shows actual pwm/speed, temperature, and error flags. Presence reports returned
presence/missing booleans without inferring one from the other. Temperature and
speed are raw values without conversion. R320 temp/temperature reads sensor temp /
state.value; other models read state.temperature. Full state reads remain separate.
Sensor details include thresholds and
alarm flags. Inventory uses readable labels; missing fields are noted separately.
PWM reads show actual state.pwm. Config reads show configured targets and mode.
PWM writes set manual_pwm_target and FCM_MANUAL together; control-mode manual
requires the explicit target too. Auto writes FCM_AUTO only, preserving targets.
The CLI restricts manual targets to integers 0-100; LOTD may impose further limits.
Every fan control write requires interactive confirmation and verifies the
changed configuration fields, not RPM or actual PWM. JSON mode keeps these checks.
Record config before changes; restore the original manual target and mode afterward
when performing a development check. No automatic rollback is performed.
Existing LED writes remain available.

  get fan <ids> led [<power|fault|both|all>]
  set fan <ids> led <power|fault|both|all> <on|off>

Fan IDs are 1-3. Select one ID, a comma-separated list, a range, a mixture,
or all. Use both or all (or omit the LED name on GET) to read both LEDs.
SET with both or all updates and verifies power and fault. Multi-ID sets require confirmation.
Multi-ID operations show progress and stop after an endpoint dial failure.

Example:
  lotd.py 10.13.191.75 set fan all led power off""",
    }
    sensors = """Read-only sensor details and environmental reads:
  get mems state
  get mems inventory
  get mems sensors [exact-sensor-id]
  get mems <temp|temperature> <exact-sensor-id>
  get mems <correction-temp|correction-temperature>
  get mems humidity
  get midplane sensors [exact-sensor-id]
  get cv sensors [exact-sensor-id]
  get hvdac <card-id> sensors [exact-sensor-id] [--model R300|R320]
  get cv state
  get cv voltages
  get cv control

Sensors show value, returned alarm/failure flags, thresholds/hysteresis, and
inventory descriptions. Omitting the sensor ID lists all returned sensors.
Use exact IDs from that list: R300-L examples use A-mems-temp/B-mems-temp;
R300-G examples use north-mems-temp/south-mems-temp. Other IDs include
ob-temp and humidity. No automatic translation between those names is made.
MEMS inventory reads module ID 1 and shows all returned fields. The supplied unit
returns only a description; omitted identity fields are not invented. mirror is
an alias here too. Per-mirror inventory (get mems <port-id> inventory) is unsupported.
On R320, get mems humidity reads sensor humidity/state (value and returned flags),
because relative_humidity is reserved in the module-state schema. JSON identifies
the sensor with id/value/flags. Other models keep reading state.relative_humidity
and relative_humidity_failed. These sources are distinct; full state is unchanged.
The correction temperature is also a separate state value from ob-temp.
CV voltages lists cv_voltages regulator IDs and raw cv_voltage values; these
are distinct from channel IDs and their model-specific limits.
CV control reads config for control-loop ID 1 on MEMS module 1 / CV card 1.
It shows returned coefficients, dead band, logging, loop interval, number of
readings, and enable flag when present. Unknown fields are retained. No units
or supported write ranges are inferred from these values.
All values and flags are reported without conversion or pass/fail judgments.
Sensor commands are read-only. CV control writes:
  set cv control dead-band <nonnegative-integer>
  set cv control logging <on|off>
  set cv control <kinit-coeff|ki-coeff|filter-coeff|loop-interval|num-readings> <integer>
Each requires interactive confirmation and verifies only the changed
field with a GET. Integers are a CLI input constraint; firmware validates supported
ranges. No unit conversion, coefficient scaling, or interval units are inferred.
The enable field is read-only in this CLI. MEMS burn-in is excluded."""
    topics["ps"] = topics["psu"] = """Read-only power-supply commands (ps and psu are aliases):
  get ps <ids> <overview|state|inventory|presence|temp|temperature>
  get ps <ids> sensors [exact-sensor-id]

IDs 1-2 accept lists, ranges, mixtures, or all. Every command reads the selected
ps_module overview over remote TCP device:3217, then selects the requested data.
State must include a boolean missing flag. Values, status words, and flags stay
raw: there is no unit conversion, status-bit decoding, or health verdict.
On R320, temperature uses sensor temp/state.value from the same overview and JSON
data is {temperature: number}. Other models use state.read_temperature1 and keep
that JSON field name. No fallback or synthetic full-state fields are added.
Sensors show full named sensor details.
Inventory retains returned descriptions even when a supply is absent.

A scoped RC_FRU_MISSING fragment does not discard a valid present supply's data.
An absent supply is identified by successful state.missing=true; missing-FRU
placeholders are excluded from parsed data. Overview/state show raw successful
fields with an unavailable-telemetry note. Temperature/sensor reads of an absent
supply fail as unavailable, never as a valid zero reading. Multi-ID temperature
reads use a complete table. Unknown fields are preserved. JSON is supported.
Power-supply writes and local Unix-socket transport are not implemented.

Examples:
  lotd.py <device-ip> get ps all presence
  lotd.py <device-ip> get ps 1 inventory
  lotd.py <device-ip> get ps 1 sensors temp
  lotd.py <device-ip> get ps all temperature --json"""
    topics["output"] = topics["json"] = """Output modes:
  lotd.py <device-ip> get fan all speed
  lotd.py <device-ip> get cv channel 1-3
  lotd.py <device-ip> get fan all speed --json

Human output uses compact tables for multi-ID numeric reads and CV regulator
voltages; detailed state/inventory/sensors use indented sections. Single numeric
reads keep labeled values. Read OK means the read succeeded, not a health verdict.
JSON mode writes one schema_version=1 object on stdout, with progress/errors and
interactive prompts on stderr. It includes summary ID lists, verified_ids,
per-request parsed data plus request/response diagnostics, and errors. IDs in
summary/results are strings. The summary contains only the four ID lists and
notes when present; command failures retain diagnostics in errors. Exit codes
remain 0 success/cancelled, 1 failure,
2 invalid usage, 130 interrupted. cancelled is explicit. Check item_status and
summary.successful_ids before consuming data: a response may parse but fail a
later command-specific validation. accepted=true acknowledges RT_SET only;
verified_ids requires matching readback. null acceptance means unknown/unconfirmed.
JSON does not bypass SET confirmations or permit noninteractive batch writes.
A recovered fallback may retain an earlier failed attempt in results while the
command succeeds. Raw response diagnostics retain firmware key names/fields;
RC_MISSING_VALUE placeholders are excluded from parsed data and noted in summary.
Use help output without --json; JSON mode applies to device commands only."""
    topics["sensors"] = topics["sensor"] = sensors
    topics["mems"] = topics["mirror"] = topics["mirror"] + "\n\n" + sensors
    for extra_topic in ("cv", "hvdac", "midplane"):
        topics[extra_topic] += "\n\n" + sensors
    if key not in topics: raise UsageError(f"Unknown help topic: {topic}\nRun ./lotd.py help for general help.")
    print(topics[key])


def fail(text: str, report=None) -> int:
    if report is not None:
        report.error(text)
    tty=sys.stderr.isatty() and not os.getenv("NO_COLOR")
    bold="\033[1m" if tty else ""; red="\033[31m" if tty else ""; cyan="\033[36m" if tty else ""; reset="\033[0m" if tty else ""
    print(f"{bold}{red}Error:{reset} {red}{text}{reset}",file=sys.stderr)
    print(f"{cyan}Run{reset} ./lotd.py --help for command syntax.",file=sys.stderr)
    return 2


def confirm(prompt: str, report=None, *, context="batch set") -> bool:
    if not sys.stdin.isatty(): raise UsageError(f"{context} requires an interactive terminal for confirmation")
    try: answer=input(prompt)
    except EOFError: answer=""
    except KeyboardInterrupt as exc: raise Interrupted from exc
    if answer.lower() in ("y","yes"): return True
    print("Cancelled; no changes sent.")
    if report is not None:
        report.cancelled = True
    return False


def numeric_target(v: str) -> bool:
    return bool(re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?",v))


def acknowledge_write(c: Client, raw: str) -> None:
    accepted = c.parse(raw, "status")
    print("Write accepted; verifying readback..." if accepted else
          "Write response has no status code; verifying readback...")


def led_body(comp: str, eid: int, led_id: int | None, kind: str, value: str | None) -> tuple[str,dict]:
    led = {} if led_id is None else {"id":led_id,"config":{"control":({"value":value} if kind=="RT_SET" else {})}}
    if comp in ("shelf","midplane"):
        return "EquipmentPackPayloadList", {"equipment_pack":{"id":1 if comp=="shelf" else 2,"led":led}}
    if comp=="cv": return "MemsModulePayloadList", {"memsModule":{"id":1,"common_voltage_card":{"id":1,"led":led}}}
    if comp=="lds": return "LdsCardPayloadList", {"lds_card":{"id":1,"led":led}}
    if comp=="hvdac": return "MemsModulePayloadList", {"memsModule":{"id":1,"dac_cards":{"id":eid,"led":led}}}
    return "FanControlPayloadList", {"fans_module":{"id":eid,"led":led}}


def main(argv: list[str]) -> int:
    if "--json" not in argv:
        return _main(argv)
    arguments = [argument for argument in argv if argument != "--json"]
    report = JsonReport(arguments)
    with redirect_stdout(sys.stderr):
        try:
            code = _main(arguments, report)
        except Exception as exc:
            report.error(str(exc), "internal")
            print(f"Error: {exc}", file=sys.stderr)
            code = 1
    print(json.dumps(report.finish(code), ensure_ascii=True, allow_nan=False))
    return code


def _main(argv: list[str], report=None) -> int:
    def reject(text):
        return fail(text, report)
    if report is not None and argv and argv[0].lower() in ("help", "--help", "-h"):
        return reject("--json requires a device command; use lotd.py help for human-readable help")
    if argv and argv[0].lower() in ("help","--help","-h"):
        if len(argv)==1: usage(); return 0
        if len(argv)==2:
            try: help_topic(argv[1]); return 0
            except UsageError as e: print(e,file=sys.stderr); return 2
        print("Help accepts at most one topic.\n",file=sys.stderr); usage(); return 2
    model="r300"; layout="generic"; model_specified=False; args=[]; i=0
    while i<len(argv):
        if argv[i]=="--model":
            if i+1>=len(argv): return reject("--model requires R300, R300-G, R300-L, or R320")
            model=canonical_model(argv[i+1]).lower(); model_specified=True; i+=2
        else: args.append(argv[i]); i+=1
    if model=="r300-g": model="r300"; layout="r300g"
    elif model=="r300-l": model="r300"; layout="r300l"
    elif model=="r320": layout="r320"
    elif model!="r300": return reject("model must be R300, R300-G, R300-L, or R320")
    if not 4<=len(args)<=8:
        if report is not None:
            report.error("Expected a device address, operation, component, and command")
        usage(); return 2
    host=args[0]; mode=args[1].lower(); comp=args[2].lower(); endpoint=f"{host}:{PORT}"
    if comp == "mems": comp = "mirror"
    if comp == "reset": comp = "reset-manager"
    if comp == "psu": comp = "ps"
    if not valid_address(host): return reject(f"device address must be a valid IPv4 address or hostname: {host}")
    if mode not in ("get","set","dump","import"): return reject("operation must be get, set, dump, or import")
    if mode in ("dump", "import") and comp != "container":
        return reject("dump/import require container: run lotd.py help container")
    if report is not None:
        report.model = {"r300g": "R300-G", "r300l": "R300-L", "r320": "R320", "generic": "R300"}[layout]
    client=Client(endpoint, report is None and sys.stderr.isatty() and not os.getenv("NO_COLOR"), report)
    try:
        return run(client,args,mode,comp,model,layout,model_specified)
    except UsageError as exc: return reject(str(exc))
    except LotdError as exc:
        if report is not None:
            report.error(str(exc), "command")
        print(f"Error: {exc}",file=sys.stderr); return 1
    except Interrupted:
        if report is not None:
            report.error("Interrupted (Ctrl+C)", "interrupted")
        print("\nInterrupted (Ctrl+C)",file=sys.stderr); return 130


def run(c: Client, a: list[str], mode: str, comp: str, model: str, layout: str, model_specified: bool) -> int:
    """Route channel commands before the remaining command families."""
    if comp == "hvdac" and len(a) > 4 and a[4].lower() in ("channel", "dac-count", "dac_count"):
        return run_hvdac_channel(c, a, mode, model)
    if comp == "cv" and len(a) > 3 and a[3].lower() == "channel":
        return run_cv_channels(c, a, mode, model)
    return run_other(c, a, mode, comp, model, layout, model_specified)


def format_hvdac_channel(values: dict[str, int | float | bool], dac_count: bool,
                         requested: str | None = None) -> str:
    """Format validated HVDAC values after any field-specific verification."""
    fields = [("dac_count", "DAC count")] if dac_count else [("target_voltage", "Target voltage")]
    if not dac_count and requested is None:
        fields.extend([("actual_voltage", "Actual voltage"), ("oor_failure", "Out-of-range failure")])
        if "dac_count" in values:
            fields.append(("dac_count", "DAC count"))
    lines = [f"{label}: {json.dumps(values[field])}" for field, label in fields]
    if requested is not None:
        detail = f"DAC count matches {int(requested)}" if dac_count else f"target voltage matches {decimal_text(requested)}"
        lines.append(f"Verified: {detail}")
    return "\n".join(lines)


def run_hvdac_channel(c: Client, a: list[str], mode: str, model: str) -> int:
    """Read or write one HVDAC voltage/DAC-count channel with readback."""
    end = c.endpoint
    dac_count = a[4].lower() in ("dac-count", "dac_count")
    setting=mode=="set"; expected=8 if setting else 7
    if len(a)!=expected:
        if dac_count:
            raise UsageError("HVDAC DAC-count set requires card, chip, channel, and integer count values" if setting else "HVDAC DAC-count get requires card, chip, and channel IDs")
        raise UsageError("HVDAC channel set requires card, chip, channel, and target values" if setting else "HVDAC channel get requires card, chip, and channel IDs")
    try: card,chip,ch=map(int,(a[3],a[5],a[6]))
    except ValueError: raise UsageError("HVDAC card, chip, and channel IDs must be numeric")
    target=a[7] if setting else None
    maxcard=11 if model=="r320" else 10
    if not 1<=card<=maxcard: raise UsageError(f"card ID must be 1-{maxcard} for {model.upper()}")
    if not 1<=chip<=4: raise UsageError("DAC chip ID must be 1-4")
    if not 1<=ch<=32: raise UsageError("channel ID must be 1-32")
    if setting and dac_count and not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", target): raise UsageError("DAC count must be an integer")
    if setting and not dac_count and not numeric_target(target): raise UsageError("target voltage must be an integer or decimal number")
    def req(kind, val=None, scope="full"):
        field="dac_count" if dac_count else "target_voltage"
        cfg={field:json_number(val)} if kind=="RT_SET" else {field:0}
        if kind == "RT_GET" and scope == "full" and not dac_count:
            cfg = {}
        channel={"id":ch,"config":cfg}
        if kind=="RT_GET" and scope!="target_only" and not dac_count: channel["state"]={}
        return c.request(kind,"MemsModulePayloadList",{"memsModule":{"id":1,"dac_cards":{"id":card,"dac_chips":[{"id":chip,"channels":[channel]}]}}})
    label="DAC count" if dac_count else "channel voltage"
    op=f"{mode} HVDAC card {card} chip {chip} channel {ch} {'DAC count' if dac_count else 'voltage'}"
    c.current_id = str(ch)
    try:
        if setting:
            set_label="DAC count" if dac_count else "target voltage"
            print(f"Setting HVDAC card {card}, DAC chip {chip}, channel {ch} {set_label} to {target} at {end}...")
            raw=req("RT_SET",target); acknowledge_write(c, raw)
            print("Reading back HVDAC DAC count..." if dac_count else "Reading back HVDAC channel target..."); raw=req("RT_GET",scope="target_only")
            res=c.parse(raw,"dac_count_set" if dac_count else "channel_set",str(card),str(chip),str(ch),target)
            print(format_hvdac_channel(res, dac_count, target)); c.summary("Completed successfully",f"{op} to {target} at {end}",id_summary([ch],verified=True)); return 0
        print(f"Reading HVDAC card {card}, DAC chip {chip}, channel {ch} {label} at {end}..." if dac_count else f"Reading HVDAC card {card}, DAC chip {chip}, channel {ch} at {end}...")
        raw=req("RT_GET"); res=c.parse(raw,"dac_count" if dac_count else "channel",str(card),str(chip),str(ch)); print(format_hvdac_channel(res, dac_count))
        if dac_count:
            value=json.dumps(res["dac_count"])
            c.summary("Completed successfully",f"{op} at {end}",id_summary([ch])+[f"Returned DAC count: {value}"]); return 0
        c.summary("Completed successfully",f"{op} at {end}",id_summary([ch])+[f"Returned values: target={json.dumps(res['target_voltage'])}, actual={json.dumps(res['actual_voltage'])}, out-of-range failure={json.dumps(res['oor_failure'])}"]); return 0
    except LotdError as e:
        c.record_failure(e)
        if setting:
            action="DAC count SET or readback" if dac_count else "target SET or readback"
            print(f"HVDAC {action} failed: {e}",file=sys.stderr)
        else:
            action="DAC count request" if dac_count else "channel request"
            print(f"HVDAC {action} failed: {e}",file=sys.stderr)
        c.summary("Completed with 1 failure",f"{op} at {end}",id_summary([], [ch])+[str(e)],1); return 1


def format_cv_channel(values: dict[str, int | float], requested: str | None = None) -> str:
    """Format validated CV values after parsing and any readback verification."""
    lines = [f"Target voltage: {json.dumps(values['target_voltage'])}"]
    if requested is None:
        lines.append(f"Actual voltage: {json.dumps(values['actual_voltage'])}")
    else:
        lines.append(f"Verified: target voltage matches {decimal_text(requested)}")
    return "\n".join(lines)


def run_cv_channels(c: Client, a: list[str], mode: str, model: str) -> int:
    """Read or write selected CV voltage channels, preserving batch results."""
    end = c.endpoint
    setting=mode=="set"; expected=6 if setting else 5
    if len(a)!=expected: raise UsageError("CV channel set requires a channel selector and target value" if setting else "CV channel get requires a channel selector")
    maxch=89 if model=="r320" else 17; selector=a[4].lower(); ids=expand(selector,maxch,"CV channel")
    target=a[5] if setting else None
    if setting and not numeric_target(target): raise UsageError("target voltage must be an integer or decimal number")
    if setting and len(ids)>1:
        print(f"Batch set preview: CV channels {','.join(map(str,ids))} target voltage will be set to {target} at {end}.")
        if not confirm(f"Proceed with these {len(ids)} channel changes? [y/N] ", c.report): return 0
    ok=[]; bad=[]; vals=[]; failures=[]; pending=[]; stopped=False
    rows=[]
    table_headers=["Channel ID", "Target voltage", "Actual voltage", "Read result"]
    op=f"{mode} CV channel {selector if len(ids)>1 else ids[0]} voltage"+(f" to {target}" if setting else "")+f" at {end}"
    for ix,ch in enumerate(ids):
        c.current_id=str(ch); c.current_phase="RT_SET" if setting else "RT_GET"
        prog=f"[{ix+1}/{len(ids)}] " if len(ids)>1 else ""
        def req(kind, scope="full"):
            channel={"id":ch,"config":{"target_voltage":json_number(target)} if kind=="RT_SET" else {"target_voltage":0}}
            if kind=="RT_GET" and scope!="target_only": channel["state"]={"actual_voltage":0}
            return c.request(kind,"MemsModulePayloadList",{"memsModule":{"id":1,"common_voltage_card":{"id":1,"channels":[channel]}}})
        try:
            if setting:
                print(f"{prog}Setting CV channel {ch} target voltage to {target} at {end}...")
                raw=req("RT_SET"); acknowledge_write(c, raw)
                c.current_phase="readback"; print(f"{prog}Reading back CV channel target...")
                res=c.parse(req("RT_GET","target_only"),"cv_channel_set",str(ch),target)
            else:
                print(f"{prog}Reading CV channel {ch} at {end}..."); res=c.parse(req("RT_GET"),"cv_channel",str(ch))
            if setting or len(ids) == 1:
                print(format_cv_channel(res, target if setting else None))
            ok.append(ch)
            if not setting:
                target_text = json.dumps(res["target_voltage"])
                actual_text = json.dumps(res["actual_voltage"])
                vals.append(f"{ch}: {target_text}, {actual_text}")
                rows.append([ch, target_text, actual_text, "Read OK"])
        except LotdError as e:
            c.record_failure(e)
            if not setting:
                rows.append([ch, "-", "-", "Failed"])
            bad.append(ch); failures.append(f"Channel {ch}: {e}"); print(f"CV channel {ch} failed: {e}",file=sys.stderr)
            if is_endpoint_failure(e) and ix+1<len(ids): pending=ids[ix+1:]; stopped=True; break
        except Interrupted:
            pending=ids[ix+1:]
            if not setting and len(ids) > 1:
                rows.append([ch, "-", "-", "Interrupted"])
                print_read_table(c, "CV channel voltages", table_headers, rows, pending)
            details=id_summary(ok,bad,pending,verified=setting)+failures
            details.append((f"Interrupted during channel {ch} RT_SET; whether LOTD applied the target is unknown." if setting and c.current_phase=="RT_SET" else f"Interrupted during channel {ch} readback; RT_SET completed, but verification is incomplete." if setting else f"Interrupted during channel {ch} RT_GET."))
            c.summary("Interrupted (Ctrl+C)",op,details,1); return 130
        finally: c.current_id=None; c.current_phase=""
    if not setting and len(ids) > 1:
        print_read_table(c, "CV channel voltages", table_headers, rows, pending)
    details=id_summary(ok,bad,pending,verified=setting)+failures
    if vals:
        details.append(f"Returned values: {len(vals)} channels" if len(ids)>1 else "Returned values (channel: target, actual): "+summarize_items(vals))
    outcome="Stopped after endpoint connection failure" if stopped else (f"Completed with {len(bad)} failure(s)" if bad else "Completed successfully")
    c.summary(outcome,op,details,1 if bad else 0); return 1 if bad else 0


def json_number(v: str | None):
    if v is None: return None
    # Keep integer vs decimal JSON numbers, while validating before reaching here.
    if re.fullmatch(r"-?(0|[1-9][0-9]*)",v):
        return int(v)
    number = float(v)
    if not math.isfinite(number):
        raise UsageError("value cannot be represented as a finite JSON number")
    return number


def query_fru_section(c: Client, component: str, entity_id: int, section: str, *, full_state=False):
    if component in ("shelf", "midplane"):
        fru_id = 1 if component == "shelf" else 2
        payload_type = "EquipmentPackPayloadList"
        entity_key = "equipment_pack"
        request_section = {}
        body = {"equipment_pack": {"id": fru_id, section: request_section}}
    elif component == "hvdac":
        payload_type = "MemsModulePayloadList"
        entity_key = "dac_cards"
        request_section = {} if section == "inventory" or full_state else {"missing": False}
        body = {"memsModule": {"id": 1, "dac_cards": {"id": entity_id, section: request_section}}}
    elif component == "cv":
        payload_type = "MemsModulePayloadList"
        entity_key = "common_voltage_card"
        request_section = {"missing": False} if section == "state" else {}
        body = {"memsModule": {"id": 1, "common_voltage_card": {"id": 1, section: request_section}}}
    elif component == "lds":
        payload_type = "LdsCardPayloadList"
        entity_key = "lds_card"
        request_section = {} if section == "inventory" or full_state else {"missing": False}
        body = {"lds_card": {"id": 1, section: request_section}}
    else:
        raise UsageError(f"read-only status or inventory is not supported for {component}")
    raw = c.request("RT_GET", payload_type, body)
    return c.parse(raw, "fru_section", entity_key, str(entity_id), section, component)


def format_section(value, section="") -> str:
    return format_fields(value)


def format_read_section(title: str, rendered: str) -> str:
    return title + ":\n" + "\n".join("  " + line for line in rendered.splitlines())


def section_summary(section: str, value) -> str:
    if isinstance(value, dict):
        fields = ", ".join(str(key) for key in value) or "none returned"
        return f"Returned {section} fields: {fields}"
    return f"Returned {section}: {json.dumps(value)}"


FIELD_LABELS = {"serial_no": "Serial number", "serial_number": "Serial number",
                "hardware_rev": "Hardware revision", "marketing_pn": "Marketing part number",
                "internal_part_number": "Internal part number", "entity_description": "Description",
                "clei_code": "CLEI code", "pwm": "PWM", "pwm_target": "PWM target",
                "manual_pwm_target": "Manual PWM target", "eeprom_failure": "EEPROM failure",
                "config": "Configuration", "led": "LEDs", "sensor": "Sensors",
                "oper_status": "Operational status", "is_replaceable": "Replaceable",
                "pwr_error": "Power error", "oor_high": "Out-of-range high",
                "oor_low": "Out-of-range low", "oor_high_degrade": "Out-of-range high degrade",
                "oor_low_degrade": "Out-of-range low degrade", "oor_failure": "Out-of-range failure",
                "channel_oor_failure": "Channel out-of-range failure",
                "channel_oor_degrade": "Channel out-of-range degrade",
                "eeprom_memory_failure": "EEPROM memory failure", "sram_failure": "SRAM failure",
                "cpu_usage": "CPU usage", "cpu_usage_high": "CPU usage high",
                "loss_of_comms": "Loss of communications", "tpm_loss_of_comms": "TPM loss of communications",
                "kinit_coeff": "Kinit coefficient", "ki_coeff": "Ki coefficient",
                "filter_coeff": "Filter coefficient", "num_readings": "Number of readings",
                "ki_coeff_phase1": "Ki coefficient phase 1", "num_loop_phase1": "Number of loops phase 1",
                "enable": "Enabled", "fpga_failure": "FPGA failure",
                "loss_of_internal_comms": "Loss of internal communications",
                "loss_of_dac_comms": "Loss of DAC communications",
                "ps_failure": "Power supply failure", "laser_power_oor": "Laser power out of range",
                "il_delta": "IL delta", "x_tilt_voltage": "X tilt voltage", "y_tilt_voltage": "Y tilt voltage",
                "x_tilt_actual_voltage": "X tilt actual voltage", "y_tilt_actual_voltage": "Y tilt actual voltage"}
FIELD_LABELS.update({"involt_oor": "Input voltage out of range", "outvolt_oor": "Output voltage out of range",
    "outcurr_oor": "Output current out of range", "fan_fail": "Fan failure", "ps_error": "Power supply error",
    "status_cml": "Status CML", "status_vout": "Status VOUT", "status_iout": "Status IOUT",
    "status_vin": "Status VIN", "status_fans_1_2": "Status fans 1/2",
    "read_vin": "Input voltage reading", "read_iin": "Input current reading",
    "read_vout": "Output voltage reading", "read_iout": "Output current reading",
    "read_temperature1": "Temperature 1 reading", "read_fan_speed": "Fan speed reading",
    "read_pin": "Input power reading", "calculated_pout": "Calculated output power",
    "peak_pout": "Peak output power", "peak_vout": "Peak output voltage", "peak_iout": "Peak output current"})


def format_fields(value, indent=0) -> str:
    """Render nested objects and lists as labeled, indented sections."""
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return pad + "(empty object)"
        lines = []
        for key, item in value.items():
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
            label = FIELD_LABELS.get(snake, snake.replace("_", " ").capitalize())
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{label}:")
                lines.append(format_fields(item, indent + 2))
            else:
                rendered = "(empty object)" if item == {} else "(empty list)" if item == [] else item if isinstance(item, str) else json.dumps(item)
                lines.append(f"{pad}{label}: {rendered}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return pad + "(empty list)"
        lines = []
        for index, item in enumerate(value, 1):
            if isinstance(item, dict) and "id" in item:
                lines.append(f"{pad}ID {item['id']}:")
                fields = {key:field for key,field in item.items() if key != "id"}
                if fields:
                    lines.append(format_fields(fields, indent + 2))
            elif isinstance(item, (dict, list)):
                lines.append(f"{pad}Item {index}:")
                lines.append(format_fields(item, indent + 2))
            else:
                lines.append(pad + "- " + (item if isinstance(item, str) else json.dumps(item)))
        return "\n".join(lines)
    return pad + (value if isinstance(value, str) else json.dumps(value))


def format_mac_addresses(value) -> str:
    lines, seen = [], set()
    for entry in collection(value):
        if not isinstance(entry, dict) or "id" not in entry or str(entry["id"]) in seen:
            raise LotdError("Expected distinct MAC address entries with IDs")
        seen.add(str(entry["id"]))
        state, inventory = entry.get("state"), entry.get("inventory", {})
        address = field_value(state, "mac_address") if isinstance(state, dict) else None
        if not isinstance(address, str) or not address:
            raise LotdError("Response did not include a MAC address string")
        interface = field_value(inventory, "entity_description") if isinstance(inventory, dict) else None
        lines.append(f"{interface or 'ID ' + str(entry['id'])}: {address}")
    if not lines:
        raise LotdError("No MAC addresses returned")
    return "\n".join(lines)


def select_sensors(value, selected=None):
    sensors, seen = [], set()
    for sensor in collection(value):
        if not isinstance(sensor, dict) or not isinstance(sensor.get("id"), str) or sensor["id"] in seen:
            raise LotdError("Expected sensor entries with distinct string IDs")
        seen.add(sensor["id"])
        if selected is None or sensor["id"] == selected:
            sensors.append(sensor)
    if not sensors:
        available = ", ".join(str(sensor.get("id")) for sensor in collection(value) if isinstance(sensor, dict))
        raise LotdError(f"Sensor {selected!r} was not returned; available IDs: {available}")
    return sensors


def format_sensor_details(sensors):
    for sensor in sensors:
        state = sensor.get("state")
        require_numeric(state.get("value") if isinstance(state, dict) else None, "sensor value")
    return "\n\n".join(format_read_section("Sensor " + sensor["id"],
                        format_fields({k:v for k,v in sensor.items() if k != "id"})) for sensor in sensors)


def cv_control_config(value):
    matches = [item for item in collection(value)
               if isinstance(item, dict) and str(item.get("id")) == "1"]
    if len(matches) != 1:
        raise LotdError(f"Expected one CV control entry with ID 1; found {len(matches)}")
    config = field_value(matches[0], "config")
    if not isinstance(config, dict) or not config:
        raise LotdError("Response did not include a nonempty CV control config object")
    return config


def run_cv_control_set(c: Client, a: list[str]):
    integer_fields = ("kinit-coeff", "ki-coeff", "filter-coeff", "loop-interval", "num-readings")
    syntax = ("set cv control dead-band <nonnegative-integer> | set cv control logging <on|off> | "
              "set cv control <kinit-coeff|ki-coeff|filter-coeff|loop-interval|num-readings> <integer>")
    if len(a) != 6 or a[4].lower() not in ("dead-band", "logging", *integer_fields):
        raise UsageError("CV control set syntax: " + syntax)
    field = a[4].lower().replace("-", "_")
    if field != "logging":
        nonnegative = field == "dead_band"
        if not re.fullmatch(r"0|[1-9][0-9]*" if nonnegative else r"-?(0|[1-9][0-9]*)", a[5]):
            raise UsageError(f"{a[4].lower()} requires {'a nonnegative integer' if nonnegative else 'an integer'}; LOTD validates supported ranges")
        try:
            target = int(a[5])
        except ValueError:
            raise UsageError(f"{a[4].lower()} integer is too large to encode")
    else:
        if a[5].lower() not in ("on", "off"):
            raise UsageError("logging requires on or off")
        target = a[5].lower() == "on"
    label = FIELD_LABELS.get(field, field.replace("_", " ").capitalize())
    print(f"CV control write at {c.endpoint}: MEMS module 1 / CV card 1 / control-loop 1")
    print(f"  {label}: {json.dumps(target)}")
    if not confirm("Apply CV control change? [y/N] ", c.report, context="CV control set"):
        return 0
    operation = " ".join(a[1:]) + f" at {c.endpoint}"
    def request(kind, config):
        c.current_phase = kind
        return c.request(kind, "MemsModulePayloadList", {"mems_module": {"id": 1,
            "common_voltage_card": {"id": 1, "cv_control": [{"id": 1, "config": config}]}}})
    c.current_id = "1"
    accepted = None
    try:
        print(f"Writing CV control {label.lower()} at {c.endpoint}...", flush=True)
        accepted = c.parse(request("RT_SET", {field: target}), "write_status")
        print("Write accepted; verifying readback..." if accepted else
              "Write response has no status code; verifying readback...", flush=True)
        value = c.parse(request("RT_GET", {}), "read_section", "common_voltage_card", "1", "cv_control")
        config = cv_control_config(value)
        actual = field_value(config, field)
        c.capture_result({"id": 1, "config": config})
        if type(actual) is not type(target) or actual != target:
            raise LotdError(f"CV control readback mismatch for {field}: expected {json.dumps(target)}, got {json.dumps(actual)}")
        print("CV control 1:\n" + format_fields({"config": {field: actual}}, 2))
        c.summary("Completed successfully", operation, id_summary(["1"], verified=True))
        return 0
    except LotdError as exc:
        c.record_failure(exc)
        print(f"Unable to set CV control {label.lower()}: {exc}", file=sys.stderr)
        c.summary("Completed with 1 failure", operation, id_summary([], ["1"]) + [str(exc)], 1)
        return 1
    except Interrupted:
        if c.current_phase == "RT_SET":
            message = "Interrupted during RT_SET; whether LOTD applied the change is unknown."
        elif accepted:
            message = "Interrupted during readback; RT_SET was accepted but verification is incomplete."
        else:
            message = "Interrupted during readback after a CID-only write response; verification is incomplete."
        print(message, file=sys.stderr)
        if c.report is not None:
            c.report.error(message, "interrupted")
        c.summary("Interrupted (Ctrl+C)", operation, id_summary([]) + [message], 1)
        return 130
    finally:
        c.current_id, c.current_phase = None, ""


def run_read_details(c: Client, a: list[str], mode: str, comp: str, model: str):
    hvdac = comp == "hvdac"
    index = 4 if hvdac else 3
    if len(a) <= index:
        return None
    command = a[index].lower()
    mems = comp == "mirror"
    sensor_read = command == "sensors" and comp in ("midplane", "cv", "hvdac", "mirror")
    module_read = mems and command in ("state", "inventory", "correction-temp", "correction-temperature", "humidity", "temp", "temperature")
    cv_read = comp == "cv" and command in ("state", "voltages", "control")
    if not (sensor_read or module_read or cv_read):
        return None
    if mode != "get":
        raise UsageError(f"{command} is read-only")
    extra = a[index+1:]
    selected = None
    if sensor_read:
        if len(extra) > 1 or (extra and not extra[0].strip()):
            raise UsageError("sensors syntax: get mems|midplane|cv sensors [sensor-id], or get hvdac <card-id> sensors [sensor-id]")
        selected = extra[0] if extra else None
        section = "sensor"
    elif mems and command in ("temp", "temperature"):
        if len(extra) != 1 or not extra[0].strip():
            raise UsageError("MEMS temperature syntax: get mems temperature <exact-sensor-id>; use get mems sensors to list IDs")
        section, selected = "sensor", extra[0]
    else:
        if extra:
            raise UsageError(f"get {a[2]} {command} takes no extra arguments")
        section = {"voltages": "cv_voltages", "control": "cv_control", "inventory": "inventory"}.get(command, "state")
    if mems and command == "humidity" and model == "r320":
        section, selected = "sensor", "humidity"
    entity_id = 2 if comp == "midplane" else 1
    if hvdac:
        ids = expand(a[3], 11 if model == "r320" else 10, "HVDAC card")
        if len(ids) != 1:
            raise UsageError("sensor details require one HVDAC card ID")
        entity_id = ids[0]
    entity_key = "mems_module" if mems else ("equipment_pack" if comp == "midplane" else ("dac_cards" if hvdac else "common_voltage_card"))
    entity = {"id": entity_id, section: {}}
    if mems and command == "humidity" and model == "r320":
        entity[section] = [{"id": selected, "state": {}}]
    if section == "cv_control":
        entity[section] = [{"id": 1, "config": {}}]
    body = {entity_key: entity}
    payload_type = "EquipmentPackPayloadList" if comp == "midplane" else "MemsModulePayloadList"
    if comp in ("cv", "hvdac"):
        body = {"mems_module": {"id": 1, **body}}
    label = "Sensors" if sensor_read else ("Regulator voltages" if command == "voltages" else command.replace("-", " ").capitalize())
    operation = " ".join(a[1:]) + f" at {c.endpoint}"
    print(f"Reading {a[2]} {label.lower()} at {c.endpoint}...")
    c.current_id = str(entity_id)
    try:
        value = c.parse(c.request("RT_GET", payload_type, body), "read_section", entity_key, str(entity_id), section)
        if section == "sensor":
            sensors = select_sensors(value, selected)
            c.capture_result(sensors)
            if sensor_read:
                rendered = format_sensor_details(sensors)
                details = ["Returned sensor IDs: " + ", ".join(sensor["id"] for sensor in sensors)]
            else:
                state = sensors[0].get("state")
                number = state.get("value") if isinstance(state, dict) else None
                measurement = "Humidity" if command == "humidity" else "Temperature"
                require_numeric(number, "sensor " + measurement.lower())
                c.capture_result({"id": sensors[0]["id"], **state})
                rendered = measurement + ": " + json.dumps(number) + "\n" + format_fields({k:v for k,v in state.items() if k != "value"})
                details = [f"Returned {measurement.lower()}: {json.dumps(number)}"]
        elif section == "cv_control":
            config = cv_control_config(value)
            for key, setting in config.items():
                field = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
                if field in ("kinit_coeff", "ki_coeff", "dead_band", "filter_coeff", "loop_interval", "num_readings"):
                    require_numeric(setting, field)
                elif field in ("logging", "enable") and not isinstance(setting, bool):
                    raise LotdError(f"Response did not include boolean {field}")
            c.capture_result({"id": 1, "config": config})
            rendered = "CV control 1:\n" + format_fields({"config": config}, 2)
            details = [section_summary("CV control configuration", config)]
        elif section == "cv_voltages":
            lines, seen = [], set()
            for regulator in collection(value):
                if not isinstance(regulator, dict) or "id" not in regulator or str(regulator["id"]) in seen:
                    raise LotdError("Expected CV regulator entries with distinct IDs")
                seen.add(str(regulator["id"]))
                state = regulator.get("state")
                number = field_value(state, "cv_voltage") if isinstance(state, dict) else None
                require_numeric(number, "cv_voltage")
                lines.append([str(regulator["id"]), json.dumps(number)])
            rendered = "CV regulator voltages:\n" + format_table(["Regulator ID", "CV voltage"], lines)
            details = [f"Returned CV regulator voltages: {len(lines)}"]
        elif command in ("correction-temp", "correction-temperature", "humidity"):
            field = "relative_humidity" if command == "humidity" else "correction_temperature"
            number = field_value(value, field) if isinstance(value, dict) else None
            require_numeric(number, field)
            rendered = ("Relative humidity" if command == "humidity" else "Correction temperature") + ": " + json.dumps(number)
            failed = field_value(value, "relative_humidity_failed") if isinstance(value, dict) else None
            if command == "humidity" and failed is not None:
                rendered += "\nRelative humidity failed: " + json.dumps(failed)
            c.capture_result({field: number, **({"relative_humidity_failed": failed} if command == "humidity" and failed is not None else {})})
            details = ["Returned " + rendered]
        else:
            if not isinstance(value, dict):
                raise LotdError(f"Expected a {section} object")
            rendered = format_fields(value)
            details = [section_summary(section, value)]
        if sensor_read or command in ("state", "inventory"):
            title = "MEMS module 1" if mems else (f"HVDAC card {entity_id}" if hvdac else ("Midplane" if comp == "midplane" else "CV card 1"))
            rendered = format_read_section(f"{title} {label.lower()}", rendered)
        print(rendered)
        c.summary("Completed successfully", operation, id_summary([entity_id])+details)
        return 0
    except LotdError as e:
        c.record_failure(e)
        print(f"Unable to read {a[2]} {command}: {e}", file=sys.stderr)
        c.summary("Completed with 1 failure", operation, id_summary([], [entity_id])+[str(e)], 1)
        return 1


def require_numeric(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LotdError(f"Response did not include numeric {field}")


def run_ps_reads(c: Client, a: list[str], mode: str, model: str = "r300"):
    syntax = "get ps <ids> <overview|state|inventory|presence|temp|temperature|sensors> [exact-sensor-id for sensors only]"
    if mode != "get":
        raise UsageError("power-supply commands are read-only; " + syntax)
    if len(a) < 5 or a[4].lower() not in ("overview", "state", "inventory", "presence", "temp", "temperature", "sensors"):
        raise UsageError("Power-supply read syntax: " + syntax)
    command = a[4].lower()
    if len(a) != 5 and not (command == "sensors" and len(a) == 6 and a[5].strip()):
        raise UsageError("Power-supply read syntax: " + syntax)
    ids = expand(a[3].lower(), 2, "power supply")
    selected = a[5] if len(a) == 6 else None
    operation = " ".join(a[1:]) + f" at {c.endpoint}"
    ok, bad, errors, pending = [], [], [], []
    numeric_batch = command in ("temp", "temperature") and len(ids) > 1
    rows, headers = [], ["Power supply ID", "Temperature", "Read result"]
    unavailable_ids = set()
    def hidden_unavailable_notes():
        return [note for note in c.response_notes if any(
            note.startswith(f"Power supply {ps_id} is missing;") for ps_id in unavailable_ids)]
    for index, ps_id in enumerate(ids):
        c.current_id, c.current_phase = str(ps_id), "RT_GET"
        progress = f"[{index+1}/{len(ids)}] " if len(ids) > 1 else ""
        missing = None
        try:
            print(f"{progress}Reading power supply {ps_id} {command} at {c.endpoint}...", flush=True)
            entity = c.parse(c.request("RT_GET", "PsControlPayloadList", {"ps_module": {"id": ps_id}}),
                             "ps_entity", str(ps_id))
            state = field_value(entity, "state")
            missing = state["missing"]
            if missing and command in ("temp", "temperature", "sensors"):
                c.capture_result({"missing": True})
                unavailable_ids.add(ps_id)
                raise LotdError(f"{command.capitalize()} measurement is unavailable (supply missing)")
            if command == "overview":
                value = entity
                rendered = format_fields({key:item for key,item in entity.items() if key != "id"}, 2)
            elif command == "presence":
                value = {"missing": missing}
                rendered = format_fields(value, 2)
            elif command in ("temp", "temperature"):
                if model == "r320":
                    sensor = select_sensors(field_value(entity, "sensor"), "temp")[0]
                    sensor_state = field_value(sensor, "state")
                    number = field_value(sensor_state, "value") if isinstance(sensor_state, dict) else None
                    field = "temperature"
                else:
                    number, field = field_value(state, "read_temperature1"), "read_temperature1"
                require_numeric(number, field)
                value = {field: number}
                rendered = "  Temperature: " + json.dumps(number)
                rows.append([ps_id, json.dumps(number), "Read OK"])
            elif command == "sensors":
                value = select_sensors(field_value(entity, "sensor"), selected)
                rendered = format_sensor_details(value)
                rendered = "\n".join("  " + line for line in rendered.splitlines())
            else:
                value = state if command == "state" else field_value(entity, "inventory")
                if not isinstance(value, dict) or not value:
                    raise LotdError(f"Response did not include a nonempty power-supply {command} object")
                rendered = format_fields(value, 2)
            c.capture_result(value)
            if not numeric_batch:
                print(f"Power supply {ps_id} {command}:")
                if missing and command in ("overview", "state"):
                    print("  Power supply missing; telemetry unavailable. Raw returned fields follow.")
                print(rendered)
            ok.append(ps_id)
        except LotdError as exc:
            c.record_failure(exc)
            bad.append(ps_id)
            errors.append(f"Power supply {ps_id}: {exc}")
            if ps_id not in unavailable_ids or c.report is not None:
                print(f"Unable to read power supply {ps_id} {command}: {exc}", file=sys.stderr)
            if numeric_batch:
                rows.append([ps_id, "-", "Unavailable (missing)" if missing else "Failed"])
            if is_endpoint_failure(exc):
                pending = ids[index+1:]
                break
        except Interrupted:
            pending = ids[index+1:]
            message = f"Interrupted during power supply {ps_id} RT_GET."
            print(message, file=sys.stderr)
            if c.report is not None:
                c.report.error(message, "interrupted")
            if numeric_batch:
                rows.append([ps_id, "-", "Interrupted"])
                print_read_table(c, "Power supply temperatures", headers, rows, pending)
            c.summary("Interrupted (Ctrl+C)", operation, id_summary(ok, bad, pending) + errors + [message], 1,
                      hidden_notes=hidden_unavailable_notes())
            return 130
        finally:
            c.current_id, c.current_phase = None, ""
    if numeric_batch:
        print_read_table(c, "Power supply temperatures", headers, rows, pending)
    c.summary("Completed with failures" if bad else "Completed successfully", operation,
              id_summary(ok, bad, pending) + errors, 1 if bad else 0, hidden_notes=hidden_unavailable_notes())
    return 1 if bad else 0


def run_fan_set(c: Client, a: list[str]):
    command = a[4].lower()
    ids = expand(a[3].lower(), 3, "fan")
    syntax = "set fan <ids> pwm <0-100> | set fan <ids> control-mode auto | set fan <ids> control-mode manual <0-100>"
    manual = command == "pwm" or (len(a) > 5 and a[5].lower() == "manual")
    if command == "pwm" and len(a) == 6:
        target_text = a[5]
    elif command == "control-mode" and len(a) == 7 and a[5].lower() == "manual":
        target_text = a[6]
    elif command == "control-mode" and len(a) == 6 and a[5].lower() == "auto":
        target_text = None
    else:
        raise UsageError("Fan control set syntax: " + syntax)
    config = {"control_mode": {"value": "FCM_MANUAL" if manual else "FCM_AUTO"}}
    if manual:
        if not re.fullmatch(r"0|[1-9][0-9]?|100", target_text):
            raise UsageError("fan PWM target must be an integer from 0 to 100")
        config["manual_pwm_target"] = int(target_text)
    print(f"Fan control write at {c.endpoint}: IDs {','.join(map(str, ids))}")
    print(format_fields({"config": config}, 2))
    if not confirm("Apply fan control changes? [y/N] ", c.report, context="fan control set"):
        return 0
    operation = " ".join(a[1:]) + f" at {c.endpoint}"
    ok, bad, failures, pending = [], [], [], []
    for index, fan_id in enumerate(ids):
        c.current_id = str(fan_id)
        progress = f"[{index+1}/{len(ids)}] " if len(ids) > 1 else ""
        accepted = None
        def request(kind, fields):
            c.current_phase = kind
            return c.request(kind, "FanControlPayloadList", {"fans_module": {"id": fan_id, "config": fields}})
        try:
            print(f"{progress}Writing fan {fan_id} control at {c.endpoint}...", flush=True)
            accepted = c.parse(request("RT_SET", config), "write_status")
            print("Write accepted; verifying readback..." if accepted else
                  "Write response has no status code; verifying readback...", flush=True)
            actual = c.parse(request("RT_GET", {}), "read_section", "fans_module", str(fan_id), "config")
            if not isinstance(actual, dict):
                raise LotdError("Expected a fan configuration object")
            mode = field_value(actual, "control_mode")
            if not isinstance(mode, dict) or mode.get("value") != config["control_mode"]["value"]:
                raise LotdError("Fan control-mode readback mismatch")
            target = field_value(actual, "manual_pwm_target")
            if manual and (type(target) is not int or target != config["manual_pwm_target"]):
                raise LotdError("Fan manual PWM target readback mismatch")
            print(f"Fan {fan_id} configuration:\n" + format_fields(actual, 2))
            ok.append(fan_id)
        except LotdError as exc:
            c.record_failure(exc)
            bad.append(fan_id)
            failures.append(f"Fan {fan_id}: {exc}")
            print(f"Unable to set fan {fan_id} control: {exc}", file=sys.stderr)
            if is_endpoint_failure(exc):
                pending = ids[index+1:]
                break
        except Interrupted:
            pending = ids[index+1:]
            message = (f"Interrupted during fan {fan_id} RT_SET; whether LOTD applied the change is unknown."
                       if c.current_phase == "RT_SET" else
                       f"Interrupted during fan {fan_id} readback; verification is incomplete.")
            print(message, file=sys.stderr)
            if c.report is not None:
                c.report.error(message, "interrupted")
            c.summary("Interrupted (Ctrl+C)", operation, id_summary(ok, bad, pending, verified=True) + failures + [message], 1)
            return 130
        finally:
            c.current_id, c.current_phase = None, ""
    c.summary("Completed with failures" if bad else "Completed successfully", operation,
              id_summary(ok, bad, pending, verified=True) + failures, 1 if bad else 0)
    return 1 if bad else 0


def run_fan_reads(c: Client, a: list[str], mode: str, model: str = "r300"):
    commands = ("overview", "state", "config", "control-mode", "inventory", "presence", "speed", "pwm", "temp", "temperature", "sensors")
    if len(a) < 5 or a[4].lower() not in commands:
        return None
    command = a[4].lower()
    if mode != "get":
        raise UsageError(f"fan {command} is read-only")
    if len(a) != 5 and not (command == "sensors" and len(a) == 6 and a[5].strip()):
        raise UsageError("fan read syntax: get fan <ids> <overview|state|config|control-mode|inventory|presence|speed|pwm|temp|temperature|sensors> [sensor-id for sensors only]")
    ids = expand(a[3].lower(), 3, "fan")
    selected = a[5] if len(a) == 6 else None
    section = {"presence": "state", "speed": "state", "pwm": "state", "control-mode": "config", "temp": "state", "temperature": "state", "sensors": "sensor"}.get(command, command)
    sensor_temperature = model == "r320" and command in ("temp", "temperature")
    if sensor_temperature:
        section = "sensor"
    operation = " ".join(a[1:]) + f" at {c.endpoint}"
    successes, failures, values, pending = [], [], [], []
    numeric_batch = command in ("speed", "pwm", "temp", "temperature") and len(ids) > 1
    rows = []
    table_headers = ["Fan ID", {"speed": "Speed", "pwm": "PWM"}.get(command, "Temperature"), "Read result"]
    stopped = False

    def summary_details():
        details = id_summary(successes,[i for i,_ in failures],pending)
        details += [f"ID {i}: {error}" for i,error in failures]
        if values:
            details.append(f"Returned {command}: {len(values)} values" if numeric_batch else f"Returned {command} (ID: value): " + "; ".join(values))
        return details

    for index, fan_id in enumerate(ids):
        progress = f"[{index+1}/{len(ids)}] " if len(ids) > 1 else ""
        c.current_id, c.current_phase = str(fan_id), "RT_GET"
        try:
            print(f"{progress}Reading fan {fan_id} {command} at {c.endpoint}...")
            entity = {"id": fan_id}
            if command != "overview":
                entity[section] = [{"id": "temp", "state": {"value": 0}}] if sensor_temperature else {}
            raw = c.request("RT_GET", "FanControlPayloadList", {"fans_module": entity})
            if command == "overview":
                value = c.parse(raw, "read_entity", "fans_module", str(fan_id))
                rendered = format_fields({key:item for key,item in value.items() if key != "id"})
                returned = f"{len(value) - 1} sections displayed"
            else:
                value = (c.parse(raw, "identity_sensor", "fans_module", str(fan_id), "sensor", "temp")
                         if sensor_temperature else c.parse(raw, "read_section", "fans_module", str(fan_id), section))
                if command == "sensors":
                    sensors = select_sensors(value, selected)
                    rendered = format_sensor_details(sensors)
                    c.capture_result(sensors)
                    returned = ", ".join(sensor["id"] for sensor in sensors)
                elif command in ("speed", "pwm", "temp", "temperature"):
                    field = command if command in ("speed", "pwm") else "temperature"
                    number = value if sensor_temperature else (value.get(field) if isinstance(value, dict) else None)
                    require_numeric(number, field)
                    rendered = {"speed": "Speed", "pwm": "PWM", "temperature": "Temperature"}[field] + ": " + json.dumps(number)
                    returned = json.dumps(number)
                    c.capture_result({field: number})
                    rows.append([fan_id, returned, "Read OK"])
                elif command == "control-mode":
                    control = field_value(value, "control_mode") if isinstance(value, dict) else None
                    if not isinstance(control, dict) or not isinstance(control.get("value"), str) or not control["value"]:
                        raise LotdError("Response did not include a fan control-mode value")
                    rendered = "Control mode: " + control["value"]
                    returned = control["value"]
                    c.capture_result({"control_mode": control})
                elif command == "presence":
                    if not isinstance(value, dict):
                        raise LotdError("Expected a fan state object")
                    flags = {key:value[key] for key in ("presence", "missing") if key in value}
                    if not flags or any(not isinstance(flag, bool) for flag in flags.values()):
                        raise LotdError("Response did not include boolean fan presence/missing fields")
                    rendered = format_fields(flags)
                    c.capture_result(flags)
                    returned = ", ".join(f"{key}={json.dumps(flag)}" for key,flag in flags.items())
                else:
                    if not isinstance(value, dict):
                        raise LotdError(f"Expected a fan {section} object")
                    rendered = format_fields(value)
                    returned = f"{len(value)} fields displayed"
            if not numeric_batch:
                if command not in ("speed", "pwm", "temp", "temperature", "control-mode"):
                    rendered = format_read_section(f"Fan {fan_id} " + ("configuration" if command == "config" else command), rendered)
                print(rendered)
            successes.append(fan_id)
            values.append(f"{fan_id}: {returned}")
        except LotdError as e:
            c.record_failure(e)
            if numeric_batch:
                rows.append([fan_id, "-", "Failed"])
            failures.append((fan_id, str(e)))
            print(f"Fan {fan_id} {command} read failed: {e}", file=sys.stderr)
            if is_endpoint_failure(e) and index+1 < len(ids):
                pending, stopped = ids[index+1:], True
                break
        except Interrupted:
            pending = ids[index+1:]
            if numeric_batch:
                rows.append([fan_id, "-", "Interrupted"])
                print_read_table(c, "Fan readings", table_headers, rows, pending)
            details = summary_details() + [f"Interrupted during fan {fan_id} RT_GET."]
            c.summary("Interrupted (Ctrl+C)", operation, details, 1)
            return 130
        finally:
            c.current_id, c.current_phase = None, ""
    outcome = "Stopped after endpoint connection failure" if stopped else (f"Completed with {len(failures)} failure(s)" if failures else "Completed successfully")
    if numeric_batch:
        print_read_table(c, "Fan readings", table_headers, rows, pending)
    c.summary(outcome, operation, summary_details(), 1 if failures else 0)
    return 1 if failures else 0


def unsupported_component_command(a, comp, mode, command_index):
    command = " ".join(a[command_index:]) or "(missing command)"
    topic = comp
    hint = ""
    if comp == "fan" and len(a) > command_index and a[command_index].lower() in ("auto", "manual"):
        if mode == "get":
            hint = f" Read the current mode with: get fan {a[3]} control-mode."
        else:
            target_mode = a[command_index].lower()
            hint = f" Use: set fan {a[3]} control-mode {target_mode}" + (" <0-100>." if target_mode == "manual" else ".")
    raise UsageError(f"Unrecognized {comp} {mode.upper()} command: {command}.{hint} Run lotd.py help {topic} for supported commands.")


def run_container_action(c: Client, a: list[str], mode: str, layout: str) -> int:
    if mode not in ("dump", "import") or len(a) != 6:
        raise UsageError("Container syntax: <dump|import> container <container-type> <instance> <filename>. Run lotd.py help container.")
    container_type, instance_text, filename = a[3].upper(), a[4], a[5]
    if container_type not in CONTAINER_TYPES:
        raise UsageError("Unsupported container type; run lotd.py help container for supported targets")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", instance_text) or int(instance_text) > 4294967295:
        raise UsageError("Container instance must be one integer from 1 to 4294967295; supported instances are defined by LOTD")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", filename):
        raise UsageError("Container filename must be a bare filename (1-255 ASCII letters/digits/._-, starting with a letter or digit), without directory paths")
    if container_type == "CC_LDS_INVENTORY" and layout == "r300l":
        raise UsageError("R300-L has no LDS card; skip LDS container actions on that model")
    instance = int(instance_text)
    unit_path = "/sftpmnt/" + filename
    print(f"Container {mode} at {c.endpoint}:")
    print(format_fields({"container_type": container_type, "instance_number": instance,
                         "filename": filename, "unit_path": unit_path}, 2))
    if mode == "import":
        print("  Writes the selected container from the unit file. Validation after write: true.")
    else:
        print("  Writes a unit file. Use a new filename; existing-file overwrite behavior is unconfirmed.")
        print("  The response cannot verify file creation; check the unit file separately.")
    if not confirm(f"Submit container {mode}? [y/N] ", c.report, context="Container action"):
        return 0
    action = "dump_container_to_file" if mode == "dump" else "write_file_to_container"
    request = {"container_type": {"value": container_type}, "instance_number": instance,
               "calibration_file_path": filename}
    if mode == "import":
        request["validate_after_write"] = True
    operation = f"{mode} container {container_type} instance {instance} {filename} at {c.endpoint}"
    c.current_id, c.current_phase = str(instance), "RT_ACTION"
    try:
        print(f"Submitting container {mode}...", flush=True)
        raw = c.request("RT_ACTION", "EquipmentPackPayloadList",
                        {"equipment_pack": [{"id": 1, "actions": {action: {"request": request}}}]})
        result = c.parse(raw, "container_action", action)
        data = {"container_type": container_type, "instance_number": instance,
                "filename": filename, "unit_path": unit_path, "independently_verified": False, **result}
        c.capture_result(data)
        if mode == "dump":
            note = "Dump submitted; file creation is unverified. CID-only responses can also accompany file-open failures."
            c.response_notes.append(note)
            print(note)
            print(f"Check on unit: {unit_path}")
            c.summary("Submitted; file creation unverified", operation,
                      id_summary([]) + [f"Submitted instance: {instance}", f"Unit destination: {unit_path}"])
        else:
            if not result["success"]:
                raise LotdError("LOTD reported container import success=false")
            note = "Import reported successful by LOTD with validation requested; independent readback was not performed."
            c.response_notes.append(note)
            print(note)
            c.summary("Completed successfully", operation, id_summary([instance]))
        return 0
    except LotdError as exc:
        c.record_failure(exc)
        print(f"Container {mode} failed: {exc}", file=sys.stderr)
        c.summary("Completed with 1 failure", operation, id_summary([], [instance]) +
                  [str(exc), "No automatic retry was performed. Inspect the unit before repeating an uncertain action."], 1)
        return 1
    except Interrupted:
        message = "Interrupted during RT_ACTION; whether the action completed is unknown. Inspect the unit before retrying."
        print(message, file=sys.stderr)
        c.summary("Interrupted (Ctrl+C)", operation, id_summary([]) + [message], 1)
        return 130
    finally:
        c.current_id, c.current_phase = None, ""


def run_reset_manager(c: Client, a: list[str], mode: str) -> int:
    if mode != "get" or len(a) != 4 or a[3].lower() not in ("state", "last-reset-reason"):
        raise UsageError("Reset Manager is read-only: get reset-manager <state|last-reset-reason>. Run lotd.py help reset-manager for details.")
    command = a[3].lower()
    operation = f"get reset-manager {command} at {c.endpoint}"
    c.current_id, c.current_phase = "1", "RT_GET"
    print(f"Reading Reset Manager {command.replace('-', ' ')} at {c.endpoint}...")
    try:
        raw = c.request("RT_GET", "ResetManagerPayloadList", {"reset_manager": {"id": 1, "state": {}}})
        state = c.parse(raw, "read_section", "reset_manager", "1", "state")
        if not isinstance(state, dict):
            raise LotdError("Response did not include a Reset Manager state object")
        if command == "last-reset-reason":
            reason = field_value(state, "last_reset_reason")
            value = reason.get("value") if isinstance(reason, dict) else None
            if not isinstance(value, str) or not value.strip():
                raise LotdError("Response did not include a nonempty last_reset_reason.value string")
            c.capture_result({"last_reset_reason": value})
            print(f"Last reset reason: {value}")
        else:
            print(format_read_section("Reset Manager 1 state", format_fields(state)))
        c.summary("Completed successfully", operation, id_summary(["1"]))
        return 0
    except LotdError as exc:
        c.record_failure(exc)
        print(f"Unable to read Reset Manager {command}: {exc}", file=sys.stderr)
        c.summary("Completed with 1 failure", operation, id_summary([], ["1"]) + [str(exc)], 1)
        return 1
    except Interrupted:
        c.summary("Interrupted (Ctrl+C)", operation, id_summary([]) + ["Interrupted during Reset Manager RT_GET."], 1)
        return 130
    finally:
        c.current_id, c.current_phase = None, ""


def run_other(c: Client, a: list[str], mode: str, comp: str, model: str, layout: str, model_specified: bool) -> int:
    """Preserve routing precedence; family handlers own execution and validation."""
    if comp == "container":
        return run_container_action(c, a, mode, layout)
    if comp == "reset-manager":
        return run_reset_manager(c, a, mode)
    if comp == "ps":
        return run_ps_reads(c, a, mode, model)
    if comp == "cv" and mode == "set" and len(a) > 3 and a[3].lower() == "control":
        return run_cv_control_set(c, a)
    if comp == "fan":
        if mode == "set" and len(a) > 4 and a[4].lower() in ("pwm", "control-mode"):
            return run_fan_set(c, a)
        result = run_fan_reads(c, a, mode, model)
        if result is not None:
            return result
    detailed = run_read_details(c, a, mode, comp, model)
    if detailed is not None:
        return detailed
    identity = run_identity_reads(c, a, mode, comp, model)
    if identity is not None:
        return identity
    if comp in ("shelf", "midplane") and mode == "get" and len(a) == 4 and a[3].lower() in ("state", "inventory"):
        return run_equipment_pack_read(c, a, comp)
    if mode == "get" and (
        (comp == "hvdac" and len(a) > 4 and a[4].lower() in ("presence", "inventory", "state"))
        or (comp == "cv" and len(a) > 3 and a[3].lower() in ("presence", "inventory"))
        or (comp == "lds" and len(a) > 3 and a[3].lower() in ("presence", "state", "inventory"))
    ):
        return run_fru_reads(c, a, comp, model, layout)
    if comp == "hvdac" and mode == "get" and len(a) > 4 and a[4].lower() in ("temp", "temperature"):
        return run_hvdac_temperature(c, a, model)
    if comp == "mirror":
        return run_mirror(c, a, mode, layout)
    if comp == "connection":
        return run_connection(c, a, mode, layout)
    return run_leds(c, a, mode, comp, model)


def run_identity_reads(c: Client, a: list[str], mode: str, comp: str, model: str = "r300") -> int | None:
    """Handle shelf identity and focused temperatures; return None if unmatched."""
    end = c.endpoint
    identity_commands = ("mac-addresses", "firmware-version")
    temperature_commands = ("temp", "temperature", "inlet-temp", "inlet-temperature", "outlet-temp", "outlet-temperature")
    if comp in ("shelf", "midplane", "cv") and len(a) > 3 and a[3].lower() in identity_commands + temperature_commands:
        command = a[3].lower()
        command = {"inlet-temp": "inlet-temperature", "outlet-temp": "outlet-temperature"}.get(command, command)
        if mode != "get":
            raise UsageError(f"{command} is read-only")
        if command in identity_commands and comp != "shelf":
            raise UsageError(f"{command} is supported for shelf only; use get {comp} inventory for card identity")
        if command == "firmware-version":
            if len(a) != 5 or a[4].lower() not in ("active", "inactive"):
                raise UsageError("firmware syntax: get shelf firmware-version active|inactive")
            section, selection = "load_version", "1" if a[4].lower() == "active" else "2"
            requested = {"id": int(selection)}
            label = f"{a[4].capitalize()} firmware load version"
        else:
            if len(a) != 4:
                raise UsageError(f"get {comp} {command} takes no extra arguments")
            selection = ""
            if command == "mac-addresses":
                section, requested, label = "mac_addresses", {}, "MAC addresses"
            elif comp == "shelf":
                section, requested = "state", {}
                label = {"temp": "Temperature", "temperature": "Temperature",
                         "inlet-temperature": "Inlet temperature", "outlet-temperature": "Outlet temperature"}[command]
                if model == "r320":
                    selection = {"inlet-temperature": "inlet-temp", "outlet-temperature": "outlet-temp"}.get(command, "cpu-temp")
                    section, requested = "sensor", [{"id": selection, "state": {"value": 0}}]
            else:
                if command not in ("temp", "temperature"):
                    raise UsageError("inlet/outlet temperature is supported for shelf only")
                section, selection, requested, label = "sensor", "temp", [{"id": "temp", "state": {"value": 0}}], "Temperature"
        entity_id = 2 if comp == "midplane" else 1
        entity_key = "common_voltage_card" if comp == "cv" else "equipment_pack"
        entity = {"id": entity_id, section: requested}
        body = {"memsModule": {"id": 1, entity_key: entity}} if comp == "cv" else {entity_key: entity}
        payload_type = "MemsModulePayloadList" if comp == "cv" else "EquipmentPackPayloadList"
        operation = " ".join(a[1:]) + f" at {end}"
        print(f"Reading {comp} {label.lower()} at {end}...")
        c.current_id = str(entity_id)
        try:
            raw = c.request("RT_GET", payload_type, body)
            value = c.parse(raw, "identity_sensor", entity_key, str(entity_id), section, selection)
            if comp == "shelf" and command in temperature_commands:
                field = {"inlet-temperature": "inlet_temperature", "outlet-temperature": "outlet_temperature"}.get(command, "temperature")
                if section == "state":
                    value = field_value(value, field)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise LotdError(f"Response did not include numeric {field}")
                c.capture_result({field: value})
            rendered = format_mac_addresses(value) if section == "mac_addresses" else (value if section == "load_version" else json.dumps(value, sort_keys=True))
            print(f"{label}: {rendered}")
            c.summary("Completed successfully", operation, id_summary([entity_id])+[f"Returned {label.lower()}: {rendered}"])
            return 0
        except LotdError as e:
            c.record_failure(e)
            print(f"Unable to read {comp} {label.lower()}: {e}", file=sys.stderr)
            c.summary("Completed with 1 failure", operation, id_summary([], [entity_id])+[str(e)], 1)
            return 1


def run_equipment_pack_read(c: Client, a: list[str], comp: str) -> int:
    """Read full shelf or midplane state/inventory."""
    end = c.endpoint
    section=a[3].lower(); fru_id=1 if comp=="shelf" else 2
    print(f"Reading {section} for {comp} (entity ID {fru_id}) at {end}...")
    c.current_id = str(fru_id)
    try:
        value=query_fru_section(c,comp,fru_id,section)
        print(format_read_section(f"{comp.capitalize()} {section}", format_section(value, section)))
        c.summary("Completed successfully",f"get {comp} {section} at {end}",id_summary([fru_id])+[section_summary(section,value)]); return 0
    except LotdError as e:
        c.record_failure(e)
        print(f"Unable to read {section} for {comp}: {e}",file=sys.stderr)
        c.summary("Completed with 1 failure",f"get {comp} {section} at {end}",id_summary([], [fru_id])+[str(e)],1); return 1


def run_fru_reads(c: Client, a: list[str], comp: str, model: str, layout: str) -> int:
    """Read card presence, state or inventory, including HVDAC batches."""
    end = c.endpoint
    if comp == "hvdac":
        if len(a) != 5 or a[4].lower() not in ("presence", "inventory", "state"):
            raise UsageError("HVDAC read syntax is: get hvdac <card-ids> <presence|inventory|state>")
        max_card=11 if model=="r320" else 10
        ids=expand(a[3].lower(),max_card,"HVDAC card")
        label=a[4].lower()
        section="state" if label in ("presence", "state") else "inventory"
        identity="card"
    elif comp == "cv":
        if len(a) != 4 or a[3].lower() not in ("presence", "inventory"):
            raise UsageError("CV read syntax is: get cv <presence|inventory>")
        ids=[1]; section="state" if a[3].lower()=="presence" else "inventory"
        label="presence" if section=="state" else "inventory"; identity="card"
    else:
        if len(a) != 4:
            raise UsageError("LDS read syntax is: get lds <presence|state|inventory>")
        if layout == "r300l":
            raise UsageError("R300-L has no LDS card; skip LDS commands on that model")
        ids=[1]; label=a[3].lower(); section="inventory" if label=="inventory" else "state"; identity="card"
    component_label={"hvdac":"HVDAC","cv":"CV","lds":"LDS"}[comp]
    if len(ids)>1:
        print(f"Reading {component_label} {label} for IDs {','.join(map(str,ids))} at {end}.")
    successes=[]; failures=[]; values=[]; pending=[]; stopped=False
    operation=f"get {component_label} {label}"+(f" on {identity} {','.join(map(str,ids))}" if len(ids)>1 else "")+f" at {end}"
    for index,entity_id in enumerate(ids):
        progress=f"[{index+1}/{len(ids)}] " if len(ids)>1 else ""
        c.current_id=str(entity_id); c.current_phase="RT_GET"
        try:
            if len(ids)==1:
                if comp=="hvdac":
                    print(f"Reading {label} for HVDAC card ID {entity_id} at {end}...")
                elif comp=="cv":
                    print(f"Reading CV {label} for card ID {entity_id} at {end}...")
                else:
                    print(f"Reading LDS {label} for card ID {entity_id} at {end}...")
            else:
                print(f"{progress}Reading {label} for {component_label} card ID {entity_id} at {end}...")
            value=query_fru_section(c,comp,entity_id,section,full_state=label == "state")
            rendered=format_section(value, section)
            print(format_read_section(f"{component_label} card {entity_id} {label}", rendered))
            successes.append(entity_id)
            if label=="presence" and isinstance(value,dict) and "missing" in value:
                values.append(f"{entity_id}: missing={json.dumps(value['missing'])}")
            elif isinstance(value,dict):
                values.append(f"{entity_id}: {', '.join(str(key) for key in value) or 'no fields'}")
        except LotdError as e:
            c.record_failure(e)
            failures.append((entity_id,str(e)))
            print(f"{component_label} {label} read failed for ID {entity_id}: {e}",file=sys.stderr)
            if is_endpoint_failure(e) and index+1<len(ids):
                pending=ids[index+1:]; stopped=True; break
        except Interrupted:
            pending=ids[index+1:]
            details=id_summary(successes,[item[0] for item in failures],pending)
            details += [f"ID {item_id}: {error}" for item_id,error in failures]
            details.append(f"Interrupted during ID {entity_id} RT_GET.")
            c.summary("Interrupted (Ctrl+C)",operation,details,1); return 130
        finally:
            c.current_id=None; c.current_phase=""
    details=id_summary(successes,[item[0] for item in failures],pending)
    details += [f"ID {item_id}: {error}" for item_id,error in failures]
    if values:
        details.append(f"Returned state: {len(successes)} {'card' if len(successes) == 1 else 'cards'}" if label == "state" else f"Returned {label} (ID: value): "+summarize_items(values))
    outcome="Stopped after endpoint connection failure" if stopped else (f"Completed with {len(failures)} failure(s)" if failures else "Completed successfully")
    c.summary(outcome,operation,details,1 if failures else 0); return 1 if failures else 0


def run_hvdac_temperature(c: Client, a: list[str], model: str) -> int:
    """Read HVDAC temperature using its established sensor/legacy paths."""
    end = c.endpoint
    if len(a)!=5: raise UsageError("HVDAC temperature get requires one card ID")
    try: card=int(a[3])
    except ValueError: raise UsageError("HVDAC card ID must be numeric")
    maximum=11 if model=="r320" else 10
    if not 1<=card<=maximum: raise UsageError(f"card ID must be 1-{maximum} for {model.upper()}")
    print(f"Reading temperature for HVDAC card {card} at {end}...")
    fallback=False
    c.current_id = str(card)
    try:
        try:
            raw=c.request("RT_GET","MemsModulePayloadList",{"memsModule":{"id":1,"dac_cards":{"id":card,"sensor":[{"id":"temp","state":{"value":0}}]}}})
            value=c.parse(raw,"temperature_sensor",str(card))
        except LotdError as sensor_error:
            if model == "r320" or is_endpoint_failure(sensor_error):
                raise
            fallback=True
            raw=c.request("RT_GET","MemsModulePayloadList",{"memsModule":{"id":1,"dac_cards":{"id":card,"state":{"temperature":0}}}})
            value=c.parse(raw,"temperature_legacy",str(card))
        print("Temperature: " + json.dumps(value))
        c.summary("Completed successfully",f"get HVDAC card {card} temperature at {end}",id_summary([card])+(["Read path: legacy fallback"] if fallback else [])+[f"Returned temperature: {json.dumps(value)}"]); return 0
    except LotdError as e:
        c.record_failure(e)
        print("Unable to read HVDAC temperature using the sensor path." if model == "r320" else "Unable to read HVDAC temperature using either known LOTD path.",file=sys.stderr)
        print(str(e),file=sys.stderr); c.summary("Completed with 1 failure",f"get HVDAC card {card} temperature at {end}",id_summary([], [card])+[str(e)],1); return 1


def format_mirror_target(values: dict[str, int | float], axis: str,
                         requested: str | None = None) -> str:
    """Format a validated mirror target after any exact readback check."""
    lines = [f"{axis.upper()} target voltage: {json.dumps(values[axis + '_tilt_voltage'])}"]
    if requested is not None:
        lines.append(f"Verified: {axis.upper()} target voltage matches {decimal_text(requested)}")
    return "\n".join(lines)


def run_mirror(c: Client, a: list[str], mode: str, layout: str) -> int:
    """Read mirror sections or execute tilt target reads/writes."""
    end = c.endpoint
    section_read = mode == "get" and len(a) == 5 and a[4].lower() in ("state", "config")
    if mode=="get":
        if len(a)!=5: raise UsageError("mirror GET syntax: get mirror <port-ids> <state|config|x-tilt|y-tilt>")
        selector,axis=a[3].lower(),a[4].lower(); target=None
    else:
        if len(a)!=6: raise UsageError("mirror SET syntax: set mirror <port-ids> <x-tilt|y-tilt> <target>")
        selector,axis,target=a[3].lower(),a[4].lower(),a[5]
        if not numeric_target(target): raise UsageError("target voltage must be an integer or decimal number")
    axis=axis if section_read else {"x":"x","x-tilt":"x","x_tilt_voltage":"x","y":"y","y-tilt":"y","y_tilt_voltage":"y"}.get(axis)
    if not axis: raise UsageError("mirror GET supports state, config, x-tilt or y-tilt; SET supports x-tilt or y-tilt")
    ids=mirror_ids(selector,layout); field=axis+"_tilt_voltage"; display_field=axis.upper()+" target voltage"
    if section_read: display_field = "configuration" if axis == "config" else "state"
    if mode=="set" and len(ids)>1:
        print(f"Batch set preview: mirror ports {','.join(map(str,ids))} {display_field} will be set to {target} at {end}.")
        if not confirm(f"Proceed with these {len(ids)} port changes? [y/N] ", c.report): return 0
    ok=[]; bad=[]; errors=[]; values=[]; pending=[]; stopped=False
    rows=[]
    table_headers=["Port ID", display_field, "Read result"]
    label=selector if len(ids)>1 else str(ids[0]); op=f"{mode} mirror {display_field} on port {label}"+(f" to {target}" if mode=="set" else "")+f" at {end}"
    for ix,port in enumerate(ids):
        progress=f"[{ix+1}/{len(ids)}] " if len(ids)>1 else ""; c.current_id=str(port); c.current_phase="RT_SET" if mode=="set" else "RT_GET"
        try:
            if section_read:
                print(f"{progress}Reading {display_field} for mirror port {port} at {end}...")
                raw=c.request("RT_GET","MemsModulePayloadList",{"mems_module":{"id":1,"mirrors":{"id":port,axis:{}}}})
                value=c.parse(raw,"read_section","mirrors",str(port),axis)
                print(format_read_section(f"Mirror port {port} {display_field}", format_fields(value)))
                values.append(str(port))
            elif mode=="get":
                print(f"{progress}Reading {display_field} for mirror port {port} at {end}...")
                raw=c.request("RT_GET","MemsModulePayloadList",{"mems_module":{"id":1,"mirrors":{"id":port,"config":{field:0}}}})
                result=c.parse(raw,"mirror",str(port),axis)
                if len(ids)==1:
                    print(format_mirror_target(result, axis))
                value=json.dumps(result[field]); values.append(f"{port}: {value}")
                rows.append([port,value,"Read OK"])
            else:
                print(f"{progress}Setting {display_field} for mirror port {port} to {target} at {end}...")
                raw=c.request("RT_SET","MemsModulePayloadList",{"mems_module":{"id":1,"mirrors":{"id":port,"config":{field:json_number(target)}}}})
                acknowledge_write(c, raw)
                c.current_phase="readback"; print(f"{progress}Reading back mirror port target...")
                raw=c.request("RT_GET","MemsModulePayloadList",{"mems_module":{"id":1,"mirrors":{"id":port,"config":{field:0}}}})
                result=c.parse(raw,"mirror_set",str(port),axis,target); print(format_mirror_target(result, axis, target))
            ok.append(port)
        except LotdError as e:
            c.record_failure(e)
            if mode=="get":
                rows.append([port,"-","Failed"])
            bad.append(port); errors.append(f"Port {port}: {e}"); print(f"Mirror port {port} failed: {e}",file=sys.stderr)
            if is_endpoint_failure(e) and ix+1<len(ids): pending=ids[ix+1:]; stopped=True; break
        except Interrupted:
            pending=ids[ix+1:]
            details=id_summary(ok,bad,pending,verified=mode=="set")+errors
            if mode=="get" and len(ids)>1 and not section_read:
                rows.append([port,"-","Interrupted"])
                print_read_table(c, "Mirror tilt targets", table_headers, rows, pending)
            details.append(f"Interrupted during mirror port {port} {c.current_phase}.")
            c.summary("Interrupted (Ctrl+C)",op,details,1); return 130
        finally: c.current_id=None; c.current_phase=""
    details=id_summary(ok,bad,pending,verified=mode=="set")+errors
    if mode=="get" and len(ids)>1 and not section_read:
        print_read_table(c, "Mirror tilt targets", table_headers, rows, pending)
    if values: details.append(f"Returned {display_field}: {len(values)} {'port' if len(values) == 1 else 'ports'}" if section_read else (f"Returned values: {len(values)} ports" if len(ids)>1 else "Returned values (port: target voltage): "+summarize_items(values)))
    outcome="Stopped after endpoint connection failure" if stopped else (f"Completed with {len(bad)} failure(s)" if bad else "Completed successfully")
    c.summary(outcome,op,details,1 if bad else 0); return 1 if bad else 0


def format_connection(values: dict[str, int | bool], action: str = "get",
                      requested: str | None = None) -> str:
    """Format parsed connection values after any selected-field verification."""
    fields = [("peer_port_number", "Peer port number"),
              ("optical_blocking_enable", "Optical blocking enable")]
    if action != "get":
        fields = [fields[0] if action == "peer" else fields[1]]
    lines = [f"{label}: {json.dumps(values[field])}" for field, label in fields]
    if requested is not None:
        wanted = str(int(requested)) if action == "peer" else requested
        lines.append(f"Verified: {fields[0][1].lower()} matches {wanted}")
    return "\n".join(lines)


def run_connection(c: Client, a: list[str], mode: str, layout: str) -> int:
    """Read Connection Manager state or set peer/blocking with readback."""
    end = c.endpoint
    if mode=="get":
        if len(a)!=4: raise UsageError("connection GET syntax: get connection <port-ids>")
        selector=a[3].lower(); action="get"; target=None
    else:
        if len(a)!=6: raise UsageError("connection SET syntax: set connection <port-ids> <peer|blocking> <value>")
        selector=a[3].lower(); action=a[4].lower(); target=a[5].lower()
        if action not in ("peer","blocking"): raise UsageError("connection SET field must be peer or blocking")
    ids=mirror_ids(selector,layout)
    field=""; set_value=None; expected=None; display=None
    if action=="peer":
        field="peer_port_number"
        if target in ("disconnect","none","-1"): set_value=-1; expected=0; display="disconnect"
        elif re.fullmatch(r"0|[1-9][0-9]*",target):
            if len(target)>5: raise UsageError("peer target port number is out of range")
            set_value=int(target); expected=int(target); display=target
        else: raise UsageError("peer target must be a port number, 0, or disconnect")
        if mode=="set":
            for port in ids:
                if port>320: raise UsageError(f"peer SETs can only start from north/A-side connection ports (IDs 1-320); port {port} is south/B-side")
                if set_value not in (-1,0) and (set_value<10001 or not any(lo<=set_value<=hi for lo,hi in ({"r300g":[(10001,10148),(10161,10168),(10181,10320)],"r300l":[(10001,10300)],"r320":[(10001,10320)],"generic":[(10001,10148),(10161,10168),(10181,10300)]}[layout]))):
                    raise UsageError(f"peer target {display} must be a valid south/B-side port number for {layout.upper()}")
    elif action=="blocking":
        field="optical_blocking_enable"
        if target in ("on","true"): set_value=True; expected="true"; display="on"
        elif target in ("off","false"): set_value=False; expected="false"; display="off"
        else: raise UsageError("blocking value must be on or off")
    display_field = {"peer_port_number": "peer port number", "optical_blocking_enable": "optical blocking enable"}.get(field, field)
    if mode=="set" and len(ids)>1:
        print(f"Batch set preview: connection ports {','.join(map(str,ids))} {display_field} will be set to {display} at {end}.")
        if not confirm(f"Proceed with these {len(ids)} port changes? [y/N] ", c.report): return 0
    ok=[]; bad=[]; errors=[]; peers=[]; blocks=[]; pending=[]; stopped=False
    label=selector if len(ids)>1 else str(ids[0])
    op=f"{mode} Connection Manager {'state for port '+label if mode=='get' else display_field+' on port '+label+' to '+display} at {end}"
    for ix,port in enumerate(ids):
        progress=f"[{ix+1}/{len(ids)}] " if len(ids)>1 else ""; c.current_id=str(port); c.current_phase="RT_SET" if mode=="set" else "RT_GET"
        try:
            body={"connection_manager":[{"id":1,"port_array":[{"id":port,"config":{} if mode=="get" else {field:set_value}}]}]}
            if mode=="get":
                print(f"{progress}Reading Connection Manager state for port {port} at {end}...")
                result=c.parse(c.request("RT_GET","ConnectionManagerPayloadList",body),"connection",str(port)); print(format_connection(result))
                peers.append(f"{port}->{json.dumps(result['peer_port_number'])}")
                blocks.append(f"{port}={json.dumps(result['optical_blocking_enable'])}")
            else:
                print(f"{progress}Setting Connection Manager {display_field} for port {port} to {display} at {end}...")
                raw=c.request("RT_SET","ConnectionManagerPayloadList",body); acknowledge_write(c, raw)
                c.current_phase="readback"; print(f"{progress}Reading back Connection Manager state...")
                rb={"connection_manager":[{"id":1,"port_array":[{"id":port,"config":{}}]}]}
                result=c.parse(c.request("RT_GET","ConnectionManagerPayloadList",rb),"connection_peer_set" if action=="peer" else "connection_block_set",str(port),str(expected)); print(format_connection(result, action, str(expected)))
            ok.append(port)
        except LotdError as e:
            c.record_failure(e)
            bad.append(port); errors.append(f"Port {port}: {e}"); print(f"Connection Manager port {port} failed: {e}",file=sys.stderr)
            if is_endpoint_failure(e) and ix+1<len(ids): pending=ids[ix+1:]; stopped=True; break
        except Interrupted:
            pending=ids[ix+1:]
            details=id_summary(ok,bad,pending,verified=mode=="set")+errors
            details.append(f"Interrupted during connection port {port} {c.current_phase}.")
            c.summary("Interrupted (Ctrl+C)",op,details,1); return 130
        finally: c.current_id=None; c.current_phase=""
    details=id_summary(ok,bad,pending,verified=mode=="set")+errors
    if mode=="get" and peers: details += ["Peer mappings (port->peer): "+summarize_items(peers),"Optical blocking (port=value): "+summarize_items(blocks)]
    outcome="Stopped after endpoint connection failure" if stopped else (f"Completed with {len(bad)} failure(s)" if bad else "Completed successfully")
    c.summary(outcome,op,details,1 if bad else 0); return 1 if bad else 0


def run_leds(c: Client, a: list[str], mode: str, comp: str, model: str) -> int:
    """Validate and execute fixed-FRU or selected-card LED operations."""
    end = c.endpoint
    fixed={"shelf":1,"midplane":2,"cv":1,"lds":1}
    if comp in fixed:
        if mode=="get":
            if len(a)==4 and a[3].lower()=="led": selector="both"
            elif len(a)==5 and a[3].lower()=="led": selector=a[4].lower()
            else:
                if len(a) > 3 and a[3].lower() == "led":
                    raise UsageError(f"{comp} LED GET syntax is: get {comp} led [power|fault|both|all]")
                unsupported_component_command(a, comp, mode, 3)
            ids=[fixed[comp]]
        else:
            if a[3].lower() != "led":
                unsupported_component_command(a, comp, mode, 3)
            if len(a) != 6:
                raise UsageError(f"{comp} LED SET syntax is: set {comp} led <power|fault|both|all> <on|off>")
            selector=a[4].lower(); status=a[5].lower(); ids=[fixed[comp]]
    elif comp in ("hvdac","fan"):
        if mode=="get":
            if len(a)==5 and a[4].lower()=="led": selector="both"
            elif len(a)==6 and a[4].lower()=="led": selector=a[5].lower()
            else:
                if len(a) <= 4 or a[4].lower() != "led":
                    unsupported_component_command(a, comp, mode, 4)
                raise UsageError(f"{comp} LED GET syntax is: get {comp} <ids> led [power|fault|both|all]")
            id_selector=a[3].lower()
        else:
            if len(a) <= 4 or a[4].lower() != "led":
                unsupported_component_command(a, comp, mode, 4)
            if len(a) != 7:
                raise UsageError(f"{comp} LED SET syntax is: set {comp} <ids> led <power|fault|both|all> <on|off>")
            id_selector=a[3].lower(); selector=a[5].lower(); status=a[6].lower()
        maximum=(11 if model=="r320" else 10) if comp=="hvdac" else 3
        ids=expand(id_selector,maximum,comp)
    else:
        raise UsageError("unknown component: " + comp)
    if selector == "all": selector = "both"
    if selector not in ("power","fault","both"): raise UsageError("LED must be 'power', 'fault', 'both', or 'all'")
    if mode=="set" and status not in ("on","off"): raise UsageError("status must be 'on' or 'off'")
    value="LC_ON" if mode=="set" and status=="on" else "LC_OFF"
    if mode=="set" and (len(ids)>1 or selector=="both"):
        changes=len(ids)*(2 if selector=="both" else 1)
        print(f"Batch set preview: {'power and fault' if selector=='both' else selector} LEDs on {comp} IDs {','.join(map(str,ids))} will be set to {value} at {end}.")
        if not confirm(f"Proceed with these {changes} LED changes? [y/N] ", c.report): return 0
    ok=[]; bad=[]; errors=[]; vals=[]; pending=[]; stopped=False
    display="power and fault LEDs" if selector=="both" else selector+" LED"
    operation=f"{mode} {display} on {comp} at {end}"
    for ix,eid in enumerate(ids):
        progress=f"[{ix+1}/{len(ids)}] " if len(ids)>1 else ""; c.current_id=str(eid)
        try:
            if mode=="get":
                print(f"{progress}Reading {display} on {comp} (entity ID {eid}) at {end}...")
                typ,body=led_body(comp,eid,None if selector=="both" else (1 if selector=="power" else 2),"RT_GET",None)
                result=c.parse(c.request("RT_GET",typ,body),"led_all" if selector=="both" else "led",str(1 if selector in ("both", "power") else 2),comp,str(eid))
                if selector=="both":
                    print(f"Power LED (ID 1): {result['1']}\nFault LED (ID 2): {result['2']}"); vals.append(f"{eid}: power={result['1']}, fault={result['2']}")
                else: print(f"{selector.title()} LED (ID {1 if selector=='power' else 2}): {result}"); vals.append(f"{eid}: {result}")
            else:
                led_ids=(1,2) if selector=="both" else ((1,) if selector=="power" else (2,))
                for lid in led_ids:
                    lname="power" if lid==1 else "fault"; c.current_phase=f"RT_SET {lname} LED" if selector=="both" else "RT_SET"
                    print(f"{progress}Setting {lname} LED on {comp} (entity ID {eid}) to {value} at {end}...")
                    typ,body=led_body(comp,eid,lid,"RT_SET",value); raw=c.request("RT_SET",typ,body); acknowledge_write(c, raw)
                c.current_phase="readback"
                if selector=="both":
                    print(f"{progress}Reading back power and fault LED values..."); typ,body=led_body(comp,eid,None,"RT_GET",None); rb=c.parse(c.request("RT_GET",typ,body),"led_all","1",comp,str(eid))
                    print(f"Power LED (ID 1): {rb['1']}\nFault LED (ID 2): {rb['2']}")
                    if rb["1"]!=value or rb["2"]!=value: raise LotdError(f"both LED values did not match requested {value}")
                    print(f"Verified: both LEDs are {value}.")
                else:
                    typ,body=led_body(comp,eid,led_ids[0],"RT_GET",None); rb=c.parse(c.request("RT_GET",typ,body),"led",str(led_ids[0]),comp,str(eid))
                    print(f"Reading back {selector} LED...\n{selector.title()} LED (ID {led_ids[0]}): {rb}")
                    if rb!=value: raise LotdError(f"SET succeeded but verification returned {rb}, expected {value}")
                    print(f"Verified: LED is {value}.")
            ok.append(eid)
        except LotdError as e:
            c.record_failure(e)
            bad.append(eid); errors.append(f"ID {eid}: {e}"); print(f"Operation failed for {comp} ID {eid}: {e}",file=sys.stderr)
            if is_endpoint_failure(e) and ix+1<len(ids): pending=ids[ix+1:]; stopped=True; break
        except Interrupted:
            pending=ids[ix+1:]
            details=id_summary(ok,bad,pending,verified=mode=="set")+errors
            if mode=="set": details.append(f"Interrupted during ID {eid} {c.current_phase}; whether LOTD applied the change is unknown." if c.current_phase.startswith("RT_SET") else f"Interrupted during ID {eid} readback; verification is incomplete.")
            else: details.append(f"Interrupted during ID {eid} RT_GET.")
            c.summary("Interrupted (Ctrl+C)",operation,details,1); return 130
        finally: c.current_id=None; c.current_phase=""
    details=id_summary(ok,bad,pending,verified=mode=="set")+errors
    if mode=="get" and vals: details.append(("Returned LED values (ID: power, fault): " if selector=="both" else "Returned LED values (ID: value): ")+summarize_items(vals))
    outcome="Stopped after endpoint connection failure" if stopped else (f"Completed with {len(bad)} failure(s)" if bad else "Completed successfully")
    c.summary(outcome,operation,details,1 if bad else 0); return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
