# CC2 CANVAS tray filament info write (`cc2_set_tray_filament`, method 2003)

## Why
On 2026-11-11 the CANVAS reported A1 = ASA-CF and A2 = PETG while the spools physically loaded were
grey PLA and black PLA (Nathan swapped spools without updating the trays). OrcaSlicer's Send dialog
then warned `type_mismatch` / `temp_out_of_range` on both trays and the printer would refuse or
mis-heat. The fork had no way to correct a tray's recorded filament; only the touchscreen or the
Elegoo app could. Nathan asked for it in the MCP ("oh we need to add that to our mcp then").

## What changes
- A new direct CC2 tool `cc2_set_tray_filament` that writes one CANVAS tray's filament info with
  method **2003 `SetFilamentInfo`**, the exact nine-field frame Elegoo's own LAN web client sends
  (see `protocol-evidence.md`), two-phase like every other CC2 control: without `confirm=True` it
  sends only the read-only 2005 and shows what it would send.
- The filament catalog Elegoo's client ships (50 names → type, code, nozzle range; see
  `filament-catalog.txt`) becomes data in the new module, so type, code and temperatures are derived
  from the chosen name exactly as the vendor UI does; a name outside it refuses before any session.
- Roster, annotations, open-world table, reply-key allowlist, README, CHANGELOG and the generated
  `lhm.plugin.json` updated; sixteen direct tools become seventeen.

## Out of scope
- `SetMonoFilamentInfo` (1055, printers without CANVAS), load/unload, deletion, fan.
- Any live printer call. Every reply carries `verified_on_this_printer: false` until the open live
  test below has run with Nathan's yes.

## Done when
- `uv run pytest -q` passes in full (all existing tests plus the new `tests/test_cc2_tray.py`).
- `uv run python scripts/gen_lhm_tools.py --check` passes (listing regenerated).
- With no printer address configured, `cc2_set_tray_filament` answers `not_configured` and opens
  nothing (covered by the zero-network sweep and the fresh-interpreter probe).
- Against a scripted transport: the preview sends only the read-only 1002 and 2005; a confirmed call
  sends exactly one 2003 with the nine fields after those two reads and then one 2005; a printer that is
  not idle refuses with no 2003; a refusal (nonzero `error_code`) earns no read; a lost
  answer is ambiguous and never resent.

## Open live test (needs Nathan's yes, after merge and install)
Idle printer; preview for tray A1; `confirm=True` for A1 → PLA / Generic / grey; one 2005 read to
confirm the tray now reports PLA; repeat for A2 → PLA / Generic / black; record what the printer
answered and whether the 2005 readback lagged. Only then may the `verified_on_this_printer` label be
retired in a later change.
