## Skills
- none

## Tools
read, edit, write, bash, grep, find, ls

## Files
- src/orcaslicer_mcp/printer/cc2_upload.py
- src/orcaslicer_mcp/printer/cc2_receipts.py
- src/orcaslicer_mcp/printer/cc2_start.py
- src/orcaslicer_mcp/server.py
- tests/test_cc2_upload.py
- tests/test_cc2_start.py
- tests/test_server_cc2.py
- tests/fixtures/cc2_two_tool.gcode
- README.md
- CHANGELOG.md
- lhm.plugin.json
- openspec/changes/cc2-multi-tool-print/tasks.md

## Rules
- No printer, network, credential or environment file is touched; every test is a scripted reply.
- The real G-code file under ~/src/3d-printing is read-only input for the fixture and is never committed.
- Keep every existing refusal design.md does not change; widen the allow list only with a reason and a test.
- Method 1020 params keep exactly storage_media, filename, config.slot_map, config.printer_check.

## Checks
- uv run python -c "import orcaslicer_mcp.server"
