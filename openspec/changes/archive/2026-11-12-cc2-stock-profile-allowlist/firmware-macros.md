# R3: CC2 firmware macros and Elegoo-specific G-code: what each one does

Research only. Nothing here touched the printer. Written 2026-11-11.

## Source basis and its limits (read this first)

- **Primary source:** Elegoo's published Centauri Carbon 2 firmware source, `github.com/elegooofficial/CentauriCarbon2`, commit `5a2ea7fc03e707552701b1a69f463699cbd39230` (2026-03-11). Short link base used below: `SRC = https://github.com/elegooofficial/CentauriCarbon2/blob/5a2ea7fc03e707552701b1a69f463699cbd39230/`.
- **Nothing in that repo ties it to firmware 02.01.00.00.** It carries no version string. The running printer may differ. The best grade any claim below can earn is **"published source"**, never "running firmware".
- **Macro bodies and defaults** (`[g180]`, `[canvas_dev]`) come from `debugfile/printer_dsp.cfg`. `debugfile/printer.cfg` has **no `[g180]` section at all**. The printer's own config cannot be read back over the LAN protocol, so a machine whose config differs would behave differently.
- **Build scope matters.** `elegoo/extras/CMakeLists.txt` builds `mmu.cpp` (line 70) and **not** `canvas_dev.cpp`. `cavity_temperature.cpp` is commented out (line 67). So handlers in `canvas_dev.cpp` are not live code. Every CANVAS citation below is to `mmu.cpp`.
- **Bed mesh is closed.** `BED_MESH_*` exists only as a prebuilt binary, `elegoo/extras/bedmesh/arm/libelegoo_bed_mesh.so` (header `bed_mesh.h` only). Claims about it come from `strings` output and are graded "inferred".
- **Corroborating sources:**
  - Neme77, `centauri-carbon-2-community`, `cc2-control/docs/GCODE-PROTOCOL.md`: binary/log analysis of stock firmware ("V4.2-R5", `libelegoo_extras.so`).
  - bjan, `pycentauri` `docs/PROTOCOL.md`: covers transport only, no macro detail.
  - OrcaSlicer `resources/profiles/Elegoo/machine/ECC2/Elegoo Centauri Carbon 2 0.4 nozzle.json` and ElegooSlicer's identical template: what the slicer emits.
- **Evidence that the published firmware is stale.** ElegooSlicer's `src/libslic3r/GCode/ElegooGCodeProcessorHelper.cpp` (commits of 2026-07-06 to 07-08), function `estimate_M6211_time_for_centauri_carbon_2`, models a **newer M6211** than the March source:
  - K is clamped 10–300 (source: 10–120), with defaults K=75 and P=3000.
  - The first flush is 15 mm (source: `e_flush_dist` 10).
  - R and S are clamped **185**–350 (source: **200**–350).
  - The flush loop uses a G180 **S819** macro, which the published `[g180]` does not have.

  This is a time estimator, not firmware, but it is Elegoo's own code. It strongly suggests that 02.01.00.00 differs from commit `5a2ea7fc` in M6211 clamps and macro numbers. The fork's 200–350 window may be stale. **[vendor slicer code; inf about firmware]**
- **Checked and found nothing more on CC2 macros:**
  - `gh search code` for M6211 and G180 across `elegooofficial` returned only `CentauriCarbon2`, `ElegooSlicer`, and an unrelated `CentauriCarbon` hit.
  - The OpenCentauri org repo list has CC1-focused firmware only.
- **Not checked:** the `elegoo-link` source text, docs.opencentauri.cc, and the Elegoo wiki.

Confidence grades used: **src** = read in published source text; **cfg** = read in published `printer_dsp.cfg`; **comm** = community notes; **inf** = inferred (binary strings, or reasoning from source).

## Findings that change the fork's allow-list (`cc2_upload.py`)

1. **The firmware's own cold-extrusion guard is off.** `printer_dsp.cfg:142` sets `min_extrude_temp=-100`, and `heaters.cpp:103,326` computes `can_extrude = smoothed_temp >= min_extrude_temp`. So nothing on the printer refuses a cold E move, and the upload gate is the only gate. **[cfg+src]**
2. **BED_MESH_CALIBRATE: I disagree with the fork.** The fork says it "sets no nozzle target". The binary's strings say otherwise. They include:
   - a `BED_TEMP` parameter, plus `M140 S` and `M190 S` (it can set and wait on the bed);
   - `M109 S140 F20000` (a nozzle wait at 140 °C);
   - `G180 S3` / `G180 S5` (home, then run S1, which waits at `M109 S200` and `M109 S180`, then sets `M104 S140`, then S2);
   - `G180 S6` (every heater off) and `M140 S0`.

   Which of these the in-file `FROM_SLICER=1` path runs is not readable. Neme77 reports that the calibration path depends on print-start flags set over MQTT (`SLICE_CFG_MODEL`, `EXECUTE_CALIBRATE_FROM_SLICER`), not on anything in the file. In the stock template the next line that matters is `M109 S[first layer]`, which re-arms the nozzle before any extrusion. Nothing in the template re-asserts the bed if S6 fired. **Grade it unverified: it may change nozzle and bed targets.** **[inf]**
3. **SET_CONDITION is a runtime "if" that silently drops lines.** While its flag is set, the dispatcher skips every line except `SET_CONDITION_END` (`gcode.cpp:1174-1185`). It has a single flag with no nesting, and it ignores `J`. In an uploaded file it could hide an `M109` that a file reader counted as having run. **Keep it refused in files.** Inside G180 bodies, treat the bracketed lines as possibly skipped. **[src]**
4. **M109 does not wait for the exact target.**
   - Both `[extruder]` and `[heater_bed]` use `control=pid` (`printer_dsp.cfg:129,158`).
   - The PID settle test is ±5 °C with slope under 0.5 °C/s (`heaters.cpp:514-515,586-590`). Klipper uses 1.0 °C and 0.1 °C/s.
   - The wait also ends early on a print stop or a heater fault (`heaters.cpp:1111`).
   - `M109 S0` does not wait at all (`heaters.cpp:1003`, `if (wait && temp)`).

   So the fork's "waited at the very target" means "within 5 °C of it". **[src]**
5. **A bare `T<n>` does nothing.** See section C. The tray switch lives only inside M6211, and M6211 chooses the tray from a color table that persists across prints and reboots. **[src+comm]**
6. **M6211 extrudes during sub-steps the fork's model doesn't list.** With `A=0`, which is the default because the change-filament template omits A, these run while the nozzle is still moving toward Q under an unwaited `M104`:
   - the channel check (about 60 mm);
   - the feed (+5 mm, then +13 mm of E and a 200 mm CANVAS motor move per `G180 S820` pass, up to 3 passes);
   - the plug-out (E-18 / E-60).

   An empty-tray branch also heats to a fixed **250 °C** and waits. See M6211 below. **[src+cfg]**

## A. Per-command table

### M6211: Elegoo flush/purge with CANVAS filament switch **[src+cfg]**

Source: `SRC elegoo/extras/mmu.cpp#L3229-L3383` (handler), `#L3222` (`limit_check`), `#L1164-L1178` (config defaults), `printer_dsp.cfg:370-395` (defaults on this config).

**Letters.** The firmware reads **14** letters: T A K L M N I J P Q S R C D. The fork's comment and refusal message say "thirteen", but its two sets (`_PURGE_FIELDS` + `_PURGE_OTHERS`) do hold all 14, so only the text is off. A missing letter takes its value from the printer config. Every value is clamped by `limit_check`, which silently moves it into range rather than refusing.

| Letter | Firmware name | Clamp | Default (cfg) | Effect |
|---|---|---|---|---|
| T | next_extruder | 0–31 (int) | 0 | tool, looked up in the CANVAS color table |
| A | need_extrude_check | 0–1 (int) | 0 | 1 = travel to the bin and **M109** wait at Q; 0 = **M104** Q, no wait |
| K | flush_length_single | 10–120 | 80 | flush mm per loop |
| L | flush_length | 10–1000 | (code 80) | total flush mm |
| M | old_filament_e_feedrate | 10–600 | 300 | E feedrate, first flush |
| N | new_filament_e_feedrate | 10–600 | 300 | E feedrate, later flushes |
| I | fan_speed_1 | 0–255 | 0 | part fan during and after |
| J | fan_speed_2 | 0–255 | 179 | part fan for cooling between loops |
| P | cool_time | 0–20000 ms | 5000 | `G4` dwell per loop |
| Q | old_filament_temp | **200–350** | 250 | first setpoint |
| S | new_filament_temp | **200–350** | 250 | final, waited setpoint |
| R | nozzle_temperature_range_high | **200–350** | cfg S + `r_offset` (20) = **270** | flush setpoint, unwaited |
| C | old_retract_length_toolchange | 0–10 | 2 | retract before and after |
| D | new_retract_length_toolchange | 0–10 | 2 | retract, new filament |

Notes on the table:
- R's default is the *config* S plus 20. It is **not** the command's S plus 20 (`mmu.cpp:1175-1176`).
- The 200 °C floor means a purge written at 190 °C runs at 200 °C.

**Order of operations:**

0. **Early exit.** If `get_T_channel(T) == feed_channel_current`, it returns at once and does nothing (`#L3252`). This includes the case where T is not in the table (−1) and no channel is loaded (−1).
1. `M204 S10000`, then `G1 Z<min(z+z_up_height, printable_height+0.5)>`, `G92 E0`, `G1 E-<C> F1800`, `M400`. This runs at whatever temperature is in force. The handler never sets `M83` itself.
2. Set Q:
   - `A=1`: `G180 S10` (an X/Y travel), then **`M109 S<Q>`**, then `M106 S<I>`.
   - `A=0`: **`M104 S<Q>`** with no wait, then `M106 S<I>`.
3. `CANVAS_SWITCH_FILAMENT T=<T>` (see below): cut, plug-out, plug-in, and the extrusions those include. If `error_code` is set afterwards, it restores accel and returns.
4. `M400`, `G92 E0`, **`M104 S<R>`** (no wait), then `G1 E<e_flush_dist=10> F<M>`. It then runs one block of 8 alternating `G1 E` moves.
   - **Firmware bug:** the last three lines of that block have no newlines between them: `"G1 E-C F1800" "G1 E C F300" "M400"`. The dispatcher (`gcode.cpp:1124-1155`) keeps the **last** value for each letter, so they run as one `G1 E+C F300`. The intent was a net-zero retract and unretract; the result is an extra C mm (2 mm by default) of extrusion. This is inferred from the parser, not observed (`#L3295-L3311`).
5. Loops `ceil((L-10)/K) - 1` times. Each pass travels to the cool position, sets `M106 S<J>`, dwells `G4 P<P>`, wipes with small `E-0.1` moves, then flushes 5×(0.18+0.02) of the per-loop length and does a −D/+D retract. All of this runs at R.
6. `M106 S<J>`, travel, **`M109 S<S>`**, then `G1 E6`. After that come wipes with small negative E, `G180 S818` (travel/wipe), travel, `M106 S<I>`, and accel restored.

**Heaters:** nozzle only (Q, R, S, plus a fixed 250 °C in the empty-tray branch). **Filament:** cut, retract, feed, and about **L + 6 + C** mm of purge (L already includes the 10 mm first flush, plus the final E6, plus C from the newline bug). That is L+8 with defaults, before any sub-step extrusion. **Waits:** at S always; at Q only when A=1.

**Fork comparison:**
- **Agree:** the step order, and that Q, R and S are heaters.
- **Disagree on detail:** the letter count in the text; the R default; the newline bug; and the sub-step extrusions under an unwaited Q.
- **Possibly stale:** the fork's 200–350 window for Q, R and S. Elegoo's July 2026 slicer model uses 185–350. A purge written at 185–199 °C would be refused under the fork's window; if the newer floor is real, that refusal would be wrong.

### G180 S\<n\>: macro dispatcher **[src+cfg]**

Source: `SRC elegoo/extras/gcode_move.cpp#L29-L55`. It loads `[g180]` keys `S1`..`S998` as templates, each defaulting to an empty body. `cmd_G180` reads `S` as an int and runs the macro only when `0 < S < tws.size()` (998).

- **S998 is loaded but can never be selected.** The fork says "S1…S998"; the usable range is S1–S997.
- A number with no body is empty, so it does nothing.

The full table is in section B.

### SET_CONDITION / SET_CONDITION_END: runtime if-block **[src]**

Source: `SRC elegoo/gcode.cpp#L769-L922` (SET_CONDITION), `#L924-L937` (END), `#L1174-L1185` (the skip).

- **Effect:** sets a single `is_ignore` flag. While it is set, the dispatcher drops every line except `SET_CONDITION_END`.
- **J** is never read. **B** is the comparison value.

What each `A` value skips on:

| `A=` | Skips the block when |
|---|---|
| `MACHINE_MODEL` | B ≠ "C" |
| `PRINT_CALIBRATE` | B ≠ bed_mesh `execute_calirate_from_slicer` |
| `SURFACE` or `BED_MESH_PROFILE` | B ≠ the current print surface (A/B side) |
| `ENABLE_CANVAS` | B ≠ CANVAS connect state |
| `BED_MESH_DATEA` / `BED_MESH_DATEB` | mesh-presence bit XOR B |
| `PRINT_TOOL` | CANVAS is not connected, or tool B is already the loaded channel. It also stores B for `SET_CONDITION_END B=` |
| `NOZZLE_TEMP` | the current nozzle temperature is ≥ B |

- An **unknown A** leaves the flag as it was. The flag is never reset by SET_CONDITION itself, only by END.
- `SET_CONDITION_END` with no B clears the flag. With a B, it clears the flag only if B matches the stored `PRINT_TOOL` B.
- **Heats, waits, extrudes:** none directly, but it can suppress any of those.

### CANVAS_SWITCH_FILAMENT T= [SELECT=] **[src]**

Source: `SRC elegoo/extras/mmu.cpp#L2811-L2879`.

1. Returns immediately if the CANVAS MCU is not connected.
2. Maps T to a channel through the color table (`get_T_channel`, `#L2530`). If the channel is −1, it logs "do nothing" and stops.
3. Runs `extruder_channel_check()` (`#L1896`). If the loaded channel is unknown, this runs `channel_check_gcode_1`: about **60 mm of E** (`printer_dsp.cfg:355`), `M106 S200`, a 5 s dwell, then G180 S811/S810/S800. It may then cut and `G1 E-80`, or run `channel_check_gcode_2` (E20).
4. If the current channel ≠ the requested one, it runs `CANVAS_CUT_FILAMENT` and `CANVAS_PLUG_OUT_FILAMENT`.
5. It always runs `CANVAS_PLUG_IN_FILAMENT`.

On failure it retries; after 2 failures it sets error 3 and **blocks**, waiting for a user retry or exit.

- `SELECT` is read but unused in this revision.
- **Heaters:** none directly. Sub-steps can wait at 140 °C (G180 S800, inside the channel check) or at 250 °C (`clean_waste`).

### CANVAS_CUT_FILAMENT **[src+cfg]**

`mmu.cpp#L2926-L2968`. Skips if the head filament sensor is empty. Otherwise it runs:
- `move_to_cutting_knift_macro` = `G28 X Y TRY` + `G180 S813`, which pushes the head into the knife;
- then `release_cutting_knift_macro` = `M83`, `G1 E-1 F120`, travel, `G180 S800` (a conditional `M109 S140`).

It checks the knife sensor and raises an MMU error if the knife did not trigger.

### CANVAS_PLUG_OUT_FILAMENT **[src+cfg]**

`mmu.cpp#L2970-L3031`. Skips if the head sensor is empty. Otherwise it calls `clean_waste()` (`#L1724`): if the current tray reports empty, it runs `filament_load_gcode_1` (`G28 X Y TRY`, **`M104 S250`**, `G180 S800`, **`M109 S250`**). It then loops `filament_load_gcode_2` (`G180 S812`, about 85.6 mm of purge, plus S811/S810/S800) until the head sensor clears or 360 s pass.

The `#` in that config value (`M204 S5000#\nM104 S0`) cuts off the rest of the line (`configfile.cpp:902`), so the trailing `M104 S0` is inert.

It then moves the CANVAS gear back, runs `M83`, `G1 E-18 F700`, and `extruder_retract`, which is `G1 E-60 F1800` plus a CANVAS motor move of −400 mm (`#L1753`).

### CANVAS_PLUG_IN_FILAMENT [CHANNEL=] **[src+cfg]**

`mmu.cpp#L3033-L3062`. Skips if the head sensor already sees filament. Raises "out of filament" if the tray is empty. Otherwise it runs `extruder_feed` (`#L1820`):
- the CANVAS motor feeds up to 1500 mm until the head sensor trips;
- then `load_fila_ex_gcode_1` (`M83`, `G1 E5`);
- then up to 3× `load_fila_ex_gcode_2` (`G180 S820`: E5, a 200 mm motor move, E3, E5).

If the feed is blocked, it runs `PRINTER_UNLOAD_FILAMENT` and raises.

### Other CANVAS and filament macros

| Command | What it does | Heats / waits | Filament | Source | Grade |
|---|---|---|---|---|---|
| CANVAS_LOAD_FILAMENT CHANNEL= | MOVE_TO_WASTE_BOX, channel check, then (if a different channel) CUT, PLUG_OUT, PLUG_IN, then CLEAN_WASTE_FILAMENT | yes: 250 °C waited (move_to_waste_box), then `M104 S0` at the end of the `clean_waste_filament_macro` that CANVAS_CLEAN_WASTE_FILAMENT runs (`cfg:341`; not the `clean_waste()` function, whose trailing `M104 S0` is inert) | cut/unload/load/purge | `mmu.cpp#L2662` | src+cfg |
| CANVAS_UNLOAD_FILAMENT | MOVE_TO_WASTE_BOX (waits at 250), channel check, CUT, PLUG_OUT | yes, 250 °C waited | cut + withdraw | `mmu.cpp#L2717` | src+cfg |
| PRINTER_UNLOAD_FILAMENT | returns if CANVAS is not connected or the head sensor is empty; otherwise G180 S800 (conditional M109 S140), channel check, CUT, PLUG_OUT | conditional 140; 250 via clean_waste | cut + withdraw | `mmu.cpp#L2769` | src |
| CANVAS_MOVE_TO_WASTE_BOX | `G28 X Y TRY`, **M104 S250**, G180 S800, **M109 S250** | 250 °C waited | none | `mmu.cpp#L2881`, `cfg:339` | src+cfg |
| CANVAS_CLEAN_WASTE_FILAMENT | G180 S812 (≈85.6 mm purge), S811, S810, S800, `M106 S0`, **`M104 S0`** | turns the nozzle **off** | purge | `mmu.cpp#L2887`, `cfg:341` | src+cfg |
| CANVAS_MOTOR_CONTROL CHANNEL= DISTANCE= SPEED= ACCEL= TIMEOUT= | drives the CANVAS feeder motors | none | moves filament in the CANVAS (not the extruder) | `mmu.cpp#L3386`; motor behaviour lives on the CANVAS MCU | **unverified** |
| CANVAS_SET_COLOR_TABLE T= CHANNEL= [COLOR=] | replaces the tool→channel table, which M6211 relies on | none | none, but it decides what later M6211s switch to | Neme77 (binary analysis); body not read here | **comm** |

Other registered CANVAS words exist, all in `mmu.cpp`: MESH_GEAR, PLUG_*, DETECT, SET_FILAMENT_INFO, RFID_SELECT, ABNORMAL_RETRY, SET_AUTO_PLUG_IN. None appears in a slice, and all should stay refused.

### BED_MESH_CALIBRATE (with FROM_SLICER, ADAPTIVE, ADAPTIVE_MARGIN, mesh_min/max, PROBE_COUNT, ALGORITHM) **[inf, unverified]**

- **Code:** binary only, `libelegoo_bed_mesh.so`.
- **Parameter names present in its strings:** `FROM_SLICER`, `ADAPTIVE`, `adaptive_margin`, `mesh_min`, `mesh_max`, `PROBE_COUNT`, `ALGORITHM`, `BED_TEMP`, and `PROFILE`.
- **Heater-related strings** (they appear in one cluster): `BED_TEMP`, `M140 S`, `G180 S5`, `G180 S3`, `M190 S`, `G1 X128 Y128 F20000`, `M109 S140 F20000`, `PROBE SAMPLES_TOLERANCE=0.015 SAMPLES=5 POSITION_ENDSTOP_UPDATE=1`, `G180 S6`, `G180 S4`, `M140 S0`.
- **Log line:** `cmd_BED_MESH_CALIBRATE: from_slicer {} execute_calirate_from_slicer {} adaptive_ignore_state {}`. This suggests the in-file call acts only when the print-start flag `EXECUTE_CALIBRATE_FROM_SLICER` is set. Neme77 says this flag is set by the start request, not the file.
- **Can:** probe; move Z; set and wait the bed; wait the nozzle at 140; possibly run S1's 200 → 180 → 140 sequence; possibly switch every heater off (S6).
- **Fork:** disagree with "sets no nozzle target". It is safe in the stock template only because `M109 S[first layer]` follows before any extrusion.
- **Recommended change:** have the gate drop the nozzle's verified-wait state at this line, so the next M109 must re-verify. Stock slices already have one.
- **Do not refuse on the bed.** Stock slices have no `M140`/`M190` after this line, so "bed unknown until the next M140/M190" would mean unknown for the whole print. Refusing on that would refuse every stock file. Instead, mark the receipt's bed setpoint "commanded, not verified after the mesh".

### BED_MESH_PROFILE LOAD=\<name\> [FROM_SLICER=1] **[inf]**

Binary only. The strings show `LOAD`, `SAVE`, `REMOVE` and `FROM_SLICER`. Used inside G180 S7, S8, S9 and S910. No heater strings are tied to it specifically. Loading a mesh is a Z-compensation change. Unverified in the binary.

### SET_PRINT_STATS_INFO TOTAL_LAYER= CURRENT_LAYER= **[src]**

`SRC elegoo/extras/print_stats.cpp#L127-L150`. Layer bookkeeping only. **Agree** with the fork.

### M73 [P] [R] [L] **[src]**

`print_stats.cpp#L41,L151`. Progress and layer display; `L` is the current layer. No mechanism.

### M2202 GCODE_ACTION_REPORT=… **[src]**

`gcode.cpp#L753-L766`. Sends a status feedback message only. The firmware itself emits it from M109/M190 (codes 1045/1405).

### Heaters **[src]**

| Cmd | Behaviour | Source |
|---|---|---|
| M104 S [T] | sets the nozzle target, no wait. `T` picks `extruder`/`extruder<n>`. `T≥1` raises "Extruder not configured" unless S ≤ 0. S defaults to 0 | `extruder.cpp#L489-L516` |
| M109 S [T] | sends M2202 1045, then sets the target and waits until within ±5 °C and slope < 0.5 °C/s. S0 does not wait. A print stop or heater fault ends the wait. The wait runs in both directions (it also waits down to a lower target) | `extruder.cpp#L518-L525`, `heaters.cpp#L514-L590,L991-L1005,L1093-L1118` |
| M140 S | sets the bed target; reads only S | `heater_bed.cpp#L54-L64` |
| M190 S | sends M2202 1405, then sets the bed target and waits. Only S is read; the bare `A` in Elegoo's start block is dropped by the parser, since a lone letter has no value pair (`gcode.cpp#L1148-L1155`) | `heater_bed.cpp#L66-L73` |
| SET_HEATER_TEMPERATURE, TEMPERATURE_WAIT, TURN_OFF_HEATERS | registered (`heaters.cpp:780-786`), with standard Klipper meaning | src |

### Fans **[src]**

| Cmd | Behaviour | Source |
|---|---|---|
| M106 [P] [S] | `S` defaults to **255** (full) and is divided by 255. `P` is truncated to a whole number. **P0 or no P** = the part fan (`[fan]`). **P2** = `SET_FAN_SPEED FAN=fan1` (auxiliary fan). **P3** is commented out, so it does nothing (the cavity fan runs itself from `[cavity_fan]`). Any other P does nothing | `fan.cpp#L300-L318` |
| M107 | part fan off; **ignores all parameters, including P** | `fan.cpp#L320-L323` |

### Others seen around slices **[src]**

| Cmd | Behaviour | Source |
|---|---|---|
| M600 [S] [TEMP=] | filament-change pause. Runs `filament_load_gcode_1` and an **`M109 S<TEMP>`, defaulting to 250**. It is Orca's `machine_pause_gcode`, so it appears in a file only if the user inserts a pause | `filament_load_unload.cpp#L25,L45-L66` |
| M400 | waits for queued moves | src |
| G4, M204, M220, M221, M82, M83, G90, G91, G92, G20, G21, G28 | registered, Klipper-standard | `gcode_move.cpp#L130-L132` etc. |

### Commands with no handler in the published source

All of these were checked by name in every built `.cpp` and with `strings | grep -x` on the bed-mesh `.so`: G10, G11, G12, M620, M621, M141, M191, M6210, M6212 (and the rest of M62xx other than M6211), and M1xxx/M9xxx. None has a handler.

An unregistered command falls through to `cmd_default` (`gcode.cpp#L686-L733`), which only replies `Unknown command: X`. It neither raises an error nor acts, except that it raises when the printer is not ready.

On this source the fork's blocked G10/G11/G12 are therefore **no-ops**, not filament moves. Keeping them refused is still reasonable, because the running firmware is unverified. **[src; negative result]**

## B. Full G180 S-table (published `printer_dsp.cfg` lines 644-686) **[cfg]**

Numbers not listed have empty bodies and do nothing. S998 can never be selected.

| S | Body (summary) | Heats / waits | Extrudes / filament | Line |
|---|---|---|---|---|
| 1 | `G28 X Y TRY`, travel to bin, **M109 S200**, `M106 S200`, **M109 S180**, `M104 S140`, X wipes, `M106 S100` | waits 200 then 180; sets 140 | none (no E) | 646 |
| 2 | `M104 S140`, `BED_MESH_CLEAR`, `PROBE`, `SET_KINEMATIC_POSITION Z=0`, nozzle scrub with G2/G3 including Z-0.6, **M109 S140**, a long Z-wiggle series | sets 140, waits 140 | none; clears the mesh, re-zeroes Z | 647 |
| 3 | `G28 X Y TRY`, `G90`, `M204`, **G180 S1**, **G180 S2**, **M109 S140**, `M106 S0`, travel, `G28` | as S1+S2, ends waited at 140 | none | 648 |
| 4 | `M400` | – | – | 649 |
| 5 | same as S3 | same as S3 | none | 650 |
| 6 | `G90`, Z up, travel, **M104 S0, M140 S0**, all fans off (P3 is a no-op) | every heater off | none | 651 |
| 7 | `SET_CONDITION A=BED_MESH_PROFILE B=0` → load `default`; `B=1` → load `default1`; `M400` | none | none | 652 |
| 8 | `BED_MESH_PROFILE LOAD=ADAPTIVE FROM_SLICER=1`, `M400` | none (binary unverified) | none | 653 |
| 9 | if `ENABLE_CANVAS` = 1: **PRINTER_UNLOAD_FILAMENT**; then travel, `G92 E0`, the S7 mesh loads | conditional 140; 250 possible via clean_waste | cut + withdraw; channel check may extrude about 60 mm | 654 |
| 10 | travel to bin `G1 X51 Y245`, `G1 Y264` | none | none | 657 |
| 800 | if nozzle < 100 °C: **M109 S140**; then travel to bin | conditional wait at 140 | none | 660 |
| 810 | X wipes with 3× `E-0.1` | none | −0.3 mm | 663 |
| 811 | `M204 S18000`, Y shakes with 4× `E-0.1` | none | −0.4 mm | 666 |
| 812 | `M83` + 10×(E7.708 + E0.856) ≈ **85.6 mm purge**, `M106 S225`, `G4 P5000` | none (runs at the current temperature) | **+85.6 mm** | 669 |
| 813 | travel into the cutter (X245 Y25 → X254.5 → Y2.5), which **cuts** | none | mechanical cut | 672 |
| 818 | wipe and travel moves, no E | none | none | 675 |
| 820 | S829, `G1 E5`, `CANVAS_MOTOR_CONTROL DISTANCE=200`, `M83`, `E3`, S829, `E5` | none | **+13 mm E plus 200 mm CANVAS feed** | 678 |
| 821 | stop motors, `CANVAS_MOTOR_CONTROL DISTANCE=-200`, `M83`, **E-50**, S829, `E5` | none | withdraw | 681 |
| 829 | `CANVAS_MOTOR_CONTROL CHANNEL=0..3 SPEED=0` | none | stops the feeders | 684 |
| 910 | S7-style conditional loads of `ADAPTIVE` | none | none | 655 |
| 911 | like S2, but probes with `G28 Z I128 J255 L3 K0.05` | sets 140, waits 140 | none | 656 |

**Fork comparison (`_G180_MACROS`):**
- **S7 and S8:** agree.
- **S9:** agree with the body and with treating it as ending the print. Its description leaves out that the unload's channel check can extrude and that `clean_waste` can heat to 250. Its ends-print accounting still covers this, so this is a gap in detail, not a safety hole.
- **Every other number:** agree with refusing it unless read. S1, S2, S3, S5, S6, S800, S812, S820 and S821 all heat, wait, purge, cut or withdraw. S4, S10, S810, S811, S818 and S829 are motion-only, with small negative E at most. They could be admitted with E accounting, but no slice emits them.

## C. Bare `T<n>` on this firmware

**It does nothing.** `T0` reaches the dispatcher as the command name `T0`, and no handler is registered for it:
- `"T"` is commented out of the CANVAS_SWITCH_FILAMENT registration (`mmu.cpp#L1355-L1361`);
- `canvas_dev.cpp` is not built;
- the `"T"+n` string in `extruder.cpp:274` is only a heater's G-code id.

So `T0` falls to `cmd_default` and produces `Unknown command: T0`, with no switch and no heat. **[src]**

The tray switch happens **only** inside `M6211`, which calls `CANVAS_SWITCH_FILAMENT T=<n>`. It picks the physical tray from the color table that `CANVAS_SET_COLOR_TABLE` sets at print start. Neme77 adds that this table **persists across prints and reboots**: the touchscreen clears it before single-filament prints, but LAN method 1020 with an empty `slot_map` does not.

So a single-tool file's start-block `M6211 A1 ... T0` can still cut and switch filament if a stale table maps T0 to some channel. Neme77 logged exactly that on 2026-10-14: an externally fed print paused at layer 0 with error 1210 or 1220. **Lead only:** this signature is worth comparing with the open "PETG layer-zero pause" note in `~/src/3d-printing/AGENTS.md`. It is not a conclusion. **[comm]**

The fork treats bare `T<n>` as the tool select and the M6211 `T` as the switch. Its reading of M6211 is right. The bare `T` itself is inert here, so it is harmless to allow, but it is not evidence of which tool runs.

## D. What I could not establish

1. Whether commit `5a2ea7fc` (2026-03-11) matches the running firmware 02.01.00.00, and whether the machine's config matches `debugfile/printer_dsp.cfg`. Neither can be read back over LAN.
   - ElegooSlicer's July 2026 M6211 model points to a **newer firmware**: different K and R/S clamps (185 floor), a 15 mm first flush, and a G180 **S819** that is missing from the published `[g180]`.
   - So the M6211 clamp windows, its default letters, and the G180 table above may all be out of date for this printer.
   - The only Elegoo-specific prebuilt binary in the repo is `libelegoo_bed_mesh.so`. The "no handler" results therefore cover the source plus that binary, and nothing newer.
2. What `BED_MESH_CALIBRATE` / `BED_MESH_PROFILE` actually execute with `FROM_SLICER=1` (binary only), and specifically whether they change nozzle or bed targets in the in-file path.
3. What the color table holds at print start for a given job. It is set by the start request or the touchscreen, never by the file.
4. CANVAS motor and sensor behaviour on the CANVAS unit's own controller (`CANVAS_MOTOR_CONTROL`, the feeder distances).
5. The extruder's E mode when M6211 starts. The handler issues `G1 E-C` without `M83`; the slicer's start block sets `M83` only later, after the first M6211. So the first purge's retract depends on whatever mode was in force before. That is likely absolute after `G92 E0`, where `G1 E-2` is still a −2 mm move, but this is unconfirmed. In absolute mode, the flush's small `G1 E<x>` moves after `G1 E10` would move *back toward zero*, so the amount purged depends on the E mode. The gate is unaffected, because the fork counts M6211 as an extrusion either way.
6. The `CANVAS_SET_COLOR_TABLE` body in this revision (not read here; Neme77's description is from their binary analysis).
7. The fork's claim about failed number conversions **is confirmed**: `get_double` catches the `stod` failure and goes on to use the uninitialised `d_value` (`gcode.cpp:200-227`). It is listed here only because the result is undefined behaviour.

Counting: N = rows and sections documented above (M6211, G180, SET_CONDITION, SET_CONDITION_END, CANVAS_SWITCH_FILAMENT, CANVAS_CUT_FILAMENT, CANVAS_PLUG_OUT_FILAMENT, CANVAS_PLUG_IN_FILAMENT, CANVAS_LOAD_FILAMENT, CANVAS_UNLOAD_FILAMENT, PRINTER_UNLOAD_FILAMENT, CANVAS_MOVE_TO_WASTE_BOX, CANVAS_CLEAN_WASTE_FILAMENT, CANVAS_MOTOR_CONTROL, CANVAS_SET_COLOR_TABLE, BED_MESH_CALIBRATE, BED_MESH_PROFILE, SET_PRINT_STATS_INFO, M73, M2202, M104, M109, M140, M190, M106, M107, M600, M400, T\<n\>, G10/G11/G12, M620/M621, M141/M191, M62xx-other, M1xxx/M9xxx). K = commands whose behaviour cannot be read in published source text (BED_MESH_CALIBRATE, BED_MESH_PROFILE, CANVAS_MOTOR_CONTROL, CANVAS_SET_COLOR_TABLE). Every count is relative to the published revision, not the running firmware.

R3 done: 34 commands documented, 4 unverified
