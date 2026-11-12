# R1: G-code emitted by the stock OrcaSlicer 2.4.2 templates for the Elegoo Centauri Carbon 2

Date: 2026-11-11. Read-only research. Nothing was sent to the printer and no repo was edited.

## Sources and how this was checked

- **Installed copy (primary):** Mac mini `/Applications/OrcaSlicer.app` (Info.plist `CFBundleShortVersionString` = `2.4.2`). Profile root: `/Applications/OrcaSlicer.app/Contents/Resources/profiles/`. `Elegoo.json` vendor version is `02.04.00.06`. Below, `P/` means that profile root.
- **GitHub:** tag `v2.4.2` exists (the repo is now `OrcaSlicer/OrcaSlicer`; `SoftFever/OrcaSlicer` redirects). Commit `8500fcdccaa10b5099ac20d252af3a7c560046f1`. Tree `resources/profiles/Elegoo` = `1e4d3d5505c468e637afa130cc8c4bfdf70f1f3a`. Base URL: `https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/v2.4.2/resources/profiles/Elegoo/...`.
- **Installed vs GitHub:** I computed a git blob SHA-1 for every file under `P/Elegoo/` plus `P/Elegoo.json` on the Mac (662 files) and compared each one with the v2.4.2 git tree. **All 662 match byte for byte. No file is missing on either side.** I did the same check for the three OrcaFilamentLibrary files used by one user preset (`filament/Generic ASA @System.json`, `filament/base/fdm_filament_asa.json` and `filament/base/fdm_filament_common.json`), and they also match. Everything below therefore applies to both sources.
- **Inheritance** was resolved with a script that follows `inherits` by preset `name` inside `P/Elegoo/**` and merges keys from the root down. CC2 machine chain: `P/Elegoo/machine/fdm_machine_common.json` → `P/Elegoo/machine/fdm_elegoo_3dp_001_common.json` → `P/Elegoo/machine/ECC2/Elegoo Centauri Carbon 2 0.4 nozzle.json` → (for 0.2/0.6/0.8) `P/Elegoo/machine/ECC2/Elegoo Centauri Carbon 2 0.{2,6,8} nozzle.json`.
- **Unset template keys** fall back to the OrcaSlicer defaults in `https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/v2.4.2/src/libslic3r/PrintConfig.cpp`. Placeholder values come from `.../v2.4.2/src/libslic3r/GCode.cpp` (lines cited inline).
- The model file `P/Elegoo/machine/ECC2/Elegoo Centauri Carbon 2.json` (type `machine_model`, `model_id` `Elegoo-CC2`, nozzles `0.4;0.2;0.6;0.8`) holds no G-code.
- **Not the CC2:** `EC2/` (Centauri 2), `ECC/` (Centauri Carbon) and `EC/` (Centauri) were excluded.

### Presets covered (66)

I checked every chain: all 66 resolve to a root file with no `inherits`. None points outside `P/Elegoo/`.

- **Machine (4):** `Elegoo Centauri Carbon 2 0.2 nozzle`, `0.4 nozzle`, `0.6 nozzle` and `0.8 nozzle`, all under `P/Elegoo/machine/ECC2/`. Only the 0.4 file sets G-code keys. The 0.2, 0.6 and 0.8 files inherit from the 0.4 file and override no G-code key, so **all four nozzle sizes have identical effective templates** (checked key by key).
- **Process (21):** every file in `P/Elegoo/process/ECC2/`, from `0.08mm Optimal @Elegoo CC2 0.2 nozzle` to `0.48mm Draft @Elegoo CC2 0.8 nozzle`. None contains a G-code template. The only G-code-related key they set is `gcode_label_objects` = `0`, inherited from the chain `fdm_process_elegoo_0xxxx.json` → `fdm_process_elegoo_common.json` → `fdm_process_common.json`. The 0.20mm Standard 0.4 preset also has `exclude_object` = `0` and `enable_prime_tower` = `1`.
- **Filament (41):** every preset whose resolved `compatible_printers` names a CC2 machine.
  - All 30 files in `P/Elegoo/filament/ECC2/*@ECC2.json`, each with `compatible_printers` = the four CC2 machines.
  - 11 files in `P/Elegoo/filament/Generic/`: `Generic ABS|ABS-CF|ASA-CF|PET|PET-CF|PETG-CF|PLA @Elegoo Centauri`, `Generic PA|PA6-CF @Elegoo` and `Generic PC|PC-CF @Elegoo`. Each lists a CC2 machine among 9 to 32 printers.
  - No preset uses `compatible_printers_condition` instead.

### Template keys and where each effective value comes from (CC2 machine)

| key | effective value from | notes |
|---|---|---|
| machine_start_gcode | `ECC2/Elegoo Centauri Carbon 2 0.4 nozzle.json` | "CC2_START_GCODE, date 2026-01-16-001" |
| machine_end_gcode | same | "CC2_END_GCODE". It overrides the `fdm_elegoo_3dp_001_common.json` end G-code dated 20240510 |
| change_filament_gcode | same | "CC2_CHANGE_FILAMENT_GCODE" |
| layer_change_gcode | same | |
| before_layer_change_gcode | `fdm_machine_common.json` | |
| machine_pause_gcode | `0.4 nozzle.json` (also set in `fdm_elegoo_3dp_001_common.json`) | `M600` |
| gcode_flavor | `0.4 nozzle.json` | `klipper` (`fdm_machine_common` sets `marlin`) |
| time_lapse_gcode, template_custom_gcode, printing_by_object_gcode, wrapping_detection_gcode, change_extrusion_role_gcode, process_change_extrusion_role_gcode, file_start_gcode | not set in any CC2 chain file. Only `EOSGIGA/Elegoo OrangeStorm Giga 0.4 nozzle.json` sets any of these | PrintConfig.cpp default is empty (`""`), so they emit nothing |
| filament_start_gcode / filament_end_gcode | filament chain (see section A, rows M106 P3) | |
| filament_change_extrusion_role_gcode | not set | default empty |

Other machine values that decide which branches fire, all resolved: `support_air_filtration` = `0`, `support_chamber_temp_control` = `0`, `auxiliary_fan` = `1`, `manual_filament_change` = `0`, `single_extruder_multi_material` = `1`, `printable_height` = `256`, `printable_area` = `0x0,256x0,256x256,0x256`.

## A. Every distinct command head the templates can emit

Placeholders appear in the form the template writes them. `{…}` is an expression evaluated by Orca's placeholder parser, and `[x]` is a config value. Unless a row says otherwise, "machine" means all 4 CC2 machine presets (identical templates). Comment lines (`;…`) are listed separately after the table.

| # | head | template(s) | exact forms emitted (every branch) | preset(s) |
|---|---|---|---|---|
| 1 | `G90` | start; end | `G90` (no args) | machine |
| 2 | `G1` | start; end; change_filament | start, always: `G1 F20000`. start `{if first_layer_print_min[1] > 0.5}` branch: `G1 X{print_bed_max[0]*0.5-1} Y-1.2 F20000`; `G1 Z0.5 F900`; `G1 E6 F{min(1200, max(120, filament_max_volumetric_speed[initial_no_support_extruder]*60/2/2.5043))}`; `G1 X{print_bed_max[0]*0.5-41} E20 F{min(12000, max(1200, …same…))}`; `G1 F6000`; `G1 X{print_bed_max[0]*0.5-46} E0.8`. start `{else}` branch: `G1 E30 F{min(1200, max(120, …same…))}`. end: `G1 E-1.5 F1800`; `{if max_layer_z > 75}G1 Z{min(max_layer_z+5, printable_height+0.5)} F20000{else}G1 Z80 F20000 {endif}`. change_filament: `G1 Z{min(max_layer_z+3, printable_height+0.5)} F1200`. Letters used: X Y Z E F. Y can be negative (`Y-1.2`) and E can be negative (`E-1.5`). | machine |
| 3 | `G2` | end | `G2 I0 J1 Z{max_layer_z+0.5} F3000` (letters I J Z F) | machine |
| 4 | `G28` | start | `G28` (no args) | machine |
| 5 | `G92` | start (×2); end; before_layer_change | `G92 E0` only | machine |
| 6 | `G180` | start; end | `G180 S7` (start, only in the `first_layer_print_min[1] > 0.5` branch); `G180 S8` (start, always); `G180 S9` (end). Only the S letter appears, with only the values 7, 8 and 9 | machine |
| 7 | `M73` | layer_change | `M73 L{layer_num+1}` (L letter only: layer number, an integer ≥1) | machine |
| 8 | `M83` | start (if branch); end | `M83` (no args) | machine |
| 9 | `M84` | end | `M84` (no args) | machine |
| 10 | `M104` | start; end | `M104 S140` (literal); `M104 S0` | machine |
| 11 | `M106` | start; end; change_filament; filament_start | start: `M106 S0` (×2), `M106 S200` (if branch). end and change_filament: `M106 S0`, `M106 P2 S0`. filament_start (group A, below): `M106 P3 S200` if bed or first-layer bed temp >55; `M106 P3 S150` if >50; `M106 P3 S50` if >45; nothing otherwise. Then `{if activate_air_filtration[current_extruder] && support_air_filtration}` `M106 P3 S{during_print_exhaust_fan_speed_num[current_extruder]}`. Letters P (0 omitted, 2, 3) and S (0–255). | machine; group-A filaments |
| 12 | `M109` | start (×2) | `M109 S[nozzle_temperature_initial_layer]`. The second copy is only in the `first_layer_print_min[1] > 0.5` branch | machine |
| 13 | `M140` | start; end | `M140 S[bed_temperature_initial_layer_single]`; `M140 S0` | machine |
| 14 | `M190` | start | `M190 S[bed_temperature_initial_layer_single] A`. Note the trailing **bare `A` flag** (no value) | machine |
| 15 | `M204` | start | `M204 S{min(20000,max(1000,outer_wall_acceleration))}`, so S is between 1000 and 20000 (0.20mm Standard 0.4: `outer_wall_acceleration` = 5000) | machine (+ process value) |
| 16 | `M600` | machine_pause_gcode | `M600` (no args). Emitted only where a pause is inserted | machine |
| 17 | `M6211` | start; change_filament | start: `M6211 A1 L200 T[initial_no_support_extruder] Q[nozzle_temperature_initial_layer] R[nozzle_temperature_initial_layer] S[nozzle_temperature_initial_layer]` (letters A L T Q R S, with A=1 and L=200 literal). change_filament: `M6211 T[next_extruder] L[flush_length] M{old_filament_e_feedrate} N{new_filament_e_feedrate} Q[old_filament_temp] R[nozzle_temperature_range_high] S[new_filament_temp]` (letters T L M N Q R S). `flush_length` is a float (GCode.cpp:950/7935), so L can be fractional. M and N are ints (GCode.cpp:923-924) | machine |
| 18 | `T<n>` | start; change_filament | `T[initial_no_support_extruder]`; `T[next_extruder]`, a filament or slot index from 0 (single-extruder multi-material, `single_extruder_multi_material` = 1) | machine |
| 19 | `BED_MESH_CALIBRATE` | start | `BED_MESH_CALIBRATE mesh_min={adaptive_bed_mesh_min[0]},{adaptive_bed_mesh_min[1]} mesh_max={adaptive_bed_mesh_max[0]},{adaptive_bed_mesh_max[1]} ALGORITHM=[bed_mesh_algo] PROBE_COUNT={bed_mesh_probe_count[0]},{bed_mesh_probe_count[1]} ADAPTIVE=0 ADAPTIVE_MARGIN=0 FROM_SLICER=1`. Parameters: `mesh_min`/`mesh_max` (float pair `x,y`), `ALGORITHM` = `bicubic` or `lagrange` (GCode.cpp:2987-2989: lagrange if the probe grid has ≤6 points, else bicubic with each axis ≥4 on Klipper), `PROBE_COUNT` (int pair, each ≥3), and the literals `ADAPTIVE=0`, `ADAPTIVE_MARGIN=0`, `FROM_SLICER=1`. Note: lower-case `mesh_min`/`mesh_max` and upper-case others | machine |
| 20 | `SET_PRINT_STATS_INFO` | start; layer_change | start: `SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count] CURRENT_LAYER=0`. layer_change: `SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count] CURRENT_LAYER={layer_num+1}` | machine |

**Comment-only lines the templates emit** (no command head). Start: `;===== CC2_START_GCODE ================`, `;===== date: 2026-01-16-001 =====================`, `;LAYER_COUNT:[total_layer_count]`, `;LAYER:0`, and the trailing comments `;Call exterior wall print acceleration` and `;Reset Extruder`. End: `;===== CC2_END_GCODE ================`, `;===== date: …`, plus trailing `;Turn-off bed`, `; zero the extruder`, `; lower z a little` and `; Move print head up`. Change filament: `;==========CC2_CHANGE_FILAMENT_GCODE==========`, `;===== date: …`. Layer change: `;LAYER:{layer_num+1}`. Before layer change: `;BEFORE_LAYER_CHANGE`, `;[layer_z]`. Filament start: `; filament start gcode` or `; Filament start gcode` (stock Elegoo presets). `; Filament gcode` appears only through the user's OrcaFilamentLibrary-based preset (section C). Filament end: `; filament end gcode ` (`; filament end gcode` in that library preset). An allow list must accept or strip comments and inline `;` comments, including after `G1 Z80 F20000 ` in the end template's else branch.

**Filament groups.** Values were resolved per preset. The source file is the first file in the chain that sets the key.

- **Group A, active M106 P3 logic** (17 presets). The start template is defined in each ECC2 file itself, or in `Generic/Generic PLA @Elegoo Centauri.json`. Presets: Elegoo PLA, PLA Basic, PLA Galaxy, PLA Glow, PLA Marble, PLA Matte, PLA PRO, PLA Silk, PLA Sparkle, PLA Translucent2, PLA Wood, PLA+, PLA-CF, Rapid PLA+, PAHT-CF and PC (all `@ECC2`), plus Generic PLA @Elegoo Centauri. Which branch fires for the stock values (hot and textured plate):
  - Every PLA in the group has hot or textured plate 60 or 65, so it emits `M106 P3 S200`.
  - PAHT-CF has hot plate 60 and textured plate 100, so `S200`.
  - PC has bed 100 or 110, so `S200`.
  - No stock preset lands in the `S150` (>50) or `S50` (>45) bands; those are reachable only through user edits. On the cool plate (35, or 45 for PLA-CF) none of the conditions is true, so the template emits no `M106 P3`. On the engineering plate the PLA value is 0, also none.
  - The air-filtration branch never fires on a stock CC2, because the machine has `support_air_filtration` = `0`, even though `Elegoo PAHT-CF @ECC2` has `activate_air_filtration` = `1`.
- **Group B, comment only** (21 presets). The start comment is always `; Filament start gcode`. Its source (first file in the resolved chain that sets the key) is one of three. From `fdm_filament_common.json`: Elegoo ABS, ASA, ASA-CF and the Generic ABS, ABS-CF, ASA-CF, PA, PA6-CF, PC and PC-CF. From `BASE/Elegoo PETG @base.json`: Elegoo PETG, PETG HF, PETG PRO, PETG Translucent, PETG-CF, PETG-GF, PET-CF and Rapid PETG. From `BASE/Generic PET @base.json` or `BASE/Generic PETG @base.json`: Generic PET, PET-CF and PETG-CF. Presets: Elegoo ABS, ASA, ASA-CF, PET-CF, PETG, PETG HF, PETG PRO, PETG Translucent, PETG-CF, PETG-GF and Rapid PETG (all `@ECC2`), plus the 10 non-PLA Generic presets.
- **Group C, comment only** (3 presets): Elegoo PC-FR, TPU 95A and Rapid TPU 95A (`@ECC2`). The start comment `; filament start gcode` comes from `BASE/Elegoo PC @base.json` or `BASE/Elegoo TPU @base.json`.
- **All 41 presets** end with the comment-only `filament_end_gcode` value `; filament end gcode `. That value comes from `fdm_filament_pla.json` (PLA group A), `fdm_filament_tpu.json` (TPU), the BASE PETG/PET files, or `fdm_filament_common.json` (the rest).

**Process presets:** none emits template G-code.

## B. Verbatim effective templates

These are the 0.4 nozzle CC2 machine (`P/Elegoo/machine/ECC2/Elegoo Centauri Carbon 2 0.4 nozzle.json`, identical for 0.2, 0.6 and 0.8), Elegoo PLA (`P/Elegoo/filament/ECC2/Elegoo PLA @ECC2.json`) and Elegoo PETG (start and end values from `P/Elegoo/filament/BASE/Elegoo PETG @base.json`, which the ECC2 file inherits). They were dumped programmatically from the resolved JSON, so whitespace is exact (some lines end in a space). `time_lapse_gcode`, `template_custom_gcode`, `printing_by_object_gcode` and `wrapping_detection_gcode` are empty.

#### machine_start_gcode

```gcode
;===== CC2_START_GCODE ================
;===== date: 2026-01-16-001 =====================

G90
M104 S140
M140 S[bed_temperature_initial_layer_single]
M190 S[bed_temperature_initial_layer_single] A
M106 S0
BED_MESH_CALIBRATE mesh_min={adaptive_bed_mesh_min[0]},{adaptive_bed_mesh_min[1]} mesh_max={adaptive_bed_mesh_max[0]},{adaptive_bed_mesh_max[1]} ALGORITHM=[bed_mesh_algo] PROBE_COUNT={bed_mesh_probe_count[0]},{bed_mesh_probe_count[1]} ADAPTIVE=0 ADAPTIVE_MARGIN=0 FROM_SLICER=1
M204 S{min(20000,max(1000,outer_wall_acceleration))} ;Call exterior wall print acceleration
G28
M109 S[nozzle_temperature_initial_layer]
M6211 A1 L200 T[initial_no_support_extruder] Q[nozzle_temperature_initial_layer] R[nozzle_temperature_initial_layer] S[nozzle_temperature_initial_layer]
T[initial_no_support_extruder]

{if first_layer_print_min[1] > 0.5}
G180 S7
G1 X{print_bed_max[0]*0.5-1} Y-1.2 F20000
G1 Z0.5 F900
M109 S[nozzle_temperature_initial_layer]
M83
G92 E0 ;Reset Extruder
G1 E6 F{min(1200, max(120, filament_max_volumetric_speed[initial_no_support_extruder]*60/2/2.5043))} 
M106 S200
G1 X{print_bed_max[0]*0.5-41} E20 F{min(12000, max(1200, filament_max_volumetric_speed[initial_no_support_extruder]*60/2/2.5043))} 
G1 F6000
G1 X{print_bed_max[0]*0.5-46} E0.8
{else}
G1 E30 F{min(1200, max(120, filament_max_volumetric_speed[initial_no_support_extruder]*60/2/2.5043))}
{endif}
M106 S0
G180 S8
G1 F20000
G92 E0 ;Reset Extruder
SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count] CURRENT_LAYER=0
;LAYER_COUNT:[total_layer_count]
;LAYER:0
```

#### machine_end_gcode

```gcode
;===== CC2_END_GCODE ================
;===== date: 2026-01-16-001 =====================


M140 S0 ;Turn-off bed
M83
G92 E0 ; zero the extruder
G1 E-1.5 F1800
G2 I0 J1 Z{max_layer_z+0.5} F3000 ; lower z a little
M106 S0
M106 P2 S0
G90
{if max_layer_z > 75}G1 Z{min(max_layer_z+5, printable_height+0.5)} F20000{else}G1 Z80 F20000 {endif}; Move print head up 
G180 S9
M104 S0
M84

```

#### change_filament_gcode

```gcode

;==========CC2_CHANGE_FILAMENT_GCODE==========
;===== date: 2026-01-16-001 =====================
M106 S0
M106 P2 S0
G1 Z{min(max_layer_z+3, printable_height+0.5)} F1200
M6211 T[next_extruder] L[flush_length] M{old_filament_e_feedrate} N{new_filament_e_feedrate} Q[old_filament_temp] R[nozzle_temperature_range_high] S[new_filament_temp]
T[next_extruder]

```

#### layer_change_gcode

```gcode
M73 L{layer_num+1}
;LAYER:{layer_num+1}
SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count] CURRENT_LAYER={layer_num+1}
```

#### before_layer_change_gcode

```gcode
;BEFORE_LAYER_CHANGE
;[layer_z]
G92 E0

```

#### machine_pause_gcode

```gcode
M600
```

#### Elegoo PLA @ECC2 — filament_start_gcode

```gcode
; filament start gcode
{if  (bed_temperature[current_extruder] >55)||(bed_temperature_initial_layer[current_extruder] >55)}M106 P3 S200
{elsif(bed_temperature[current_extruder] >50)||(bed_temperature_initial_layer[current_extruder] >50)}M106 P3 S150
{elsif(bed_temperature[current_extruder] >45)||(bed_temperature_initial_layer[current_extruder] >45)}M106 P3 S50
{endif}

{if activate_air_filtration[current_extruder] && support_air_filtration}
M106 P3 S{during_print_exhaust_fan_speed_num[current_extruder]} 
{endif}
```

#### Elegoo PLA @ECC2 — filament_end_gcode

```gcode
; filament end gcode 

```

#### Elegoo PETG @ECC2 — filament_start_gcode

```gcode
; Filament start gcode

```

#### Elegoo PETG @ECC2 — filament_end_gcode

```gcode
; filament end gcode 

```

## C. What the user's presets on the Mac override

User folder: `~/Library/Application Support/OrcaSlicer/user/<id>/` on the Mac. **No user preset overrides any G-code template key** for a CC2 machine, process or filament. Secret-like keys were redacted while reading, and none were present in these files.

| user preset (file) | inherits | keys overridden | G-code impact |
|---|---|---|---|
| `machine/Elegoo Centauri Carbon 2 0.4 nozzle - Copy.json` | `Elegoo Centauri Carbon 2 0.4 nozzle` | `print_host` = `10.88.88.190`, `printer_agent` = `bbl`, `printer_extruder_id` = `["1"]`, `printer_extruder_variant` = `["Direct Drive Standard"]`, `printer_settings_id`, `name`, `from`, `inherits`, `version` = `2.4.0.0` | none. The templates are exactly those in section B. `printer_agent` = `bbl` changes the network/upload agent, not the template text. Whether it changes `is_BBL_printer()` code paths in GCode.cpp was not checked (see D) |
| `process/0.28mm Extra Draft @Elegoo CC2 0.4 nozzle - PandaMod.json` | `0.28mm Extra Draft @Elegoo CC2 0.4 nozzle` | `filename_format` = `ECC2_{nozzle_diameter[0]}_{input_filename_base}_{filament_name}_{chamber_temperature[0]}C_{layer_height}_{print_time}.gcode`, `top_shell_layers` = `3`, `wall_loops` = `3`, `print_extruder_id`, `print_extruder_variant` and ids | none (the filename only) |
| `filament/Elegoo PETG @ECC2 - Copy.json` | `Elegoo PETG @ECC2` | `textured_plate_temp` = `["80"]`, `textured_plate_temp_initial_layer` = `["85"]` | template text unchanged. Values change: `M140`/`M190 S85` on the textured plate. The filament start template is comment-only (group B), so there is no M106 P3 even though the bed is above 55 |
| `filament/inland PLA @Elegoo Centauri 2- Copy.json` | `Generic PLA @Elegoo Centauri` (group A, CC2-compatible) | `nozzle_temperature` = `["225"]`, `nozzle_temperature_initial_layer` = `["220"]` | template text unchanged. `M109`/`M6211` Q, R and S take 220 at the start (R in change_filament is `nozzle_temperature_range_high`, unchanged at 240) |
| `filament/Generic ASA @System -hotter.json` | `Generic ASA @System` (OrcaFilamentLibrary) | `compatible_printers` = `["Elegoo Centauri Carbon 2 0.4 nozzle"]`, no G-code keys | adds a non-Elegoo filament to the CC2. Effective `filament_start_gcode` = `; Filament gcode`, `filament_end_gcode` = `; filament end gcode` (from `P/OrcaFilamentLibrary/filament/base/fdm_filament_common.json`). Comment-only |
| `filament/Generic ASA-CF @Elegoo Centauri - Copy.json` | `Generic ASA-CF @Elegoo Centauri` | no G-code keys | comment-only (group B) |
| others: `machine/Elegoo Centauri 2 0.4 nozzle - Copy.json` (Centauri 2, **not** the CC2; also has `print_host` `10.88.88.190`), `Endorphin3d`, `MyKlipper*` and their processes, `Generic PETG… CopyENDER/HF` | non-CC2 | `Endorphin3d.json` and `MyKlipper 0.4 nozzle - Elegoo.json` override start/end G-code, for other printers | out of scope |

Observation, not an override: the CC2 "- Copy" machine and the Centauri 2 "- Copy" machine have the same `print_host`. That matters if the MCP picks a machine profile by host.

## D. What I could not establish

1. **G-code Orca generates outside the templates.** This report covers templates only. Toolpaths and temperatures from the slicer core (`G0`/`G1`/`G2`/`G3` moves, `M104`/`M109`/`M140` per-layer temperature changes, `M106`/`M107` part-cooling and auxiliary fan, `M204`/`SET_VELOCITY_LIMIT` per-feature acceleration on Klipper, retraction `G10`/`G11` if firmware retraction is enabled (unset in the CC2 chain), `M73` progress, `G92 E0` resets, wipe-tower or prime-tower G-code (`enable_prime_tower` = 1 on CC2 processes), the retract/lift that GCode.cpp:7956 prepends to change_filament_gcode, and machine-limit lines such as `M201`/`M203`/`M205` when `emit_machine_limits_to_gcode` is true (default `true` in PrintConfig.cpp; unset in the Elegoo chain)) all come from `GCode.cpp`/`GCodeWriter.cpp`. They need a separate pass (source read, or a sliced file per preset). The allow list must include them.
2. **Behaviour of `printer_agent` = `bbl`** in the user CC2 copy: whether it switches any `is_BBL_printer()` branch (GCode.cpp:2019/2047/4622, which for example changes `;Z:` to `; Z_HEIGHT:` comments) was not traced. `is_BBL_printer()` is normally vendor-based.
3. **Firmware semantics** of the vendor commands `G180 S7/S8/S9`, `M6211` (letters A L T Q R S M N), the `M190 … A` flag, `M106 P2`/`P3` fan mapping and `M73 L` are not in the profiles. This report gives only their syntax.
4. **Color change and pause in practice:** the template for a GUI pause is `M600`. I did not confirm which template a single-filament GUI "color change" uses on this machine (the PrintConfig has no separate color-change key). It is `change_filament_gcode` (with `M6211` + `T`) or `M600`, depending on Orca's custom-G-code handling.
5. **The user preset `version` strings** (`2.4.0.0` / `2.4.0.6`) are older than the installed vendor bundle (`02.04.00.06`). Orca resolves the parent at load time, so the effective templates are the current ones. I did not confirm that the GUI shows no "modified" flag on these.
6. **OrcaFilamentLibrary `@System` filaments** can also be made CC2-compatible, as the user did for one. Their templates are not in the Elegoo tree. A scan of all 482 files under `P/OrcaFilamentLibrary/` found non-comment filament template lines only of the forms already in row 11: the same `M106 P3 S200|S150|S50` bed-temperature logic (16 Elegoo `@System` files), the `M106 P3 S{during_print_exhaust_fan_speed_num…}` air-filtration branch (17 files), and one `filament_end_gcode` with `M106 P3 S0`. So they add **no new command head**, only the value `M106 P3 S0`.
7. **Harness copy:** a persisted tool-output file containing a summary of the CC2 preset names and chains stayed in this session's Claude tool-results folder. That file is outside my control, and it holds no secrets. The working dumps in the scratch folder were deleted.
8. **3MF project files:** a 3MF stores its own copy of the presets and templates. Jobs sliced from older 3MF projects may carry older CC2 templates. None was inspected.

R1 done: 20 distinct command heads from 66 presets
