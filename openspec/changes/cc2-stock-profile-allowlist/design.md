# Design: stock-profile allow list

Evidence: `inventory.md` (union table §A, gaps §D, open questions §E), `stock-templates.md`
(verbatim stock templates §B), `firmware-macros.md` (M6211 step order, G180 table, M109 ±5 °C).

## Decisions
1. **Printer-model gate** (`inventory.md` E7). `_WANTED_KEYS` gains `printer_model`; the parse
   refuses unless the value is exactly `Elegoo Centauri Carbon 2` (reason `printer_model`,
   message naming what the file said). `Cc2GcodeSettings` gains `printer_model: str`. The receipt
   is NOT changed (no version bump); the gate lives at upload.
2. **Layer-two transition** (gap D1; generator `GCode.cpp:4811`). Today an `M104` to a new value
   after a wait revokes extrusion until a matching `M109`. New rule: if the current tool's
   declared `nozzle_temperature_initial_layer` is the wait in force and the `M104` value equals
   that tool's declared `nozzle_temperature`, the wait stays armed, the new value becomes the
   target, and the value is appended to the tool's `extrusion_nozzle_temps` (both temperatures
   must then fit the tray at start). Any other bare `M104` keeps today's rule. Rationale: this is
   the slicer's own transition on every Klipper printer; the firmware's `M109` already tolerates
   ±5 °C (`firmware-macros.md` finding 4), and both values are the file's declared, per-tool,
   range-checked config.
3. **Mixed-temperature tool change** (gap D2; `inventory.md` B.2 O2). Observed order at a tower
   change: `M220 S100`, `G4 S0`, `M104 S<next>`, `G4 S0`, `G1 E-2`, travel, `M6211 T<next> L M N
   Q<old> R<range_high> S<next>`, `T<next>`. Rules:
   a. An `M104` whose value equals the declared `nozzle_temperature` of some tool other than the
      current one is a *pending tool change* setpoint: it does not revoke the wait for NEGATIVE E
      moves (retracts run at the old, still-waited temperature) but any POSITIVE E before the
      purge refuses; the pending value must be consumed by an `M6211` whose `S` equals it, else
      the next positive E refuses as today.
   b. The purge: `Q` must equal the wait in force (the old tool's verified temperature) and `S`
      must equal the declared `nozzle_temperature` of `purge.tool`; `R` keeps the range-high rule
      and the flush margin. `S` is waited by the firmware itself (M6211 step 6), so after the purge
      the wait in force is `S` and the current tool is `purge.tool`; `S` is appended to that tool's
      `extrusion_nozzle_temps`.
   c. Equal-temperature changes (today's shape) satisfy a and b unchanged.
4. **Bed mesh resets the wait** (`inventory.md` E2, `firmware-macros.md` finding 2). On
   `BED_MESH_CALIBRATE` set `waited = None` (target kept): the next extrusion needs a fresh `M109`.
   Stock start templates issue `M109 S[nozzle_temperature_initial_layer]` after it, so stock files
   pass; the comment claiming the command sets no nozzle target is rewritten to say the library
   is binary-only and may heat.
5. **Stock-template identity.** New data module `src/orcaslicer_mcp/printer/cc2_stock_templates.py`
   holding the verbatim 2.4.2 stock CC2 `machine_start_gcode`, `machine_end_gcode`,
   `change_filament_gcode`, `layer_change_gcode`, `before_layer_change_gcode` from
   `stock-templates.md` §B (copy exactly; keep a `SOURCE` constant naming the GitHub tag path).
   `_WANTED_KEYS` gains those five keys; the parse unescapes the config value (Orca writes the
   template on one line with `\n` escapes) and compares after stripping trailing whitespace per
   line. `Cc2GcodeSettings.vendor_templates: str` is `"stock"` when all present ones match,
   otherwise `"modified: <key>, <key>"`. Never a refusal; the upload reply carries it and a note.
6. **Tray-table note** (`inventory.md` E5, `firmware-macros.md` §C). `cc2_start.py` adds
   `_TRAY_TABLE_NOTE` to every result: the file's `T<n>` is inert on this firmware; the physical
   tray is chosen inside `M6211` from the printer's persistent colour table, which this adapter
   cannot read; the mapping sent is the request Elegoo's own client sends, nothing more. The
   `cc2_start_print` docstring says the same in one sentence.
7. **Text fixes**: `_PURGE_FIELDS` message "thirteen" → fourteen; G180 comment S1–S997; `M600`
   stays unread but `_command_policy` names its 250 °C `M109` in the refusal hint
   (`firmware-macros.md` "Others").

## Files
`src/orcaslicer_mcp/printer/cc2_upload.py`, new `cc2_stock_templates.py`, `cc2_start.py`,
`server.py`; `tests/test_cc2_upload.py`, `tests/test_cc2_start.py`, `tests/test_server_cc2.py`;
`README.md`, `CHANGELOG.md`, `lhm.plugin.json`.

## Not verified
Whether the running firmware 02.01.00.00 matches the published source (`inventory.md` E1); what
the bed-mesh library really heats (E2); the colour table's contents at start (E5).

## Amendment (2026-11-12, review rounds)
8. The layer-two step (decision 2) fires only within `LAYER_TWO_STEP_MAX_C` = 10 °C of the
   first-layer wait (stock presets step 5 or 10 °C; firmware M109 tolerance ±5 °C); a larger
   declared step needs its own M109. Both exceptions (2 and 3a) require the wait to be in force
   (`nozzle_target == waited` and not broken); any ordinary bare M104 that moves the target
   breaks the wait and only an M109 or a purge's own waited S mends it. The retract exception
   (3a) requires relative-E mode (M83 seen). A purge naming the tool in force, or any purge
   while no tool is in force yet, may be skipped by the firmware and so must keep S at the wait
   in force. "Some other tool" in 3a means a tool the file selects or purges to (pre-scan).
