# LOTD CLI

A Python command-line client for reading device inventory, sensors and state,
and applying supported configuration changes through LOTD. It builds curated
requests for `lotd.Operations.RequestOp` and runs `grpcurl` as its transport.

Supported model selections are `R300`, `R300-G`, `R300-L` and `R320`. Model names
are case-insensitive; `R300G` and `R300L` also work. The model controls bounds,
mirror layouts and selected request paths; it does not discover installed cards
or guarantee that a particular firmware implements every command.

## Requirements and setup

- Python 3.10 or later.
- [grpcurl](https://github.com/fullstorydev/grpcurl) installed and on `PATH` for
  device commands. No third-party Python packages are required.
- Network access to the device's LOTD service on TCP port **3217**, with server
  reflection available. The client uses plaintext gRPC, `LV_1` and CID `123`.

Keep these files together:

| File | Purpose |
|---|---|
| [lotd.py](./lotd.py) | CLI entry point, command validation, dispatch and human output. |
| [lotd_client.py](./lotd_client.py) | Transport, response parsing and JSON reports; required by the CLI. |
| [mock_grpcurl.py](./mock_grpcurl.py) | Offline response fixtures and request capture. |
| [test_lotd.py](./test_lotd.py) | Offline unit and CLI integration tests. |
| [live_test_lotd.py](./live_test_lotd.py) | Optional checks against a device; reads only unless SET checks are explicitly requested. |

Start with local help; these commands do not contact a device:

```bash
python3 lotd.py --help
python3 lotd.py help fan
python3 lotd.py help cv
python3 lotd.py help json
```

On Windows PowerShell, use `py` in place of `python3`.

## Reading a device

The general form is:

```text
python3 lotd.py <device-ip-or-hostname> <operation> <component> <arguments> [--model <model>] [--json]
```

The address is an IPv4 address or hostname without a port. The port is fixed.
The default model is `R300`, a shared profile for 300G and 300L devices, so you
can omit `--model` for common commands. Add `--model R300G`, `--model R300L` or
`--model R320` when you need that model's limits, mirror layout or request paths.
For mirror commands, choosing a specific model enables its full valid ID range
and the `all` selector.

For Bash/WSL, substitute your device address:

```bash
IP=unit.example

python3 lotd.py "$IP" get shelf inventory
python3 lotd.py "$IP" get shelf firmware-version active
python3 lotd.py "$IP" get shelf firmware-version inactive
python3 lotd.py "$IP" get shelf mac-addresses
python3 lotd.py "$IP" get shelf inlet-temp
python3 lotd.py "$IP" get midplane sensors temp
python3 lotd.py "$IP" get fan all speed
python3 lotd.py "$IP" get ps all temperature
python3 lotd.py "$IP" get cv channel 1-3
python3 lotd.py "$IP" get hvdac 1 state
python3 lotd.py "$IP" get reset last-reset-reason
```

Examples with an explicit model:

```bash
python3 lotd.py "$IP" get mirror 1-3 state --model R300G
python3 lotd.py "$IP" get cv channel 18 --model R320
python3 lotd.py "$IP" get shelf temp --model R320
```

Use the model that matches the device at `$IP`. R320 needs an explicit model
for its sensor request paths and higher channel/card limits.

PowerShell example:

```powershell
$unitAddress = "unit.example"
py lotd.py $unitAddress get fan all speed
```

Commands that accept multiple IDs support comma-separated lists, ascending
ranges, mixtures such as `1-3,5`, and `all`. Support is command-specific: an
HVDAC channel read takes one card, chip and channel, whereas HVDAC state accepts
multiple cards. Use installed card IDs when a device is partially populated.

## Command coverage

Use `python3 lotd.py help <topic>` for detailed syntax and behavior.

| Component | Reads | Writes/actions |
|---|---|---|
| Shelf controller | State, inventory, MAC addresses, active/inactive firmware versions, CPU/inlet/outlet temperature, LEDs. | LEDs. |
| Midplane | State, inventory, temperature, sensor details, LEDs. | LEDs. |
| HVDAC | Presence, state, inventory, temperature, sensors, LEDs, channel target/actual voltage and optional DAC count, focused DAC count. | Channel target voltage, DAC count, LEDs. |
| CV card | Presence, state, inventory, temperature, sensors, LEDs, channel target/actual voltage, regulator voltages, control configuration. | Channel target, LEDs; control dead band, logging, Kinit/Ki/filter coefficients, loop interval and number of readings. |
| MEMS module | State, inventory, sensors, named temperature, correction temperature, humidity. | No module configuration writes. |
| Mirrors | Full state/configuration, X/Y target voltages. | X/Y target voltages with selected-field readback. |
| Connection Manager | Peer mapping and optical blocking. | Peer connection/disconnection and blocking. |
| Fans | Overview, state, configuration, control mode, inventory, presence, speed, actual PWM, temperature, sensors, LEDs. | Manual PWM target, manual/automatic control mode, LEDs. |
| Power supplies | Overview, state, inventory, presence, temperature and sensors. | None. |
| LDS | Presence, state, inventory, LEDs. | LEDs. |
| Reset Manager | State and last reset reason. | None; `reset` is a read-only alias, not a reset action. |
| Containers | No host file download or inspection. | Dump a container to a unit file; import a unit file with validation requested. |

`mems` and `mirror` are aliases, including per-mirror and module environmental
reads. `psu` aliases `ps`; `reset` aliases `reset-manager`. `temp` and
`temperature` are aliases, as are `inlet-temp`/`inlet-temperature` and
`outlet-temp`/`outlet-temperature`.

Important limits and model differences:

- Shelf is equipment-pack ID 1; midplane is ID 2. MAC/firmware reads are shelf-only.
- Fans use IDs 1–3; power supplies use IDs 1–2.
- HVDAC cards use IDs 1–10 on R300 models and 1–11 on R320. Chips use IDs 1–4;
  channels use IDs 1–32. CV channels use IDs 1–17 on R300 and 1–89 on R320.
  CV regulator IDs are a separate collection.
- Mirror IDs follow each model's internal layout, within 1–640. Connection peer
  targets use separate south/B `10001`-style numbers; these are not mirror IDs.
- R300-L has no LDS card. R320 temperature/humidity reads use named sensor paths
  where older state fields are absent. HVDAC temperature uses the sensor path
  on R320; other models support a legacy temperature fallback.
- A missing supply can return zero placeholders. Focused measurements fail as
  unavailable; state/overview retain raw fields with an explicit missing note.

CV control enable writes, mirror inventory/sensors, Reset Manager actions and
arbitrary raw requests are not implemented. MEMS burn-in is excluded. gNOI return
loss and shell/Docker/FPGA utilities use separate interfaces and are outside this CLI.

## Configuration changes and verification

Capture the current configuration before changing it and compare the relevant
fields afterward. Examples below show syntax; choose values appropriate for
your device:

```bash
python3 lotd.py "$IP" get fan 1 config
python3 lotd.py "$IP" set fan 1 control-mode auto
python3 lotd.py "$IP" set fan 1 pwm 40
python3 lotd.py "$IP" get cv control
python3 lotd.py "$IP" set cv control logging off
```

Fan PWM writes select manual mode and set its manual target. Verification reads
configuration; it does not prove that actual PWM or RPM has reached that target.
Fan/CV control changes, container actions, both-LED writes and multi-ID writes
require interactive confirmation. Some existing single-item writes execute
without a prompt. `--json` does not bypass confirmation.

Writes are followed by selected-field readback. A CID-only reply does not prove
acceptance; the CLI requires a matching readback before reporting verification.
Explicit rejection and mismatched readback fail. Verification of one field does
not verify other coupled settings or overall hardware health.

Each request has a 20-second timeout. Endpoint failures stop the remaining batch;
individual LOTD failures can leave a partial result. Timed-out or interrupted
writes may have completed on the unit. Inspect current state before deciding
whether to retry; the CLI does not automatically retry those writes.

Container commands accept a bare filename for a file under `/sftpmnt` on the
unit, not a local path. Dump reports submission, not verified file creation;
import requires LOTD `success=true` with validation requested. Neither performs
independent file/readback comparison. See `help container` before using them.

## Human and JSON output

Human output uses headings, nested fields and tables for numeric batches, followed
by a summary. Firmware views print the selected version string. Numeric telemetry
stays in the raw units returned by LOTD; scaling and status-bit meanings are not
guessed. A successful GET means the read succeeded, not that the hardware is healthy.

Append `--json` for one machine-readable object on stdout. Progress, diagnostics
and prompts go to stderr, so a terminal may show both streams together:

```bash
python3 lotd.py "$IP" get fan all speed --json > fan-readings.json
```

Schema version 1 includes command, endpoint, canonical model, exit code,
cancellation, summary ID lists, results and errors. Each exchange retains its
request/response, native parsed data, error, acceptance and verification status.
The summary lists successful, failed, not-attempted and verified IDs. GETs alone
do not mark IDs verified. SET acceptance is `null` for CID-only replies and
`true` for a valid explicit acknowledgement; readback verification is separate.

Exit codes: `0` completed command or declined confirmation, `1` operation failure,
`2` invalid usage, `130` interruption. In JSON mode inspect `cancelled` and
per-result status as well: an unverified container dump submission also exits 0.

## Tests and device checks

Run all offline tests in WSL/Linux:

```bash
python3 -m unittest -v
```

Current coverage is 152 tests: 23 portable parser/runner-plan/in-process CLI tests
and 129 POSIX integration tests using a mock launcher and PTYs. Offline tests
do not contact hardware or require an installed grpcurl.

Native Windows supports the portable tests:

```powershell
py -m unittest -v test_lotd.LotdParserTests test_lotd.LotdPortableCLITests
```

Windows discovery with `py -m unittest -v` skips the 129 POSIX tests explicitly.
Coverage includes request construction, model bounds, aliases, response scoping,
fragment conflicts (including nested boolean/numeric conflicts), numeric types, absent components, JSON streams, acknowledgement
and readback failures, cancellation, interruption and transport timeouts.

The optional live runner contacts the device. Its default checks are read-only:

```bash
python3 live_test_lotd.py "$IP" --model R300G
python3 live_test_lotd.py "$IP" --model R300L
```

Use the command matching the device. `--skip-lds` handles other units without
LDS. `--hvdac-cards 5,8` selects installed cards; `--mirror-ports 500,501` selects
available mirrors. These two selectors take comma-separated IDs, not ranges or
`all`. LDS inventory is an opt-in diagnostic via `--include-lds-inventory`.

`--include-set` offers confirmed same-value writes from captured baselines. It
excludes mirror, fan/CV control and container writes; it also excludes numeric
HVDAC writes on R320. A same-value write can still affect coupled settings.
The runner may reapply captured baselines after a SET failure or interruption;
this is separate from the CLI's no-retry behavior and does not verify complete
coupled-state restoration. Use manual CLI steps when inspection is needed between
changes.
Read-only regression success is not evidence for untested write operations.

## Known firmware and validation limits

- Tested R300-L firmware returns `RC_OPERATION_FAILED` for midplane state despite
  working inventory, LEDs and sensors. A firmware fix is pending validation; the
  CLI keeps this failure visible. R320 midplane state returned `RC_UNKNOWN_FIELD`.
- On a partially populated R320, mirrors backed by absent HVDAC cards returned
  failed actual-voltage/IL fields. Available mirrors returned complete state.
  Failed fragments are not turned into valid zero measurements.
- An R320 DAC-count write left the selected count unchanged but changed its
  reported voltage target. Actual-voltage reads failed briefly afterward and
  later recovered. The intended coupling and cause remain unresolved; the runner
  excludes numeric R320 HVDAC writes. Explicit CLI setters still exist.
- Some LDS inventory reads return mixed successful and failed fragments. The
  failure remains visible, with raw responses retained in JSON.
- Broader model/firmware, batch-write, container and changed-value control behavior
  still needs device validation. Offline fixtures establish software behavior,
  not physical response or support on every release.
