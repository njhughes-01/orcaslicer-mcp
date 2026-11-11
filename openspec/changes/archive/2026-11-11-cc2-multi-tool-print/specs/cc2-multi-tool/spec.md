# cc2-multi-tool

## ADDED Requirements

### Requirement: Per-tool parsing
The upload parser SHALL accept a G-code file that selects several tools and SHALL read per-filament config lists one value per tool.

#### Scenario: two-tool PLA file parses
- **WHEN** a file selects T0 and T1 with `filament_type = PLA;PLA` and `nozzle_temperature = 210,210`
- **THEN** parsing returns two tools, each PLA at 210 °C, and refuses nothing

#### Scenario: short config list refuses
- **WHEN** a file selects T0 and T1 but `filament_type` lists one value
- **THEN** the file refuses before any byte is sent

### Requirement: Purge R rule
An `M6211` whose `R` differs from the verified wait SHALL be accepted only when R equals the declared `nozzle_temperature_range_high` of both tools involved, and R SHALL be recorded as a flush temperature of both tools, apart from their extrusion temperatures (design amendment 9).

#### Scenario: vendor change gcode passes
- **WHEN** `M6211 T1 ... Q210 R240 S210` follows a verified 210 °C wait and both tools declare range high 240
- **THEN** the purge is accepted and 240 appears in both tools' flush temperatures while their extrusion temperatures stay (210,)

#### Scenario: R outside the declared range refuses
- **WHEN** R is 250 and the declared range high is 240
- **THEN** the file refuses

### Requirement: Multi-slot start
`cc2_start_print` SHALL send one method 1020 whose `config.slot_map` carries one complete `{t, canvas_id, tray_id}` entry per tool the receipt names, and SHALL refuse a mapping that does not name exactly those tools once each on distinct trays.

#### Scenario: two tools start
- **WHEN** the receipt names tools 0 and 1 and the mapping is `{0: 1, 1: 2}` with both trays present, PLA, rated 190–230
- **THEN** one 1020 is sent with `slot_map` `[{t:0,canvas_id:0,tray_id:1},{t:1,canvas_id:0,tray_id:2}]`

#### Scenario: flush within the vendor margin is noted, not refused
- **WHEN** a tool's flush temperatures include 240 and its tray is rated to 230
- **THEN** the start is accepted and a note names 240 and 230

#### Scenario: flush beyond the margin refuses
- **WHEN** a tool's flush temperatures include 250 and its tray is rated to 230 (more than 10 °C over)
- **THEN** the start refuses with nothing sent

#### Scenario: printing temperature above the rating refuses
- **WHEN** a tool's extrusion temperatures include 240 and its tray is rated to 230
- **THEN** the start refuses with nothing sent

### Requirement: Tool changes only through the purge
A bare `T<n>` naming a different tool than the one in force SHALL refuse the file; a tool change arrives only through `M6211 T<n>`.

#### Scenario: bare switch refuses
- **WHEN** a file selects T0, extrudes, then writes `T1` with no `M6211 T1` before it
- **THEN** the file refuses before any byte is sent

### Requirement: Receipt compatibility
A version-2 receipt SHALL still be read as a one-tool receipt; a version-3 receipt SHALL carry every tool.

#### Scenario: old receipt reads
- **WHEN** a version-2 receipt file is loaded
- **THEN** it yields one tool with the flat fields' values
