# Multi-tool (CANVAS multi-colour) uploads and starts

## Why
On 2026-11-11 the two-colour Pēnelópē tag (grey PLA body, black PLA lettering, sliced in Orca for the
CC2 with two filaments mapped to trays A1/A2) could not be sent through the fork: `cc2_upload_gcode`
refuses any file that selects more than one tool, and `cc2_start_print` accepts exactly one
tool-to-tray entry. Nathan: "u should be able to send with the fucking mcp". Elegoo's own LAN web
client starts multi-colour jobs with the same 1020 shape the fork already sends, one `slot_map` entry
per tool (see `protocol-evidence.md`), so the wire is pinned; only this adapter's one-tool rules stand
in the way.

## What changes
- The G-code parser reads per-filament config lists (`filament_type = PLA;PLA`, `nozzle_temperature =
  210,210`, ...) one value per tool instead of demanding one value, accepts several `T<n>` selects and
  a purge whose `T` names any tool the file selects, admits `M220` (speed factor, S only) to the
  allow list with its reason, and admits a purge `R` that differs from the verified wait under the one
  rule design.md states (R equals the declared `nozzle_temperature_range_high` of both tools involved,
  and R is recorded as a flush temperature, apart from the printing temperatures; design amendment 9).
  A bare `T<n>` tool change with no `M6211` purge before it refuses.
- The upload receipt records every tool with its material and verified temperatures (version 3; a
  version-2 receipt is still read as a one-tool receipt).
- `cc2_start_print` takes a mapping with one entry per tool the receipt names (1 to 4), checks each
  tray against its own tool (present, same material, rated range covering that tool's extrusion
  temperatures and the purge R), and sends one 1020 whose `slot_map` carries one complete entry per
  tool.
- Server wrappers, reply-key allow list, README, CHANGELOG and `lhm.plugin.json` follow.

## Out of scope
- Two CANVAS units, U-disk storage, timelapse (`delay_video`), `print_layout`, `bedlevel_force`:
  not sent, exactly as today.
- Any live printer call. The open live test below runs only on Nathan's yes.

## Done when
- `uv run pytest -q` passes in full and `uv run python scripts/gen_lhm_tools.py --check` passes.
- `parse_gcode_text` on `~/src/3d-printing/artifacts/penelope-tag/penelope-tag-grey-black-20261111-v2.gcode`
  (a copy of its first 300 and last 700 lines plus one tool-change block is the fixture; the real
  file is not committed) returns two tools, both PLA at 210 °C, extrusion temps (210,) and flush temps (240,).
- A one-tool file parses exactly as before (every existing upload/start test still passes unchanged
  except where its text asserted the one-tool refusal).
- Against a scripted transport, a two-tool start with `{0: 1, 1: 2}` sends one 1020 whose
  `config.slot_map` is `[{"t":0,"canvas_id":0,"tray_id":1},{"t":1,"canvas_id":0,"tray_id":2}]`; a
  mapping missing a tool, naming a tool the receipt lacks, or pointing two tools at one tray refuses
  with nothing sent; a tray whose material or rated range does not fit its tool's printing
  temperatures refuses; a flush temperature up to 10 °C over the tray's rated maximum is noted,
  beyond that it refuses.

## Open live test (Nathan's yes, after merge and install)
Upload the Pēnelópē v2 G-code with `cc2_upload_gcode`, then `cc2_start_print` with
`tool_to_tray={"0":"1","1":"2"}`, `bed_clear=True`, `printer_check=False`, and watch the first
filament change.
