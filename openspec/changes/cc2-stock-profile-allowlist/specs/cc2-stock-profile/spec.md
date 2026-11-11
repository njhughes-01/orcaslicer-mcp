# cc2-stock-profile

## ADDED Requirements

### Requirement: Printer-model gate
The upload parser SHALL refuse a file whose config block does not declare `printer_model = Elegoo Centauri Carbon 2`.

#### Scenario: Centauri 2 slice refuses
- **WHEN** a file declares `printer_model = Elegoo Centauri 2`
- **THEN** the file refuses before any byte is sent, naming the model it found

### Requirement: Layer-two setpoint transition
A bare `M104` to the current tool's declared `nozzle_temperature`, after a verified wait at its declared `nozzle_temperature_initial_layer`, SHALL be accepted without a new wait and both temperatures SHALL be recorded as that tool's extrusion temperatures.

#### Scenario: Inland PLA copy parses
- **WHEN** a one-tool file waits at 220, extrudes, then writes `M104 S225` and extrudes again, declaring 220/225
- **THEN** it parses with extrusion temperatures (220, 225)

#### Scenario: an undeclared setpoint still refuses
- **WHEN** the same file writes `M104 S230` instead
- **THEN** the next extrusion refuses

### Requirement: Mixed-temperature tool change
A tool change between filaments at different declared temperatures SHALL be accepted when the tower setpoint equals the next tool's declared temperature, only retracts follow it before the purge, the purge's `Q` equals the wait in force and its `S` equals the next tool's declared temperature.

#### Scenario: PLA 210 to PLA Basic 220
- **WHEN** the change block is `M104 S220`, `G1 E-2`, `M6211 T1 ... Q210 R240 S220`, `T1` with tool 1 declared at 220
- **THEN** it parses with tool 1's extrusion temperatures including 220

#### Scenario: wrong Q refuses
- **WHEN** the purge says `Q230` while the wait in force is 210
- **THEN** the file refuses

### Requirement: Bed mesh resets the wait
After `BED_MESH_CALIBRATE` the parser SHALL require a fresh `M109` before any extrusion.

#### Scenario: no wait after mesh refuses
- **WHEN** a file runs `BED_MESH_CALIBRATE` and then `G1 E1` with no `M109` between
- **THEN** the file refuses

### Requirement: Stock-template identity
The parser SHALL compare the file's embedded start, end and change-filament templates with the stock OrcaSlicer 2.4.2 Elegoo CC2 templates and report `stock` or the differing keys, never refusing on it.

#### Scenario: stock slice
- **WHEN** the Pēnelópē fixture is parsed
- **THEN** `vendor_templates` is `stock`

### Requirement: Tray-table note
Every `cc2_start_print` reply SHALL carry a note that the physical tray is chosen by the printer's persistent colour table inside `M6211`, not by the file's `T`.

#### Scenario: note present
- **WHEN** any start returns
- **THEN** its notes include the tray-table sentence
