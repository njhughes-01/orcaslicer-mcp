# Direct CC2 print capability

> Historical requirements: conflicting exposure and scope restrictions are superseded by ../../revised-plan.md. Revise these requirements as the expanded capability inventory is verified.

## ADDED Requirements

### Requirement: Accurate printer and tray state
The server MUST retrieve and validate live CC2 state directly.

#### Scenario: Active filament
- **WHEN** a connected CANVAS tray reports status 2
- **THEN** the tool reports the tray as active and available, with its material and rated nozzle range.

#### Scenario: Unknown state
- **WHEN** status or tray data is missing, malformed, stale or unknown
- **THEN** the tool reports uncertainty and start is refused.

### Requirement: Upload without replacing files
The server MUST transfer a validated local G-code file under a new basename and verify it on the printer.

#### Scenario: Collision or interrupted transfer
- **WHEN** the file already exists, listing is incomplete, a chunk fails or an acknowledgment is ambiguous
- **THEN** the operation stops, does not overwrite or retry, and reports the result accurately.

#### Scenario: File provenance
- **WHEN** a verified file is uploaded
- **THEN** a private receipt binds printer identity, remote name and size to the uploaded bytes' hash and parsed print settings; a start without a matching receipt is refused.

### Requirement: Explicitly requested start
The server MUST start only the explicitly named file and tray after fresh checks.

#### Scenario: Safe start
- **WHEN** the caller confirms a clear bed, the printer is idle, the file and settings are verified, and the explicit mapping matches present compatible filament
- **THEN** send one start command and distinguish acceptance from observed printing.

#### Scenario: Ambiguous start
- **WHEN** the start response is lost
- **THEN** do not retry and report the state as uncertain unless a read-only observation proves the job is running.

### Requirement: Privacy and exposure
Credentials and connection secrets MUST not appear in output or logs; printer calls remain hidden unless explicitly opted in, and write operations are marked non-idempotent.

#### Scenario: Default exposure
- **WHEN** the server starts with its default policy
- **THEN** direct printer tools remain hidden, server-side gates reject direct calls before network access, and credentials are not emitted

### Requirement: Commands accepted only with a read justification
Every accepted command MUST be written down with the reason it cannot change a heater target or move filament, and everything else MUST refuse the file before a byte is sent. A command that only *dispatches* to printer-side code MUST be decided by the dispatched number, never by its own name.

#### Scenario: An unread CC2 macro number
- **WHEN** a file issues `G180` with a macro number whose body in the printer's `[g180]` config section has not been read against the published Elegoo firmware and configuration, or in a spelling whose number cannot be determined
- **THEN** the upload is refused, the message names the line and what is unread, and nothing is skipped silently

#### Scenario: A read macro number that can move the filament path
- **WHEN** a file issues one of the read numbers whose published body can heat, cut or withdraw filament (`G180 S9`, the end-of-print unload) and anything after it would extrude or purge
- **THEN** the file is refused, because that body's effects depend on live printer state the file cannot show

#### Scenario: A command whose inertness is a claim about its parameters
- **WHEN** a file issues an accepted command whose reason holds only for the parameters its dialects document (the `G4` dwell, the `M107` fan off) with any other parameter letter, an `E` included
- **THEN** the file is refused before a byte is sent, because the reason that admitted the command does not cover that line

#### Scenario: A parameter letter written in lower case
- **WHEN** a file writes a command or parameter letter in lower case (`M104 s140`, `G180 s7`, `g1 x1e6`)
- **THEN** it is read exactly as its upper-case spelling is read, while the number of setpoints a command carries is still counted, so a mixed-case `M104 S140 s200` is still refused as the two setpoints it is

#### Scenario: A purge whose own setpoints or tool contradict the file
- **WHEN** a file's `M6211` flush names a setpoint (`Q`, `R` or `S`) other than the nozzle temperature verified by an `M109` wait at that point, names a tool (`T`) the file never selects, omits one of those four parameters, or gives a value outside the clamp window the published firmware reads it through
- **THEN** the upload is refused naming the letter and the value, because that firmware sets `R` with a bare `M104` and extrudes at it unwaited, waits at `Q` only when `A=1`, and switches that tool's filament mid-purge, and nothing is skipped silently

#### Scenario: A heat command whose parameters continue past a welded first one
- **WHEN** a file welds a heat code to its first parameter and writes another parameter after it (`M109S260 S0`, `M104S140 R200`), the same line spaced or packed either way, on any of `M104`, `M109`, `M140`, `M190`
- **THEN** every parameter written on the line is counted, so the duplicate setpoint or the second numbered parameter refuses the file exactly as it does when the command is spaced — the firmware resolves such a line by keeping the last value of a repeated letter, so dropping the tail would report a setpoint the printer would not hold

### Requirement: Truthful file-listing pages
The server MUST certify the printer's file inventory before reporting any of it, and MUST state of every reply whether that reply holds the whole inventory, so no page can be mistaken for a complete answer and nothing is ever withheld without the means to fetch it.

#### Scenario: A reply that cannot hold every certified file
- **WHEN** the certified inventory is larger than one reply can carry
- **THEN** the reply reports the printer's total and the number it holds, `complete: false`, `has_more`, the exact `next_offset` that fetches the remainder and a note naming how many were left out, and following `next_offset` to the end returns every name exactly once

#### Scenario: Where a documented paging walk stops
- **WHEN** a caller pages a shelf too big for one reply and follows the documentation to know when to stop asking
- **THEN** the stop named is `has_more` turning false, never `complete`: `complete` stays the claim of the one reply that held the whole listing, so it is false on every page of a paged shelf, the last one included, and a loop written against it would never end — while `inventory_complete` still carries the printer-side certification on every page

#### Scenario: A request for more than one reply can carry
- **WHEN** a caller asks for a limit above the most one reply may hold, or for a negative, fractional, text or boolean offset or limit
- **THEN** the call refuses in-process naming that cap and how to page, before any address is resolved or any connection is opened, rather than answering as a short page that dropped rows silently
.
