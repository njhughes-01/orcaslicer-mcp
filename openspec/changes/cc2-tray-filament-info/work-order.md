## Skills
- none

## Tools
read, edit, write, bash, grep, find, ls

## Files
- src/orcaslicer_mcp/printer/cc2_tray.py
- tests/test_cc2_tray.py
- src/orcaslicer_mcp/server.py
- tests/test_server_cc2.py
- README.md
- CHANGELOG.md
- lhm.plugin.json
- openspec/changes/cc2-tray-filament-info/tasks.md

## Rules
- No printer, network, credential or environment file is touched; every test is a scripted reply.
- Do not create or rename anything containing the string `cc2_filament`; the new module is `cc2_tray.py`.
- Import the acceptance vocabulary from `cc2_control`; never reword it.
- Keep the nine 2003 fields exactly as design.md lists them; never add a caller-supplied temperature.

## Checks
- uv run python -c "import orcaslicer_mcp.server"
- uv run pytest -q tests/test_cc2_canvas.py tests/test_cc2_control.py
