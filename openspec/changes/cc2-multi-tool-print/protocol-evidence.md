# Multi-colour start on the CC2 — protocol evidence (2026-11-11)

Source: Elegoo's published LAN web client, `elegooofficial/elegoo-fdm-web` release 20260625,
`lan_service_web.zip` → `index.html` (unpacked at `~/scratch/tmp/cc2-tray-2026-11-11/gh-release/unz/`).

## Start print (method 1020 = `Pt.StartPrint`) — multi-colour branch, verbatim
```
_={filename:r.fileData.name,storage_media:In.Local,config:{delay_video:d.value.timeLapse,
   printer_check:d.value.bedLeveling,print_layout:d.value.selectedBed,bedlevel_force:!1,
   slot_map:s.value.map(F=>({t:F.t,canvas_id:F.canvasId,tray_id:F.trayId}))}};
we.log("MQTT多色打印",...)      // "MQTT multi-colour print"
```
Single-colour branch sends the same without `slot_map` and without `bedlevel_force`
(`"MQTT单色打印"`). The fork today sends `{storage_media, filename, config:{slot_map:[one entry],
printer_check}}` — the same shape with one entry; the measured firmware behaviour (partial entry →
error_code 0 and silent tray 0) is in `cc2_start.py`'s docstring.

## What the two-colour Orca slice contains (penelope-tag-grey-black-20261111-v2.gcode)
Command heads: G1 G2 G4 G21 G28 G90 G92 G180(S7,S8,S9) M220 M6211 M73 M83 M84 M104 M106 M109 M140
M190 M204 T0 T1 BED_MESH_CALIBRATE SET_PRESSURE_ADVANCE SET_PRINT_STATS_INFO SET_VELOCITY_LIMIT.
Rules of the current parser it breaks (Strata sweep, 2026-11-11):
1. `M6211 T0 L0 M523 N523 Q210 R240 S210` (line 196 and every change): R ≠ verified 210.
   Template: `M6211 T[next_extruder] L[flush_length] M{old_e_feedrate} N{new_e_feedrate}
   Q[old_filament_temp] R[nozzle_temperature_range_high] S[new_filament_temp]`;
   `nozzle_temperature_range_high = 240,240`.
2. `M220 S100` (7×): not on the allow list.
3. `T1`/`T0` selects: two tools.
4. `M6211 T1 ...`: purge switching to a tool "the file never selected" (it did; rule 3 masks it).
Per-filament config lists: `filament_type = PLA;PLA`, `nozzle_temperature = 210,210`,
`nozzle_temperature_initial_layer = 210,210`, plate temps `60,60` (dedupe to one today).
Not violations: G180 S7/S8/S9 are allow-listed; cooldown after last extrusion; M190 S60 A flag.

## Not established
Whether this firmware honours a two-entry slot_map from this adapter (Elegoo's client proves the
shape, not our session), and whether the flush at R really runs without a wait (the parser's model).
