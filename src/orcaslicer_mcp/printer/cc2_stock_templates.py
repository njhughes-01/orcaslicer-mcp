"""The verbatim stock G-code templates of the Elegoo Centauri Carbon 2, OrcaSlicer 2.4.2.

Design 5: an uploaded file carries its own templates as slicer-config comments, and whether
they are still the stock ones is a fact a human should see on the upload reply — never a
refusal, only a report (the parse calls this module and records the verdict). The five keys
here are the machine-level templates the CC2 chain resolves to; the constants below were
generated from ``openspec/changes/cc2-stock-profile-allowlist/stock-templates.md`` §B — the
verbatim dump that was itself byte-checked, file by file, against both the installed
OrcaSlicer 2.4.2 profile bundle and the v2.4.2 GitHub tag — never retyped from memory or
from a slice.

Comparison (``compare`` / ``compare_verdict``): a config value is one line with ``\\n`` for
every newline and ``\\\\`` for every literal backslash (the way Orca writes the template
into the config block); it is unescaped, then compared line by line with trailing whitespace
stripped on every line and trailing blank lines dropped on both sides. Why the tail is
loose and everything else is not: the profile's own trailing newline is represented
differently again when the slicer writes the config block (the reference two-tool slice ends
``machine_end_gcode`` at ``M84\\n`` where the dumped profile text carries the line plus its
newline), and no added or removed *line* can hide behind that — the first difference in the
line lists is reported. Everything else is exact: a leading blank line (the vendor's own
``change_filament_gcode`` starts with one) is kept and compared, and so is every inline
comment and trailing space before a line's end. A key the file does not carry is skipped,
never guessed at: ``compare_verdict`` reports how many of the five it actually looked at.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

# The one place these bytes came from: the OrcaSlicer repo at tag v2.4.2, the CC2 machine
# profile the vendor ships. machine_start_gcode, machine_end_gcode, change_filament_gcode
# and layer_change_gcode are set in the 0.4 nozzle file (0.2/0.6/0.8 inherit them unchanged);
# before_layer_change_gcode is inherited from the Elegoo machine common profile at the same
# tag. Verified byte-for-byte against both sources (stock-templates.md, "Sources").
SOURCE = ("https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/v2.4.2/resources/profiles/"
          "Elegoo/machine/ECC2/Elegoo Centauri Carbon 2 0.4 nozzle.json (before_layer_change_"
          "gcode inherited from .../resources/profiles/fdm_machine_common.json, same tag)")

MACHINE_START_GCODE = (
    ";===== CC2_START_GCODE ================\n"
    ";===== date: 2026-01-16-001 =====================\n"
    "\n"
    "G90\n"
    "M104 S140\n"
    "M140 S[bed_temperature_initial_layer_single]\n"
    "M190 S[bed_temperature_initial_layer_single] A\n"
    "M106 S0\n"
    "BED_MESH_CALIBRATE mesh_min={adaptive_bed_mesh_min[0]},{adaptive_bed_mesh_min[1]} mesh_max={adaptive_bed_mesh_max[0]},{adaptive_bed_mesh_max[1]} ALGORITHM=[bed_mesh_algo] PROBE_COUNT={bed_mesh_probe_count[0]},{bed_mesh_probe_count[1]} ADAPTIVE=0 ADAPTIVE_MARGIN=0 FROM_SLICER=1\n"
    "M204 S{min(20000,max(1000,outer_wall_acceleration))} ;Call exterior wall print acceleration\n"
    "G28\n"
    "M109 S[nozzle_temperature_initial_layer]\n"
    "M6211 A1 L200 T[initial_no_support_extruder] Q[nozzle_temperature_initial_layer] R[nozzle_temperature_initial_layer] S[nozzle_temperature_initial_layer]\n"
    "T[initial_no_support_extruder]\n"
    "\n"
    "{if first_layer_print_min[1] > 0.5}\n"
    "G180 S7\n"
    "G1 X{print_bed_max[0]*0.5-1} Y-1.2 F20000\n"
    "G1 Z0.5 F900\n"
    "M109 S[nozzle_temperature_initial_layer]\n"
    "M83\n"
    "G92 E0 ;Reset Extruder\n"
    "G1 E6 F{min(1200, max(120, filament_max_volumetric_speed[initial_no_support_extruder]*60/2/2.5043))} \n"
    "M106 S200\n"
    "G1 X{print_bed_max[0]*0.5-41} E20 F{min(12000, max(1200, filament_max_volumetric_speed[initial_no_support_extruder]*60/2/2.5043))} \n"
    "G1 F6000\n"
    "G1 X{print_bed_max[0]*0.5-46} E0.8\n"
    "{else}\n"
    "G1 E30 F{min(1200, max(120, filament_max_volumetric_speed[initial_no_support_extruder]*60/2/2.5043))}\n"
    "{endif}\n"
    "M106 S0\n"
    "G180 S8\n"
    "G1 F20000\n"
    "G92 E0 ;Reset Extruder\n"
    "SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count] CURRENT_LAYER=0\n"
    ";LAYER_COUNT:[total_layer_count]\n"
    ";LAYER:0\n"
)

MACHINE_END_GCODE = (
    ";===== CC2_END_GCODE ================\n"
    ";===== date: 2026-01-16-001 =====================\n"
    "\n"
    "\n"
    "M140 S0 ;Turn-off bed\n"
    "M83\n"
    "G92 E0 ; zero the extruder\n"
    "G1 E-1.5 F1800\n"
    "G2 I0 J1 Z{max_layer_z+0.5} F3000 ; lower z a little\n"
    "M106 S0\n"
    "M106 P2 S0\n"
    "G90\n"
    "{if max_layer_z > 75}G1 Z{min(max_layer_z+5, printable_height+0.5)} F20000{else}G1 Z80 F20000 {endif}; Move print head up \n"
    "G180 S9\n"
    "M104 S0\n"
    "M84\n"
    "\n"
)

CHANGE_FILAMENT_GCODE = (
    "\n"
    ";==========CC2_CHANGE_FILAMENT_GCODE==========\n"
    ";===== date: 2026-01-16-001 =====================\n"
    "M106 S0\n"
    "M106 P2 S0\n"
    "G1 Z{min(max_layer_z+3, printable_height+0.5)} F1200\n"
    "M6211 T[next_extruder] L[flush_length] M{old_filament_e_feedrate} N{new_filament_e_feedrate} Q[old_filament_temp] R[nozzle_temperature_range_high] S[new_filament_temp]\n"
    "T[next_extruder]\n"
    "\n"
)

LAYER_CHANGE_GCODE = (
    "M73 L{layer_num+1}\n"
    ";LAYER:{layer_num+1}\n"
    "SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count] CURRENT_LAYER={layer_num+1}\n"
)

BEFORE_LAYER_CHANGE_GCODE = (
    ";BEFORE_LAYER_CHANGE\n"
    ";[layer_z]\n"
    "G92 E0\n"
    "\n"
)

# The five keys a file's config block may carry, mapped to the stock text each must equal.
STOCK: dict[str, str] = {
    "machine_start_gcode": MACHINE_START_GCODE,
    "machine_end_gcode": MACHINE_END_GCODE,
    "change_filament_gcode": CHANGE_FILAMENT_GCODE,
    "layer_change_gcode": LAYER_CHANGE_GCODE,
    "before_layer_change_gcode": BEFORE_LAYER_CHANGE_GCODE,
}


def _unescape(raw: str) -> str:
    """A template config value as Orca writes it — one line, ``\\n`` per newline, ``\\\\``
    per literal backslash — back into real text. A backslash before anything else is not an
    escape Orca writes; it is left exactly as it stands, where it will simply not match the
    stock text (a report, never a guess)."""
    out: list[str] = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _lines(text: str) -> list[str]:
    """The comparison form of a template: split on newlines, trailing whitespace stripped per
    line, trailing blank lines dropped (see the module docstring for why the tail alone is
    loose)."""
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def compare_verdict(config_values: Mapping[str, Iterable[str]]) -> tuple[str, int]:
    """The report over one file's config values: ``("stock", n)`` when every present key (all
    of its config-block occurrences — the slicer repeats a key per section, and a file whose
    repeats disagree with stock in even one of them is not stock) matches the template,
    otherwise ``("modified: key, key", n)`` with the differing keys in STOCK's own order.
    ``n`` counts the stock keys the file carried at all; keys it never wrote are skipped and
    not counted, never assumed. This is a report, not a gate: nothing here raises."""
    compared = 0
    modified: list[str] = []
    for key, template in STOCK.items():
        occurrences = list(config_values.get(key) or ())
        if not occurrences:
            continue
        compared += 1
        wanted = _lines(template)
        if any(_lines(_unescape(raw)) != wanted for raw in occurrences):
            modified.append(key)
    if modified:
        return "modified: " + ", ".join(modified), compared
    return "stock", compared


def compare(config_values: Mapping[str, str]) -> str:
    """:func:`compare_verdict` reduced to its verdict for one value per key: ``"stock"`` or
    ``"modified: key, key"``."""
    return compare_verdict({key: (value,) for key, value in config_values.items()})[0]
