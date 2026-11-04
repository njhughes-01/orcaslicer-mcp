# Direct Centauri Carbon 2 printing

> Scope update: conflicting restrictions below are superseded by [revised-plan.md](revised-plan.md).

## Goal
Let OrcaSlicer MCP inspect a CC2, upload a sliced file and start it directly, without Home Assistant. Nathan chooses the file and tray; the MCP verifies the printer's actual responses.

## Evidence and scope
On this printer, direct UDP discovery, MQTT registration, full status (1002), CANVAS status (2005), file list (1044), 1 MiB HTTP upload chunks and one start (1020) worked. The fourth tray reported status 2 (active), which the Home Assistant helper incorrectly treated as unavailable. The upload replies returned success; offset fields were not checked, so offset semantics were not verified. The start command returned success and the printer reported the requested file printing. This is evidence for this printer and firmware, not every CC2 variant.

Add explicit CC2 status, file-list, upload and start tools. Do not add pause, stop, delete, presets, printer configuration, or automatic retries. Keep the current read-only default exposure; print tools require a separate per-project opt-in and explicit task authorization. Do not modify the existing installation, printer or project while planning.

## Done when
- Offline tests show actual tray status 0=empty, 1=present and 2=active, with unknown values rejected; no Home Assistant dependency exists.
- Upload and start are separate calls. Refusals, timeouts, partial transfers and lost start responses never result in retries or an unverified claim of success.
- Start requires a verified idle state, a confirmed clear bed, the exact verified uploaded file, explicit tray, matching material and temperature, and a complete tool mapping. Missing, stale or unverifiable data blocks the start. PETG is not a hard-coded requirement for other materials.
- Credentials are never returned, logged or stored in the repository; opt-in exposure and annotations classify printer writes correctly.
- Mock protocol tests and the existing relevant suite pass. A real-printer check is separately authorized and never starts an extra print.
