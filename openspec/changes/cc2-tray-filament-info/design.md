# Design: `cc2_set_tray_filament`

Read `protocol-evidence.md` in this folder first; every wire fact below comes from it.

## Module
New file `src/orcaslicer_mcp/printer/cc2_tray.py` (NOT named `cc2_filament*`: `tests/test_cc2_filament.py`
forbids that string in the roster, README and manifests). Mirror `cc2_canvas.py` (auto-refill)
function for function; import the acceptance vocabulary from `cc2_control` (`ControlStatus`,
`NOTE_CAP`), never reword it.

```
METHOD_SET_FILAMENT_INFO = 2003      # SetFilamentInfo
SET_WAIT_S = 10.0                    # client passes no override (5 s default); integration waits 10 s; longer wins
UNVERIFIED = "..."                   # sentence: never offered to a CC2 by anyone on this project
CATALOG: dict[str, Filament]         # 50 entries from filament-catalog.txt: name -> (type, code, min, max, elegoo, generic)
```

### Inputs (tool arguments) and the frame
`tray_id: int` (0-3, the printer's own numbering: label A0..A3), `filament_name: str` (exact catalog
key, e.g. "PLA", "PLA+", "PETG"; refuse unknown before any session, list the valid names in the
refusal), `colour: str` (`#RRGGBB`, case-insensitive on input, sent upper-case), `brand: str = "Generic"`
(non-empty, ≤ 32 chars, stripped; "ELEGOO" and "Generic" are what the vendor dialog offers, free text is
allowed because the dialog allows it), `canvas_id: int = 0`, `confirm: bool = False`.

Exact params sent, all nine fields every time, nothing else:
```
{"canvas_id": canvas_id, "tray_id": tray_id, "brand": brand,
 "filament_type": CATALOG[name].type, "filament_name": name, "filament_code": CATALOG[name].code,
 "filament_color": "#RRGGBB", "filament_min_temp": CATALOG[name].min, "filament_max_temp": CATALOG[name].max}
```
Type, code and temperatures are never caller-supplied (the vendor UI derives them; a caller-supplied
temperature would be the guess). The write spells temps `filament_min_temp`/`filament_max_temp`
while the 2005 read reports `min_nozzle_temp`/`max_nozzle_temp`; keep both spellings straight.

### Two phases
- `read_preview(transport, params)`: one read-only 2005 (`cc2_state.canvas()` style read, but like
  cc2_canvas read the raw frame so an unreadable tray stays "unknown" rather than raising). Reply:
  `confirm_required: True`, `action: "set_tray_filament"`, `preview: {will_send: {method: 2003, params}, waits_up_to_s,
  tray_now: {tray_id, material, name, brand, colour, min_nozzle_temp, max_nozzle_temp, status}}`,
  `verified_on_this_printer: False`, `unverified`, `notes`. Gate (both phases): the (canvas_id, tray_id)
  pair must appear in the 2005 reply and its unit must report connected, else refuse before any
  command (a tray the printer never named cannot be written). A 2005 that will not answer refuses the
  change (unlike auto-refill, a tray write without a readable tray roster is a write to nowhere).
  Job-state gate (review finding, 2026-11-11): both phases also read 1002 once (`cc2_state.fetch_machine_status`) and refuse unless `ready_for_job()` says the machine is idle, exactly as `cc2_motion` gates a move; the vendor edit dialog has no such gate, but a tray whose recorded type changes while the printer feeds from it is a mid-print surprise nobody has tested, so the write waits for an idle machine. The preview reports the machine state it read.
- `set_tray_filament(transport, params)` → `Cc2TrayResult.as_dict()`: the gate read above, then exactly
  one 2003, `SET_WAIT_S`, classified accepted (`error_code` 0) / refused (nonzero, kept, no follow-up
  read) / ambiguous (timeout, unreadable code, dead session; never resent). After an accepted command, or a timeout on a still-usable session, one
  quiet 2005 (an unreadable code or a dead session earns no read, the bar cc2_canvas/cc2_control/cc2_start hold, because a read there would tempt a second command); `observed` carries that tray's fields; a `_contradiction_note` when the observed
  name/type/colour differ from what was sent (the auto-refill readback lagged, so say "may lag").

### server.py
Clone `_cc2_auto_refill_tool` → `_cc2_tray_filament_tool(params, confirm)`; `@mcp.tool() cc2_set_tray_filament(
tray_id, filament_name, colour, brand="Generic", canvas_id=0, confirm=False)` with Annotated/Field
descriptions; parse with `cc2_tray.parse_request(...)` before any session (PrinterError → `_cc2_err`).
Add to `CC2_TOOL_NAMES`, `_TOOL_ANNOTATIONS` (`("Set a CC2 CANVAS tray's filament info", False, False)`),
`_OPEN_WORLD_TOOLS`; add every new reply key to `_CC2_REPLY_KEYS` (regex `_?[a-z][a-z0-9_]+`); update
the "sixteen"/"fourteen of the sixteen" prose to seventeen / fifteen of the seventeen.

### Tests
`tests/test_cc2_tray.py` copies `tests/test_cc2_canvas.py`'s `FakeCanvasTransport`/`frame_2005`/`scripted`
pattern with a four-tray `canvas_list` (reuse the roster shape from `tests/test_server_cc2.py` `CANVAS`
/ `_tray`). Pin: catalog lookups and refusals, colour normalisation, brand bounds, the nine-field frame
byte-for-byte, preview sends `[1002, 2005]` only, confirmed sends `[1002, 2005, 2003, 2005]`, refusal `[1002, 2005, 2003]`, a printing or paused 1002 refuses with no 2005 and no 2003,
ambiguous never resent, unknown tray refuses with no 2003, notes ≤ `NOTE_CAP`, `verified_on_this_printer`
false everywhere, no token/host in replies. `tests/test_server_cc2.py`: add both lambdas to `ALL_TOOLS`,
the reply-key walk, an input-schema block (`required == {"tray_id","filament_name","colour"}`), the
classify tuple, `PROBE_CALLS` preview + confirmed entries, prose counts.

### Docs
README direct-printing section: one bullet (model it on the auto-refill bullet; name 2003, the nine
fields, the catalog, the idle-machine gate, the open live test), counts 16→17 and 9-of-16→10-of-17,
reword the "2004 sits beside 2003" sentence. CHANGELOG `### Added` bullet in the house style.
Regenerate `lhm.plugin.json` with `uv run python scripts/gen_lhm_tools.py`.
