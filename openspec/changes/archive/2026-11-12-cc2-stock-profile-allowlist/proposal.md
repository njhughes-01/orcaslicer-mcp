# Stock-profile allow list: close the gaps a stock Elegoo CC2 slice still hits

## Why
Nathan (2026-11-11): the upload check should follow Elegoo's own stock profiles instead of
hand-reading every command. A three-agent research sweep plus a verifier (`inventory.md`,
`stock-templates.md`, `firmware-macros.md` in this folder; full set in
`~/docs/3d-printing/cc2-gcode-allowlist/`) produced the complete inventory: 41 command heads,
every command in all 11 real CC2 slices explained, and nine gaps where a stock slice is still
refused today. Two of them need only stock presets (and one is Nathan's own Inland PLA copy):
a first-layer temperature that differs from the rest, and a two-colour change between filaments
at different temperatures. The sweep also found that the parser never checks the printer model
(a Centauri 2 slice passed), that `BED_MESH_CALIBRATE` may heat on its own, and that the tray a
print really runs from comes from the printer's persistent colour table, not the file's `T`.

## What changes
1. **Printer-model gate.** A file whose config block does not say
   `printer_model = Elegoo Centauri Carbon 2` refuses before any byte is sent.
2. **Layer-two setpoint change.** A bare `M104` to the current tool's declared
   `nozzle_temperature` after a verified wait at its `nozzle_temperature_initial_layer` is the
   slicer's own first-layer-to-rest transition: allowed without a new wait, both temperatures
   recorded as that tool's extrusion temperatures.
3. **Mixed-temperature tool change.** The wipe tower's `M104 S<next tool's temperature>` and
   its retract before the purge, and a purge whose `Q` is the wait in force and whose `S` is the
   next tool's declared temperature, are the slicer's own shape for two filaments at different
   temperatures: allowed under the rules design.md states.
4. **Bed mesh resets the wait.** After `BED_MESH_CALIBRATE` no extrusion is allowed until a fresh
   `M109` (the stock start template has one), because the firmware's mesh library can heat.
5. **Stock-template identity.** The file's embedded start, end and change-filament templates are
   compared with the stock OrcaSlicer 2.4.2 Elegoo CC2 templates (verbatim in
   `stock-templates.md` §B); a match is reported as `vendor_templates: stock`, a difference is
   reported (never refused) naming which template differs.
6. **Tray-table note on starts.** Every start reply says that the firmware chooses the physical
   tray from its persistent colour table inside `M6211`, not from the file's `T`, so the mapping
   sent is a request the printer's own table may override.
7. Text fixes the sweep found (fourteen purge letters, G180 S1–S997, the bed-mesh comment,
   the `M600` refusal naming its 250 °C wait), README, CHANGELOG, listing.

## Out of scope
- Admitting `M600`, chamber heating (`M141`/`M191`), `G10`/`G11`, `EXCLUDE_OBJECT_*`, `G17`,
  `SET_INPUT_SHAPER`, `M82`: each stays refused with the evidence in `inventory.md` §D.
- Reading the printer's colour table over the LAN protocol (unverified whether exposed).
- Any live printer call.

## Done when
- `uv run pytest -q` passes in full; `uv run python scripts/gen_lhm_tools.py --check` passes.
- The Pēnelópē v2 fixture (`tests/fixtures/cc2_two_tool.gcode.xz`) still parses to two PLA
  tools, extrusion (210,), flush (240,), with `vendor_templates == "stock"`.
- A frame with `printer_model = Elegoo Centauri 2` refuses (`printer_model`).
- A one-tool frame with first layer 220 / rest 225 and the generator's layer-two `M104 S225`
  with no wait parses, with extrusion temps (220, 225).
- The Pēnelópē frame edited to PLA 210 → PLA Basic 220 (tower `M104 S220`, `G1 E-2`, purge
  `Q210 R240 S220`) parses with tool 1 at 220; the same frame with purge `Q230` refuses.
- After a `BED_MESH_CALIBRATE` with no later `M109`, the first `G1 E` refuses.
- Every start reply carries the tray-table note.
