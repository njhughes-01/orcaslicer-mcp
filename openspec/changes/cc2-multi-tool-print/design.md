# Design: multi-tool uploads and starts

## Decisions
1. **Wire.** Method 1020 params stay `{storage_media:"local", filename, config:{slot_map, printer_check}}`.
   `slot_map` becomes one entry per tool, each `{t, canvas_id, tray_id}` complete, ordered by `t`.
   This is byte-for-byte what Elegoo's LAN client builds for a multi-colour print
   (`protocol-evidence.md`); the extra keys it also sends (`delay_video`, `print_layout`,
   `bedlevel_force`) are NOT added — unchanged from today.
2. **Per-tool config.** `_config_value` splits on `,` and on `;` (Orca writes string lists with `;`
   and number lists with `,`). A per-filament key yields one value per tool, indexed by tool number;
   a file whose list is shorter than its highest tool refuses. Tools are the distinct `T<n>` the file
   selects, in first-seen order. `filament_type`, `nozzle_temperature`, `nozzle_temperature_initial_layer`
   are per tool; plate temperatures stay single (every tool shares the bed; a list that does not
   dedupe to one value refuses as now).
3. **Purge R rule.** Today an `M6211` whose Q/R/S differ from the verified wait refuses. The vendor's
   own `change_filament_gcode` sets `R=[nozzle_temperature_range_high]`, so every multi-colour file
   trips it. New rule, applied only to R: R may differ from the verified target when R equals the
   declared `nozzle_temperature_range_high` of the tool being left AND of the tool being switched to
   (both read from the file's config list), R is within the sanity fence, and R is appended to
   `extrusion_nozzle_temps` for both tools so the start refuses any mapped tray whose rated maximum
   is below it. Q and S keep today's rule unchanged. Rationale: the flush really extrudes at R
   (`_PURGE_LETTER_WHY["R"]`), so R is treated as an extrusion temperature the spool must be rated
   for, not waved through.
4. **Tool switches.** `T<n>` may appear for several n. A purge's `T` must name a tool the file
   selects (today's rule, now with several tools legal). The first `T` after the start block fixes
   the tool for the first extrusion as now; after a purge the current tool is the purge's T.
5. **M220.** Added to `_SAFE_CODE_COMMANDS` with reason "speed factor override: scales feedrates of
   moves already gated; commands no heater and no filament", parameters enforced via
   `_PARAMETER_LETTERS` as `S` only.
6. **Receipt v3.** `Cc2UploadReceipt` gains `tools: tuple[ToolReceipt, ...]` where `ToolReceipt`
   is `(tool_index, material, nozzle_first_layer_temp, nozzle_temp, extrusion_nozzle_temps)`.
   `material`, `tool_index`, `nozzle_*` and `extrusion_nozzle_temps` stay as the first tool's
   values for readers that know only them. `RECEIPT_VERSION = 3`; `from_dict` reads version 2 by
   building a one-entry `tools` from the flat fields, and refuses anything else as now.
7. **Start.** `tool_to_tray` is a dict with 1–4 entries whose key set equals the receipt's tool set
   exactly (missing or extra tool refuses, reason `tool_map_mismatch`); two tools on one tray refuses
   (`tool_map`). Every entry runs today's per-tray checks against its own tool's material and
   temperatures plus the shared purge R. `slot_map` is built from the trays that just answered.
8. **Server.** `_cc2_tool_map` accepts 1–4 string-or-int pairs; the upload reply gains `tools`
   (list of `{tool_index, material, nozzle_temp_c}`) and keeps `material`/`tool_index` for the first
   tool; the start reply's `sent.slot_map` shows every entry. New reply keys go in `_CC2_REPLY_KEYS`.

## Files
`src/orcaslicer_mcp/printer/cc2_upload.py`, `cc2_receipts.py`, `cc2_start.py`, `server.py`;
`tests/test_cc2_upload.py`, `tests/test_cc2_start.py`, `tests/test_server_cc2.py`,
`tests/fixtures/cc2_two_tool.gcode` (new, built from the real file's head, one change block, tail);
`README.md`, `CHANGELOG.md`, `lhm.plugin.json`.

## Not verified
Whether the firmware honours a two-entry `slot_map` is proven only by Elegoo's client sending it;
the fork has never sent one. Every reply of a multi-tool start carries a note saying so until the
live test runs.

## Amendment (2026-11-11, after task 1.2): flush temperature is not a printing temperature
Elegoo's catalog rates every PLA tray 190–230 °C while Elegoo's own `change_filament_gcode` flushes PLA
at `nozzle_temperature_range_high` = 240 °C. Decision 3 as written would refuse every PLA multi-colour
start on the vendor's own numbers. Decision 9 replaces the "appended to extrusion_nozzle_temps" part
of decision 3:
9. **Flush temperatures are their own field.** `ToolSettings` and `ToolReceipt` gain
   `flush_nozzle_temps: tuple[float, ...]` (every purge R the tool was left or entered at, distinct,
   first-seen order); R is NOT appended to `extrusion_nozzle_temps`. The parser still requires R to
   equal both tools' declared `nozzle_temperature_range_high` (so a flush hotter than the file's own
   declared range still refuses). At start, the tray's rated range must cover `extrusion_nozzle_temps`
   (printing temperatures verified by an M109 wait) and no commanded setpoint may exceed the rated
   maximum except a flush temperature, which is compared to the tray's rated maximum and reported in a
   note either way ("flush at 240 °C against tray rated to 230 °C: the vendor's own profile and
   catalog disagree by 10 °C; not refused"). Rationale: both numbers are Elegoo's; refusing their own
   default makes the feature useless for PLA, while the file's declared range already bounds the flush.

## Amendment (2026-11-11, after Opus review round 1)
10. **Flush margin.** The note-not-refuse rule of decision 9 holds only within `FLUSH_MARGIN_C`
    = 10 °C of the tray's rated maximum (the measured vendor gap for PLA). A flush further above
    the rating refuses (`flush_over_tray_max`), because the upload bounds R only by the file's
    own declared range-high, which is file text, not a vendor number.
11. **Tool changes only through the purge.** A bare `T<n>` naming a different tool than the one
    in force refuses the file: the vendor's slicer changes tool only through `M6211 T<n>` (which
    switches the filament, then restates `T<n>`), and what a bare `T<n>` does on this firmware
    is unread.
12. **Spare filaments.** A per-tool config list longer than the file's highest selected tool is
    read and the extra entries ignored (tools the file never selects never extrude and are never
    mapped); the plan's earlier "more values refuse" is withdrawn.
13. **Receipt union.** The flat `extrusion_nozzle_temps` must equal the union of the tools'
    lists in both directions, and every entry must be a commanded setpoint; the looser rule task
    1.3 allowed is dropped.
