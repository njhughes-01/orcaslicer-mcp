## Skills
- none

## Tools
read, edit, write, bash, grep, find, ls

## Files
- src/orcaslicer_mcp/printer/cc2_upload.py
- src/orcaslicer_mcp/printer/cc2_stock_templates.py
- src/orcaslicer_mcp/printer/cc2_start.py
- src/orcaslicer_mcp/server.py
- tests/test_cc2_upload.py
- tests/test_cc2_start.py
- tests/test_server_cc2.py
- README.md
- CHANGELOG.md
- lhm.plugin.json
- openspec/changes/cc2-stock-profile-allowlist/tasks.md

## Rules
- No printer, network, credential or environment file is touched; every test is a scripted reply.
- The real G-code under ~/src/3d-printing is read-only input and is never committed; the fixture is tests/fixtures/cc2_two_tool.gcode.xz.
- Keep every existing refusal design.md does not change; M600, M141/M191, G10/G11, EXCLUDE_OBJECT_*, G17, SET_INPUT_SHAPER, M82 stay refused.
- Stock templates are copied verbatim from stock-templates.md §B; never retyped from memory.

## Checks
- uv run python -c "import orcaslicer_mcp.server"
