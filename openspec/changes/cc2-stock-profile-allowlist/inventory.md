# INVENTORY: the CC2 G-code union, checked against R1-R3, the fork and every real slice

Verifier, 2026-11-11. Read-only: no printer contact, no repo edits, no `~/.config` or `.env` reads. This file is the only thing written outside the session scratchpad. The fork was read at `~/src/orcaslicer-mcp` main `87441ff`, file `src/orcaslicer_mcp/printer/cc2_upload.py` (cited below as `upload.py:<line>`).

Sources: **R1** = `R1-orca-profile-templates.md`, **R2** = `R2-slicer-emitted-commands.md`, **R3** = `R3-firmware-macros.md` (sections cited as R1§A, R2#17, R3§B, and so on).

Extra evidence gathered here:
- **Head sets** of 12 real Orca slices on Spinx, the fork's fixture, and the Mac files (section C).
- **The fork's own parser** (`parse_gcode_settings`), run locally on every real file and on five in-memory variants. These runs made no printer contact. Script: `/home/killerherts/scratch/tmp/claude-1000/-home-killerherts-src-3d-printing/5423a388-008a-4b3c-8a7b-0b44a5e1ece9/scratchpad/synth.py`, deleted after the run; its logic is restated in D so it can be redone.
- **Resolved nozzle temperatures** for all 41 CC2-compatible stock filament presets in the Mac's installed OrcaSlicer 2.4.2. I resolved the `inherits` chains and copied off only the numbers.

**Counting rule.** N counts only what a stock CC2 slice can contain: R1§A plus R2 table A, including the conditional rows. Keys:
- `G180` is split by S value.
- `M6211` is split by letter set.
- `M106` is split by P index.
- `T<n>` is one key.
- Every other head is one key, with its parameter forms in the row.

R2§A2 (unreachable for this printer) and R3's macros that no slice emits are listed separately and left out of N.

## A. Union table

Column meanings:
- **Src**: T = template (R1), G = generator (R2).
- **Stock?**: "yes" = emitted with the stock CC2 profile; "cond" = needs a non-default setting or a user action.
- **FW effect** (R3): H = heats, W = waits, E = extrudes or moves filament, M = moves axes, I = inert, ? = R3 silent or binary-only.
- **Fork**: ✓ = allowed; ✓* = allowed with a state or parameter rule that can still refuse; ✗ = refused.
- **Seen**: number of real CC2 files (out of 11 CC2-profile Orca slices, section C) that contain it.

| # | Key | Src (where) | Parameter forms | Stock? | FW effect (R3) | Conf. | Fork | Seen |
|---|---|---|---|---|---|---|---|---|
| 1 | G1 | T start/end/change_filament (R1§A row 2); G everywhere (R2#1) | X Y Z E F; negative Y and E; `G1 F` only; spiral-lift XYZ | yes | M, E | src | ✓* (any E is gated by a verified M109, `upload.py:1342-1378`) | 11 |
| 2 | G2 | T end `G2 I0 J1 Z F3000` (R1 row 3); G only with arc fitting (R2#2) | I J Z F; generator adds X Y E, and P1 on spiral lift | yes (template) | M (also used inside G180 S2, R3§B) | src | ✓* (E gated) | 11 (template only) |
| 3 | G3 | G with arc fitting only (R2#2) | X Y I J E / Z I J P1 F | cond (`enable_arc_fitting=1`) | M (R3§B S2 body uses it) | src | ✓* | 0 |
| 4 | G17 | G, spiral lift with arc fitting (R2#3) | bare | cond | ? (not checked in R3) | src | ✗ (unread code) | 0 |
| 5 | G21 | G preamble (R2#5) | bare | yes | I (registered, R3 "Others") | src | ✓ | 11 |
| 6 | G28 | T start (R1 row 4) | bare | yes | M | src | ✓ | 11 |
| 7 | G90 | T start/end (R1 row 1); G preamble (R2#4) | bare | yes | I | src | ✓ | 11 |
| 8 | G92 | T start/end/before_layer (R1 row 5); G wipe tower (R2#8) | `E0` only | yes | I | src | ✓ | 11 |
| 9 | G4 | G wipe tower (R2#10) | `S0` at every tower tool change; `S<sec>` only with ramming or MMU3 | yes (multi-colour) | I (registered) | src | ✓* (P/S/D only, `upload.py:355-359`) | 2 (`S0` only) |
| 10 | G10 | G with firmware retraction (R2#9) | bare | cond (`use_firmware_retraction=1`) | I: no handler, "Unknown command" (R3, no-handler list) | src | ✗ (blocked, `upload.py:378`) | 0 |
| 11 | G11 | same (R2#9) | bare | cond | I: no handler (R3) | src | ✗ (blocked) | 0 |
| 12 | M82 | G when relative E is off (R2#7) | bare | cond (`use_relative_e_distances=0`) | mode (registered) | src | ✗ (unread) | 0 |
| 13 | M83 | T start/end (R1 row 8); G preamble (R2#6) | bare | yes | mode | src | ✓ | 11 |
| 14 | M84 | T end (R1 row 9) | bare | yes | ? (R3 silent; Klipper: motors off) | src | ✓ | 11 |
| 15 | M73 | T layer_change `L` (R1 row 7); G post-process `P R` (R2#21) | `L<n>`; `P R`; `Q S` in stealth mode; `C`/`D` with stop events | yes (L, P R); cond (Q S C D) | I (R3 "M73") | src | ✓ (no parameter check) | 11 (L, P R only) |
| 16 | M104 | T start `S140`, end `S0` (R1 row 10); G wipe tower first change and temperature change (R2#11); G layer 2 when the initial-layer temperature differs (R2#11, GCode.cpp:4811) | `S<int>` | yes (template, tower); **cond on preset** (layer 2) | H (R3 Heaters) | src | ✓* (a new target revives the M109 requirement, `upload.py:1265-1270`) | 11 (template); 2 (tower `S210`) |
| 17 | M109 | T start ×2 (R1 row 12); G tower interface only (R2#12) | `S<int>` | yes | H, W within ±5 °C (R3 finding 4) | src | ✓ (the evidence every extrusion rides on) | 11 |
| 18 | M140 | T start/end (R1 row 13); G layer 2 (R2#13) | `S<int>` | yes | H (bed) | src | ✓ | 11 |
| 19 | M190 | T start `S<t> A` (R1 row 14); G by-object path only (R2#14) | `S<t>` plus a bare `A` flag | yes | H, W (bed); the lone A is dropped (R3 Heaters) | src | ✓ (a bare flag is tolerated, `upload.py:241`) | 11 |
| 20 | M141 | G chamber control (R2#15) | `S<t>` | cond (`activate_chamber_temp_control`) | I: no handler (R3) | src | ✗ (unread) | 0 |
| 21 | M191 | G chamber control, wrapped by `M106 P2 S255`/`S0` (R2#16) | `S<t>` | cond | I: no handler (R3) | src | ✗ (unread) | 0 |
| 22 | M106 (no P) | T start/end/change_filament (R1 row 11); G per layer (R2#17) | `S0-255` | yes | I: part fan; a missing S means 255 (R3 Fans) | src | ✓ (no parameter check) | 11 |
| 23 | M106 P2 | T end/change_filament; G before start/end and after tool change (R1 row 11, R2#17) | `P2 S<n>` | yes | I: aux fan (`fan1`) | src | ✓ | 11 |
| 24 | M106 P3 | T filament_start for group-A presets (R1 row 11, group A); G with air filtration (R2#17) | `P3 S200/S150/S50`; `P3 S<exhaust>`; library `P3 S0` (R1§D6) | yes (group-A filament on a bed above 45 °C) | I: P3 handler commented out, a no-op (R3 Fans) | src | ✓ | 1 (penelope v2); also in the fixture and the Centauri 2 file |
| 25 | M204 | T start (R1 row 15) | `S1000-20000` | yes | I (registered) | src | ✓ | 11 |
| 26 | M220 | G wipe tower (R2#22) | `S100` only (B/R are Marlin) | yes (multi-colour) | I (registered) | src | ✓* (S only) | 2 |
| 27 | M600 | T `machine_pause_gcode` at a user pause (R1 row 16, R2#27) | bare | cond (user inserts a pause) | **H, W: `M109 S<TEMP>`, default 250, plus filament_load_gcode_1** (R3 "Others") | src | ✗ (unread) | 0 |
| 28 | M6211 {A L Q R S T} | T start (R1 row 17) | `A1 L200 T<n> Q R S` = first-layer temperature | yes | H, W, E: Q waited (A=1), flush at R, final wait at S, CANVAS switch (R3 M6211) | src+cfg | ✓* (Q = S = verified target; R exception; 200-350 clamp, `upload.py:965-984, 1306-1322`) | 11 |
| 29 | M6211 {L M N Q R S T} | T change_filament (R1 row 17), including the start-of-print initial-tool block (see B, O1) | `T L<float> M N Q[old] R[range_high] S[new]`; A omitted, so A=0 | yes (multi-colour) | H (Q unwaited with A=0), E under unwaited Q and at R; W at S (R3 finding 6) | src+cfg | ✓* (same rule) | 2 |
| 30 | T\<n\> | T start `T[initial]`, change_filament `T[next]` (R1 row 18); G bare `T0` in a multi-slot project that prints only slot 0 (R2#23a) | `T0`..`T31` | yes | **I: no handler; the tray is chosen only inside M6211** (R3§C) | src | ✓* (a bare change without a preceding M6211 is refused, `upload.py:1387-1410`) | 11 (`T0`); 2 (`T1`) |
| 31 | G180 S7 | T start, in the `first_layer_print_min[1] > 0.5` branch (R1 row 6) | `S7` | yes | I: mesh profile load (R3§B) | cfg | ✓ (`upload.py:429`) | 11 |
| 32 | G180 S8 | T start (R1 row 6) | `S8` | yes | I: adaptive mesh load | cfg | ✓ | 11 |
| 33 | G180 S9 | T end (R1 row 6) | `S9` | yes | E, H, W: conditional unload; may extrude about 60 mm in the channel check; 140 or 250 °C waits (R3§B) | cfg | ✓* (`ends_print`: nothing may extrude after it) | 11 |
| 34 | BED_MESH_CALIBRATE | T start (R1 row 19) | `mesh_min= mesh_max= ALGORITHM= PROBE_COUNT= ADAPTIVE=0 ADAPTIVE_MARGIN=0 FROM_SLICER=1` | yes | **?: binary only; its strings include M140/M190, `M109 S140`, G180 S3/S5/S6** (R3 finding 2) | inf | ✓ (as "sets no nozzle target": disputed, see B6) | 11 |
| 35 | SET_PRINT_STATS_INFO | T start/layer_change (R1 row 20) | `TOTAL_LAYER= CURRENT_LAYER=` | yes | I (R3, `print_stats.cpp`) | src | ✓ | 11 |
| 36 | SET_VELOCITY_LIMIT | G (R2#18) | `ACCEL=` (stock); `ACCEL_TO_DECEL=`, `SQUARE_CORNER_VELOCITY=`, `MINIMUM_CRUISE_RATIO=0` (cond) | yes | ? (R3 did not check; Klipper: limits only) | src (slicer) | ✓ (no parameter check) | 11 (ACCEL only) |
| 37 | SET_PRESSURE_ADVANCE | G (R2#19) | `ADVANCE=<pa>`; `ADVANCE=0` with ramming | yes | ? (R3 did not check; Klipper: flow tuning) | src (slicer) | ✓ | 11 |
| 38 | SET_INPUT_SHAPER | G, calibration modes only (R2#20) | `SHAPER_TYPE= SHAPER_FREQ_X/Y= DAMPING_RATIO_X/Y=` | cond (calibration print) | ? | src | ✗ (word not listed) | 0 |
| 39 | EXCLUDE_OBJECT_DEFINE | G (R2#24) | `NAME= CENTER= POLYGON=` | cond (`exclude_object=1`; the stock process sets 0, R1 Presets) | ? (R3 did not check) | src | ✗ | 0 |
| 40 | EXCLUDE_OBJECT_START | G (R2#25) | `NAME=` | cond | ? | src | ✗ | 0 |
| 41 | EXCLUDE_OBJECT_END | G (R2#26) | `NAME=` | cond | ? | src | ✗ | 0 |

**Not in N, recorded for completeness:**
- **Left out with evidence.** No real file contains any of these:
  - G0: the generator never writes it (R2#1).
  - M107: no caller (R2§A2).
  - M201/M203/M205: Marlin and RRF only (R2§A2, row 40).
  - Everything else in R2§A2: BBL-only, Marlin, RRF, MakerWare and Machinekit codes.
- **Firmware macros that no slice emits** (R3§A):
  - SET_CONDITION / SET_CONDITION_END and BED_MESH_PROFILE appear only inside G180 bodies.
  - CANVAS_* and PRINTER_UNLOAD_FILAMENT run only inside M6211 and G180.
  - M2202 and M400 are firmware-internal.
  - The fork refuses all of them in files, which is correct: no stock emitter exists.
- **Fork allows, no emitter at all:** G0 and M107. These are harmless: G0 is E-gated, and M107 turns the part fan off. R3 (Fans) says M107 ignores P, so the fork's `{P,S}` set for M107 (`upload.py:357`) admits a P that does nothing.

## B. Contradictions, omissions, and brief-versus-reality

### B.1 Contradictions (report vs report, or report vs fork): 15

| # | Claim A | Claim B | Resolution |
|---|---|---|---|
| 1 | R1§D1: `emit_machine_limits_to_gcode` defaults true, so M201/M203/M205 lines appear | R2 row 40 and §A2: Klipper never gets the machine-envelope block (GCode.cpp:3939-4000) | **R2 is right.** Zero M201/M203/M205 in all 11 CC2 slices (C) |
| 2 | R1§D1: the slicer core writes M107 for the fan | R2§A2: `WipeTowerWriter2::set_fan` has no caller; the writer uses `M106 S0` | **R2 is right.** Zero M107 in all files |
| 3 | R1§D1: G0 moves come from the core | R2#1: no G0 emitter in libslic3r | **R2 is right.** Zero G0 in all files |
| 4 | R1§D1: M204 per-feature acceleration on Klipper | R2§A2: Klipper uses SET_VELOCITY_LIMIT; the one M204 is template text | **R2 is right.** Exactly 1 `M204 S5000` per file |
| 5 | R1§D2: `printer_agent = bbl` in Nathan's "- Copy" machine may switch `is_BBL_printer()` branches | R2 row 2: BBL is set only for a printer_model starting "Bambu Lab" | **R2 is right, confirmed by observation.** penelope v2 and eva-chube were sliced from the "- Copy" profile and contain no M1020/M981/M976/M400/M624 |
| 6 | Fork `upload.py:362-364`: BED_MESH_CALIBRATE "sets no nozzle target" | R3 finding 2: binary strings show `M109 S140`, `M140 S`/`M190 S`, `G180 S3/S5/S6` | **Unresolved** (binary only). Stock files are safe only because `M109 S<first layer>` follows. See E2 |
| 7 | Fork `upload.py:984` text and its refusal message say "thirteen" M6211 letters | R3 M6211: the firmware reads 14 | **Text-only.** I counted the sets: `_PURGE_FIELDS` (T Q R S A) plus `_PURGE_OTHERS` (K L M N I J P C D) = 14. Behaviour is right |
| 8 | Fork `_PURGE_FIELDS`: Q/R/S clamp 200-350 (published source) | R3 "Source basis": the ElegooSlicer July 2026 model clamps at 185-350 | **Unresolved for the running firmware, moot for stock presets.** All 41 CC2 stock filament presets resolve to initial and other-layer temperatures ≥ 210 and range-high ≥ 220. No stock slice writes Q/R/S below 200 |
| 9 | Fork comment `upload.py:396`: G180 loads "S1…S998" | R3 G180: S998 can never be selected | **Text-only.** The fork admits only 7, 8 and 9 |
| 10 | Fork S9 body text omits the channel-check extrusion and the 250 °C `clean_waste` path | R3§B S9 row | **Covered.** `ends_print` already forbids anything after it (R3 agrees). Detail gap only |
| 11 | Fork: a bare `T<n>` is the tool select a receipt is built on | R3§C: no handler, inert; the tray comes from a persistent colour table read inside M6211 | **Partly resolved.** The fork refusing bare changes is conservative and correct. But a receipt's "tool" is not evidence of which physical tray runs. See E5 |
| 12 | Fork `_BLOCKED_COMMANDS`: G10/G11/G12 "may move filament" | R3: no handler, a no-op | **No action.** Keep refused either way; the running firmware is unverified |
| 13 | R1 row 11: M106 P3 is emitted | R2#17: "P3 no" for CC2 | **Both right.** R1 is the filament_start template, R2 the generator path. Files show P3 only from the template (group-A PLA on a 60 °C bed). R3: P3 is a no-op |
| 14 | Fork `_PARAMETER_LETTERS["M107"] = {P,S}` "a fan's index (P)" | R3 Fans: M107 ignores every parameter, including P | **Harmless.** M107 turns the part fan off whatever P says; no slice emits M107 |
| 15 | R1 row 19: the template writes `ADAPTIVE_MARGIN=0` (upper case) | R3 BED_MESH: the binary string is `adaptive_margin` (lower case) | **Unresolved, minor.** Whether Elegoo's C++ port folds parameter case is unread. Impact unknown, and it is the same in every stock file |

### B.2 Omissions (one report should have covered something): 6

- **O1. The start-of-print change_filament block.** Every multi-colour slice runs change_filament once at print start for the initial tool, before layer 1. penelope v2 lines 191-197: `M106 S0`, `M106 P2 S0`, `G1 Z3 F1200`, `M6211 T0 L0 M523 N523 Q210 R240 S210`, `T0`.
  - R1 does not say change_filament runs at start. R2 B1 counts 7 such M6211 for "6 tool changes" without explaining the seventh.
  - Firmware (R3 M6211): step 0 exits early when T0's channel is already the one loaded, which the start block's `M6211 A1 ... T0` just did. Otherwise `L0` is clamped to 10 mm.
  - The fork accepts it: L is read only as a number, `upload.py:984`.
- **O2. Wipe-tower order.** R2 does not say where the tower's `M104 S<new>` sits. In penelope v2 at lines 1353869-1353892 the tool-change block runs `M220 S100`, `G4 S0`, **`M104 S210`**, `G4 S0`, **`G1 E-2`**, travel, then change_filament (`M6211 …`, `T1`). So the new-filament setpoint and a retract come **before** the M6211. This decides gap D2.
- **O3. Layer-2 M104 for stock presets.** Neither R1 nor R2 checked which stock presets trigger R2#11's layer-2 `M104`. My resolution of the 41 presets: **Generic ABS @Elegoo Centauri (260→270)** and **Generic ABS-CF @Elegoo Centauri (250→260)**. Nathan's own `inland PLA @Elegoo Centauri 2- Copy` (R1§C: initial layer 220, other layers 225) does too. All other stock presets have equal temperatures.
- **O4. Firmware handlers R3 did not check.** SET_VELOCITY_LIMIT, SET_PRESSURE_ADVANCE, M84, G17, SET_INPUT_SHAPER and EXCLUDE_OBJECT_*. G2/G3 are known to exist because the G180 S2 body uses them.
- **O5. No printer-model gate.** None of the reports, and nothing in `cc2_upload.py`, checks `printer_model`. `grep printer_model` finds it only in `target.py`. A Centauri 2 slice passed the parser (C, file 9). See E7.
- **O6. M600's heater effect.** R1 row 16 lists M600 "no args" as harmless syntax. R3 shows it heats and waits at `TEMP`, default 250 °C. R1 is syntax-only by design, but D3 depends on this.

### B.3 Brief versus reality: not counted

- **The earlier CC2 slices** (seat grip, fit ring, benchy, test squares, beacon and EVA parts) are on **Spinx in `~/.orcaslicer-mcp/gcode/`**, not on the Mac.
  - The Mac holds only the two penelope slices, MD5-identical to Spinx's: `fca28640…` (v2) and `b941cce7…`.
  - `find ~` on the Mac returned only those two plus Ender-5 `CE5_*` files under `~/toth/Backup/3dPrints` (18 over 100 KB; 19 in all by glob) and 2 Marlin source test files. I read the Cura header (Cura 4.9.0/4.13.0, `;FLAVOR:Marlin`) on 3 sampled files. All 19 lack an Orca version line and carry Marlin-only heads.
- **"The second has PETG-preset overrides"** means `filament_settings_id = "Elegoo PETG @ECC2"` ×2 with `filament_type = PLA` and 210 °C. Group-B PETG start G-code is comment-only (R1 group B), so that file has **no `M106 P3`**, unlike v2.
- **"A mount with a 140 °C flush hold"**: every stock CC2 slice has `M104 S140` before the `M109` (start template, R1§B). Not one file's feature.
- **`denbjj-part-flat-petg-2026-11-03.gcode` is not a CC2 slice.** Its printer_model is Centauri 2, filament `Elegoo PLA @EC2` with type PETG.

## C. Per-file reconciliation

Method: each file was read on Spinx by a throwaway Python script. Comments after `;` were stripped and the first token upper-cased. Klipper words were kept whole. G180 was keyed by S, M6211 by letter set, M106 by P, and the flag and parameter letters of M104/M109/M140/M190/M73/G4/M220/SET_* were noted. Mac files were read over ssh, and only their version and profile lines and head sets came back.

**Common head set**, present in every CC2 single-colour file (1-8 and 8b below): BED_MESH_CALIBRATE, G1, G180 S7, G180 S8, G180 S9, G2, G21, G28, G90, G92, M104 [S], M106 (no P), M106 P2, M109 [S], M140 [S], M190 [S + bare A], M204 [S], M6211{ALQRST}, M73 [L] and [P R], M83, M84, SET_PRESSURE_ADVANCE [ADVANCE] ×1, SET_PRINT_STATS_INFO, SET_VELOCITY_LIMIT [ACCEL], T0 ×1. That is 25 keys.
- The single `SET_PRESSURE_ADVANCE` is R2#19's `set_extruder` path.
- The single `T0` is the template's: no generator T0 (R2#23a) was observed.

| # | File (Spinx unless noted) | Version line | Printer / process / filament | Heads beyond the common set | Unexplained | Fork parser |
|---|---|---|---|---|---|---|
| 1 | `.orcaslicer-mcp/gcode/cc2-mcp-test-square-20261107.gcode` | OrcaSlicer 2.4.2, 2026-11-06 23:12:05 | `Elegoo Centauri Carbon 2 0.4 nozzle` / 0.28mm Extra Draft / Elegoo PETG @ECC2 (260/80) | none | 0 | ACCEPT |
| 2 | `scoped-attribution-test-20261107-344.gcode` | 2.4.2, 2026-11-07 10:22:12 | same as #1 | none | 0 | ACCEPT |
| 3 | `cc2-elegoo-petg-3dbenchy-20261107.gcode` | 2.4.2, 2026-11-07 00:33:29 | same as #1 | none | 0 | ACCEPT |
| 4 | `den-fit-ring-20261108.gcode` | 2.4.2, 2026-11-07 23:52:39 | same as #1 | none | 0 | ACCEPT |
| 5 | `den-seat-grip-test-petg-2026-11-08.gcode` | 2.4.2, 2026-11-08 06:49:17 | same, at 250/70 | none | 0 | ACCEPT |
| 6 | `beacon-horns-petg-260-80-fit.gcode` | 2.4.2, 2026-11-04 10:37:05 | same as #1 | none | 0 | ACCEPT |
| 7 | `beacon-center-fixing-petg-260-80.gcode` | 2.4.2, 2026-11-04 14:45:06 | same as #1 | none | 0 | ACCEPT |
| 8 | `bottom_horns_modified_for_beacon.stl_20261105-035039.gcode` | 2.4.2, 2026-11-04 20:50:07 | same as #1 | none | 0 | ACCEPT |
| 8b | `eva-chube-petg-260c-80c.gcode` | 2.4.2, 2026-11-03 22:56:36 | `Elegoo Centauri Carbon 2 0.4 nozzle - Copy` / 0.20mm Standard / **Generic PETG @System** (OrcaFilamentLibrary) | none (no new head from the library filament, matching R1§D6) | 0 | ACCEPT |
| 9 | `denbjj-part-flat-petg-2026-11-03.gcode` | 2.4.2, 2026-11-03 15:23:48 | **`Elegoo Centauri 2 0.4 nozzle - Copy`, printer_model Elegoo Centauri 2 (NOT CC2)** / 0.20mm Standard @Elegoo C2 / Elegoo PLA @EC2 with type PETG at 260/70 | M106 P3 ×1 (`S200`: a group-A-style start template on a 70 °C bed) | **0 by head, but explained only by coincidence: R1 excluded the EC2 templates, so these heads have no evidence base** | **ACCEPT (no printer_model gate)** |
| 10 | `artifacts/penelope-tag/penelope-tag-grey-black-20261111-v2.gcode` (also in `.orcaslicer-mcp/gcode/` and on the Mac, all MD5-identical) | 2.4.2, 2026-11-11 07:28:36 | `Elegoo Centauri Carbon 2 0.4 nozzle - Copy` / 0.28mm Extra Draft / Elegoo PLA @ECC2 ×2 (210/60); prime tower type2 | G4 [S0] ×21, M220 [S100] ×7, M106 P3 ×7 (`S200`), M6211{LMNQRST} ×7 (`Q210 R240 S210`, including one `T0 L0`), T1 ×3, T0 ×5, M104 includes tower `S210`, SET_PRESSURE_ADVANCE ×148 | 0 | ACCEPT (flush 240 recorded) |
| 11 | `artifacts/penelope-tag/penelope-tag-grey-black-20261111.gcode` (also Mac and `.orcaslicer-mcp`, identical) | 2.4.2, 2026-11-11 07:22:29 | `Elegoo Centauri Carbon 2 0.4 nozzle` / 0.28mm Extra Draft / **Elegoo PETG @ECC2 ×2 overridden to PLA 210/60** | same as #10 **minus M106 P3** (PETG start template is comment-only) | 0 | ACCEPT |
| 12 | fork fixture `tests/fixtures/cc2_two_tool.gcode.xz` | 2.4.2, 07:28:36 | an excerpt of #10 (R2 B2) | subset of #10 | 0 | (fixture) |
| x | 19 Mac `toth/Backup/3dPrints/**/CE5_*.gcode` | no Orca line in any; 3 sampled show `;Generated with Cura_SteamEngine 4.9.0/4.13.0`, `;FLAVOR:Marlin` | Ender-5 (CE5), not a CC2 | excluded. They carry G0, M105, M107, M201/M203/M205, M221, M82, G91, G29 and M500 (Marlin) | n/a | n/a |
| x | Mac `toth/.../Marlin-bugfix-2.0.x/.../syntax_test_G-code.gcode`, `M808-loops.gcode` | none | Marlin source-tree test files | excluded | n/a | n/a |

Note: rows 1-8 and 8b are nine single-colour CC2-profile files; with the two penelope slices that makes 11 CC2-profile files, the denominator for "Seen" in A. The fixture is an excerpt and not counted.

**Result.** Every head in every CC2 file is explained by the union: **K = 0**. In the one non-CC2 Orca file (row 9) every head is also explained, but only by coincidence, so it is flagged above.

**Union items not observed in any real file:**
- **Heads:** G3, G17, G10, G11, M82, M141, M191, M600, SET_INPUT_SHAPER, EXCLUDE_OBJECT_DEFINE/START/END.
- **Forms of observed heads:**
  - the generator layer-2 `M104`;
  - generator M109 and M190;
  - generator G2;
  - `G4 S>0`;
  - `M73 Q/S/C/D`;
  - `M106 P3 S150/S50/S0`;
  - the extra SET_VELOCITY_LIMIT parameters;
  - `SET_PRESSURE_ADVANCE ADVANCE=0`;
  - the generator bare `T0` (R2#23a);
  - the start `{else}` branch without `G180 S7` (every file took the S7 branch);
  - an M6211 with Q ≠ S.

## D. Allow-list gaps for stock slices

Each gap is something a stock Elegoo CC2 slice can contain that the fork refuses today. Gaps 1 and 2 need only stock presets. Gap 3 needs a user action. Gaps 4-9 need a non-default slicer setting. Every refusal below was reproduced on the fork's parser locally, in memory:
- (a) the test-square file with `M104 S270 ; set nozzle temperature` after its layer-2 `M140`;
- (b) penelope v2 with the first `M6211 T1 …` changed to `S220`;
- (c) the test-square file with `M600`;
- (d) the test-square file with `EXCLUDE_OBJECT_START NAME=x`.

The unmodified control was accepted. All four variants were refused with the messages quoted.

| # | Gap | Who emits it | Fork today | Safe? Evidence |
|---|---|---|---|---|
| 1 | **Layer-2 `M104 S<new>` with no M109** | Generator, GCode.cpp:4811 (R2#11), whenever `nozzle_temperature ≠ nozzle_temperature_initial_layer`. Stock **Generic ABS @Elegoo Centauri (260→270)** and **Generic ABS-CF (250→260)**; Nathan's own **inland PLA @Elegoo Centauri 2- Copy (220→225)** (R1§C). A second emitter, from source only: the temperature-tower calibration writes `M104` changes mid-print (GCode.cpp:4697, R2#11) | **Refused** at the next E move: variant (a) gave "extrudes at a commanded nozzle target of 270 °C that no M109 was ever waited at" | **Plausibly safe, but needs a design decision.** The nozzle moves between two printing temperatures that both sit inside the filament's declared range, so it is not a cold-extrusion path. But the firmware has no backstop (`min_extrude_temp=-100`, R3 finding 1), and the receipt's claim "every extrusion ran behind a verified wait" would be false for the ramp. Option: admit an M104 change only while the nozzle is already waited at a non-zero printing temperature, and only to a target inside the file's own declared nozzle range, recorded as "commanded, not waited". **Not observed in any real file**: source plus stock presets plus synthetic only |
| 2 | **Multi-colour change between filaments at different temperatures** | Wipe tower `M104 S<new>` then `G1 E-2` **before** the M6211 (B.2 O2, R2#11), and change_filament `M6211 … Q[old] R[range_high] S[new]` with Q ≠ S (R1 row 17). Any stock pair with different temperatures, for example Elegoo PLA 210 with PLA Basic 220 or PLA Silk 230 | **Refused**: variant (b) gave "sets S to 220 °C while the nozzle target verified … is 210 °C". The tower M104 plus `G1 E-2` before it would also trip the E gate | **Unsafe as modelled today; partly modellable.** The firmware waits at S itself (R3 M6211 step 6: `M109 S<S>` before the final `G1 E6`), so the purge could re-arm `waited = S`. But with A=0, Q is set by an unwaited `M104` and the channel check, feed and plug-out extrude under it (R3 finding 6). The tower's retract also runs at an unwaited new target. Relaxing only the M6211 rule will not admit these files. Needs firmware-level evidence of how much E runs before the S wait |
| 3 | **`M600` (user pause)** | Template `machine_pause_gcode` at a GUI pause (R1 row 16, R2#27) | Refused (variant c: "a code this parser does not read") | **Keep refused.** R3 "Others": M600 runs `filament_load_gcode_1` and `M109 S<TEMP>`, default **250 °C**. That is a nozzle change and wait the file does not state, so a PLA print at 210 would be held at 250. Admitting it would need TEMP pinned to the verified target and the load macro accounted for |
| 4 | **EXCLUDE_OBJECT_DEFINE/START/END** | Generator with `exclude_object=1` (R2#24-26). Stock CC2 processes set 0 (R1 Presets) | Refused (variant d) | Unknown. R3 did not check whether the CC2 firmware registers them; if unregistered they fall to `cmd_default` and do nothing (R3 no-handler note). A user who turns the option on gets a refusal |
| 5 | **G17** with arc fitting | Generator spiral lift (R2#3) when `enable_arc_fitting=1` | Refused (unread code) | Unknown firmware handler. Inert in Klipper (plane select). G2/G3 are already allowed and E-gated |
| 6 | **M141 / M191** (plus `M106 P2 S255`) | Generator chamber control (R2#15-16) | Refused | **Keep refused.** R3: no handler, so the chamber would not heat as the file claims. Nathan's rule: never heat the chamber for PETG |
| 7 | **SET_INPUT_SHAPER** | Generator calibration prints only (R2#20) | Refused | Unknown firmware handler; calibration only |
| 8 | **G10 / G11** | Generator with `use_firmware_retraction=1` (R2#9) | Refused (blocked) | **Keep refused.** R3: no handler, so the retractions would silently not happen |
| 9 | **M82** | Generator with `use_relative_e_distances=0` (R2#7), together with writer `G92 E0` resets | Refused | Keep refused: since PR #13 the tool-change retract exception trusts the sign of E, which only means a retract in relative-E mode (M83), so absolute E would weaken the gate. R3 D5 notes M6211's flush amount depends on the E mode. Not stock |

**Explicit non-gaps.** These are covered today or have no stock emitter:
- **SET_CONDITION**: only inside G180 bodies; no template or generator writes it (R1§A, R2§A). The fork refusing it in files is right, per R3 finding 3.
- **M220**: the S form is the only Klipper form (R2#22) and the fork admits S. The B and R forms are Marlin only.
- **The colour-change template on a single-filament printer**: it is either M600 (gap 3) or change_filament (M6211 + T, admitted when the temperatures are equal). See E3.
- **M6211 `L0`**: admitted.
- **Generator bare `T0` (R2#23a)**: admitted, because it is the same tool.
- **`M73 Q/S/C/D`, `G4 S<t>`, `SET_VELOCITY_LIMIT` extras, `SET_PRESSURE_ADVANCE ADVANCE=0`, `M106 P3 S150/S50/S0`, tower-interface `M109`, by-object `M190`**: all admitted.
- **Q/R/S below 200**: no stock preset reaches it (B6/B8).

## E. Still unverified, and the experiment that would settle each (none was run)

1. **Running firmware vs published source.** Commit `5a2ea7fc` versus firmware 02.01.00.00 and the machine's `printer_dsp.cfg`: `[g180]` bodies, M6211 clamps (185 vs 200), and G180 S819 (R3§D1). *Settles it:* a printer-side config or log dump, or a firmware image for 02.01.00.00 to diff.
2. **BED_MESH_CALIBRATE `FROM_SLICER=1`**: does it change the nozzle or bed target? (B6, R3 finding 2.) *Settles it:* watch heater targets over MQTT status during a normal start, before and after the mesh line, or disassemble `libelegoo_bed_mesh.so`. The fork could also drop the verified-wait state at this line (R3's recommendation); stock files would still pass because `M109` follows.
3. **What a GUI "colour change" emits on a single-filament CC2 plate** (R1§D4, R2#27): M600 or change_filament? *Settles it:* slice one plate with a colour change and grep. That is a slicer-only run.
4. **The gap D1 form in a real slice.** *Settles it:* slice a single-colour Generic ABS @Elegoo Centauri plate, or Nathan's inland PLA copy, and grep for `M104 S270 ; set nozzle temperature` at layer 2 with no M109.
5. **Which tray actually runs.** A receipt's T comes from a file line the firmware ignores; the tray comes from the persistent colour table (R3§C; community lead on the layer-0 pause). *Settles it:* read the colour table over the LAN protocol before a start, if the protocol exposes it.
6. **The mixed-temperature tool change (gap D2).** *Settles it:* slice a two-colour plate with filaments of different temperatures and capture the tower and change_filament block order and values. Then establish from the firmware (logs during a real change) how much E runs before M6211's S wait.
7. **No printer-model gate.** The fork accepted a Centauri 2 slice (C, row 9). AGENTS.md treats the Centauri 2 and Centauri Carbon 2 as distinct profiles. *Settles it:* a design decision to require `printer_model = Elegoo Centauri Carbon 2` in the config block, and a test with row 9 as the control that must be refused.
8. **Firmware handlers R3 did not check**: SET_VELOCITY_LIMIT, SET_PRESSURE_ADVANCE, M84, G17, SET_INPUT_SHAPER, EXCLUDE_OBJECT_*. *Settles it:* grep their registrations in the published `elegoo/` tree.
9. **Flush at R without a wait.** Every stock multi-colour purge flushes at R = range-high (240 for Elegoo PLA) under an unwaited `M104` (R3 M6211 step 4). The fork admits this as a recorded flush temperature (`upload.py:1314-1322`). Not verified: what the nozzle reaches during the flush, and whether the M6211 newline bug adds C mm (R3 step 4).
10. **E mode during the first M6211** (R3§D5). *Settles it:* firmware logs of the E position across the start-block purge.
11. **BED_MESH `adaptive_margin` case folding** (B15). *Settles it:* read the Elegoo C++ `GCodeCommand` getter for case handling.
12. **Older 3MF projects** may carry older CC2 templates (R1§D8). No 3MF was inspected; the two Mac `.3mf` files were not opened.

VERIFY done: 41 union heads, 0 unexplained in real files, 15 contradictions, 9 allow-list gaps
