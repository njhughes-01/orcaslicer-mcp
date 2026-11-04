# Direct CC2 print capability

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
- **THEN** direct printer tools remain hidden, server-side gates reject direct calls before network access, and credentials are not emitted.
