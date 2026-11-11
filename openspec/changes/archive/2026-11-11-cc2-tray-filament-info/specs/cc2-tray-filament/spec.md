# cc2-tray-filament

## ADDED Requirements

### Requirement: Write one CANVAS tray's filament info with method 2003
The server SHALL offer `cc2_set_tray_filament` which, when confirmed, sends exactly one
`{"method": 2003, "params": {canvas_id, tray_id, brand, filament_type, filament_name, filament_code,
filament_color, filament_min_temp, filament_max_temp}}` frame, the shape Elegoo's LAN client sends.

#### Scenario: preview sends only the read
- **WHEN** `cc2_set_tray_filament` is called without `confirm=True`
- **THEN** only the read-only methods 1002 and 2005 leave the transport and the reply shows the exact 2003 params it would send

#### Scenario: confirmed write is sent once and read back once
- **WHEN** called with `confirm=True` and the printer answers `error_code` 0
- **THEN** the recorded wire is `[1002, 2005, 2003, 2005]`, the result is `accepted`, and the tray's observed fields are reported separately from acceptance

#### Scenario: refusal earns no read, ambiguity is never resent
- **WHEN** the printer answers a nonzero `error_code`, or never answers
- **THEN** the wire holds exactly one 2003, the code is kept (refused) or the outcome is `ambiguous`, and nothing is resent

### Requirement: Type, code and temperatures come from the vendor catalog
The server SHALL derive `filament_type`, `filament_code`, `filament_min_temp` and `filament_max_temp`
from the chosen `filament_name` using Elegoo's own 50-entry catalog and SHALL refuse an unknown name
before opening any session.

#### Scenario: unknown name
- **WHEN** `filament_name` is not a catalog key
- **THEN** the call refuses with the valid names listed and no session is opened

### Requirement: Only a tray the printer names can be written
The server SHALL refuse a write whose (canvas_id, tray_id) the 2005 reply does not list or whose unit is not connected.

#### Scenario: tray absent from the roster
- **WHEN** the 2005 reply lists trays 0-3 on canvas 0 and the call names tray 7
- **THEN** no 2003 is sent and the reply says the tray was not reported

### Requirement: Only an idle machine takes a tray write
The server SHALL read 1002 once in both phases and refuse the write unless the machine is idle (`ready_for_job`), as the motion candidate does.

#### Scenario: printing
- **WHEN** the 1002 reply reports a print running or paused
- **THEN** no 2003 is sent and the reply names the machine state

### Requirement: Unverified label
Every reply SHALL carry `verified_on_this_printer: false` and a sentence naming why until a live test has run.

#### Scenario: refusals are labelled too
- **WHEN** the tool refuses (unknown catalog name, no printer address, or the idle or roster gate)
- **THEN** the reply still carries `verified_on_this_printer: false` and the unverified sentence
