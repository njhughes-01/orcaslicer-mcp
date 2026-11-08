from __future__ import annotations
import asyncio
import contextlib
import json
import math
import os
import re
import datetime
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Iterable, Literal
from mcp.server.mcpserver import MCPServer, Image, Context
from pydantic import BeforeValidator, Field
from mcp.types import TextContent, ToolAnnotations
from .config import load_config
from .client import OrcaClient
from .errors import (ApiError, Validation, NotFound, Conflict, ConfigError, UiTimeout, NotReachable,
                     Unauthorized)
from .models import summarize_slice
from . import settings_schema
from . import placement
from .knowledge_index import load_knowledge, search_knowledge
from .physics_check import run_checks, CHECK_LAYERS
from .breakdown import build_breakdown
from .compare import compute_comparison
from .guard import allow_override_warnings
from .stdio_compat import run_stdio
from . import notes as _notes
from . import outcomes as _outcomes
import hashlib
import time
from . import plate_describe as _plate
from .printer import target as _ptarget
from .printer import wait as _pwait
from .printer import history as _phistory
from .printer import match as _pmatch
from .printer.errors import PrinterError
from .printer.snapshot import take_snapshot
from .printer.status import _dur as _duration_text
from .printer import cc2_canvas as _cc2_canvas
from .printer import cc2_control as _cc2_control
from .printer import cc2_fan as _cc2_fan
from .printer import cc2_history as _cc2_history
from .printer import cc2_info as _cc2_info
from .printer import cc2_peripherals as _cc2_peripherals
from .printer import cc2_pool as _cc2_pool
from .printer import cc2_snapshot as _cc2_snapshot
from .printer import cc2_start as _cc2_start
from .printer import cc2_state as _cc2_state
from .printer import cc2_temperature as _cc2_temperature
from .printer import cc2_transport as _cc2_transport
from .printer import cc2_upload as _cc2_upload

try:
    import importlib.metadata as _md
    _VERSION = _md.version("orcaslicer-mcp")
except Exception:  # source checkout without an installed dist
    _VERSION = "0.0.0"

mcp = MCPServer("orcaslicer", version=_VERSION)


def _client() -> OrcaClient:
    try:
        return OrcaClient(load_config())
    except (RuntimeError, ValueError) as e:
        raise ConfigError(str(e))


# The fork's terminal slice states: done, error, and idle (= cancelled).
# Anything else ("slicing", or transitional states like "starting") means
# keep polling — returning early hands the caller a stale result (F1).
_TERMINAL_SLICE_STATES = frozenset({"done", "error", "idle"})


async def _wait_for_slice(c, timeout: int) -> dict:
    """Poll slice_status until it reaches a terminal state (done/error/idle) or timeout. Returns the final status dict."""
    deadline = asyncio.get_running_loop().time() + timeout
    s = await c.slice_status()
    while s.get("state") not in _TERMINAL_SLICE_STATES and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1.0)
        s = await c.slice_status()
    return s


async def _start_slice(c) -> dict:
    """POST /slice with one retry: right after a slice completes, the fork's
    background process can transiently decline to restart (422 slice_not_started,
    surfaced by the F2 wedge detection). One short pause clears it."""
    try:
        return await c.slice()
    except Validation as e:
        if "slice_not_started" not in str(e):
            raise
        await asyncio.sleep(1.5)
        return await c.slice()


def _err(e: ApiError) -> dict:
    out = {"error": str(e)}
    if isinstance(e, Validation):
        out["errors"] = e.errors
        if "blocked_by_remote_api_policy" in (e.errors or {}).values():
            out["hint"] = ("OrcaSlicer itself refused these keys: Preferences > Remote API > "
                           "'Allow script, G-code and connection edits' is off. They stay a "
                           "change for the user to make in the OrcaSlicer GUI.")
    return out


@mcp.tool()
async def get_status() -> dict:
    """Snapshot of the current OrcaSlicer session: app and project info, the active
    print/filament/printer presets with which of their keys are modified (dirty), whether the
    last slice is still valid, and whether a slice is running. Read-only.

    Call it first to orient before slicing or editing, to see which settings drift from their
    preset, or to check slice_result_valid before trusting earlier stats."""
    try:
        async with _client() as c:
            return await c.get_status()
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_config(keys: list[str] | None = None) -> dict:
    """Read merged config values (optionally filtered to `keys`)."""
    try:
        async with _client() as c:
            return {"config": await c.get_config(keys)}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def set_config(
    changes: Annotated[dict, Field(description=(
        "Map of OrcaSlicer config key to new value, e.g. "
        "{'layer_height': 0.2, 'sparse_infill_density': '15%'}. Values must match each "
        "setting's type; percent settings take strings like '15%'. Discover valid keys "
        "with search_settings, find_config_keys, or describe_setting. Do NOT echo back a "
        "get_config value for a per-filament setting (hot_plate_temp, fan_min_speed, "
        "nozzle_temperature_initial_layer, ...): get_config reports those merged across "
        "every loaded filament ('70,100,70,70,35'), while this writes the one filament "
        "preset open in the Filament tab, so pass that preset's single value instead."))],
) -> dict:
    """Apply config changes to the active project as unsaved overrides, atomically: if any
    key is invalid the whole batch is rejected and nothing changes. Returns {applied, errors}.

    Overrides show as modified in get_status, are not written to any preset file, and revert
    if the preset is reselected; call save_preset to persist them. Each apply invalidates the
    last slice, so re-slice afterwards. It does not run the physics gate, so for temperature,
    speed, acceleration, or flow keys run check_profile_physics before trusting the result. To
    edit a stored preset rather than the live project, use edit_preset.

    On a multi-filament setup do filament edits before print edits: changing another slot's
    value means select_preset for that slot, which discards unsaved print overrides
    (docs/order-of-operations.md)."""
    try:
        async with _client() as c:
            return await c.put_config(changes)
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def slice() -> dict:
    """Start slicing the current plate in the background and return immediately, without waiting
    for the result. The reply is 'started' (a slice began), 'already_valid' (the plate is
    unchanged and the last result still holds), or a conflict if a slice is already running.

    Fire-and-forget: poll get_slice_status for progress and stats, or cancel_slice to stop it.
    Prefer slice_and_wait when you want the finished stats back in one call."""
    try:
        async with _client() as c:
            return await c.slice()
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_status() -> dict:
    """State of the current or most recent slice: state (slicing, done, error, or idle), stats
    (print time and filament use when done), and any warnings or errors. Read-only.

    Poll this after slice to follow progress and read the result; 'idle' means no slice has run
    or it was cancelled. For only the pass/fail warnings use get_slice_warnings; for a
    per-feature time and filament breakdown use get_slice_breakdown."""
    try:
        async with _client() as c:
            return summarize_slice(await c.slice_status())
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_warnings() -> dict:
    """Just the warnings/errors from the last (or current) slice, plus validity: the
    fast 'did anything go wrong' check and the way to confirm a fix cleared.

    NOTE: only as complete as the API exposes. On the current fork build this may report
    valid with an empty warnings list even when the GUI shows a plate-boundary toast -
    the fork must populate the plater warning list (tracked as the fork batch). Once it
    does, this reports the real warnings with no change here."""
    try:
        async with _client() as c:
            st = await c.slice_status()
            status = await c.get_status()
        s = summarize_slice(st)
        return {"state": s["state"], "valid": status.get("slice_result_valid"),
                "warnings": s["warnings"], "errors": st.get("errors", []),
                "message": s["message"]}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_breakdown() -> dict:
    """Per-feature breakdown of the last slice + a stateless predicted-vs-observed flow check.

    Returns per-role time/filament + speed/flow ranges, global time-weighted metric
    distributions, per-layer aggregates, and a prediction_check flagging where the profile's
    speed was silently throttled at the flow ceiling ('clamped'). Answers 'which feature is
    the time hog' directly instead of by trial slicing.

    Degrades to {"available": false, "reason": ...} on fork builds that don't emit the
    breakdown, or when there is no valid slice."""
    try:
        async with _client() as c:
            status = await c.slice_status()
            cfg = await c.get_config(None)
        return build_breakdown(status, cfg)
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def cancel_slice() -> dict:
    """Abort a running slice, or unwedge a stale 'slicing' state (e.g. after an
    object outside the bed). Safe when idle."""
    try:
        async with _client() as c:
            return await c.cancel_slice()
    except ApiError as e:
        return _m4_err(e, "the F2 fork fix")


@mcp.tool()
async def slice_and_wait(
    timeout: Annotated[int, Field(description=(
        "Maximum seconds to wait for the slice to finish before returning the last known "
        "state. Default 300; raise it for large or textured plates that slice slowly."))] = 300,
) -> dict:
    """Slice the current plate and block until it finishes, then return the final stats and
    warnings in one call. If the plate is already sliced and unchanged, it returns the existing
    result without re-slicing.

    This is the usual way to slice when you want the outcome immediately. For a non-blocking
    start, use slice then poll get_slice_status; to sweep one setting across values, use
    compare_settings."""
    try:
        async with _client() as c:
            started = await _start_slice(c)
            if started.get("already_valid"):
                return summarize_slice(await c.slice_status())
            return summarize_slice(await _wait_for_slice(c, timeout))
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def apply_and_slice(
    changes: Annotated[dict, Field(description=(
        "Map of OrcaSlicer config key to new value to apply before slicing, e.g. "
        "{'layer_height': 0.2}. Same format and validation as set_config; discover keys with "
        "search_settings or find_config_keys."))],
) -> dict:
    """Apply config overrides and then slice in one step, returning {applied, errors, result}
    with the resulting stats and warnings. The changes are atomic (any invalid key rejects the
    whole batch) and unsaved, exactly like set_config, so they revert if the preset is reselected.

    Use this to test the effect of a tweak in a single call. Use set_config then slice_and_wait
    to keep the steps separate, or compare_settings to try several values of one key."""
    try:
        async with _client() as c:
            applied = await c.put_config(changes)
            started = await _start_slice(c)
            if not started.get("already_valid"):
                await _wait_for_slice(c, 300)
            result = summarize_slice(await c.slice_status())
            return {"applied": applied.get("applied", []),
                    "errors": applied.get("errors", {}), "result": result}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def compare_settings(key: str, values: list, extra: dict | None = None) -> dict:
    """For each value of `key`, slice and collect stats/warnings; restore the original when done.

    Non-destructive: the original value of `key` is put back even on error.
    """
    try:
        async with _client() as c:
            snapshot_keys = [key] + list((extra or {}).keys())
            originals = await c.get_config(snapshot_keys)
            rows = []
            restore_error = None
            try:
                for v in values:
                    row = {"value": v, "stats": None, "warnings": [], "error": None}
                    try:
                        await c.put_config({key: v, **(extra or {})})
                        started = await _start_slice(c)
                        if not started.get("already_valid"):
                            await _wait_for_slice(c, 300)
                        s = summarize_slice(await c.slice_status())
                        row["stats"] = s["stats"]
                        row["warnings"] = s["warnings"]
                    except ApiError as e:
                        row["error"] = str(e)
                    rows.append(row)
            finally:
                if originals:
                    try:
                        await c.restore_config(originals)
                    except ApiError as e:
                        restore_error = str(e)  # preserve collected rows even if restore fails
            result = {"key": key, "rows": rows}
            if restore_error is not None:
                result["restore_error"] = restore_error
            return result
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def compare_slices(variants: list[dict], baseline: str | None = None,
                         detail: bool = False, timeout: int = 300) -> dict:
    """Slice the current plate under several named variants and compare the cost of each.

    Each variant is {"name": str, "changes": {setting: value}}; changes={} means the
    current config as-is (a natural baseline row). Applies each variant over the ORIGINAL
    config (resetting between variants, so they don't stack), slices it, then restores your
    config exactly as it was - nothing is left changed (slice validity is left false, as
    after any un-resliced edit).

    Returns a ready-to-relay `headline` and `table_markdown`, plus structured `variants`.
    All deltas and percentages are ALREADY computed and rounded against `baseline`
    (defaults to the changes={} variant, else the first) - relay them as given rather than
    recomputing. `recommended` names one pick; `recommended_is_dominant` says whether it
    beats every variant on every axis (time, filament, warnings) or is only the fastest
    warning-free option amid a genuine trade-off (`tradeoff` then names the frontier).

    Each variant is a full slice (minutes); capped at 8. Set detail=True only when a
    per-feature (wall/infill/support) split is wanted - it grows the response ~N x. With
    more than ~5 variants, lead with the recommendation and the extremes, not all rows.
    """
    if not isinstance(variants, list) or len(variants) < 2:
        return {"error": "need_at_least_two_variants"}
    if len(variants) > 8:
        return {"error": "too_many_variants", "max": 8, "given": len(variants)}
    names = [v.get("name") for v in variants]
    if any(not n for n in names):
        return {"error": "each_variant_needs_a_name"}
    if len(set(names)) != len(names):
        return {"error": "variant_names_must_be_unique"}
    if baseline is not None and baseline not in names:
        return {"error": "unknown_baseline", "baseline": baseline, "names": names}

    try:
        async with _client() as c:
            union = sorted({k for v in variants for k in (v.get("changes") or {})})
            snapshot = await c.get_config(union) if union else {}
            results: list[dict] = []
            restore_error = None
            try:
                for v in variants:
                    changes = v.get("changes") or {}
                    r = {"name": v["name"], "changes": changes, "time_s": None,
                         "filament_g": None, "warnings": [], "valid": False,
                         "error": None, "roles": None}
                    try:
                        if snapshot:
                            await c.restore_config(snapshot)  # reset to baseline so variants don't stack
                        if changes:
                            applied = await c.put_config(changes)
                            if applied.get("errors"):
                                r["error"] = "invalid_keys"
                                r["errors"] = applied["errors"]
                                results.append(r)
                                continue
                        started = await _start_slice(c)
                        if not started.get("already_valid"):
                            await _wait_for_slice(c, timeout)
                        st = await c.slice_status()
                        s = summarize_slice(st)
                        stats = s.get("stats") or {}
                        status = await c.get_status()
                        r["time_s"] = stats.get("estimated_time_seconds")
                        r["filament_g"] = stats.get("filament_used_g")
                        r["warnings"] = s.get("warnings") or []
                        r["valid"] = bool(status.get("slice_result_valid"))
                        if s.get("state") == "error" or r["time_s"] is None:
                            r["error"] = r["error"] or (s.get("message") or "slice_failed")
                        if detail:
                            r["roles"] = build_breakdown(st, await c.get_config(None)).get("roles")
                    except ApiError as e:
                        r["error"] = str(e)
                    results.append(r)
            finally:
                if snapshot:
                    try:
                        await c.restore_config(snapshot)
                    except ApiError as e:
                        restore_error = str(e)
    except ApiError as e:
        return _err(e)

    out = compute_comparison(results, baseline, detail)
    out["restored"] = restore_error is None
    if restore_error is not None:
        out["restore_error"] = restore_error
    return out


def _m4_err(e: ApiError, milestone: str) -> dict:
    # Only a route-level 404 means the build lacks the capability; a resource-level
    # 404 (unknown_object / unknown_preset / missing file) must keep its message (F7).
    if isinstance(e, NotFound) and e.route_missing:
        return {"error": f"not available on this OrcaSlicer build (needs {milestone})"}
    return _err(e)


def _m4a_err(e: ApiError) -> dict:
    return _m4_err(e, "M4a")


def _m4b_err(e: ApiError) -> dict:
    return _m4_err(e, "M4b")


def _m4c_err(e: ApiError) -> dict:
    return _m4_err(e, "M4c")


def _plates_err(e: ApiError) -> dict:
    return _m4_err(e, "fork v2.4.2-mcp.13 or later")


def _slots_err(e: ApiError) -> dict:
    out = _m4_err(e, "fork v2.4.2-mcp.14 or later")
    body = getattr(e, "body", None) or {}
    extra = ""
    entry = body.get("entry")
    if entry:
        extra += " for entry " + json.dumps(entry)
    if body.get("reason"):
        extra += " (" + str(body["reason"]) + ")"
    if body.get("message"):
        extra += ": " + str(body["message"])
    if extra and "error" in out:
        out["error"] += extra
    return out



@mcp.tool()
async def list_objects() -> dict:
    """List objects in the project: id (stable), name, size_mm, transform (offset/rotation/scale),
    how many copies the object holds (`instances`) and, on fork v2.4.2-mcp.13 and later, the 0-based
    plate each one sits on (-1 when it is off every plate). That `plate` says where the object's FIRST
    instance sits: for an object whose copies are spread over more than one plate it says nothing about
    the other copies, so what a plate actually prints is in `list_plates`, whose per-plate `objects`
    carry each copy as {id, instance}."""
    try:
        async with _client() as c:
            return await c.get_objects()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def set_object_config(object_id: int, changes: dict) -> dict:
    """Set per-object config overrides on an object by id, e.g. {"wall_loops": 4, "sparse_infill_density": "30%"}. Atomic (nothing applied if any key is invalid)."""
    try:
        async with _client() as c:
            return await c.set_object_config(object_id, changes)
    except ApiError as e:
        return _m4c_err(e)


@mcp.tool()
async def duplicate_object(object_id: int) -> dict:
    """Duplicate an object on the plate by id (adds a copy, offset from the original)."""
    try:
        async with _client() as c:
            return await c.duplicate_object(object_id)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def delete_object(
    object_id: Annotated[int, Field(description=(
        "Integer id of the object to remove, taken from the 'id' field of list_objects "
        "(not the array index or the file name)."))],
) -> dict:
    """Remove one object from the current plate by id. This is permanent within the session
    and cannot be undone through the API; the other objects keep their ids.

    Call list_objects first to get the id. Deleting leaves the last slice invalid, so re-slice
    afterwards. To drop just one copy made with duplicate_object, pass that copy's id."""
    try:
        async with _client() as c:
            return await c.delete_object(object_id)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def transform_object(object_id: int, translate: list[float] | None = None,
                           rotate: list[float] | None = None,
                           scale: list[float] | None = None) -> dict:
    """Move/rotate/scale an object by id. translate=[dx,dy,dz] mm (relative), rotate=[rx,ry,rz] degrees (relative), scale=[sx,sy,sz] absolute factor. Provide at least one.

    On fork v2.4.2-mcp.13 and later the object then belongs to whichever plate it sits on,
    as after a drag in the window; list_objects shows the plate. To send an object to
    another plate, move_object_to_plate is simpler than working out coordinates."""
    try:
        async with _client() as c:
            return await c.transform_object(object_id, translate, rotate, scale)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def arrange_plate() -> dict:
    """Auto-arrange all objects on the plate (async job; poll get_job_status until idle).

    With several plates this is OrcaSlicer's "Arrange all": it re-packs the objects of every
    unlocked plate together and deletes plates left empty at the end, which undoes a
    one-part-per-plate layout. To shift one object, use transform_object instead."""
    try:
        async with _client() as c:
            return await c.arrange()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def auto_orient() -> dict:
    """Auto-orient all objects for printing (async job; poll get_job_status until idle)."""
    try:
        async with _client() as c:
            return await c.orient()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def list_plates() -> dict:
    """List the plates in the project: each one's 0-based index, name, whether it is the
    current plate (the one slice, get_gcode, render_plate and get_job_status act on), lock
    state, build volume and the copies it holds, each as {id, instance}, that is the object's id and
    which copy of it, the same pair move_object_to_plate takes. That per-plate list, not an object's own
    `plate` (which names only its first instance), is what says what a plate prints. Also says how many
    plates the build allows (max) and whether one can be added or deleted right now. Read-only.

    Plate numbers shown in the OrcaSlicer window are 1-based; every tool here takes the 0-based index
    from this list. Needs fork v2.4.2-mcp.13 or later."""
    try:
        async with _client() as c:
            return await c.get_plates()
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def get_printer_slots() -> dict:
    """Read the filament slots (trays) of the connected Elegoo Centauri Carbon 2 with a CANVAS
    unit: which tray is active, and for each tray its label (A1, A2, ...), status, material,
    brand and colour. Read-only. Works only for that printer; needs fork v2.4.2-mcp.14 or later.
    The call asks the printer over the network and can take up to about 18 seconds, and
    OrcaSlicer's API serves one request at a time while it runs, so other tools wait.

    Per tray: status is loaded, preloaded (filament waiting at the feeder, not yet pulled in),
    disconnected or error. ready is true when the tray can be printed from now.
    unknown_filament is true when the tray reports no filament type or name. Use canvas_id and tray_id from here in
    set_filament_slot_mapping.

    When the printer is not supported or not reachable the reply is supported false with a
    reason (not_elegoo_cc2, no_canvas, unreachable, too_many_clients, registration_refused,
    timeout, printer_error, firmware_too_old) and a message. Relay the message to the user."""
    try:
        async with _client() as c:
            return await c.get_printer_slots()
    except ApiError as e:
        return _slots_err(e)


@mcp.tool()
async def get_filament_slot_mapping() -> dict:
    """Show which CANVAS tray each filament would use if the user clicked Send now. The
    filaments listed are the ones on the plate the next Send would use. Read-only; needs fork
    v2.4.2-mcp.14 or later. It reads the printer when no slot list is cached, so it can take up
    to about 18 seconds, and OrcaSlicer's API serves one request at a time meanwhile, so other
    tools wait.

    The reply names the plate (0-based, the current plate) and supported (true, or false with
    a reason). Each filament has its 0-based index, type, name, colour, the tray it maps to (or null), the
    source of that choice (saved, matched, user, api or none) and any warnings. valid is true
    when that Send would go through; if not, problems says why (an unmapped filament, a tray that
    is not ready). Call it after slicing or changing filaments, before telling the user the
    plate is ready to send. Nothing is sent to the printer. A supported false reply carries a
    reason to relay, as get_printer_slots does."""
    try:
        async with _client() as c:
            return await c.get_slot_mapping()
    except ApiError as e:
        return _slots_err(e)


@mcp.tool()
async def set_filament_slot_mapping(
    mapping: Annotated[list[dict], Field(description=(
        "One entry per filament of the plate the next Send uses (the indexes "
        "get_filament_slot_mapping lists): {filament: 0-based filament index, canvas_id, "
        "tray_id} from get_printer_slots."))],
) -> dict:
    """Choose which CANVAS tray each filament of the plate the next Send would use prints from.
    Returns the same reply as get_filament_slot_mapping, so check valid and problems. Needs
    fork v2.4.2-mcp.14 or later and an Elegoo Centauri Carbon 2 with a CANVAS. It reads the
    printer when no slot list is cached, so it can take up to about 18 seconds, and
    OrcaSlicer's API serves one request at a time meanwhile, so other tools wait.

    The list REPLACES the whole mapping set through the API: a filament left out gets no tray
    from the API, and an empty list clears the API choices so Send falls back to the saved
    choices and automatic matching. Include every filament you want pinned.

    This starts nothing. OrcaSlicer's own Send dialog opens pre-filled with this mapping and
    the user still clicks Send. Each tray can serve only one filament (422 duplicate_tray), the
    tray must be ready (422 tray_not_ready), and the indexes must exist (422 unknown_filament_index, which
    also covers a project filament that is not on the plate; unknown_tray, slots_unavailable when the printer could not be read; the reply then
    says why). Malformed lists get 400: each filament at most once, and integers for
    filament, canvas_id and tray_id. Use get_printer_slots first to pick trays whose material and colour fit."""
    try:
        async with _client() as c:
            return await c.put_slot_mapping(mapping)
    except ApiError as e:
        return _slots_err(e)


@mcp.tool()
async def add_plate(
    select: Annotated[bool, Field(description=(
        "Make the new plate the current one, as the toolbar button does. Pass false to "
        "keep working on the plate that is current now."))] = True,
) -> dict:
    """Add an empty plate to the project, the same way the toolbar's "Add plate" button
    does. Returns the new plate's 0-based index for move_object_to_plate and select_plate.

    Use it when parts should print separately: load them, add a plate, move the second
    part across with move_object_to_plate, then select_plate and slice each one. A plate
    cannot be added while a slice is running (409) or at the build's plate limit (422)."""
    try:
        async with _client() as c:
            return await c.create_plate(select)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def select_plate(
    index: Annotated[int, Field(description=(
        "0-based plate index from list_plates (the window shows plates 1-based)."))],
) -> dict:
    """Make a plate the current one, exactly like clicking it: slice_and_wait, get_gcode,
    render_plate, get_slice_status and get_job_status then act on that plate.

    get_slice_status then reports that plate's own result: its stats if it is still sliced,
    otherwise idle with "plate N is not sliced". Switching plates leaves unsaved config
    overrides alone; it changes only which plate is sliced. Selecting the plate that is
    already current is a no-op."""
    try:
        async with _client() as c:
            return await c.select_plate(index)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def delete_plate(
    index: Annotated[int, Field(description=(
        "0-based plate index from list_plates. Later plates shift down one index."))],
) -> dict:
    """Delete a plate. The objects it held are NOT deleted: OrcaSlicer parks them beside
    the bed, as the toolbar's delete does, and the reply says how many were moved off-plate
    (objects_moved_off_plate). Use move_object_to_plate to put them somewhere printable.
    OrcaSlicer parks them where the next plate appears, so an add_plate straight after a
    delete picks them back up.

    The only plate cannot be deleted (422 last_plate). Call list_plates first for the index;
    this cannot be undone through the API."""
    try:
        async with _client() as c:
            return await c.delete_plate(index)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def move_object_to_plate(
    object_id: Annotated[int, Field(description=(
        "Integer id of the object, from the 'id' field of list_objects."))],
    plate: Annotated[int, Field(description=(
        "0-based index of the destination plate, from list_plates or add_plate."))],
    instance: Annotated[int, Field(description=(
        "Which copy to move when the object has several (duplicate_object); 0 is the "
        "original."))] = 0,
) -> dict:
    """Move an object onto another plate. Coming from a plate it keeps its position relative
    to that plate (front-left of plate 1 lands front-left of plate 2); coming from off-plate
    (parked beside the bed) it lands at the destination's centre. If it then overlaps a
    part, shift it with transform_object; arrange_plate would re-pack every plate together.

    list_objects reports each object's plate (-1 when off every plate). Moving leaves both
    plates' slices invalid, so re-slice. An object already on that plate is left where it
    is (moved: false); a locked plate at either end refuses the move (plate_locked)."""
    try:
        async with _client() as c:
            return await c.set_object_plate(object_id, plate, instance)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def get_job_status() -> dict:
    """Whether the plate's background job worker is idle or still running. Read-only.

    arrange_plate and auto_orient start async jobs; poll this until it reports idle before you
    read object positions or slice, so you act on the settled layout rather than a mid-move
    state."""
    try:
        async with _client() as c:
            return await c.job_status()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def watch_events(seconds: int = 10) -> dict:
    """Collect live events (slice.*/config.changed/project.opened) over a bounded window."""
    try:
        async with _client() as c:
            return {"events": await c.collect_events(seconds=seconds)}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def find_config_keys(substring: str) -> dict:
    """Find config keys containing `substring` (helps discover among the ~600 keys)."""
    try:
        async with _client() as c:
            cfg = await c.get_config(None)
            return {"keys": sorted(k for k in cfg if substring in k)}
    except ApiError as e:
        return _err(e)


_PLATE_CFG_KEYS = ["printable_area", "bed_exclude_area", "skirt_loops", "skirt_distance",
                   "skirt_height", "brim_type", "brim_width", "brim_object_gap",
                   "extruder_clearance_radius", "draft_shield"]


@mcp.tool()
async def diagnose_plate() -> dict:
    """One-call plate diagnosis: app/slice status, objects on the plate, bed + active
    skirt/brim/clearance settings, and the last slice's warnings, so you don't have to
    chain status->objects->config. Start here for 'why won't this slice / fit'.

    Slice warnings are only as complete as the fork exposes today (see get_slice_warnings).
    For a 'does it fit the bed' estimate, pair with check_placement."""
    try:
        async with _client() as c:
            status = await c.get_status()
            try:
                objs = await c.get_objects()
            except NotFound:
                objs = {"count": 0, "objects": [], "note": "objects API needs M4b build"}
            cfg = await c.get_config(_PLATE_CFG_KEYS)
            sl = summarize_slice(await c.slice_status())
        return {"status": status, "objects": objs, "adhesion_bed": cfg,
                "slice": {"state": sl["state"], "warnings": sl["warnings"],
                          "valid": status.get("slice_result_valid")}}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def check_placement() -> dict:
    """Estimate whether every object (plus its skirt/brim ring) fits inside the printable
    area. Returns per-object fit, expanded first-layer bbox, per-edge clearance (mm), and
    overflow.

    APPROXIMATE: uses the object footprint from size+offset, not the sliced toolpath (skirt
    arcs, half-line-width, travel/wipe excluded); single-instance objects only. At ~mm
    margins the true verdict needs get_slice_warnings - this is a fast first-pass."""
    try:
        async with _client() as c:
            objs = await c.get_objects()
            cfg = await c.get_config(placement.CFG_KEYS)
        return placement.check_placement(objs, cfg)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def consult(query: str) -> dict:
    """Retrieve curated slicing knowledge + saved context notes for a topic,
    symptom, or intent. ALWAYS call before deriving or changing settings for
    a user goal. Composes principles per situation, never returns preset
    bundles. Falls back to find_config_keys/web search if empty.

    When recommending, present 2-3 concrete options quantified with
    predicted print time and filament mass from real slice results (slice +
    status tools) - never adjectives alone."""
    chunks = [{"file": c.relpath, "title": c.title, "content": c.body}
              for c in search_knowledge(query)]
    return {"chunks": chunks, "notes": _notes.search_notes(query)}


@mcp.tool()
async def check_profile_physics(changes: dict | None = None) -> dict:
    """Deterministic pre-save gate: fetches the live config, overlays optional
    proposed `changes`, and runs flow/temperature/geometry/cooling math.
    RUN THIS BEFORE save_preset. verdict=blocked means DO NOT SAVE."""
    try:
        async with _client() as c:
            cfg = await c.get_config(None)
    except ApiError as e:
        return _err(e)
    if changes:
        cfg = dict(cfg) | {k: str(v) for k, v in changes.items()}
    results = run_checks(cfg)
    grouped = {"pass": [], "warn": [], "fail": []}
    for r in results:
        grouped[r.status].append({"name": r.name, "detail": r.detail})
    grouped["verdict"] = "blocked" if grouped["fail"] else ("warnings" if grouped["warn"] else "ok")
    return grouped


@mcp.tool()
async def remember(note: str, scope: str) -> dict:
    """Persist a context fact for future sessions. scope: 'machine:<printer>/<filament>',
    'user', or 'project:<name>'. Local plain files; user-readable and deletable."""
    return {"saved": str(_notes.append_note(note, scope))}


@mcp.tool()
def describe_setting(
    key: Annotated[str, Field(description=(
        "Exact OrcaSlicer config key, e.g. 'layer_height' or 'sparse_infill_density'. Find keys "
        "with search_settings or find_config_keys. Unknown keys return an error."))],
) -> dict:
    """Authoritative definition of one OrcaSlicer setting: label, tooltip, type, unit, valid
    range, enum values, and default. Read-only and offline, so it works even when OrcaSlicer is
    not running.

    Use it to learn a setting's exact type and allowed values before writing it with set_config
    or edit_preset. To find candidate keys by keyword first, use search_settings."""
    rec = settings_schema.describe(key)
    if rec is None:
        return {"error": "unknown_setting", "key": key}
    return rec


@mcp.tool()
def search_settings(query: str, limit: int = 25) -> dict:
    """Search settings by keyword across key/label/tooltip; returns compact matches (key, label, category, short tooltip), ranked key/label first. Offline."""
    return {"results": settings_schema.search(query, limit)}


# --- MCP resources: the offline reference data, addressable as context ---
# All of these read the packaged settings schema / knowledge base, so they
# work even when OrcaSlicer is not running.


@mcp.resource("orca://knowledge", title="Slicing knowledge index",
              description="Index of the curated slicing knowledge base shipped with this server; each entry lists its orca://knowledge/{slug} URI.")
def knowledge_index_resource() -> str:
    lines = ["# Curated slicing knowledge", ""]
    for c in load_knowledge():
        slug = c.relpath[:-3].replace("/", "__") if c.relpath.endswith(".md") else c.relpath.replace("/", "__")
        topics = f" (topics: {', '.join(c.topics)})" if c.topics else ""
        lines.append(f"- orca://knowledge/{slug} — {c.title}{topics}")
    return "\n".join(lines)


@mcp.resource("orca://knowledge/{slug}", title="Slicing knowledge chunk",
              description="One knowledge chunk by slug (relpath with '/' as '__', no .md); see orca://knowledge for the index.")
def knowledge_chunk_resource(slug: str) -> str:
    for c in load_knowledge():
        rel = c.relpath[:-3] if c.relpath.endswith(".md") else c.relpath
        if rel.replace("/", "__") == slug:
            return c.body
    raise ValueError(f"unknown knowledge slug: {slug}")


@mcp.resource("orca://setting/{key}", title="Setting definition",
              description="Authoritative definition of one OrcaSlicer setting (label, tooltip, type, unit, range, enum, default), from OrcaSlicer's own source.")
def setting_resource(key: str) -> str:
    import json as _json
    rec = settings_schema.describe(key)
    if rec is None:
        raise ValueError(f"unknown setting: {key}")
    return _json.dumps(rec, indent=2)


# --- MCP prompts: guided workflows that encode this server's intended use ---


@mcp.prompt(name="slice-a-model", title="Slice a model, safely",
            description="Guided load -> check -> slice -> read-back workflow for one model file.")
def prompt_slice_a_model(model_path: str) -> str:
    return (
        f"Slice {model_path} in OrcaSlicer via the orcaslicer tools, step by step:\n"
        "1. get_status — confirm OrcaSlicer is reachable and note the selected presets.\n"
        "2. list_objects — see what is already on the plate before adding anything.\n"
        f"3. load_model with path {model_path}, then check_placement; fix placement with "
        "auto_orient / arrange_plate / transform_object if it reports problems.\n"
        "4. recall_prints — if this model (or one like it) was printed before, say how it went "
        "(result, any verdict such as warped or stringing, and the settings used) BEFORE proposing changes.\n"
        "5. consult with the model's material and purpose before touching settings.\n"
        "6. slice_and_wait, then get_slice_warnings and get_slice_breakdown; report "
        "print time, filament mass, and any warnings. Call describe_plate and relay its summary "
        "before proposing any orientation or support change; use render_plate to show the result.\n"
        "7. If the user wants to print it, save_gcode (this records the slice so the real outcome "
        "can be learned from later) and hand the returned path to the printer tools.\n"
        "Do not start a print or change temperatures without asking me first."
    )


@mcp.prompt(name="optimize-print-time", title="Optimize print time",
            description="Data-driven print-time reduction from a real slice breakdown, never adjectives alone.")
def prompt_optimize_print_time(constraints: str = "") -> str:
    extra = f"\nMy constraints: {constraints}" if constraints else ""
    return (
        "Reduce the print time of the current plate, driven by data:\n"
        "1. recall_prints — check whether faster settings for this model already failed or "
        "warped before; do not re-propose a known failure.\n"
        "2. slice_and_wait (if no valid slice), then get_slice_breakdown — identify which "
        "feature roles actually dominate time.\n"
        "3. consult with 'print time optimization' plus the dominant roles; only propose "
        "levers the knowledge base or breakdown supports.\n"
        "4. Apply candidate changes with set_config, then check_profile_physics — drop "
        "anything blocked.\n"
        "5. Re-slice and quantify: present 2-3 options as minutes and grams versus the "
        "baseline, with the quality trade-off of each. Do not save presets unless I say so."
        + extra
    )


@mcp.prompt(name="edit-preset-safely", title="Edit a preset, safely",
            description="Preset change with the physics gate: consult -> set_config -> check_profile_physics -> save_preset.")
def prompt_edit_preset_safely(change: str) -> str:
    return (
        f"I want this preset change: {change}\n"
        "Follow the safe path — never edit_preset directly for physics-relevant keys:\n"
        "1. consult and describe_setting for every key involved; confirm units and ranges.\n"
        "2. Apply with set_config on the live config first.\n"
        "3. check_profile_physics — if verdict is blocked, stop and show me why; if "
        "warnings, explain them before proceeding.\n"
        "4. Only then save_preset, and confirm with get_status that nothing is left dirty."
    )


@mcp.tool()
async def load_model(path: str) -> dict:
    """Load a model file (path on the OrcaSlicer host) onto the current plate.
    Accepts .stl/.obj/.3mf, plus .step/.stp on fork v2.3.2-mcp.3+. Large STEP files
    can take a minute to tessellate; the call waits."""
    try:
        async with _client() as c:
            return await c.load_model(path)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def select_preset(
    type: Annotated[str, Field(description=(
        "Which preset group to switch: 'print' (process/quality), 'filament' (material), "
        "or 'printer' (machine)."))],
    name: Annotated[str, Field(description=(
        "Exact name of an existing preset in that group, as returned by list_presets "
        "(e.g. '0.20mm Standard'). Unknown names are rejected."))],
) -> dict:
    """Make the named preset the active one for its group (print, filament, or printer).

    Discards unsaved overrides in EVERY preset group, not just the one being switched: a
    filament switch also drops unsaved print and printer edits, including ones made by hand
    in the OrcaSlicer window. That is deliberate (OrcaSlicer consults the other groups when
    switching, and any unsaved one would open a modal dialog nothing can dismiss remotely),
    so call save_preset first if any unsaved edit should survive. Newer slicer builds report
    which groups were affected as 'discarded_changes' in the reply.

    This also makes it the canonical way to reset dirty config. It leaves the last slice
    invalid, so re-slice afterwards. Use list_presets for valid names. Because of this, make
    filament selections and edits before print-group edits (docs/order-of-operations.md)."""
    try:
        async with _client() as c:
            return await c.select_preset(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def save_preset(type: str, name: str, detach: bool = False) -> dict:
    """Save the currently edited settings as a named user preset (create or update,
    visible in the GUI immediately). type = print|filament|printer. detach=True saves
    it standalone instead of inheriting the current base preset; use it when creating a
    filament preset for a DIFFERENT material than the one selected, otherwise the new
    preset inherits the base's filament_type and temperatures.

    Run check_profile_physics first; do not save when verdict=blocked."""
    try:
        async with _client() as c:
            return await c.save_preset(type, name, detach)
    except ApiError as e:
        return _m4a_err(e)


_PRESET_TYPES = ("print", "filament", "printer")


def _filter_presets(presets: dict, ptype: str | None, include_system: bool) -> dict:
    """F12 filter: unless include_system, keep only user presets plus whatever is
    currently selected (a selected system preset is the active config, so it stays
    visible). Reports how many system presets were hidden."""
    cats = [ptype] if ptype else list(_PRESET_TYPES)
    out: dict = {}
    hidden = 0
    for cat in cats:
        items = presets.get(cat, []) or []
        if include_system:
            kept = items
        else:
            kept = [p for p in items if not p.get("system") or p.get("selected")]
        hidden += len(items) - len(kept)
        out[cat] = kept
    out["hidden_system"] = hidden
    return out


@mcp.tool()
async def list_presets(type: str | None = None, include_system: bool = False) -> dict:
    """List print/filament/printer presets with system/selected/visible flags.

    F12: by default returns only USER presets plus whatever is currently SELECTED -
    the built-in system presets are ~400 entries of noise. Pass include_system=True
    for the full list, and/or type='print'|'filament'|'printer' to restrict to one
    category. `hidden_system` reports how many system presets were filtered out."""
    if type is not None and type not in _PRESET_TYPES:
        return {"error": "invalid_type", "type": type, "valid": list(_PRESET_TYPES)}
    try:
        async with _client() as c:
            presets = await c.get_presets()
    except ApiError as e:
        return _m4a_err(e)
    return _filter_presets(presets, type, include_system)


@mcp.tool()
async def set_layer_height(object_id: int, mode: str, quality: float = 0.5) -> dict:
    """Variable layer height for one object. mode='adaptive' (quality 0..1, higher = finer
    detail) generates an adaptive profile; mode='reset' (aliases: 'default', 'none')
    restores uniform layers."""
    # F6: the fork only understands adaptive|reset; accept the spellings that
    # were documented for the clear path instead of bouncing them as unknown_mode.
    if mode in ("default", "none"):
        mode = "reset"
    try:
        async with _client() as c:
            return await c.set_layer_height(object_id, mode, quality)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def set_height_range(
    object_id: Annotated[int, Field(description="Integer id of the target object, from list_objects.")],
    min_z: Annotated[float | None, Field(description="Lower Z bound of the band in mm (object-relative). Required unless clear=True.")] = None,
    max_z: Annotated[float | None, Field(description="Upper Z bound of the band in mm. Required unless clear=True.")] = None,
    layer_height: Annotated[float | None, Field(description="Layer height in mm to use within the band, e.g. 0.1. Required unless clear=True.")] = None,
    clear: Annotated[bool, Field(description="If True, remove all height-range overrides on the object and ignore the z and layer_height args.")] = False,
) -> dict:
    """Override the layer height over a Z band of one object (e.g. 0 to 5 mm printed at 0.1 mm
    for finer detail near the base). Passing the same min_z and max_z again updates that band's
    height; clear=True removes every band on the object.

    Bands are per-object and invalidate the last slice, so re-slice afterwards. For a single
    height across the whole object use set_layer_height instead. Get the id from list_objects."""
    try:
        async with _client() as c:
            return await c.set_height_range(object_id, min_z, max_z, layer_height, clear)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def get_preset_config(type: str, name: str) -> dict:
    """Read the full settings of a named preset without selecting it.
    type = print|filament|printer."""
    try:
        async with _client() as c:
            return await c.get_preset_config(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def delete_preset(type: str, name: str) -> dict:
    """Delete a USER preset (system presets and the currently-selected one are refused).
    type = print|filament|printer."""
    try:
        async with _client() as c:
            return await c.delete_preset(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def edit_preset(type: str, name: str, changes: dict) -> dict:
    """Edit a named preset's settings and persist them: selects it, applies the
    changes atomically, saves under the same name. Runs the check_profile_physics
    gate first (F15) and refuses with error=physics_blocked if the changes would
    INTRODUCE a failing physics check (pre-existing failures do not block
    unrelated edits). For type='filament', checks that also depend on the currently
    selected PRINT preset (flow_ceiling, temp_vs_flow) do not block; they come back
    as cross_layer_warnings, because a filament preset pairs with many print presets."""
    try:
        async with _client() as c:
            await c.guard_preset_edit(type, name, changes)  # refuse before selecting anything
            await c.select_preset(type, name)
            cfg = await c.get_config(None)
            overlay = dict(cfg) | {k: str(v) for k, v in changes.items()}
            fails_before = {r.name for r in run_checks(cfg) if r.status == "fail"}
            fails_after = [r for r in run_checks(overlay) if r.status == "fail"]
            new_fails = [r for r in fails_after if r.name not in fails_before]
            # A filament preset pairs with many print presets, so a check that mixes
            # its inputs with the currently selected print preset's speeds cannot
            # fairly block the filament edit; it is reported instead. (Print-preset
            # edits are still blocked by those checks: they are tuned for the
            # filament selected at the time.)
            blocking, cross_layer = [], []
            for r in new_fails:
                layers = CHECK_LAYERS.get(r.name, frozenset({"print", "filament", "printer"}))
                if type == "filament" and not layers <= {"filament", "printer"}:
                    cross_layer.append(r)
                else:
                    blocking.append(r)
            if blocking:
                return {"error": "physics_blocked", "preset": name,
                        "fails": [{"name": r.name, "detail": r.detail} for r in blocking],
                        "hint": "these changes introduce a physics failure; adjust them or "
                                "inspect with check_profile_physics(changes)"}
            applied = await c.put_config(changes)
            if applied.get("errors"):
                return {"error": "invalid_keys", "errors": applied["errors"]}
            saved = await c.save_preset(type, name)
            out = {"preset": name, "applied": applied.get("applied", []), "saved": saved}
            if cross_layer:
                out["cross_layer_warnings"] = [
                    {"name": r.name, "detail": r.detail,
                     "hint": "judged against the currently selected print preset's speeds; this "
                             "filament preset was saved anyway. Pair it with a slower print preset, "
                             "or re-run check_profile_physics with the print preset you intend to use."}
                    for r in cross_layer]
            return out
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def rename_preset(
    type: Annotated[str, Field(description="Preset group: 'print', 'filament', or 'printer'.")],
    old_name: Annotated[str, Field(description="Current name of the user preset to rename, as shown by list_presets.")],
    new_name: Annotated[str, Field(description="New name for the preset. Should not collide with an existing preset of the same type.")],
) -> dict:
    """Rename a user preset by copying it to new_name, selecting the copy, and deleting the
    original. Only user presets can be renamed; system presets are read-only.

    Because it selects the renamed preset, this leaves it active and discards unsaved config
    overrides, the same as select_preset, and leaves the last slice invalid, so re-slice
    afterwards."""
    try:
        async with _client() as c:
            await c.select_preset(type, old_name)
            await c.save_preset(type, new_name)
            await c.select_preset(type, new_name)
            deleted = await c.delete_preset(type, old_name)
            return {"renamed": old_name, "to": new_name, "deleted": deleted}
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def get_gcode() -> dict:
    """Retrieve the last successful slice's G-code as text."""
    try:
        async with _client() as c:
            data = await c.get_gcode()
            return {"bytes": len(data), "gcode": data.decode("utf-8", errors="replace")}
    except Conflict:
        return {"error": "not_sliced"}
    except ApiError as e:
        return _m4a_err(e)


_DESCRIBE_CACHE: dict[str, dict] = {}   # single entry in practice: the current slice


def _gcode_cache_key(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(data[:65536])
    h.update(data[-65536:])
    return f"{len(data)}:{h.hexdigest()}"


@mcp.tool()
async def describe_plate() -> dict:
    """Machine-readable plate facts from the last slice's G-code, per object, so you can answer
    orientation and placement questions instead of guessing from Euler angles or a picture:
    how the part stands (flat / tilted / on an edge or corner, from first-layer contact versus its
    widest layer), the first-layer footprint as islands, where overhang extrusions concentrate by
    10 mm height band, where support stands and where its interface touches the part, and which
    side the outer-wall seams sit on (checked against seam_position). Each object gets a
    server-written summary sentence; relay it rather than recomputing. Read-only. Needs a valid
    slice; returns {"error": "not_sliced"} otherwise. Copies of one object are aggregated (Orca
    labels every copy 0); footprint islands still show per-copy contact. The parsed result is
    cached per slice (the G-code itself is still downloaded each call to detect a new slice).
    All bboxes are [x0, y0, x1, y1] in plate millimetres."""
    try:
        async with _client() as c:
            data = await c.get_gcode()
            try:
                objs = (await c.get_objects()).get("objects", [])
            except ApiError:
                objs = []
    except Conflict:
        return {"error": "not_sliced"}
    except ApiError as e:
        return _m4a_err(e)
    key = _gcode_cache_key(data)
    hit = _DESCRIBE_CACHE.get(key)
    if hit is not None:
        return {**hit, "cached": True}
    text = data.decode("utf-8", errors="replace")
    try:
        t0 = time.perf_counter()
        parsed = await asyncio.to_thread(_plate.parse_gcode, text)
        out = _plate.describe(parsed, objs)
        out["parse_seconds"] = round(time.perf_counter() - t0, 2)
    except Exception as e:
        return {"error": "parse_failed", "detail": str(e)[:200]}
    _DESCRIBE_CACHE.clear()
    _DESCRIBE_CACHE[key] = out
    return {**out, "cached": False}


def _safe_gcode_name(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    base = re.sub(r"\.\.+", "_", base).strip("._")
    if not base or base == "gcode":
        base = f"print_{datetime.datetime.now():%Y%m%d-%H%M%S}"
    return base if base.lower().endswith(".gcode") else base + ".gcode"


def _unique_gcode_path(out_dir: Path, fname: str) -> tuple[Path, str]:
    """Never overwrite an existing file: append -2, -3, ... (then a uuid4 suffix as a last resort)."""
    path = out_dir / fname
    if not path.exists():
        return path, fname
    stem, suffix = fname[: -len(".gcode")], ".gcode"
    for n in range(2, 1000):
        candidate = f"{stem}-{n}{suffix}"
        path = out_dir / candidate
        if not path.exists():
            return path, candidate
    candidate = f"{stem}-{uuid.uuid4().hex[:6]}{suffix}"
    return out_dir / candidate, candidate


def _gcode_base_dir() -> Path:
    """Where save_gcode writes the G-code file: PRINT_OUTCOMES_DIR if set (whitespace counts as unset),
    else the shared print-outcomes folder if it already exists (klipper-mcp's start_print finds files
    there), else ~/.orcaslicer-mcp (the folder `remember` already owns). The outcome store itself lives
    at outcomes.store_dir(), which defaults to ~/.orcaslicer-mcp/outcomes."""
    if os.environ.get("PRINT_OUTCOMES_DIR", "").strip() or _outcomes.LEGACY_DIR.exists():
        return _outcomes.store_dir()
    return Path.home() / ".orcaslicer-mcp"


# --- Which plate the saved slice belongs to -------------------------------------------------------
# get_gcode, slice_status and the settings snapshot answer for the CURRENT plate, while /api/v1/objects
# lists every object in the PROJECT. Membership therefore does not come from the object list: an
# object's own `plate` (fork v2.4.2-mcp.13) names only where that object's FIRST instance sits, so an
# object with three copies laid out over two plates reports one plate for all three, and it says -1 for
# an object a delete_plate left parked beside the bed. What a plate actually prints is in the /plates
# reply instead, where each plate carries its own `objects`: [{"id": ..., "instance": ...}, ...] - the
# object id and which copy of it, exactly the pair move_object_to_plate takes as arguments. Recording a
# different plate's part, and the settings that produced it, against one plate's G-code is the one thing
# the outcome store exists to get wrong, so membership is read from the plate's own list of copies, and
# wherever that cannot be established the row keeps only what is true of it.

_ATTR_PLATE = "plate"            # the sliced plate named the copies it prints, from its own list
_ATTR_SINGLE = "single-plate"    # one-plate project: every copy of every object sits on that plate
_ATTR_PROJECT = "project"        # pre-v2.4.2-mcp.13 fork: no plate route, so the project is all we see
_ATTR_UNKNOWN = "unknown"        # anything else: attribute nothing, and say which case it was

_READ = "read"            # the plate route answered something a plate can be read out of
_LEGACY = "legacy"        # the route itself is absent: a fork before v2.4.2-mcp.13
_UNCERTAIN = "uncertain"  # it answered, but not with anything a plate can be read out of


class _Plates:
    """What one GET /api/v1/plates tells us, in a shape that cannot be confused between the three.

    `_READ`      `current`, `count`, `names` (index -> its name) and `by_plate` (index -> the copies
                 that plate reports for itself, or None when that plate's entry has no `objects` key at
                 all). A `_READ` reply can still be silent about a plate's copies, and silence is a fact
                 about the reply, not a fault in it: the caller still reports the plate it was given and
                 attributes nothing to it.
    `_LEGACY`    the endpoint itself is absent, which a clean 404 of the route is the only evidence of.
                 This is the one state that makes "the project is the plate" true: such a build has one
                 plate, lists no objects against any of them, and shows nothing else.
    `_UNCERTAIN` it answered, but not in a way to build on - it did not answer in time, it could not be
                 reached, it refused (401/422), it answered 404 for a resource rather than for the
                 route, the body was not JSON (a build answering an unknown path with a page), the JSON
                 was not an object, or the object contradicts itself. Never collapsed into `_LEGACY`:
                 "this build has no plate API" is the claim that licenses attributing a whole project to
                 one plate's file, and a build that has the route and stumbled on it has not earned it.
    """

    __slots__ = ("state", "current", "count", "names", "by_plate", "reason")

    def __init__(self, state: str, current: int | None = None, count: int | None = None,
                 names: dict | None = None, by_plate: dict | None = None, reason: str | None = None):
        self.state = state
        self.current = current
        self.count = count
        self.names = names or {}
        self.by_plate = by_plate or {}
        self.reason = reason


class _Copies:
    """One plate's own membership list as reported: `pairs` the {(id, instance)} copies it names, `ids`
    every object id it names (a member listed without an `instance` still names the object), and
    `unnumbered` for whether any member was listed that way - which is enough to know an object sits
    here when it has one copy, and not enough to count them when it has more."""

    __slots__ = ("pairs", "ids", "unnumbered")

    def __init__(self):
        self.pairs: set[tuple[int, int]] = set()
        self.ids: set[int] = set()
        self.unnumbered = False


def _plain_int(value) -> int | None:
    """A whole number as the slicer reports it, or None for anything that is not one: a string, a
    float, a boolean (bool is an int subclass in Python, and True is not the number 1 here), or an
    absent key. A negative number stays a number: it is the slicer reporting something it measured, and
    a caller that cannot use it says so rather than reading it as absent."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _read_reason(e: Exception) -> str:
    """Why a plate read could not be had, in our own words. The exception's own sentence is never
    quoted: it can carry the slicer's address, and this is a diagnostic label, not a transcript."""
    if isinstance(e, UiTimeout):
        return "the slicer did not answer the plate list in time"
    if isinstance(e, NotReachable):
        return "the slicer could not be reached for the plate list"
    if isinstance(e, Unauthorized):
        return "the slicer refused the plate list as unauthorised"
    if isinstance(e, Validation):
        return "the slicer refused the plate list as invalid"
    if isinstance(e, NotFound):
        return "the plate list answered 404 for a resource, not for a missing route"
    return "the slicer answered the plate list with an error"


async def _plate_read(c) -> _Plates:
    """Ask the slicer for its plates once and say what the answer was worth (see `_Plates`). The one
    thing this must not do is call a failure a legacy build: a modern multi-plate fork whose plate read
    timed out looks exactly like a pre-v2.4.2-mcp.13 fork to a caller that only asks 'did it work?'."""
    try:
        reply = await c.get_plates()
    except NotFound as e:
        if e.route_missing:
            return _Plates(_LEGACY)
        return _Plates(_UNCERTAIN, reason=_read_reason(e))
    except ApiError as e:
        return _Plates(_UNCERTAIN, reason=_read_reason(e))
    except ValueError:
        # Not JSON at all: a build answering an unknown path with an HTML page, a truncated body, or a
        # proxy's error document. None of those is evidence about which routes this build has.
        return _Plates(_UNCERTAIN, reason="the plate reply was not JSON at all")
    return _parse_plates(reply)


def _plate_copies(entry: dict, problems: list) -> _Copies | None:
    """One plate entry's own `objects` list as the copies it prints, or None when the entry has no such
    key: that plate stays silent, which is not the same thing as a plate that lists nothing."""
    if "objects" not in entry:
        return None
    listed = entry.get("objects")
    if not isinstance(listed, list):
        problems.append("a plate's `objects` is not a list")
        return None
    copies = _Copies()
    for member in listed:
        if not isinstance(member, dict):
            problems.append("a plate member that is not an object")
            continue
        oid = _plain_int(member.get("id"))
        if oid is None or oid < 0:
            problems.append("a plate member with no usable object id")
            continue
        copies.ids.add(oid)
        if "instance" not in member:
            copies.unnumbered = True
            continue
        inst = _plain_int(member.get("instance"))
        if inst is None or inst < 0:
            problems.append("a plate member with no usable instance index")
            continue
        copies.pairs.add((oid, inst))
    return copies


def _parse_plates(reply) -> _Plates:
    """Read a plate reply into a `_Plates`, failing closed on anything it says that contradicts the rest
    of what it says: a `count` that does not match the list published with it, a `current` outside that
    list or disagreeing with the entry flagged current:true, a plate listed twice, two plates flagged
    current, or one copy of one object printed on two plates at once. Each of those says the reply as a
    whole cannot be trusted, which is why a `count` of 3 beside a one-entry list is read as neither
    'three plates' nor 'one plate' but as nothing."""
    if not isinstance(reply, dict):
        return _Plates(_UNCERTAIN, reason="the plate reply was not an object")
    problems: list[str] = []
    listed = reply.get("plates")
    if listed is not None and not isinstance(listed, list):
        problems.append("its `plates` is not a list")
        listed = None
    by_plate: dict[int, _Copies | None] = {}
    names: dict[int, str] = {}
    flagged: list[int] = []
    where: dict[tuple[int, int], int] = {}
    for entry in (listed or []):
        if not isinstance(entry, dict):
            problems.append("a plate entry that is not an object")
            continue
        idx = _plain_int(entry.get("index"))
        if idx is None or idx < 0:
            problems.append("a plate entry with no usable index")
            continue
        if idx in by_plate:
            problems.append(f"plate {idx} listed twice")
        if entry.get("current") is True:
            flagged.append(idx)
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            names[idx] = name.strip()
        by_plate[idx] = _plate_copies(entry, problems)
        if by_plate[idx] is not None:
            for pair in by_plate[idx].pairs:
                if pair in where:
                    problems.append(f"one copy is listed on plate {where[pair]} and on plate {idx}")
                where[pair] = idx
    count = _plain_int(reply.get("count"))
    if count is not None and count < 0:
        problems.append("a `count` below zero")
        count = None
    if count is None and listed is not None:
        count = len(listed)
    if count is not None and listed is not None and count != len(listed):
        n = len(listed)
        problems.append(f"its `count` of {count} does not match the {n} plate{'s' if n != 1 else ''} "
                        "it lists")
    cur = _plain_int(reply.get("current"))
    if cur is not None and cur < 0:
        problems.append("a `current` below zero")
        cur = None
    if flagged:
        if len(flagged) > 1:
            problems.append("more than one plate flagged current:true")
        elif cur is None:
            cur = flagged[0]
        elif cur not in flagged:
            problems.append(f"`current` of {cur} disagrees with the plate the list flags as current")
    if cur is not None and count is not None and cur >= count:
        problems.append(f"`current` plate {cur} is outside its own count of {count}")
    if cur is not None and listed is not None and cur not in by_plate:
        problems.append(f"`current` plate {cur} is not in the plate list")
    if problems:
        return _Plates(_UNCERTAIN, reason="the plate reply contradicts itself: " + "; ".join(problems))
    return _Plates(_READ, current=cur, count=count, names=names, by_plate=by_plate)


def _plate_name(plates: _Plates, index: int | None) -> str | None:
    """A plate's own name ("Plate 2" in the window), or None when it has none or the reply never named
    it. Lets a slice we cannot attribute to an object still say which plate it came from."""
    if index is None:
        return None
    return plates.names.get(index)


def _instance_count(obj: dict) -> int:
    """How many copies the slicer says this one object holds, and 1 when it does not say: one copy is
    the smallest claim here that can be wrong, and it is what a build reporting no count means."""
    n = _plain_int(obj.get("instances"))
    return n if n is not None and n >= 1 else 1


def _printed_item(obj: dict, instance: int) -> dict:
    """One printed copy, in the form the fingerprint is made from and no wider: the object's name, its
    size, and which copy it is. The object id is left out on purpose - ids are assigned per session, so
    a fingerprint that carried them could not recall the same model next week."""
    name = obj.get("name")
    return {"name": name if isinstance(name, str) else "", "size_mm": obj.get("size_mm") or [],
            "instance": instance}


def _expand_copies(objs: list) -> list[dict]:
    """Objects as a printed list when no per-plate membership is going to arrive (a one-plate project,
    or a fork with no plate route at all): every copy of every object, from the object's own count."""
    printed: list[dict] = []
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        printed.extend(_printed_item(obj, i) for i in range(_instance_count(obj)))
    return printed


def _unknown(plates: _Plates, reason: str, stats_ok: bool) -> dict:
    """Nothing attributed, and the sentence saying why. `stats_ok` says whether the estimates read
    between the two plate reads may still be this plate's (see `_plate_attribution`)."""
    cur = plates.current
    return {"objects": [], "printed": [], "plate": cur, "plate_name": _plate_name(plates, cur),
            "attribution": _ATTR_UNKNOWN, "reason": reason, "stats_ok": stats_ok}


def _plate_membership(plates: _Plates, index: int | None):
    """What one read says a plate holds, as a value two reads of it can be compared on, or None when that
    read does not say. The comparison is the membership itself - the `{id, instance}` copies, plus the
    object ids a member named without an instance, plus whether any member came unnumbered - because the
    plate index alone cannot close the window: a copy added, deleted or moved to another plate leaves the
    current index exactly where it was while changing what that plate's G-code prints."""
    if index is None:
        return None
    copies = plates.by_plate.get(index)
    if copies is None:
        return None
    return (frozenset(copies.pairs), frozenset(copies.ids), copies.unnumbered)


def _plate_attribution(objs: list, before: _Plates, after: _Plates) -> dict:
    """Which copies the plate that was sliced actually prints, and how sure we are of that.

    `before` is the plate read taken BEFORE the G-code and `after` the one taken after the LAST read
    whose answer goes into the row - the objects, the settings and the estimates are each the current
    plate's - so a plate the user clicks anywhere inside that window shows up as a changed index and the
    bytes in hand can no longer be paired with an object list or an estimate. A plate that stays current
    is not therefore the same plate: what it holds may have changed inside that window too, so the
    closing read's membership for that index is compared pair for pair, and a read that never named what
    the plate held cannot confirm it either. Silence is judged the same way twice: a plate that stayed
    silent in both reads is a build that does not publish membership, which is a fact about the reply the
    caller still reports, while a plate that named its copies at the start and kept them to itself at the
    end has changed what its reply says about it, and the last chance to confirm the membership is gone -
    that fails closed with its estimates, like any other change. When a fork has no plate
    route at all both reads are `_LEGACY`: the project is all such a build shows, so those keep the
    behaviour they always had, labelled as what they are rather than passed off as a plate read.

    An object's own `plate` field is never consulted, so nothing here depends on which instance that
    field happens to describe: it is not the tie-breaker when the plate list and the object list
    disagree, because that disagreement is the known shape of the field rather than a fault to judge.

    Returns {objects, printed, plate, plate_name, attribution, reason, stats_ok}; `objects` and
    `printed` are empty when nothing can be attributed, `printed` is what the fingerprint is made from,
    `objects` the same subset as the slicer reported it (the candidate for matching rows written before
    copies were counted), and `reason` is a sentence naming why only for "unknown".
    """
    if before.state == _LEGACY and after.state == _LEGACY:
        return {"objects": objs, "printed": _expand_copies(objs), "plate": None, "plate_name": None,
                "attribution": _ATTR_PROJECT, "reason": None, "stats_ok": True}
    if _UNCERTAIN in (before.state, after.state):
        bad = after if after.state == _UNCERTAIN else before
        return _unknown(after, bad.reason or "the plate list could not be read", stats_ok=False)
    if before.count != after.count:
        return _unknown(after, "the number of plates changed while the slice information was being read",
                        stats_ok=False)
    cur = after.current
    if cur is None:
        return _unknown(after, "the slicer's plate list does not say which plate is current",
                        stats_ok=False)
    if before.current != cur:
        # Either the plate moved under us, or the read that was meant to bracket the window never named
        # a plate at all, which cannot be told apart from a plate that did move. Neither lets this file
        # be paired with a plate, and neither says its estimates all came from the same one.
        reason = ("the current plate changed while the slice, its objects, its settings and its "
                  "estimates were being read" if before.current is not None else
                  "the plate that was current when the reads began was never named, so this slice "
                  "cannot be confirmed as the current plate's")
        return _unknown(after, reason, stats_ok=False)
    # The index being unchanged is only half the promise. The copies sitting on that index may have
    # changed while the G-code, the objects, the settings and the estimates were read - a duplicate, a
    # delete, a move to or from another plate, or the closing reply simply ceasing to say what the plate
    # holds after the opening one said it - and a row built then would fingerprint a scene that was never
    # sliced and carry estimates measured on a different one, so it keeps neither.
    was = _plate_membership(before, cur)
    now = _plate_membership(after, cur)
    if now is None:
        # The opening read named the plate's copies and the closing one went silent about them. Silence
        # from a plate that has never spoken is a fact about the build, and both reads agree on it, so it
        # is the caller's to report; silence arriving where there was an answer is a change in what the
        # reply says about the plate, and the last chance to confirm the membership is gone. The row may
        # not keep an identity it can no longer prove, and the estimates were read into the same window,
        # so they go with it.
        if was is not None:
            return _unknown(after, "the plate just sliced stopped saying which copies it holds between "
                                   "the reads that bracket the job, so the copies named at the start "
                                   "cannot be confirmed as the ones that were sliced", stats_ok=False)
    elif was != now:
        reason = ("the copies the plate holds changed while the slice, its objects, its settings and its "
                  "estimates were being read" if was is not None else
                  "the plate just sliced did not say which copies it held when the reads began, so the "
                  "copies it names at the end cannot be confirmed as the ones that were sliced")
        return _unknown(after, reason, stats_ok=False)
    known = {"plate": cur, "plate_name": _plate_name(after, cur)}
    copies = after.by_plate.get(cur)
    if copies is None:
        if after.count == 1:
            # Reached only when neither read named the plate's copies (a plate that named them at the
            # start and went silent at the end already failed closed above), so this is the build that
            # reports plates without saying what sits on them. With one plate every copy of every object
            # is on it, so the whole list still is that plate's list.
            return {"objects": objs, "printed": _expand_copies(objs), **known,
                    "attribution": _ATTR_SINGLE, "reason": None, "stats_ok": True}
        return _unknown(after, "the slicer lists several plates but the plate just sliced does not say "
                               "which objects, or which copies of them, sit on it", stats_ok=True)
    by_id: dict[int, dict] = {}
    for obj in objs:
        oid = None if not isinstance(obj, dict) else _plain_int(obj.get("id"))
        if oid is None:
            return _unknown(after, "the object list contains an entry with no usable id, so the copies "
                                   "the plate lists cannot be matched to it", stats_ok=True)
        by_id[oid] = obj
    missing = copies.ids - set(by_id)
    if missing:
        n = len(missing)
        return _unknown(after, f"the plate just sliced names {n} object{'s' if n != 1 else ''} the "
                               "object list does not report, so what it prints cannot be identified",
                        stats_ok=True)
    if copies.unnumbered and any(_instance_count(by_id[i]) > 1 for i in copies.ids):
        return _unknown(after, "the plate lists its objects without saying which copy each one is, and "
                               "at least one of them has more than one", stats_ok=True)
    printed = [_printed_item(by_id[oid], inst) for oid, inst in sorted(copies.pairs)]
    numbered = {oid for oid, _ in copies.pairs}
    printed.extend(_printed_item(by_id[oid], 0) for oid in sorted(copies.ids - numbered))
    return {"objects": [by_id[oid] for oid in sorted(copies.ids)], "printed": printed, **known,
            "attribution": _ATTR_PLATE, "reason": None, "stats_ok": True}


@mcp.tool()
async def save_gcode(filename: str | None = None) -> dict:
    """Save the last successful slice's G-code and record the slice (model, a fingerprint of the
    copies that plate prints, each object's name and size once per copy, the full settings snapshot,
    and OrcaSlicer's time and filament estimates) in the local outcome store under that filename.
    When that file is printed, list_print_history (or klipper-mcp's recorder) matches the
    finished job by filename, so the real result joins back to these settings. Returns the saved
    path; hand it to klipper-mcp's start_print. Default filename: <object>_<timestamp>.gcode. Never
    overwrites an existing file: a name collision gets a -2, -3, ... suffix. Writes into a gcode
    folder under PRINT_OUTCOMES_DIR if set, else under the shared print-outcomes folder
    (~/projects/_shared/print-outcomes) if it already exists, else under ~/.orcaslicer-mcp; the folder
    is created if missing. If the store write fails, the file is still saved and outcome_recorded is
    False.

    On a project with several plates the record covers the copies the sliced plate lists for itself,
    each one named by its object id and instance index the way `list_plates` reports them, not every
    object in the project, because the G-code is only ever that one plate's: the plate's index comes
    back as `plate` and how the objects were chosen as `plate_attribution`. When which copies sit on the
    sliced plate cannot be established (the slicer reports several plates without saying what the sliced
    one holds, names no current plate, contradicts itself about its own plate list, cannot be reached
    for it, or the plate was switched or the copies it holds changed between the reads that bracket the
    whole job), the file saves and
    the row keeps its settings and printer, but with no model name, no fingerprint, and no time or
    filament estimate, and the reply says so in `attribution_warning` rather than making them up. A fork
    older than v2.4.2-mcp.13 says nothing about plates at all, and only a genuine missing plate route
    says that, not a plate read that timed out, so there nothing changed: `plate_attribution` is
    "project" and the record covers every object as it always did."""
    try:
        async with _client() as c:
            plates_before = await _plate_read(c)
            data = await c.get_gcode()
            objs = (await c.get_objects()).get("objects") or []
            cfg = await c.get_config(None)
            stats: dict = {}
            printer_name = None
            try:
                stats = (await c.slice_status()).get("stats") or {}
            except ApiError:
                pass  # the estimate is nice to have; the file still saves without it
            try:
                printer_name = ((await c.get_status()).get("presets") or {}).get("printer")
            except ApiError:
                pass
            # The second read closes the window, and it has to come after the LAST read whose answer
            # goes into the row: the settings snapshot and the estimates are the current plate's too, so
            # a plate clicked during them would otherwise land on this row unnoticed. It is not only
            # about a plate being clicked - on a one-plate project a copy duplicated onto the bed, deleted
            # or moved off it changes what that plate's G-code prints while the index, the only thing such
            # a project can compare, stays exactly where it was. So the closing read is paid for whenever
            # the opening read named a plate that could have changed under us, which a fork with no plate
            # route, or a window already known to be untrustworthy, never leaves us with.
            plates_after = (plates_before if plates_before.state in (_LEGACY, _UNCERTAIN)
                            else await _plate_read(c))
    except Conflict:
        return {"error": "not_sliced"}
    except ApiError as e:
        return _m4a_err(e)
    attr = _plate_attribution(objs, plates_before, plates_after)
    printed = attr["printed"]
    attributed = attr["attribution"] != _ATTR_UNKNOWN
    # The label names the file; only a plate we actually identified may name the row.
    label = (printed[0]["name"] if printed else None) or attr["plate_name"] or "plate"
    model_name = label if attributed else None
    fname = _safe_gcode_name(filename or f"{label}_{datetime.datetime.now():%Y%m%d-%H%M%S}")
    out_dir = _gcode_base_dir() / "gcode"
    out_dir.mkdir(parents=True, exist_ok=True)
    path, fname = _unique_gcode_path(out_dir, fname)
    try:
        path.write_bytes(data)
    except OSError as e:
        return {"error": "write_failed", "detail": str(e), "path": str(path)}
    row_id = None
    outcome_error = None
    try:
        row_id = _outcomes.record_slice(
            fname, model_name,
            _outcomes.printed_geometry_hash(printed) if attributed else None, cfg,
            printer_id=_ptarget.printer_id(printer_name),
            est_time_s=stats.get("estimated_time_seconds") if attr["stats_ok"] else None,
            est_filament_g=stats.get("filament_used_g") if attr["stats_ok"] else None)
    except Exception as e:  # noqa: BLE001 - whatever goes wrong while recording, the saved file stays
        outcome_error = str(e) or type(e).__name__
    result = {"path": str(path), "filename": fname, "bytes": len(data), "model_name": model_name,
              "plate": attr["plate"], "plate_attribution": attr["attribution"],
              "printed_copies": len(printed) if attributed else None,
              "outcome_recorded": row_id is not None, "outcome_row_id": row_id}
    if outcome_error is not None:
        result["outcome_error"] = outcome_error
    if not attributed:
        # The estimates are per-plate by construction, so a window that cannot be trusted cannot have
        # produced them; the settings snapshot stays, because it is the slicer's own active project
        # config, and the warning is where its uncertainty belongs.
        stats_note = ("" if attr["stats_ok"] else
                      "The slicer's time and filament estimates are left out as well, because they were "
                      "read while it was not known which plate was current and may be another plate's; "
                      "the settings snapshot is what the slicer held when it was read, which under the "
                      "same uncertainty may belong to the plate that was current then. ")
        result["attribution_warning"] = (
            "The file is saved and the row recorded, but with no model name and no fingerprint: "
            f"{attr['reason']}. {stats_note}The G-code file itself is unaffected, and the row still "
            "joins to its finished print by filename; only a recall by model cannot find it. If another "
            "plate should have been current, select_plate and save again.")
    return result


def _recall_summary(rows: list[dict], subject: str) -> str:
    n = len(rows)
    if n == 0:
        return f"no past prints of {subject}" if subject != "recent" else "no recorded prints"
    counts: dict[str, int] = {}
    for r in rows:
        k = r.get("result") or "unprinted"
        counts[k] = counts.get(k, 0) + 1
    parts = ", ".join(f"{v} {k}" for k, v in counts.items())
    verdict_counts: dict[str, int] = {}
    for r in rows:
        v = r.get("human_verdict")
        if v:
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
    tail = f" ({', '.join(f'{c} marked {v}' for v, c in sorted(verdict_counts.items()))})" if verdict_counts else ""
    head = f"{n} recent print{'s' if n != 1 else ''}" if subject == "recent" else f"{n} past print{'s' if n != 1 else ''} of {subject}"
    return f"{head}: {parts}{tail}"


def _copies_spelled(printed: list[dict]) -> str:
    """How many copies of each part a plate prints, spelled for a person: "3 copies of brick.stl", or,
    when the plate holds several parts, "2 copies of brick.stl, 1 copy of peg.stl". A part the slicer
    never named still gets named as unnamed, so the sentence cannot go silently vague."""
    counts: dict[str, int] = {}
    for item in printed:
        name = item.get("name") or "an unnamed part"
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(f"{n} cop{'y' if n == 1 else 'ies'} of {name}" for name, n in counts.items())


def _legacy_fingerprint_note(printed: list[dict]) -> str:
    """The disclosure that goes with a row found through the pre-copies fingerprint: it is a match on the
    part, and the copies are not on the wire in that form, so it is no proof of the same print."""
    return ("These rows were recorded before copies were counted, on a fingerprint that names each part "
            f"once and so cannot tell one copy of it from several, while this plate prints "
            f"{_copies_spelled(printed)}. They match on the part, not on what was printed: treat their "
            "settings and outcomes as the history of this part at an unknown number of copies, never as "
            "the record of a print of exactly these copies.")


def _copy_count_note(printed: list[dict]) -> str:
    """Why an answer that is not a fingerprint match on the current copies cannot be this plate's
    history: name matching cannot count copies either, and recency says nothing about the part at all."""
    return (f"No recorded print carries the fingerprint of what this plate prints ({_copies_spelled(printed)}). "
            "The rows below are matched by part name or by recency alone, and neither can say how many "
            "copies were printed: a row for one copy of a part is not the history of a print of several of "
            "them, so do not tell the user these are how this plate's prints have gone.")


@mcp.tool()
async def recall_prints(model_name: str | None = None, limit: int = 5) -> dict:
    """How did past prints of THIS model actually turn out? Matches the current plate by geometry
    (or by model_name if given / the slicer is offline), returning each past print's result
    (success/cancelled/error), your recorded verdict (e.g. 'warped'), and the settings it was sliced
    with. Call this BEFORE slicing and tell the user anything relevant (a past warp, a failed layer
    height). Each row also carries failure_reason (the printer's own words, when known) and the slicer's
    estimates (est_time_s, est_filament_g). duration_s is Moonraker's whole job time, including heating
    up and any pauses, so beside the estimates it overstates an overrun; list_print_history compares the
    print time (without heating or pauses) with the estimate. filament_g comes from the G-code file's
    own estimate, because printers don't report grams. Call list_print_history first to pull in recent
    results. Read-only. Returns available=false and nothing else when no
    outcome store exists yet.
    If neither geometry nor name matches, it returns the most recent prints of ANY model with
    matched_by='recent'; never attribute those to the current model.

    The geometry match is on the fingerprint save_gcode wrote, the printed copies of the current plate,
    each object's name and size once per copy.

    On a project with several plates 'this model' means the copies the current plate lists for itself,
    the same list save_gcode fingerprints, so a plate holding three copies never matches the row of a
    print that had one. When the slicer does not say what the current plate holds, or cannot be reached
    for the plate list, no fingerprint match is attempted at all: a different plate's part would be a
    worse answer than the recent prints it falls back to. `plate` and `plate_attribution` say what it
    looked with, and when it could not, `attribution_note` says why the answer is only "recent".

    Rows written before copies were counted carry the older fingerprint, which names each part once and so
    cannot tell one copy of it from three: those rows are still found, and a match on one comes with a
    `legacy_fingerprint_note` saying the copy count is not established. When nothing on record prints the
    copies this plate prints, `copy_count_note` says so and the answer is NOT this plate's history. Past
    prints of a part are then searched by name only when you passed `model_name` yourself - a name cannot
    count copies, so the tool never reaches for the current plate's own part name to call somebody else's
    print this model's history."""
    if not _outcomes.is_available():
        return {"available": False, "matched_by": None, "prints": [], "summary": "no outcome store"}
    attr = {"objects": [], "printed": [], "plate": None, "plate_name": None,
            "attribution": _ATTR_UNKNOWN, "reason": "the slicer did not answer, so no plate is known",
            "stats_ok": False}
    try:
        async with _client() as c:
            before = await _plate_read(c)
            objs = (await c.get_objects()).get("objects") or []
            # Bracketed like save_gcode brackets its writes: the fingerprint is only this plate's if the
            # plate, and the copies it names, are still the same after the objects were read - which a
            # one-plate project has to re-read to find out, because its index cannot say either way.
            after = (before if before.state in (_LEGACY, _UNCERTAIN) else await _plate_read(c))
            attr = _plate_attribution(objs, before, after)
    except ApiError:
        pass
    honesty = {"plate": attr["plate"], "plate_attribution": attr["attribution"]}
    if attr["attribution"] == _ATTR_UNKNOWN:
        honesty["attribution_note"] = (
            f"No fingerprint match was attempted: {attr['reason']}. These are recent prints of any "
            "model, and they are not this plate's history unless one of them names the same part.")
    limit = max(1, min(limit, 50))
    # Match on the copies the current plate prints: that is what save_gcode fingerprints, so matching
    # on the whole project, or on the objects without their copies, would never line up on a multi-plate
    # project. The objects form older rows carry is looked for separately and second, because it cannot
    # say how many copies were printed: a row found that way is disclosed, not handed over as this plate's
    # history. Nothing else may stand in for a match either, so once both forms come up empty the name
    # search runs only on a name the caller typed, never on this plate's own part name.
    printed = attr["printed"] if attr["attribution"] != _ATTR_UNKNOWN else []
    if printed:
        subject = printed[0]["name"] or attr["plate_name"] or "this plate"
        current = _outcomes.printed_geometry_hash(printed)
        rows = _outcomes.recall(geometry_hashes=[current], limit=limit)
        if rows:
            return {"available": True, "matched_by": "geometry", "prints": rows,
                    "summary": _recall_summary(rows, subject), **honesty}
        pre_fix = _outcomes.geometry_hash_for(attr["objects"])
        if pre_fix != current:
            rows = _outcomes.recall(geometry_hashes=[pre_fix], limit=limit)
            if rows:
                return {"available": True, "matched_by": "geometry", "prints": rows,
                        "summary": _recall_summary(rows, subject),
                        "legacy_fingerprint_note": _legacy_fingerprint_note(printed), **honesty}
        honesty["copy_count_note"] = _copy_count_note(printed)
    if model_name:
        rows = _outcomes.recall(model_name=model_name, limit=limit)
        if rows:
            return {"available": True, "matched_by": "name", "prints": rows,
                    "summary": _recall_summary(rows, model_name), **honesty}
    rows = _outcomes.recall(limit=limit)
    return {"available": True, "matched_by": "recent", "prints": rows,
            "summary": _recall_summary(rows, "recent"), **honesty}


@mcp.tool()
async def render_plate(view: str = "editor", angle: str = "iso",
                       width: int = 800, height: int = 600,
                       frame: str | None = None):
    """Render a PNG picture of the current plate so you can SEE it.

    view="editor": the models on the bed BEFORE slicing - use to check
    orientation, plate contact, and first-layer footprint (an Euler triple is
    near-unreadable; this is the ground truth). view="preview": the sliced
    toolpaths colored by feature role AFTER a successful slice - support is
    visibly distinct, so use it to check where support actually went.
    angle: iso|top|front|left|right|rear|bottom.
    frame: "plate" zooms out to the whole bed (where the part sits, footprint),
    "object" zooms in on the model/toolpaths (detail). Defaults to "plate" for
    the editor view and "object" for the preview view; pass it explicitly when
    a side view of a small part would otherwise be a speck on a big bed.
    """
    try:
        async with _client() as c:
            png = await c.get_plate_render(view=view, angle=angle,
                                           width=width, height=height, frame=frame)
            return Image(data=png, format="png")
    except Conflict:
        return {"error": "no_slice_result",
                "hint": "slice first, then render view='preview'"}
    except ApiError as e:
        return _m4_err(e, "plate_render (fork v2.3.2-mcp.4+)")



# --- Printer feedback (read-only) ---------------------------------------------
# The printer is found through OrcaSlicer's active printer profile (or ORCA_PRINTER_URL) and asked
# over HTTP GET only. Nothing here sends commands to a printer.


def _printer_api_key() -> str | None:
    return os.environ.get("ORCA_PRINTER_API_KEY", "").strip() or None


def _remembered_age(at) -> str | None:
    """How long ago a remembered time was, in words, or None when it is no usable time: not a number,
    NaN or Infinity (json.loads accepts both), an integer too big for a float, or a number no calendar
    can hold (all of which a hand-edited printer.json can contain). Asking the calendar to read the
    time is the test: it refuses each of those with OverflowError, OSError or ValueError."""
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    try:
        datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
        age = time.time() - at
    except (OverflowError, OSError, ValueError):
        return None
    return f"{_duration_text(max(0.0, age))} ago"


def _remembered_note(target) -> str:
    age = _remembered_age(target.remembered_at) or "earlier"
    return f"OrcaSlicer isn't running or can't be reached, so this uses the printer remembered {age}."


async def _with_printer(fn) -> dict:
    """Find the printer (override > OrcaSlicer profile > remembered), open the protocol that
    answers, and run fn(target, client). Printer problems come back as {error, message, hint, ...};
    once the printer is known, every reply (error or not) says which one it is in `printer`."""
    target = None
    try:
        target = await _ptarget.resolve_target(_client)
        target, client = await _ptarget.open_printer(target, _printer_api_key())
        async with client:
            out = await fn(target, client)
        if target.source == "remembered" and isinstance(out, dict):
            notes = out.get("notes")
            out["notes"] = (notes if isinstance(notes, list) else []) + [_remembered_note(target)]
        return out
    except PrinterError as e:
        out = e.as_dict()
        if target is not None:
            out.setdefault("printer", target.public())
        return out


@mcp.tool()
async def get_printer_status() -> dict:
    """What the 3D printer is doing right now: state (idle, heating, printing, paused, finished,
    cancelled, error, shutdown, offline), nozzle and bed temperatures with their targets, the current
    job (file, progress, layer, time elapsed and left, and what the time left is based on), and any
    problems the printer reports, each in the printer's own words with a plain-English hint for
    common ones. Relay `headline` as-is. Never invent a cause the printer did not report.

    Finds the printer from OrcaSlicer's active printer profile (or ORCA_PRINTER_URL), so it needs no
    extra setup once the printer is connected in OrcaSlicer. Klipper (Moonraker) printers report the
    most; OctoPrint reports basic status. Read-only: it never sends commands to the printer."""
    return await _with_printer(lambda t, c: take_snapshot(t, c))


@mcp.tool()
async def wait_for_printer(
    until: Annotated[Literal["heated", "printing", "first_layer_done", "finished"], Field(description=(
        "What to wait for. 'heated': every heater with a target is within 3 °C of it. 'printing': "
        "extrusion has started. OctoPrint reports no extrusion, so there it means the job is "
        "running with the heaters at temperature. 'first_layer_done': the second layer has begun "
        "(Klipper only); without layer info from the slicer it is judged from the nozzle height, so "
        "it is approximate. 'finished': the job completed, was cancelled or errored."))],
    timeout_s: Annotated[int, Field(description=(
        "Seconds to wait before returning the latest status. Default 300, at most 1800. "
        "Call again to keep waiting."))] = _pwait.DEFAULT_TIMEOUT_S,
    ctx: Context | None = None,
) -> dict:
    """Wait until the printer reaches a point in a print, checking every 5 seconds. Returns as soon as
    it happens (met: true), or at the timeout with the latest status (met: false). It also returns
    early, with stopped_early saying why, when the printer reports a fault, when the job ends before
    the point it waits for (a cancelled print, say), or after losing contact ("Lost contact with the
    printer"). A job that completes successfully while the wait is for 'printing' or 'first_layer_done'
    counts as met (a one-layer print passes both at once). A met result can carry stopped_early too,
    for example a job that finished with an error. Waiting for 'finished' when nothing is printing
    returns at once with the note 'nothing is printing'. Read-only: it never sends commands."""
    async def report(done: float, total: float, message: str) -> None:
        if ctx is not None:
            await ctx.report_progress(done, total, message)

    return await _with_printer(lambda t, c: _pwait.run_wait(t, c, until, timeout_s, report))


@mcp.tool()
async def list_print_history(limit: int = 10) -> dict:
    """How did recent prints on the printer actually end? For each finished job: result (success,
    cancelled or error), how long it took against the slicer's estimate, filament used, and for failed
    or stopped jobs the printer's own reason while its console still holds it. `summary` says how
    the slicer's time estimates compare with reality once there are a few finished prints. Also copies
    those jobs into the local outcome store (safe to repeat), so recall_prints can learn from them.
    Klipper printers only. `limit` is capped at 50."""
    return await _with_printer(
        lambda t, c: _phistory.print_history(t, c, limit, _ptarget.printer_id_for(t)))


@mcp.tool()
async def check_printer_match() -> dict:
    """Does the active OrcaSlicer profile match the printer it will print on? Compares nozzle size,
    bed size and height, the highest speeds and accelerations, nozzle and bed temperatures, and
    firmware retraction against the printer's own Klipper settings. Each check comes back ok, warn or
    unknown with both values and why it matters; relay `headline` and the warnings. Warnings only: it
    never changes or blocks anything. Run it before printing with a new or edited profile, or when
    prints take longer than estimated. Klipper printers only; needs OrcaSlicer running."""
    async def run(t, c):
        if t.kind != "klipper":
            return {"supported": False, "reason": "Only Klipper printers report their settings.",
                    "printer": t.public()}
        settings = ((await c.objects_query(["configfile=settings"])).get("configfile") or {}).get("settings") or {}
        try:
            async with _client() as fork:
                cfg = await fork.get_config(list(_pmatch.PROFILE_KEYS))
        except ApiError as e:
            raise PrinterError("orca_unreachable",
                               "check_printer_match couldn't read the active profile from OrcaSlicer.",
                               hint=_ptarget.orca_read_hint(
                                   e, "Start OrcaSlicer (MCP build) with the Remote API enabled."),
                               detail=str(e)) from e
        out = _pmatch.compare(cfg, settings)
        out["printer"] = t.public()
        return out

    return await _with_printer(run)


# --- Direct Elegoo Centauri Carbon 2 printing (configured, never switched on) ----------------
# Seventeen tools — the reads cc2_status, cc2_list_files, cc2_printer_info, cc2_file_detail,
# cc2_file_thumbnail, cc2_print_history and cc2_camera_snapshot, then cc2_upload_gcode,
# cc2_start_print, the job
# controls cc2_pause_print, cc2_resume_print and cc2_stop_print, then the peripheral controls
# cc2_set_light, cc2_set_print_speed, cc2_set_temperature and cc2_set_fan, and the CANVAS switch
# cc2_set_auto_refill — speak to the
# printer directly (LAN discovery, MQTT, HTTP upload, one HTTP camera stream, one start command, one
# control command) through
# the printer/cc2_* adapters, with no OrcaSlicer and no Home Assistant in the path.
#
# They are declared the way every other tool on this server is: a client sees all seventeen in
# tools/list whether or not a printer has been configured. What makes them safe is not hiding them.
# With no address set (ORCA_CC2_URL empty) every one of them refuses in-process and says so —
# not_configured, with printer_contacted=false — before any address is resolved and before any
# socket is opened; configuring ORCA_CC2_URL, plus the printer's access code in
# ORCA_CC2_ACCESS_CODE / ORCA_CC2_ACCESS_CODE_FILE, is the ordinary configuration step that makes
# them able to reach anything, exactly as ORCA_PRINTER_URL is for the Klipper printer tools. Nothing
# here asks a second time: the config IS the opt-in, and a not_configured refusal is the honest
# answer until it exists.
#
# One session, not one per call. The printer's own MQTT broker takes about four clients and reaps a
# departed one on its own clock, so when every tool call registered, used and closed its own session
# the fourth ordinary call in a row met "too many clients" (reason=client_limit) from an idle,
# healthy printer. So sixteen of the seventeen tools borrow the process's one registered session through
# printer/cc2_pool.py: opens are serialised so two simultaneous calls cannot register twice, while
# requests on an established session run concurrently because the transport matches every answer to
# the request id it stamped. A session that reports itself dead, or that the configuration left
# behind (address or access code, re-read on every borrow), is retired and the NEXT call opens
# afresh — which repeats no command, because the pool sends nothing and an unanswered command stays
# as ambiguous as the reply that reported it said. With nobody holding it, the session is closed
# after cc2_pool.IDLE_CLOSE_S so a printer left alone gets its slot back; and the server's shutdown
# closes it too. Two server processes are still two clients — nothing is shared across processes.
# cc2_camera_snapshot is the one exception, and a deliberate one: a camera picture is one HTTP GET of the
# printer's own stream, so it needs no MQTT session, registers no client, spends none of the printer's
# four slots, and still works when the roster is full. What it reuses from the transport is its
# configuration and its address pinning, never its session — which is why this file now speaks of HTTP in
# two voices: the upload writes to the printer over one port, the camera picture is fetched from another,
# and neither sends a protocol command.
#
# These are the server's only printer WRITEs, and MCP tool annotations are advisory metadata, a hint
# to a client, never an access control: the ten writes carry readOnlyHint=false and
# cc2_stop_print additionally destructiveHint=true, and what actually reaches a human is the
# maintainer companion's two-phase confirm, enforced in every control tool's own body so it still
# holds for a client running with permission prompts off. Credentials from the cc2_transport config
# (ORCA_CC2_ACCESS_CODE / ORCA_CC2_ACCESS_CODE_FILE) never leave the adapters: they go only into the
# MQTT password and the upload header, never into a reply, and returned text is checked against the
# configured code before it goes out. Raw printer replies never reach the model either — replies are
# built field by field from the adapters' parsed, fail-closed state, and everything passes through
# _cc2_out(): strings capped, control characters stripped, non-JSON-safe numbers turned to None.

# The seventeen direct tools, in declaration order: the seven reads first (the live state, the file
# inventory, what the printer is, what one of its files is, what that file looks like, what it has
# printed before, and what its camera sees), then the
# upload and the start, then the three job controls, then the four peripheral controls (light, speed
# mode, temperature, fan), then the
# CANVAS auto-refill switch. Each is
# registered by its own @mcp.tool() below, and tests assert that
# this roster and the registered cc2_* set are the same list, so a decorator lost in a refactor fails
# a test instead of quietly shrinking what a client is shown.
CC2_TOOL_NAMES: tuple[str, ...] = (
    "cc2_status", "cc2_list_files", "cc2_printer_info", "cc2_file_detail", "cc2_file_thumbnail",
    "cc2_print_history", "cc2_camera_snapshot",
    "cc2_upload_gcode", "cc2_start_print",
    "cc2_pause_print", "cc2_resume_print", "cc2_stop_print",
    "cc2_set_light", "cc2_set_print_speed", "cc2_set_temperature", "cc2_set_fan",
    "cc2_set_auto_refill",
)

# Every key name a CC2 reply may carry, as the strings this server and its printer/cc2_* adapters
# write, gathered from the reply builders and from every key the mocked cc2 tests have seen
# arrive. _cc2_out runs the credential pass over dictionary KEYS as well as their values, which is
# right for a key a printer invented and wrong for a key this server wrote: an access code is
# whatever the printer's LAN happens to use, and a short one made the pass rewrite the reply's own
# contract — with the code "a" the keys "filename" and "state" arrived as "filen<redacted>me" and
# "st<redacted>te", so a trusted reply no longer matched the schema the model was told to expect.
# Exact membership is what makes the exemption safe rather than a redaction hole: a listed name is
# a string this source contains, so it goes out the same way whether the code is unset, set, or
# different, and an exempt key therefore carries no fact about the secret — including the
# pathological case where a printer invents a key that happens to equal a listed name, which
# arrives as that name and nothing else. What the schema cannot do is hide a code that is a
# substring of one of these names: a credential that short is no secret, and every value is still
# passed, so a code never comes back in text. Every entry is identifier-shaped ([a-z0-9_]) on
# purpose, so an exempt key can never itself need cleaning. When a reply grows a field, add its
# name here: the two tests written against this list — one scanning the adapters' dictionary
# literals, one answering every read tool under a one-letter access code — fail with the missing
# name in their output if you forget.
_CC2_REPLY_KEYS = frozenset({
     "_filaments_answered", "accepted", "action", "active", "active_canvas_id", "active_tray_id",
    "actual_now_c", "ambiguous", "asked", "asked_percent", "attachments", "auto_refill",
    "auto_refill_is_known", "auto_refill_now", "auto_refill_raw_now", "aux_fan", "auxiliary",
    "axes", "base64_chars", "bed", "bed_c", "bed_clear_required", "bed_first_layer_temp",
    "bed_target_c",
    "bed_temp", "bed_temp_c", "bed_warning", "bed_warning_provenance", "began_at", "begin_time",
    "brand", "brightness", "box_fan", "build", "bytes", "camera_connected", "cancelled", "canvas",
    "canvas_id", "canvas_load", "canvas_unload", "ceiling_c", "chamber", "changed", "client_id",
    "color", "command_attempted", "commanded_bed_setpoints", "commanded_nozzle_setpoints",
    "commanded_nozzle_setpoints_c", "complete", "completeness", "config", "confirm_required",
    "connected", "content_sha256", "created", "current_layer", "deletion_proved", "dir",
    "display", "distance", "driving_a_filament_operation", "duration_seconds", "empty",
    "end_time", "ended_at", "entries", "entries_in_reply", "entries_kept", "error", "error_code",
    "est_filament_g", "est_print_s", "extruder", "extrusion_nozzle_temps",
    "extrusion_nozzle_temps_c", "fan", "fans_now_raw", "field", "filament_grams", "filaments",
    "file_in_this_listing", "file_name", "file_now", "file_path", "file_size_bytes", "filename",
    "files", "firmware", "first_layer", "hardware", "hardware_version", "has_more", "heater",
    "heater_bed", "heater_is_known", "hint", "home", "homed_axes", "homed_axes_now", "how", "id",
    "identity", "idle_reason", "image_kind", "inventory_complete", "job", "job_filename",
    "job_state", "job_state_known", "kind", "last_print_time", "last_printed", "layers",
    "layers_from_field", "led_status", "led_status_now", "light", "light_is_known", "light_on",
    "light_on_now", "limit", "limit_basis", "listing_certified", "listing_count",
    "listing_proves_nothing_because", "listing_total", "listing_why", "machine", "machine_status",
    "material", "max_bytes", "max_nozzle_temp_c", "max_video_connections", "may_print_from_it",
    "mcu_version", "message", "method", "min_nozzle_temp_c", "mode", "mode_is_known",
    "mode_name_now", "mode_now", "model", "move", "name", "name_matches_request", "network",
    "never_printed", "next", "next_offset", "no_temperature_condition", "not_reported", "note",
    "notes", "nozzle", "nozzle_c", "nozzle_first_layer_temp", "nozzle_target", "nozzle_target_c",
    "nozzle_temp", "nozzle_temp_c", "numbers", "observed_active_canvas_id",
    "observed_active_tray_id", "observed_actual_c", "observed_auto_refill",
    "observed_auto_refill_raw", "observed_canvas_connected", "observed_fan_changed",
    "observed_fan_raw", "observed_homed_axes", "observed_job", "observed_led_status",
    "observed_light_on", "observed_position", "observed_running", "observed_speed_mode",
    "observed_speed_name", "observed_state", "observed_target_c", "observed_tray_color",
    "observed_tray_count", "observed_tray_raw_state", "observed_tray_state",
    "observed_tray_states", "offset", "order", "ota_version", "pages_certified", "params", "path",
    "pause", "paused", "peripherals", "position_now", "power", "present", "preview",
    "print_seconds", "printer", "printer_check", "printer_display", "printer_idle",
    "printer_says", "printer_sn", "printing", "progress", "protocol_version", "raw", "raw_state",
    "readback_unit", "ready", "ready_for_job", "reason", "receipt_sha256",
    "remaining_memory_bytes", "remote_filename", "reported_size_bytes", "request_id", "requested",
    "resolution", "resume", "returned", "sent", "sent_bytes", "sent_params", "serial",
    "serial_agree", "session_serial", "setting", "sign_convention", "size_bytes", "slot_map",
    "soc_version", "speed", "speed_mode", "speed_name", "standby", "started_accepted", "state",
    "state_checked", "state_known", "state_note", "state_word", "status", "status_labels",
    "steady", "stop", "storage",
    "storage_media", "sub_status", "sub_status_word", "target_now_c", "task_id", "task_name",
    "task_status", "task_status_label", "temps", "then_reads", "time_lapse_video_duration",
    "time_lapse_video_size", "time_lapse_video_status", "time_lapse_video_url",
    "time_lapse_video_url_withheld", "tool", "tool_index", "toolhead_load", "toolhead_retreat",
    "tools", "total", "total_from_printer", "total_layers", "total_print_times", "tray_id",
    "trays", "truncated_in_reply", "type", "unverified", "uploaded", "uploaded_at", "url",
    "usb_connected", "value", "vendor_loaded_tray_id", "vendor_loaded_tray_note",
    "vendor_step_choices_mm", "verb", "verified_on_this_printer", "version", "video_connections",
    "waited_s", "waits_up_to_s", "warning", "warnings", "why_not", "will_send", "wire_formula",
    "would_refuse", "xyz_size",
})

# The camera snapshot's reply names, declared as their own addition rather than woven into the sorted
# block above: the rule is identical (a name this source contains is a name this server wrote, so the
# credential pass must not rewrite it), and keeping them together is what makes the whole surface of
# cc2_camera_snapshot answerable in one search. Nothing here is exempted that the block above would not
# have exempted — they are the same kind of string, and the two completeness tests in tests/
# test_server_cc2.py (the one-letter-code walk and the source scan) cover them exactly as they cover the
# rest, because both read this name, not the literal it came from.
_CC2_REPLY_KEYS = _CC2_REPLY_KEYS | frozenset({
    "bytes_consumed", "bytes_skipped_before_frame", "camera_method_sent", "command_sent",
    "content_type", "elapsed_s", "frame_complete", "http_status", "minimum_bytes",
    "mqtt_session_used", "printer_advertised_url", "printer_contacted", "read_window_s",
    "requests_sent", "stream_host", "stream_path", "stream_port", "stream_query",
    "stream_url", "stream_url_source",
})

_CC2_STR_MAX = 255           # retain exact printer filenames (up to 255 characters)
_CC2_LIST_MAX = 200          # per-printer lists (trays, colours, tools) inside one reply
# The most files one cc2_list_files reply may carry, and it is held equal to the sanitiser's own
# list cap above on purpose: _cc2_out would cut a longer list on its way out, and a page quietly cut
# by a second unmentioned cap is the hidden truncation this tool must never have. So the tool caps
# what it pages by the same number, and refuses a limit above it by name instead of answering short.
_CC2_FILES_MAX = _CC2_LIST_MAX

_CC2_MACHINE_WORDS = {
    0: "initializing", 1: "idle", 2: "printing", 3: "filament operating",
    4: "filament operating (alt)", 5: "auto-leveling", 6: "PID calibrating",
    7: "resonance testing", 8: "self-checking", 9: "updating", 10: "homing",
    11: "file transferring", 12: "video composing", 13: "extruder operating",
    14: "emergency stop", 15: "power-loss recovery",
}


def _cc2_upload_http_factory():
    """The seam tests use to stand a fake chunk sender in place of httpx."""
    return _cc2_upload.Cc2UploadHttp


def _cc2_snapshot_http_factory():
    """The seam tests use to stand a scripted camera stream in place of httpx. Looked up at call time
    rather than bound at import, so a test standing a fake here is believed by the tool too. What comes
    back is called with the pinned URL and nothing else: no header, no auth, no proxy, no redirect."""
    return _cc2_snapshot.Cc2StreamHttp


def _cc2_configured_code() -> str | None:
    """The configured access code's text, for checking returned text against it before it goes
    out. Loaded only to compare — never printed, never returned as part of a reply. Resolution
    failure means no configured code, which is also the honest answer for redaction purposes."""
    try:
        cfg = _cc2_transport.load_config()
    except (PrinterError, ValueError, RuntimeError, OSError):
        return None
    return cfg.access_code.value if cfg.access_code is not None else None


def _cc2_key(key: str, _depth: int, _code: str | None) -> str:
    """One dictionary key of a reply, made safe the way its key needs. A name in
    _CC2_REPLY_KEYS was written by this server, so it is the contract and goes out exactly as
    written; any other key is text that came from somewhere else and takes the same pass a value
    takes (see _CC2_REPLY_KEYS)."""
    if key in _CC2_REPLY_KEYS:
        return key
    return _cc2_out(key, _depth, _code)


def _cc2_out(value, _depth: int = 0, _code: str | None = "unset"):
    """A reply, made safe to hand the model: plain JSON types only, strings stripped of control
    characters and capped, floats finite, depth and list lengths bounded, everything else None.
    A configured access code, if one ever rode in with a string, is replaced along the way — the
    code is resolved once per top-level call, not once per string ("unset" is a sentinel so the
    environment is read at call time, never at import). Dictionary keys are the one place where the
    pass is not applied blindly: a key this server wrote is the contract, so keys go through
    _cc2_key, which exempts the names in _CC2_REPLY_KEYS and redacts every other key."""
    if _code == "unset":
        _code = _cc2_configured_code()
    if _depth > 6:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) < 2 ** 63 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if _code:
            value = value.replace(_code, "<redacted>")
        text = re.sub(r"[\x00-\x1f\x7f\u2028\u2029]", " ", value).strip()
        if len(text) > _CC2_STR_MAX:
            text = text[:_CC2_STR_MAX] + "…"
        return text
    if isinstance(value, datetime.datetime):
        return _cc2_out(value.isoformat(), _depth, _code)
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:64]:
            key = _cc2_key(str(k), _depth + 1, _code)
            if key in out:
                return {"error": _cc2_out("sanitization_key_collision", _depth + 1, _code)}
            out[key] = _cc2_out(v, _depth + 1, _code)
        return out
    if isinstance(value, (list, tuple, set, frozenset)) or isinstance(value, Iterable):
        return [_cc2_out(v, _depth + 1, _code) for v in list(value)[:_CC2_LIST_MAX]]
    return None


def _cc2_err(e: PrinterError) -> dict:
    """A printer failure in the shape every printer tool speaks ({error, message, hint, ...}),
    sanitised: no credentials and no raw replies — the adapters keep both out of their messages
    by construction, and this is the belt."""
    return _cc2_out(e.as_dict())


class _Cc2Session:
    """One tool call's borrow of the shared session: the open transport once it exists, the password
    it was met with, and the sanitised refusal when there was no session to borrow. Given back by
    _cc2_session on every path — not closed, because another call may be using it; see _CC2_POOL."""
    __slots__ = ("error", "password", "transport")

    def __init__(self) -> None:
        self.transport = None
        self.password = None
        self.error: dict | None = None


async def _cc2_open_transport(config: _cc2_transport.Cc2Config):
    """Open one **fresh** registered CC2 session; return ``(transport, password)``, the very credential
    the MQTT handshake was met with, so an upload in the same session reuses the credential discovery
    chose instead of re-guessing it. The password is captured by decorating the production link with
    the transport's own factory — the address is still resolved and pinned once inside open_transport,
    and nothing here opens a second lookup. This is also the single seam tests use to stand a scripted
    fake session in place of a live printer, and the pool's seam too: reuse lives in _CC2_POOL, so what
    reaches here really is a new registration. Never print or return the password; it lives only in the
    returned object's headers and in the pool's memory from here on."""
    seen: dict = {}

    def record(host, password, client_id):
        seen["password"] = password
        return _cc2_transport._aiomqtt_link(host, password, client_id)

    transport = await _cc2_transport.open_transport(config, link_factory=record)
    return transport, seen.get("password")


# The process's one CC2 session. Every CC2 tool call borrows it, because the printer's broker takes
# about four clients and hands a slot back slowly: registering per call meant the fourth ordinary call
# in a row was refused by an idle printer, and each refused call paid another registration to clean up.
# The config is re-read on every borrow, so an address or code that moved between calls ends the
# session instead of being talked through. Two MCP server processes still mean two sessions — see the
# module's own note on what is not shared. The lambda is not decoration: it keeps the seam looked up
# in this module at call time, so a test standing a scripted printer in `_cc2_open_transport` is
# believed by the pool too instead of being bypassed by a reference bound at import.
_CC2_POOL = _cc2_pool.Cc2SessionPool(_cc2_transport.load_config,
                                     lambda config: _cc2_open_transport(config))


@asynccontextmanager
async def _cc2_session():
    """Borrow the shared CC2 session, run the tool body, give it back on every path. When there is no
    session to borrow and none could be opened the session carries the sanitised refusal in .error, and
    no command was ever sent. Giving a session back is not closing it: what closes is the pool, when a
    session reports itself dead, when the configuration changes, when it has been idle past
    _cc2_pool.IDLE_CLOSE_S with nobody holding it, or when the server shuts down."""
    sess = _Cc2Session()
    handle: _cc2_pool.PooledSession | None = None
    try:
        try:
            handle = await _CC2_POOL.acquire()
        except PrinterError as e:
            sess.error = _cc2_err(e)
            if sess.error.get("error") == "not_configured":
                # The one fact a caller must not have to infer from a config error: this refusal
                # happened in this process, on the config check, before any address was resolved and
                # before any socket was opened. Every other failure in this module could only ever
                # mean a network attempt, so this is the field that says it did not happen.
                sess.error["printer_contacted"] = False
        else:
            sess.transport, sess.password = handle.transport, handle.password
        yield sess
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):  # cleanup never masks the failure it followed
                await handle.release()


def _cc2_tray_word(tray) -> str:
    return {"empty": "empty", "present": "present", "active": "active"}.get(tray.state.value,
                                                                            "unknown")


def _cc2_peripheral_words(m) -> dict:
    """What the printer says about its own light and print speed mode — the two read-backs this
    firmware actually reports (1002 ``led.status`` and ``gcode_move_inf.speed_mode``). Both are
    None when the printer did not report them: an absent reading is never turned into the other
    value, and the speed mode in particular is never defaulted to the protocol's "normal" mode.
    ``speed_name`` is the doc's name for a number in the table and None for any other number, which
    is reported as the number it is."""
    light_on, led_raw = _cc2_peripherals.light_word(m)
    mode, name = _cc2_peripherals.speed_word(m)
    return {"light_on": light_on, "led_status": led_raw, "speed_mode": mode, "speed_name": name}


def _cc2_status_reply(status, transport) -> dict:
    """A status reply built from the parsed, fail-closed state only — machine and canvas fields
    the adapters verified, named on purpose; the raw 1002/2005 replies are never carried."""
    m, c = status.machine, status.canvas
    ok, why = status.ready_for_job()
    known = (m.machine_status or 0) > 0     # null/absent machine_status is unknown, never 0/'idle'
    ready: dict = {"ready_for_job": ok}
    if why:
        ready["why_not"] = why
    job: dict = {"state": m.raw_print_state or "idle",
                 "state_known": m.print_state.value != "unknown"}
    if m.job_filename:
        job["filename"] = m.job_filename
    for k, v in (("current_layer", m.current_layer), ("total_layer", m.total_layer),
                 ("progress", m.progress)):
        if v is not None:
            job[k] = v
    if m.state_message:
        job["message"] = m.state_message
    trays = []
    for t in c.trays:
        entry = {"tray_id": t.tray_id, "canvas_id": t.canvas_id, "state": _cc2_tray_word(t),
                 "raw_state": t.raw_state, "may_print_from_it": t.may_run_job, "active": t.active,
                 "material": t.material, "brand": t.brand, "name": t.name, "color": t.color,
                 "min_nozzle_temp_c": t.min_nozzle_temp, "max_nozzle_temp_c": t.max_nozzle_temp}
        trays.append({k: v for k, v in entry.items() if v is not None})
    return {
        "printer": {"serial": transport.sn,
                    "display": getattr(transport, "display", None)},
        "machine": {"status": m.machine_status if known else None,
                    "state_word": _CC2_MACHINE_WORDS.get(m.machine_status, "unknown")
                    if known else "unknown"},
        "sub_status": m.sub_status,
        "error": m.error,
        "job": job,
        "temps": {"nozzle_c": m.nozzle_temp, "nozzle_target_c": m.nozzle_target,
                  "bed_c": m.bed_temp, "bed_target_c": m.bed_target},
        "peripherals": _cc2_peripheral_words(m),
        "canvas": {"connected": c.connected, "active_canvas_id": c.active_canvas_id,
                   "active_tray_id": c.active_tray_id, "auto_refill": c.auto_refill,
                   "trays": trays},
        "ready": ready,
    }


def _cc2_page_args(offset: Any, limit: Any) -> tuple[int, int]:
    """(offset, limit) checked before a session opens, because a bad page number is a caller mistake
    and no printer should be asked about it.

    The bound on ``limit`` is ``_CC2_FILES_MAX``, which is ``_cc2_out``'s own list cap: a larger
    number would be cut to it on the way out, so it is refused here by name rather than answered as
    a page that quietly arrives short. A page of at most that many is the most one reply can carry,
    which is why there is no "give me everything" spelling of ``limit`` — ``offset`` is how the rest
    is fetched, and the reply says so with its own next offset."""
    for name, value in (("offset", offset), ("limit", limit)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise PrinterError(
                "protocol_error",
                f"cc2_list_files {name} must be a whole number, not {str(value)[:40]!r}.",
                hint=f"Pass a plain integer for {name}: offset=0 starts at the first file, and "
                     f"limit=<files> is at most {_CC2_FILES_MAX}.", reason="page_bounds")
    if offset < 0:
        raise PrinterError("protocol_error",
                           f"cc2_list_files offset must not be negative (got {offset}).",
                           hint="offset counts files to skip, so 0 is the first file. To read the "
                                "next page, pass the reply's next_offset.", reason="page_bounds")
    if limit < 1:
        raise PrinterError("protocol_error",
                           f"cc2_list_files limit must be at least 1 file (got {limit}).",
                           hint=f"limit is how many files this reply carries, from 1 up to "
                                f"{_CC2_FILES_MAX}; the reply names the next offset for the rest.",
                           reason="page_bounds")
    if limit > _CC2_FILES_MAX:
        raise PrinterError("protocol_error",
                           f"cc2_list_files limit must be at most {_CC2_FILES_MAX} files (got "
                           f"{limit}): one reply cannot carry more than that.",
                           hint=f"Ask for limit<={_CC2_FILES_MAX} and follow the reply's next_offset "
                                f"(offset={_CC2_FILES_MAX} is the second page) until has_more is "
                                "false. Nothing is withheld without saying so.",
                           reason="page_bounds")
    return offset, limit


async def _cc2_files_reply(transport, offset: int = 0, limit: int = _CC2_FILES_MAX) -> dict:
    """One bounded page of a listing the printer has been made to account for in full.

    Two facts stay two keys, because they are two different claims. The printer is still asked for a
    complete listing and ``_cc2_state.list_files`` still refuses to certify a partial one — that is
    ``inventory_complete``, and no page of this reply changes it. ``complete`` is this reply's own
    claim: true only when the page in hand IS the whole listing, so a page of 200 files of an
    inventory of 212 says ``complete: false``, ``has_more: true`` and ``next_offset: 200`` instead of
    the old reply's ``complete: true`` beside a truncation flag nobody could act on. Nothing is ever
    withheld without its count and the exact offset that fetches the rest.

    Every page re-reads and re-certifies the listing: nothing here is cached, because a stale listing
    that swore a file was absent is worse than one extra read, and paging a printer listing is
    honest-but-not-free in exactly the way it says in the tool's own text."""
    inventory = await _cc2_state.list_files(transport)  # refuses incomplete listings
    page = inventory.files[offset:offset + limit]
    files = []
    for f in page:
        entry: dict = {"name": f.filename, "size_bytes": f.size}
        if f.layers is not None:
            entry["layers"] = f.layers
        if f.tools:
            entry["tools"] = list(f.tools)
        if f.created is not None:
            entry["created"] = f.created.isoformat()
        if f.print_seconds is not None:
            entry["est_print_s"] = f.print_seconds
        if f.filament_grams is not None:
            entry["est_filament_g"] = f.filament_grams
        files.append(entry)
    end = offset + len(files)
    has_more = end < inventory.count
    out = {"storage": inventory.storage,
           "complete": not has_more and offset == 0,
           "inventory_complete": True,
           "total": inventory.total,
           "offset": offset,
           "limit": limit,
           "returned": len(files),
           "has_more": has_more,
           "next_offset": end if has_more else None,
           "truncated_in_reply": has_more,
           "pages_certified": inventory.pages,
           "files": files}
    if has_more:
        out["note"] = (f"{inventory.count - end} of {inventory.total} certified files are not in "
                       f"this reply; ask again with offset={end} (limit up to {_CC2_FILES_MAX})")
    return out


def _cc2_tool_map(tool_to_tray):
    """The one-tool mapping, as whole numbers. JSON objects carry string keys, so the tool index
    and the tray number normally arrive as text like {"0": "3"}; surrounding spaces and an
    optional '#' are tolerated, and any other spelling refuses with the exact words the caller
    needs. Ambiguity is refused, never guessed at — silently falling back to tray 0 is the
    measured failure mode this adapter documents, so no partial mapping ever reaches it."""
    err = PrinterError(
        "protocol_error",
        'tool_to_tray must name exactly one tool and one tray, e.g. {"0": "3"} for tool 0 '
        f"printing from tray 3 — got {str(tool_to_tray)[:120]!r}. Nothing was sent.",
        hint="Pass one entry: the file's tool index (from cc2_upload_gcode) mapped to the tray "
             "id (from cc2_status). Plain whole numbers or their decimal text both read; "
             "nothing else does.", reason="tool_map")

    def one(value) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str):
            s = value.strip().lstrip("#").strip()
            if s.isdigit():
                return int(s)
        return None

    if not isinstance(tool_to_tray, dict) or len(tool_to_tray) != 1:
        raise err
    (raw_tool, raw_tray), = tool_to_tray.items()
    tool, tray = one(raw_tool), one(raw_tray)
    if tool is None or tray is None:
        raise err
    return {tool: tray}


@mcp.tool()
async def cc2_status() -> dict:
    """Direct state of the configured Elegoo Centauri Carbon 2: machine state, the current job,
    nozzle and bed temperatures, and every CANVAS tray (state, material, colour, rated nozzle
    range) from one fresh read. It sends status requests only, never commands. With no printer address
    configured (ORCA_CC2_URL) it reports not_configured and opens no connection; set the address and
    this is the tool that says whether the printer is idle and which tray can feed a print.

    It also reports the overhead light and the print speed mode when the printer states them (1002
    led.status and gcode_move_inf.speed_mode) and leaves both null when it does not: an absent
    reading is never turned into "off" or into the protocol's default mode. A light is on above
    zero, since the protocol documents led as 0/1 on some firmware and a 0-255 brightness on others.

    Tray state: empty has no filament, present is loaded, active is loaded and the active spool —
    active is available, never treated as empty. When data is missing, stale or unknown the tool
    says so instead of guessing, and may_print_from_it is false. Use it to pick a tray for
    cc2_start_print and to check the printer is idle first; for what is on the printer's disk use
    cc2_list_files."""
    async def run(transport):
        return _cc2_status_reply(await _cc2_state.fetch_status(transport), transport)
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            out = await run(sess.transport)
        except PrinterError as e:      # an unreadable reply is a refusal, told the same way
            return _cc2_err(e)
        return _cc2_out(out)


@mcp.tool()
async def cc2_list_files(
    offset: Annotated[int, Field(description=(
        "Files of the certified inventory to skip, 0 for the first page. To read the rest of a "
        "listing, pass the previous reply's next_offset unchanged. A negative or non-integer "
        "offset refuses before any connection is opened."))] = 0,
    limit: Annotated[int, Field(description=(
        f"Files this reply carries, 1 to {_CC2_FILES_MAX}; the default is the maximum, which is "
        "the most one reply can hold. A larger limit refuses with that cap and the offset to page "
        "with — it never answers as a short page that quietly dropped the rest."))] = _CC2_FILES_MAX,
) -> dict:
    """A bounded page of the G-code inventory of the configured CC2 printer, or an explicit failure.
    It never reports a first page as a complete listing and it never cuts a reply without saying so.
    It sends no commands, and with no printer address configured (ORCA_CC2_URL) it reports
    not_configured and opens no connection.

    The printer is always asked to account for every file it holds — a listing it cannot prove
    complete comes back as an error naming the numbers, never as a partial list, and uploads and
    starts refuse incomplete listings too — and that certified inventory is then paged in the reply,
    because one reply cannot carry an unlimited number of names. ``offset`` (default 0) says where in
    the certified inventory this reply starts and ``limit`` (default and maximum 200 files) how many
    it carries; a limit above 200 refuses by name rather than arriving short, and a bad offset or
    limit refuses before any connection is opened.

    Read the three counts as three facts. ``total`` and ``inventory_complete`` are the printer's:
    how many files it holds, and that every one of them was read and certified. ``complete`` is this
    reply's own, and it is true only when the page in hand IS the whole listing — so the second page
    of a big shelf says ``complete: false`` even though the inventory is certified, and so does the
    last page, which is why it is not the paging stop. When ``has_more`` is true, ``next_offset`` is
    the exact argument that fetches what is missing and ``note`` says how many were left out; keep
    calling with ``offset=next_offset`` until ``has_more`` is false, which is the end of the shelf.
    ``returned`` is what this page holds. Asking for a later page re-reads and re-certifies
    the listing (nothing is cached), so paging is honest but costs one certification per page.

    Each file: name, size in bytes, and when the printer reported them, layers, estimated time
    and filament."""
    try:
        page_offset, page_limit = _cc2_page_args(offset, limit)
    except PrinterError as e:          # a bad page number is refused before a session exists
        return _cc2_err(e)
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            out = await _cc2_files_reply(sess.transport, page_offset, page_limit)
        except PrinterError as e:      # an incomplete listing is reported, never half-listed
            return _cc2_err(e)
        return _cc2_out(out)


@mcp.tool()
async def cc2_printer_info() -> dict:
    """What the configured CC2 printer says about itself: model, the name its touchscreen carries,
    serial, firmware versions, hardware and protocol versions, build volume (xyz_size) and resolution,
    network type, remaining memory, and whether a camera or a USB drive is attached. It is one
    read-only method (1001, which the protocol spells with no parameters at all), asked exactly once;
    it sends no commands, and with no printer address configured (ORCA_CC2_URL) it reports
    not_configured and opens no connection.

    This is where a firmware version comes from: the protocol's `software_version.ota_version` is the
    one the printer reports for the app update channel, and nothing else in this server reads it. Read
    the reply with its `not_reported` list, because nothing here fills a gap in: every field the
    printer did not answer is null *and* named there, since "it said nothing" and "it has no camera"
    are different facts — this firmware is measured answering two of its three version strings empty,
    and an empty string is not a version. The serial the printer names is compared with the serial
    this session connected and pinned; if they disagree the reply says so under `identity`, because
    the answer may then be describing a different machine than the one you configured.

    What is deliberately absent: the printer's `ip` and `mac`. Nothing here needs them — the address is
    the one you configured, and a MAC is a permanent hardware identifier with no job in these tools.
    For live state use cc2_status; for what is on the printer's disk use cc2_list_files; for one file,
    cc2_file_detail."""
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            attrs = await _cc2_info.fetch_attributes(sess.transport)
        except PrinterError as e:      # an unreadable reply is a refusal, told the same way
            return _cc2_err(e)
        return _cc2_out(attrs.as_dict())


_FILE_ARG = (
    "The file's name on the printer, exactly as cc2_list_files printed it: a plain name, case and "
    "all, with no path and no directory. A name carrying a path separator, a control character or "
    "leading or trailing space is refused before anything is sent, and the printer's own names are "
    "never rewritten, case-folded or trimmed on the way to it.")


@mcp.tool()
async def cc2_file_detail(
    filename: Annotated[str, Field(description=_FILE_ARG)],
) -> dict:
    """One file on the configured CC2 printer, read from the printer itself (method 1046, asked once):
    its size, total layers, the slicer's estimated print seconds and filament grams, when it was
    created, how many times it has printed and when last, and the per-tool colours it was sliced with,
    where each entry's `tool` is the G-code tool index (T0, T1) and never a CANVAS tray, which is a
    confusion the protocol warns about twice. It sends no commands; with no printer address configured
    (ORCA_CC2_URL) it reports not_configured and opens no connection.

    The reply uses the same words cc2_list_files uses for the same facts, plus what only a detail read
    can say. Nothing is assumed to fill a gap: fields the printer did not answer are null and named in
    `not_reported`. Two honest cases are worth knowing before a model reads one. The layer count
    reaches this firmware under more than one field name in the wild, so the reply carries
    `layers_from_field` (the name that answered) and, if two names answer with different numbers,
    `layers_conflict` and a warning instead of an average. And if the printer answers about a different
    file than the one asked for, `name_matches_request` is false and a warning says so, because a
    detail read is only about your file if the printer said which file it read.

    The printer's refusals come back as its own: a nonzero `error_code` is kept with its number and
    never renamed "file not found", because nobody has established what this firmware's per-file codes
    mean, and a request is never sent a second time to find out. A file's contents cannot be read back
    on stock firmware by any method — this is metadata, and the picture is cc2_file_thumbnail."""
    try:
        name = _cc2_info.checked_lookup_name(filename)
    except PrinterError as e:
        return _cc2_err(e)
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            detail = await _cc2_info.fetch_file_detail(sess.transport, name)
        except PrinterError as e:      # a refusal keeps the printer's code; nothing was retried
            return _cc2_err(e)
        return _cc2_out(detail.as_dict())


@mcp.tool()
async def cc2_file_thumbnail(
    filename: Annotated[str, Field(description=_FILE_ARG)],
):
    """The preview image the printer holds for one of its files (method 1045, asked once), returned as
    an MCP image block plus one line of text naming the file, its byte count and the image type. The
    picture is what the printer itself shows in its app for that file, so it is the cheapest way to see
    what a job on the printer actually is. No commands, and with no printer address configured
    (ORCA_CC2_URL) it reports not_configured and opens no connection.

    Three things about this method are unusual and worth knowing. The image is the printer's own
    embedded preview, so a file sliced without one simply has none — that comes back as a plain
    sentence saying the printer had no thumbnail for it, never as a blank image, and the file is fine:
    cc2_file_detail still reports its metadata. The image type is read from the bytes' own signature
    (png, jpeg, gif, webp) rather than assumed, because the printer's own web interface stamps every
    thumbnail image/png whatever it is holding; bytes of a type this server has not established are
    refused with their first bytes named rather than sent on under a guessed type.

    And size: the image travels inline in one tool reply, so a decoded image over 2 MiB is refused
    with its size named and never truncated to fit — a
    half an image is a corrupt image, and the limit is about the conversation, not the printer. The
    picture is the printer's own bytes, handed over as it sent them: nothing here inspects, re-encodes
    or vouches for what is drawn in it. Nothing is asked of the printer twice: a lost or unreadable
    reply is reported as itself."""
    try:
        name = _cc2_info.checked_lookup_name(filename)
    except PrinterError as e:
        return _cc2_err(e)
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            shot = await _cc2_info.fetch_file_thumbnail(sess.transport, name)
        except PrinterError as e:      # no thumbnail, a refusal, a bad reply: each says which
            return _cc2_err(e)
        # The image goes to the client as an image; the text beside it is the sanitised metadata a
        # caller needs even where a client renders nothing. shot.kind is one of the four types this
        # module's own signature table names, so the mime type is a fact and not a guess.
        meta = _cc2_out(shot.as_dict())
        return [Image(data=shot.data, format=shot.kind),
                TextContent(type="text", text=(
                    f"{meta.get('filename')}: {meta.get('bytes')} bytes of "
                    f"{meta.get('image_kind')} preview, read from the printer's own file store "
                    f"(method {meta.get('method')})."))]


@mcp.tool()
async def cc2_print_history() -> dict:
    """The print history the configured CC2 printer keeps for itself, read from the printer (method
    1036, asked exactly once with no parameters at all). Each entry is one print: its name, the
    printer's own status number alongside the word the printer's own app paints for that number, when
    it began and ended, how long it ran, and whether a time-lapse exists. It sends no commands, never
    removes a history record, and with no printer address configured (ORCA_CC2_URL) it reports
    not_configured and opens no connection.

    Read its `unverified` field before believing a success. This is the one CC2 read whose response
    shape has never been captured from this printer: the fields it reads are exactly the fields
    Elegoo's own LAN web client reads out of `history_task_list` when it sends method 1036 with no
    parameters, and its duration is the same subtraction that client performs. A reply shaped any
    differently is refused rather than fitted to that shape, so a refusal from this tool is evidence
    about this firmware, not a broken printer, and it is worth recording.

    Three claims it deliberately does not make. It never says a list is complete unless the printer's
    own `total` meets the entries it actually sent, so `complete` comes with `completeness` saying why
    it is true or false; because the request carries no paging, a long history is capped at 100
    entries and `truncated_in_reply` then says so loudly, with both counts, and there is no next page
    to ask for (1038 removes history entries and this server does not send it). The status word is
    labelled as the app's vocabulary, not a protocol's, and the raw number is always carried too — two
    of the app's numbers share one word. And entries stay in the printer's own order: nothing here
    sorts, filters or de-duplicates what it sent.

    Method 1037 (one print's detail) is not offered, and neither is 1038 (deleting history). 1037 has
    no published request or response anywhere in the official sources — its name appears in Elegoo's
    own web bundle only in the enum that defines it, never called — so there is nothing reliable to
    read, and this tool will not take a `uuid` from a live status frame and send it somewhere as a task
    handle. A time-lapse link that carries a login in it is withheld rather than echoed: this server
    repeats no credentials, not even one a printer handed over.

    For what is on the printer now use cc2_list_files; for live state cc2_status."""
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            history = await _cc2_history.fetch_history(sess.transport)
        except PrinterError as e:      # refused, unreadable or not the LAN shape: each says which
            return _cc2_err(e)
        return _cc2_out(history.as_dict())


@mcp.tool()
async def cc2_camera_snapshot():
    """One picture from the configured CC2 printer's own camera, returned as an MCP image block with its
    metadata beside it as text: a summary line naming its size, its type and where it came from, carrying
    the single-viewer warning with it, and the sanitised metadata the adapter built as JSON. Exactly one
    bounded HTTP GET of the stream at the configured printer's address, and the first complete JPEG frame
    found in it. It sends the printer no command of any kind and opens no MQTT session, so a picture neither
    starts nor stops anything, and takes none of the printer's limited client slots.

    The address is **composed here, never advertised by the printer**, and that choice is deliberate. The
    printer's own camera-address method is still a candidate rather than a tool: no reply of it has ever
    been captured from this firmware, and the community sources describe it as a *toggle*, so asking it
    could switch a stream on or off and nobody has settled which. Nothing on this path ever asks it
    anything, never sends an enable flag, and every reply says that a picture from the stream proves a
    stream answered and says nothing at all about that method in either direction. What is used instead is
    the address the protocol manual, the Home Assistant integration and this printer's owner agree on: the
    configured host (ORCA_CC2_URL, never hardcoded here) plus the camera's port and stream query. That host
    goes through the transport's own resolve-and-pin step and this module's own guard, so it must be an IP
    literal and a private LAN or loopback one — the same refusal that gates MQTT, with no resolver left
    standing between the policy decision and the socket, no proxy honoured from the environment, and no
    redirect followed (a 3xx is a refusal naming its status, not a hop somewhere unjudged).

    Because a stream is a reply that never ends, the fetch is bounded in four places and none of them is
    quiet: one request, never a second one after a timeout or a failure; a 10 second window measured from
    the moment the request starts, enforced hard enough that a stream trickling one byte a second cannot
    outrun it; at most 8 MiB consumed while hunting for a frame, counted **before** a chunk is appended or
    accepted so a frame that only becomes reachable past that ceiling is refused with the ceiling named
    rather than kept; and the kept frame capped at 2 MiB, the
    same ceiling one image reply carries elsewhere on this server, refused with its size named and **never
    truncated to fit** — half a JPEG is a corrupt picture, not a small one. Under 128 bytes between its
    own start and end markers a frame is a marker pair rather than a picture and is refused as that.

    What is skipped is as deliberate as what is kept. The first bytes off a live stream are usually the
    tail of a frame the camera began before this server connected, and that tail is discarded rather than
    reported as a truncated picture, so a snapshot here is always a whole frame or an honest refusal. The
    hunt walks the frame's own segments instead of grabbing at the first end-of-image bytes it sees,
    because an EXIF block can carry a nested thumbnail that ends with a marker of its own, and a naive
    search would cut the real frame in half inside that block and hand the halves over as if they were
    fine. The walk does not stop at the first scan either: a multi-scan (progressive) frame carries its
    later tables and scans between its start and its end, and any of those segments' payloads can hold
    end-of-image bytes of their own, so the hunt honours each segment's declared length and stops only at
    the frame's own end marker. A marker that cannot legally appear where a scan's data should still be
    running is a refused candidate, never a frame with a hole in it. The type comes from the bytes' own
    signature, as it does for `cc2_file_thumbnail`.

    Two things a picture is not. The protocol manual describes this camera stream as usually allowing a
    **single viewer**, so opening it can be refused or can displace whoever was already watching — which
    is said in the reply instead of being found out by asking twice. And this is the room the camera
    sees, not the print: nothing here decodes, inspects or vouches for what is drawn in it, and it is not
    a way to watch a bed. With no printer address configured (ORCA_CC2_URL) it reports `not_configured`
    with `printer_contacted: false` and contacts nothing at all.

    For what the printer says about itself use `cc2_status`; for its files `cc2_list_files`; for what a
    file looks like to the printer's own app `cc2_file_thumbnail`."""
    try:
        shot = await _cc2_snapshot.capture_snapshot(http_factory=_cc2_snapshot_http_factory())
    except PrinterError as e:      # no stream, a refusal, no frame: each says which, none was retried
        out = _cc2_err(e)
        if out.get("error") == "not_configured":
            # Same fact the session helper states for every other CC2 tool, and just as true here: this
            # refusal was made in this process from the configuration, before any address was resolved and
            # before any socket was opened.
            out["printer_contacted"] = False
        return out
    # The image goes to the client as an image, and the metadata the adapter built goes with it: the first
    # text block is the line a person or a client that renders nothing reads (its size, its type, where it
    # came from, and the single-viewer warning stated rather than discovered), and the second is the whole
    # sanitised metadata as JSON, so nothing the adapter vouched for is quietly dropped on the way out. All
    # three blocks are what an MCP client actually receives. shot.kind is read from the bytes' own signature
    # by this adapter, so the image type is a fact about the bytes and not a label applied to them.
    meta = _cc2_out(shot.as_dict())
    warnings = [w for w in (meta.get("warnings") or []) if isinstance(w, str) and w]
    lead_warning = warnings[0] if warnings else (
        "no warning was carried by this reply: the adapter's own warnings list came back empty")
    summary = (f"{meta.get('bytes')} bytes of {meta.get('image_kind')} from the CC2 camera at "
               f"{meta.get('stream_host')}: the first complete frame of its stream, read once and "
               f"released. No printer command was sent and no camera-address method was asked. "
               f"WARNING: {lead_warning}")
    return [Image(data=shot.data, format=shot.kind),
            TextContent(type="text", text=summary),
            TextContent(type="text", text="cc2_camera_snapshot metadata (JSON): "
                                          + json.dumps(meta, sort_keys=True))]


@mcp.tool()
async def cc2_upload_gcode(
    path: Annotated[str, Field(description=(
        "Absolute path of the local sliced G-code file to upload, on the machine running this "
        "MCP server — typically the file save_gcode returned. The file is read, never modified, "
        "and its path is not echoed back in the reply."))],
    destination: Annotated[str, Field(description=(
        "New basename for the file on the printer: a plain name ending in .gcode, e.g. "
        "benchy-petg-2026-11-04.gcode. Never a path, and never an existing file's name — this "
        "tool does not overwrite."))],
) -> dict:
    """Upload one sliced G-code file directly to the configured CC2 printer under a NEW name,
    in 1 MiB chunks, and verify it on the printer's complete listing before writing the private
    receipt that cc2_start_print requires. This writes to the printer, so running it is a human
    decision; with no printer address configured (ORCA_CC2_URL) it reports not_configured and sends
    nothing. Not idempotent: a failed, refused or ambiguous upload is never automatically
    retried, and a partial file may sit on the printer under this name (no CC2 delete has been
    proven to work on this printer, so this tool never deletes) — the error says so, and choosing a
    fresh name is a human decision.

    The file is read as evidence: its material, first-layer and steady nozzle and bed
    temperatures, and its single tool index come from the bytes actually sent (a non-extruding
    preheat is fine; cold or unverified extrusion, multi-tool files, and files whose config and
    commands disagree are refused before anything is sent). One file tool only. The reply lists
    the verified name and size and what the file's own bytes said; tool_to_tray for
    cc2_start_print maps the reported tool_index to the tray chosen from cc2_status."""
    try:
        dest = _cc2_upload.validate_destination(destination)
    except PrinterError as e:
        return _cc2_err(e)
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            # The upload token is the very credential the MQTT session was met with — the same
            # one discovery chose, never a re-guess, and it never leaves the request headers.
            receipt = await _cc2_upload.upload_gcode(
                sess.transport, path, dest, token=sess.password,
                http_factory=_cc2_upload_http_factory())
        except PrinterError as e:
            return _cc2_err(e)
    out = {
        "uploaded": receipt.remote_filename,
        "size_bytes": receipt.reported_size_bytes,
        "content_sha256": receipt.content_sha256,
        "material": receipt.material,
        "tool_index": receipt.tool_index,
        "nozzle_temp_c": {"first_layer": receipt.nozzle_first_layer_temp,
                          "steady": receipt.nozzle_temp},
        "bed_temp_c": {"first_layer": receipt.bed_first_layer_temp,
                       "steady": receipt.bed_temp},
        "extrusion_nozzle_temps_c": sorted(receipt.extrusion_nozzle_temps),
        "commanded_nozzle_setpoints_c": sorted(set(receipt.commanded_nozzle_setpoints)),
        "uploaded_at": receipt.uploaded_at,
        "next": ("start it with cc2_start_print: filename exactly as uploaded, tool_to_tray "
                 "mapping the tool_index above to a tray from cc2_status, and bed_clear=true "
                 "only after a human has looked at the bed."),
    }
    return _cc2_out(out)


@mcp.tool()
async def cc2_start_print(
    filename: Annotated[str, Field(description=(
        "The exact uploaded basename to start, as cc2_upload_gcode reported it. Starts refuse "
        "without this workflow's private receipt for that printer and name — a file that reached "
        "the printer any other way cannot be started from here."))],
    tool_to_tray: Annotated[dict[str, str], Field(description=(
        'Exactly one entry mapping the file\'s single tool index to the tray that must feed it, '
        'e.g. {"0": "3"} for tool 0 printing from tray 3. Tool index from cc2_upload_gcode, '
        'tray id from cc2_status; the tray must report the same material the file was sliced '
        'with. Multi-tool starts are not supported.'))],
    bed_clear: Annotated[bool, Field(description=(
        "Physical bed-clear assertion: true only after a human has looked at the build plate "
        "and taken anything off it. Only true starts a print; false refuses."))],
    leveling: Annotated[bool, Field(description=(
        "Bed leveling for THIS print: true sends printer_check=true; false — the default — "
        "sends printer_check=false explicitly, because the printer remembers the last value."))] = False,
    observe_running: Annotated[bool, Field(description=(
        "After an accepted start, take one read-only look at the printer and report whether it "
        "is observed printing this exact file. Default true; false returns acceptance only."))] = True,
    bed_warning: Annotated[str | None, Field(description=(
        "OPTIONAL, normally omit: a slicer bed warning for this exact file that YOU read from "
        "the same slice, carried through verbatim so the start result keeps it. The server "
        "cannot verify it belongs to the uploaded file and never attaches slicer warnings on its "
        "own; when absent, the result says the bed warning is unavailable."))] = None,
) -> dict:
    """Start one already-uploaded file on the configured CC2 printer. Send exactly one start
    command only after every check passes, and never retry. This is a write that moves the machine,
    so it is a human decision to call: with no printer address configured (ORCA_CC2_URL) it reports
    not_configured and sends nothing. A write, and not idempotent: an accepted start
    moves the machine, and a lost answer is reported as ambiguous, never resent.

    Before the command leaves, re-read inside the per-printer lock: the private upload receipt,
    the printer's complete listing naming this file at the receipted size, idle with no error
    and no file held, and the mapped tray present or active on a connected canvas carrying the
    same material with a rated nozzle range covering every temperature the file extrudes at.
    Any failed check refuses before the printer is asked, naming the check. Bed leveling is off
    by default and sent explicitly either way; pass leveling=true only when it was requested
    for this print. The reply keeps accepted (the printer did not refuse) and observed_running
    (its status names this file as printing) strictly apart — acceptance alone never claims a
    print is running. A slicer bed warning is never invented or attached here: unless the
    caller carried one from the same slice, the reply says the warning is unavailable."""
    if not isinstance(leveling, bool) or not isinstance(observe_running, bool):
        return {"error": "invalid_argument", "message": "leveling and observe_running must be booleans"}
    try:
        mapping = _cc2_tool_map(tool_to_tray)
    except PrinterError as e:
        return _cc2_err(e)
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            result = await _cc2_start.start_print(
                sess.transport, filename, mapping, bed_clear=bed_clear,
                printer_check=leveling,
                observe_running=observe_running, bed_warning=bed_warning)
        except PrinterError as e:
            return _cc2_err(e)
    out = result.as_dict()
    if bed_warning:
        out["bed_warning_provenance"] = (
            "carried_from_slice_unverified: the caller states this warning came from the same "
            "slice as this file; this server cannot verify that, and a valid slice never proves "
            "a warning is resolved")
    else:
        out["bed_warning_provenance"] = (
            "unavailable: no slicer bed warning was carried, and this server cannot verify any "
            "slicer warning belongs to this uploaded file — bed warnings are never attached to "
            "an arbitrary file, and the G-code carries no warning marker this adapter reads")
    return _cc2_out(out)


async def _cc2_control_tool(tool: str, action: str, confirm: bool) -> dict:
    """The one body the three job-control tools share. Like the other direct tools it can only reach
    a printer once one is configured; with no address set the session seam refuses in-process, before
    any method of any kind is sent.

    Two-phase like the maintainer's Klipper MCP companion, and enforced here rather than hoped for
    in the client because a client can run with permission prompts off: a call that is not exactly
    confirm=True borrows the shared CC2 session and sends exactly one read-only 1002, and
    hands back the confirm_required preview — no control method ever leaves the wire on a preview path, which the
    tests prove by watching the scripted wire. A confirmed call sends the one control method, once,
    through the adapter — which never resends an unanswered command — and reports its honest result.
    The confirmation is a decision-for-a-human device, not an access control: it exists so the call
    carries the user's authorization for this action — which they may well have given already, since
    their standing yes for this print or session is enough, and no fresh question is owed to the
    tool — and the preview is what reaches them when nothing authorizes it yet."""
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            if confirm is True:
                out = (await _cc2_control.control_job(sess.transport, action)).as_dict()
            else:
                out = await _cc2_control.read_preview(sess.transport, action)
        except PrinterError as e:
            return _cc2_err(e)
        return _cc2_out(out)


@mcp.tool()
async def cc2_pause_print(
    confirm: Annotated[bool, Field(description=(
        "Second-call only: omitted or false returns a preview and sends nothing; exactly true sends "
        "the one pause command. Confirm once the user has authorized the pause: a yes they already "
        "gave for this print or this session covers it and needs no fresh question, so ask only when "
        "nothing authorizes it — read them the preview first then."))] = False,
) -> dict:
    """Pause the print on the configured CC2 printer. TWO-PHASE: without confirm=true this reads the
    printer and returns a preview (its current job read fresh, and the exact command a confirmed call
    would send) and changes nothing. Call it again with confirm=true once the user has authorized the
    pause: their existing explicit yes for this print or session is that authorization and needs no
    fresh question, so ask only when nothing authorizes it.

    A confirmed call sends one 1021 command, no params, and waits 60 s for the answer — the official
    Elegoo UI's own pause budget. The reply keeps three facts apart: accepted (the printer answered
    with an explicit error_code 0 — it did not refuse), refused (an explicit nonzero code kept;
    1010 is the documented 'no active print'), and ambiguous (no answer, an unreadable one, or a
    dead session). A timeout is never called a refusal and the command is never resent; after an
    accepted or unanswered command one read-only look reports what the printer says it is doing,
    apart from acceptance. Nothing here checks whether pausing makes sense right now — that is the
    human's read of the preview, and the printer itself refuses (1010) when there is nothing to
    pause."""
    return await _cc2_control_tool("cc2_pause_print", "pause", confirm)


@mcp.tool()
async def cc2_resume_print(
    confirm: Annotated[bool, Field(description=(
        "Second-call only: omitted or false returns a preview and sends nothing; exactly true sends "
        "the one resume command. Confirm once the user has authorized the resume: a yes they already "
        "gave for this print or this session covers it and needs no fresh question, so ask only when "
        "nothing authorizes it — read them the preview first then."))] = False,
) -> dict:
    """Resume the paused print on the configured CC2 printer. TWO-PHASE: without confirm=true this
    reads the printer and returns a preview (its current job read fresh, and the exact command a
    confirmed call would send) and changes nothing. Call it again with confirm=true once the user has
    authorized the resume: their existing explicit yes for this print or session is that
    authorization and needs no fresh question, so ask only when nothing authorizes it.

    Know what a resume costs: the printer is measured answering the resume command only once the
    resume has actually completed — it reheats, homes and returns to position first, 122 s to its
    error_code 0 answer on firmware 02.01.00.00 — so this call can take up to two full minutes.
    One 1023 command, no params, waited for 120 s (the official UI's own window, which the measured
    answer exceeded). An accepted resume is the printer's own completion word; an expired window is
    NOT a refusal and NOT a failure — the print may well be resuming right now — and the command is
    never resent. The result never requires the printer to report running: after an accepted or
    unanswered resume one read-only look says what the printer claims, kept strictly apart from
    acceptance. 1010 is the documented refusal when there is nothing to resume."""
    return await _cc2_control_tool("cc2_resume_print", "resume", confirm)


@mcp.tool()
async def cc2_stop_print(
    confirm: Annotated[bool, Field(description=(
        "Second-call only: omitted or false returns a preview and sends nothing; exactly true sends "
        "the one stop command. A yes for this print is not a yes for ending it early: confirm once "
        "the user has authorized the stop itself, and when nothing authorizes it yet, read them the "
        "preview and ask first."))] = False,
) -> dict:
    """Stop (cancel) the print on the configured CC2 printer, the irreversible one of the three: the
    documented machine state ends a stopped job at STOPPED and the protocol offers no way back, pause
    and resume being the reversible pair. TWO-PHASE: without confirm=true this reads the printer and
    returns a preview (its current job read fresh, and the exact command a confirmed call would send)
    and changes nothing. Call it again with confirm=true once the user has authorized ending this job:
    an existing explicit yes of theirs is that authorization, but a yes for the print is not a yes
    for stopping it early, so make sure what they authorized is the stop.

    A confirmed call sends one 1022 command, no params, and waits 60 s for the answer — the official
    Elegoo UI's own stop budget. Accepted (explicit error_code 0 — the printer did not refuse),
    refused (an explicit nonzero code kept; 1010 is the documented 'no active print') and ambiguous
    (no answer, an unreadable one, or a dead session) are kept apart; a timeout is never called a
    refusal and the command is never resent. After an accepted or unanswered command one read-only
    look reports what the printer says it is doing, apart from acceptance."""
    return await _cc2_control_tool("cc2_stop_print", "stop", confirm)


async def _cc2_peripheral_tool(setting: str, params: dict, confirm: bool) -> dict:
    """The one body the two peripheral tools share, and the same two phases the job controls run.
    Like every direct tool it can only reach a printer once one is configured; with no address set
    the session seam refuses in-process, before any method of any kind is sent.

    A call that is not exactly ``confirm=True`` borrows the shared CC2 session and sends exactly one
    read-only 1002, and hands back the ``confirm_required`` preview carrying this call's own exact
    params — so what
    a confirmed call would do is on the record before anything is changed, and no control method
    ever leaves the wire on a preview path (the tests watch the recorded wire for exactly that). A
    confirmed call sends the one command once through the adapter, which never resends an unanswered
    command. The confirmation is a decision-for-a-human device, not an access control: it carries the
    user's authorization for this action, which their standing yes for this print or session may
    already supply, so it obliges no fresh question of its own."""
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            if confirm is True:
                out = (await _cc2_peripherals.set_peripheral(sess.transport, setting,
                                                             params)).as_dict()
            else:
                out = await _cc2_peripherals.read_preview(sess.transport, setting, params)
        except PrinterError as e:
            return _cc2_err(e)
        return _cc2_out(out)


@mcp.tool()
async def cc2_set_light(
    on: Annotated[bool, Field(description=(
        "true turns the printer's overhead light on, false turns it off — a plain boolean, and the "
        "only thing it takes: the CC2 light is on or off in every source and does not dim, so a "
        "brightness level is not what this asks for. Anything the tool cannot read as exactly true "
        "or exactly false is refused with nothing sent."))],
    confirm: Annotated[bool, Field(description=(
        "Second-call only: omitted or false returns a preview and sends nothing; exactly true sends "
        "the one light command. Confirm once the user has authorized the light: a yes they already "
        "gave for this print or this session covers it and needs no fresh question, so ask only when "
        "nothing authorizes it — read them the preview first then."))] = False,
) -> dict:
    """Turn the configured CC2 printer's overhead light on or off. TWO-PHASE: without confirm=true
    this reads the printer once and returns a preview (what the printer says its light is doing now,
    and the exact command a confirmed call would send) and changes nothing; only an exact
    confirm=true sends it. The confirmation carries the user's authorization: a yes they already gave
    for this print or session is that authorization and needs no fresh question, so ask only when
    nothing authorizes it.

    One 1029 command, sent once and never resent, waited for 10 s. The CC2's own documentation and
    its own web client disagree about this parameter — the doc's table says brightness, the doc's own
    findings and the LAN client say power (1 on, 0 off) — so the command carries both spellings
    paired so they cannot contradict each other (power 1 with brightness 255, power 0 with
    brightness 0), which is what the Home Assistant integration sends for the same reason. Accepted
    (the printer answered with an explicit error_code 0), refused (an explicit code kept) and
    ambiguous (no answer, an unreadable one, or a dropped session) are kept apart; a timeout is never
    called a refusal and the command is never resent. After an accepted or unanswered command one
    read-only look reports what the printer says its light is doing now, apart from acceptance — the
    look is taken straight after the command, so what it can show is a status that has not caught up
    rather than a light that stayed off, and cc2_status is the way to ask again. If that look
    contradicts the command the printer accepted, a note says so plainly without doubting the answer.
    Nothing is pre-checked: whether the light should be on is a human judgement, and the printer
    refuses what it will not do with its own code."""
    try:
        want = _cc2_peripherals.parse_light(on)
    except PrinterError as e:
        return _cc2_err(e)
    return await _cc2_peripheral_tool("light", _cc2_peripherals.light_params(want), confirm)


@mcp.tool()
async def cc2_set_print_speed(
    mode: Annotated[int, Field(description=(
        "The printer's own speed MODE number, not a percentage: 0 Silent (50%), 1 Balanced (100%, "
        "the printer's default), 2 Sport (150%), 3 Ludicrous (200%). Any other number, a percentage "
        "or a name is refused with those four named and nothing sent."))],
    confirm: Annotated[bool, Field(description=(
        "Second-call only: omitted or false returns a preview and sends nothing; exactly true sends "
        "the one speed command. Confirm once the user has authorized the speed change — mid-print it "
        "changes how the rest of the job runs. A yes they already gave for this print or session "
        "covers it and needs no fresh question, so ask only when nothing authorizes it — read them "
        "the preview first then."))] = False,
) -> dict:
    """Set the print speed MODE on the configured CC2 printer: one of four discrete modes
    (0 Silent/50%, 1 Balanced/100% which is the printer's default, 2 Sport/150%, 3 Ludicrous/200%),
    never a percentage. Any other number, 45 and 150 included, refuses with the four modes named and
    nothing sent. TWO-PHASE: without confirm=true this reads the printer once and returns a preview
    (the mode the printer reports right now, and the exact command a confirmed call would send) and
    changes nothing; only an exact confirm=true sends it.

    There is no percentage on this protocol at all — the middleware turns Klipper's speed factor into
    that mode number, which is why a speed is never spelled in percent here, and why the Home
    Assistant integration's percentage-to-mode bucketing is an HA convenience rather than something
    this tool copies. The confirmation carries the user's authorization, which their standing yes for
    this print or session may already supply.

    One 1031 command carrying the mode number, sent once and never resent, waited for 10 s — the
    longer of the two first-party windows, since the official LAN client waits 5 s for this method
    and the Home Assistant integration waits 10 s for every command it sends. Accepted (explicit
    error_code 0 — the printer did not refuse), refused (its code kept) and ambiguous (no answer, an
    unreadable one, or a dropped session) are kept apart, and a lost answer is never a reason to
    send again. After an accepted or unanswered command one read-only look reports the mode the
    printer now reports, kept apart from acceptance; the look is taken straight after the command, so
    what it can show is a status that has not caught up rather than a mode that did not take, and
    cc2_status is the way to ask again. A number outside the documented four is reported as that
    number with no name invented for it, and a disagreement between the accepted command and the look
    after it is said out loud. Changing the mode mid-print changes how the rest of the print runs —
    that is what the preview is for."""
    try:
        want = _cc2_peripherals.parse_mode(mode)
    except PrinterError as e:
        return _cc2_err(e)
    return await _cc2_peripheral_tool("speed", {"mode": want}, confirm)


async def _cc2_temperature_tool(field: str, params: dict, confirm: bool) -> dict:
    """The one body behind cc2_set_temperature, and the same two phases the job and peripheral
    controls run. Like every direct tool it can only reach a printer once one is configured; with no
    address set the session seam refuses in-process, before any method of any kind is sent.

    A call that is not exactly ``confirm=True`` borrows the shared CC2 session and sends exactly one
    read-only 1002, and hands back the ``confirm_required`` preview carrying this call's own exact
    params and the
    ceiling it enforced — so what a confirmed call would do, and the bound that number was checked
    against, are both on the record before anything is changed, and no control method ever leaves the
    wire on a preview path (the tests watch the recorded wire for exactly that). A confirmed call
    sends the one 1028 command once through the adapter, which never resends an unanswered command.
    The confirmation is a decision-for-a-human device, not an access control: it carries the user's
    authorization for this action, which their standing yes for this print or session may already
    supply, so it obliges no fresh question of its own."""
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            if confirm is True:
                out = (await _cc2_temperature.set_temperature(sess.transport, field,
                                                              params)).as_dict()
            else:
                out = await _cc2_temperature.read_preview(sess.transport, field, params)
        except PrinterError as e:
            return _cc2_err(e)
        return _cc2_out(out)


@mcp.tool()
async def cc2_set_temperature(
    heater: Annotated[str, Field(description=(
        "Which heater to command: 'extruder' (the nozzle — 'nozzle' reads too) or 'heater_bed' "
        "('bed' reads too) — the only two heaters this protocol can command. The chamber is a "
        "reading only, so anything else refuses with these two named and nothing sent."))],
    target: Annotated[float, Field(description=(
        "The new target in degrees Celsius for that heater: 0 to 350 for the extruder, 0 to 110 for "
        "the bed — the bounds the CC2's own official LAN control accepts, which are not a hardware "
        "rating. 0 turns a heater off, which is what the printer's own end-of-print G-code sends. "
        "One heater per call: the other keeps whatever it is already at."))],
    confirm: Annotated[bool, Field(description=(
        "Second-call only: omitted or false returns a preview and sends nothing; exactly true sends "
        "the one temperature command. Confirm once the user has authorized the setpoint — it heats "
        "hardware, so a nozzle number matters while a print is running. A yes they already gave for "
        "this print or session covers it and needs no fresh question, so ask only when nothing "
        "authorizes it — read them the preview first then."))] = False,
) -> dict:
    """Set one heater's target temperature on the configured CC2 printer: heater is 'extruder' (the
    nozzle) or 'heater_bed' (the bed, also read as 'bed'), target in degrees Celsius, one heater per
    call. The ceiling a call may ask for is the printer's own official control's — 350 for the nozzle
    and 110 for the bed — and a number outside 0 to that ceiling refuses with nothing sent.

    TWO-PHASE: without confirm=true this reads the printer once and returns a preview (what the
    printer reports for that heater right now, the exact one-heater command a confirmed call would
    send, and the ceiling it enforced with its source) and changes nothing; only an exact confirm=true
    sends it. The confirmation carries the user's authorization, which their standing yes for this
    print or session may already supply, so it obliges no fresh question.

    Where the bounds come from, because a temperature tool must not invent them: the 0-350 and 0-110
    windows are the input bounds of Elegoo's own official LAN control for this printer (its web
    client, release 20260625), which clamps a typed number into them before it sends, and they are
    reported with every reply as that — a bound of the official control, never a claim about what the
    hot end or the bed is rated to. The two other numbers in the sources are not followed blindly and
    none is a slicer profile's: the printer's firmware refuses a setpoint outside its own configured
    window by itself (max_temp 600 extruder, 125 bed, a sensor envelope rather than a target), and
    Elegoo's LAN SDK records 300 for that extruder in a capability row that contradicts itself, so a
    call above 300 is sent — the machine's own control reaches that high — with the disagreement said
    out loud rather than resolved on a hunch. There is no parameter that raises a ceiling, because a
    caller-supplied ceiling is exactly the guess this refuses to make: ask for a lower number instead.

    One 1028 command carrying the one heater field, sent once and never resent, waited for 10 s — the
    longer of the two first-party windows, since the official LAN client waits 5 s for this method and
    the Home Assistant integration waits 10 s for every command. Accepted (the printer answered with
    an explicit error_code 0), refused (an explicit code kept) and ambiguous (no answer, an
    unreadable one, or a dropped session) are kept apart, and a lost answer is never a reason to send
    again. After an accepted or unanswered command one read-only look reports both the target and the
    actual temperature the printer states now, kept apart from acceptance: a target still climbing is
    never read as a failure, while a target the printer states as something other than the number it
    just accepted is said out loud without earning a second command. Nothing is pre-checked — whether
    a setpoint makes sense now is a human read of the preview, and the printer refuses what it will
    not do with its own code."""
    try:
        field = _cc2_temperature.parse_heater(heater)
        params = _cc2_temperature.temp_params(field, target)
    except PrinterError as e:
        return _cc2_err(e)
    return await _cc2_temperature_tool(field, params, confirm)


def _cc2_fan_percent(value):
    """``cc2_fan``'s argument bar, spelled the one way an argument validator may speak. The rule and its
    sentence stay in the module; a validator that raises anything but a ``ValueError`` is reported to the
    client as an anonymous crash with none of its text, and the whole point of refusing at this door is
    the sentence that names the two scales. So the module's ``PrinterError`` is re-raised here as the
    ``ValueError`` pydantic turns into an argument-validation failure the caller can read and fix — and
    the module's own bar still runs again on whatever survives, so nothing is decided twice on purpose.
    Accepted values pass through untouched, whole-number text included: that spelling is read on
    purpose."""
    try:
        return _cc2_fan.check_percent_input(value)
    except PrinterError as e:
        raise ValueError(f"{e.message} {e.hint}") from e


def _cc2_fan_err(e: PrinterError) -> dict:
    """A fan refusal, in the shape every fan reply carries. ``_cc2_err`` alone would drop the two fields
    this tool promises on every reply, and the refusal is exactly the reply that needs them: whoever
    just sent a bare 255 is whoever needs to be told which of the two scales this tool refused to pick
    between, and which one the number it sent became when it was accepted."""
    return {**_cc2_err(e), **_cc2_fan.refusal_keys()}


async def _cc2_fan_tool(values: dict, confirm) -> dict:
    """The one body behind cc2_set_fan, and the same two phases the job, peripheral and temperature
    controls run. Like every direct tool it can only reach a printer once one is configured; with no
    address set the session seam refuses in-process, before any method of any kind is sent.

    A call that is not exactly ``confirm=True`` borrows the shared CC2 session and sends exactly one
    read-only 1002, and hands back the ``confirm_required`` preview carrying this call's own exact
    params, the percent asked beside the number it becomes, and the formula that produced it — so the
    arithmetic a confirmed call would put on the wire is on the record before anything changes, and no
    1030 ever leaves the wire on a preview path (the tests watch the recorded wire for exactly that). A
    confirmed call takes one read-only 1002 first so the read-back afterwards can say whether a fan's
    number moved, then sends the one 1030 once through the adapter, which never resends an unanswered
    command, then takes its single look. The confirmation is a decision-for-a-human device, not an
    access control: it carries the user's authorization for this action, which their standing yes for
    this print or session may already supply, so it obliges no fresh question of its own.

    What is NOT in a preview, said because a two-phase control reads as a safety check: the fans' own
    numbers and this call's arithmetic are all the state it carries. It does not say whether a print is
    running or whether a fan should be running at this moment, so every reply from this tool carries
    ``state_checked: false`` and the sentence that says so, and ``cc2_status`` is the tool that reads the
    machine. That is a decision and not an unfinished gate — a refusal to command a fan while a print
    runs would break the fan's own job, since the slicer commands part cooling mid-print — and no
    first-party source conditions 1030 on the machine being idle."""
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            if confirm is True:
                out = (await _cc2_fan.set_fan_with_before(sess.transport, values)).as_dict()
            else:
                out = await _cc2_fan.read_preview(sess.transport, values)
        except PrinterError as e:
            return _cc2_fan_err(e)
        return _cc2_out(out)


@mcp.tool()
async def cc2_set_fan(
    model: Annotated[int | None, BeforeValidator(_cc2_fan_percent), Field(description=(
        "The part-cooling fan (the protocol's `fan` field) as a whole percent 0-100 — 0 stops it, 100 "
        "is what Elegoo's own LAN control's slider maxes at. Omit it (leave null) to leave this fan "
        "alone: it is NOT treated as 0. Anything that is not a whole percent 0-100 — 127, 255, 101, a "
        "float, a bool, a name — refuses with nothing sent, at the schema door and again at the "
        "printer's door, because the two scales in the sources are the one thing nobody has proven on "
        "this firmware and guessing which one a bare number meant is exactly the guess this tool will "
        "not make."))] = None,
    auxiliary: Annotated[int | None, BeforeValidator(_cc2_fan_percent), Field(description=(
        "The auxiliary fan (`aux_fan`) as a whole percent 0-100, same rules and same refusal as "
        "`model`; omit it to leave that fan alone."))] = None,
    chamber: Annotated[int | None, BeforeValidator(_cc2_fan_percent), Field(description=(
        "The chamber/box fan (`box_fan`) as a whole percent 0-100, same rules and same refusal as "
        "`model`; omit it to leave that fan alone. This is the fan field and nothing else — it is not a "
        "heater and it heats nothing."))] = None,
    confirm: Annotated[bool, BeforeValidator(_cc2_fan.strict_confirm),
                       Field(strict=True, description=(
        "Second-call only: omitted or false returns a preview and sends nothing; the JSON boolean true "
        "sends the one fan command, and a true spelled any other way (`\"true\"`, `\"yes\"`, `1`) refuses "
        "with nothing sent — a confirmation this server cannot read is not a yes. Confirm once the user "
        "has authorized the fans — a yes they already gave for this print or session covers it and needs "
        "no fresh question, so ask only when nothing authorizes it — read them the preview first then, "
        "knowing what that preview does not carry: the fans' numbers, not whether a print is "
        "running."))] = False,
) -> dict:
    """Command the configured CC2 printer's fans as `model` (part cooling), `auxiliary` or `chamber`,
    each a whole percent 0-100. Read this first: the fan scale is unproven, because no CC2 has ever
    answered method 1030 for this project. A fan you omit is left alone, never zeroed.

    TWO-PHASE like every other control here: without confirm=true this reads the printer once and
    returns a preview (what the printer reports for each fan right now, the exact 1030 a confirmed
    call would send, the percent asked beside the number it becomes, and the formula) and changes
    nothing; only an exact confirm=true sends it, and the confirmation carries the user's
    authorization, which their standing yes for this print or session may already supply. A confirm
    spelled any other way — "true", "yes", 1 — refuses with nothing sent rather than being read as a
    yes, because a two-phase control whose guard can be typed past is not a guard. An accepted
    command means only that the printer took the frame, and every reply says that beside
    `verified_on_this_printer: false`.

    WHERE THE NUMBERS COME FROM, because a fan tool that guesses is the hazard. The three first-party
    sources do not agree: the LAN control the printer serves itself takes a percent 0-100 from its
    slider and sends `percent / 100 * 255` **unreduced** (50% leaves as 127.5 because that client
    rounds nothing) under the fields `fan` / `aux_fan` / `box_fan`; the older integration's manual
    records 0-255 for those same fields; and that integration's own code validates 0-100 and sends it
    unconverted. This tool therefore takes the official UI's input (a percent), applies the official
    UI's own arithmetic to reach the wire, and calls neither number proven. There is no argument for
    a raw 0-255 value and no way to bypass the formula: a caller holding a 0-255 number converts it
    to a percent deliberately and says so. `controller_fan` and `heater_fan` appear in the printer's
    status read and in no source's command, so they cannot be commanded here at all.

    What actually goes out: one 1030 carrying only the named fans, sent once and never resent, waited
    for 10 s — the longer of the two first-party windows (the official LAN client waits 5 s for this
    method, the integration 10 s for every command). Accepted (an explicit integer error_code 0, which
    is the printer not refusing), refused (its code kept, and it earns no further read) and ambiguous
    (no answer, an unreadable one, or a dropped session) are kept apart exactly as the other controls
    keep them, and a lost answer is never a refusal and never a reason to send again.

    The read-back is the part a live test reads out, and it is deliberately uninterpreted: a confirmed
    call reads the printer once before the command and once after it, reports each fan's number
    **exactly as its 1002 answered `fans.<fan>.speed`** with no unit claimed and no percentage derived
    from it, and says per fan whether that raw number moved (true / false / unknown — an absent fan
    block stays unknown, never zero). A pre-read or a look that does not land is named as such rather
    than becoming a confident 'nothing changed'. So after an accepted 100% a printer reporting 255 is
    the official UI's formula holding; a 100 says the integration's code is right and the UI's scalar
    is decorative; and no movement at all says that field is not the fan you meant. If an accepted
    command left a commanded fan's number exactly where it was, a note says that as the raw-against-
    raw fact it is without calling it a failure, and it earns no second command. `cc2_status` reads
    the machine state on demand.

    Whether a fan should be running is not something this tool can see, and it says so rather than
    implying otherwise: a preview carries the fans' own numbers and this call's arithmetic and nothing
    else — not whether a print is running, not what the nozzle is at, not whether this is the moment for
    that fan — so every reply from this tool carries `state_checked: false` beside the sentence that
    names it, and `cc2_status` is the tool that reads the machine. No source conditions 1030 on a printer
    being idle or on a print running, and an idle gate here would be a tool refusing to do the thing the
    slicer uses the same fan field for mid-print, so the judgment stays with the person who can see the
    print; a refusal out of this tool is evidence about your firmware, not a broken printer."""
    values = {"model": model, "auxiliary": auxiliary, "chamber": chamber}
    try:
        _cc2_fan.fan_params(values)      # the argument bar is the point: it refuses before a session
    except PrinterError as e:
        return _cc2_fan_err(e)      # and the refusal is still a fan reply, caveats and all
    return await _cc2_fan_tool(values, confirm)


async def _cc2_auto_refill_tool(params: dict, confirm: bool) -> dict:
    """The one body behind cc2_set_auto_refill, and the same two phases every other CC2 control runs.
    Like every direct tool it can only reach a printer once one is configured; with no address set the
    session seam refuses in-process, before any method of any kind is sent.

    A call that is not exactly ``confirm=True`` borrows the shared CC2 session and sends exactly one
    read-only 2005 CANVAS read, and hands back the ``confirm_required`` preview carrying this call's own exact params
    and the candidate label, so what a confirmed call would do is on the record before anything is
    changed and no command ever leaves the wire on a preview path (the tests watch the recorded wire
    for exactly that). A confirmed call sends the one 2004 command once through the adapter, which
    never resends an unanswered command. The confirmation is a decision-for-a-human device, not an
    access control: it carries the user's authorization for this action, which their standing yes for
    this print or session may already supply, so it obliges no fresh question of its own."""
    async with _cc2_session() as sess:
        if sess.transport is None:
            return sess.error
        try:
            if confirm is True:
                out = (await _cc2_canvas.set_auto_refill(sess.transport,
                                                         params)).as_dict()
            else:
                out = await _cc2_canvas.read_preview(sess.transport, params)
        except PrinterError as e:
            return _cc2_err(e)
        return _cc2_out(out)


@mcp.tool()
async def cc2_set_auto_refill(
    auto_refill: Annotated[bool, Field(description=(
        "true asks the printer to refill automatically from another CANVAS tray when one runs out, "
        "false stops it doing that: a plain boolean, and the only thing it takes. Nothing else is "
        "sent with it, so this cannot change the active tray, a spool's filament info or a slot map. "
        "Anything the tool cannot read as exactly true or exactly false is refused with nothing "
        "sent."))],
    confirm: Annotated[bool, Field(description=(
        "Second-call only: omitted or false returns a preview and sends nothing; exactly true sends "
        "the one auto-refill command. Confirm once the user has authorized the switch: a yes they "
        "already gave for this print or session covers it and needs no fresh question, so ask only "
        "when nothing authorizes it, read them the preview first then."))] = False,
) -> dict:
    """Turn the configured CC2 printer's CANVAS auto-refill switch on or off with one boolean, and
    read this before calling it: the setting was changed and restored on one CC2, but automatic
    refill during a print remains unverified. The shape is Elegoo's own, method 2004 carrying the one field
    auto_refill that the printer's own LAN web client sends and that the printer answers back in its
    CANVAS info, and that field is all this tool ever sends, so a call changes the switch and nothing
    else: not which tray is active, not a spool's filament info, no slot map. The older integration
    manual spells this command's parameter enable, and that spelling is never sent here in any
    combination: the manual is the only source for it, the printer's own client and that same manual's
    own CANVAS sample both say auto_refill, and the manual makes the same class of mistake on the
    delete command, so a table row of that manual is not proof about this firmware.

    TWO-PHASE, like every other control here: without confirm=true this sends only the read-only
    CANVAS read (method 2005) and returns a preview, what the printer says the flag is now and the
    exact one-field command a confirmed call would send, and changes nothing; only an exact
    confirm=true sends it. The confirmation carries the user's authorization, which their standing yes
    for this print or session may already supply, so it obliges no fresh question. A printer with no
    CANVAS attached still gets its preview: the flag is the printer's own setting, and a CANVAS read
    that answers nothing is reported as unknown, never as off, and is never a reason to withhold the
    switch.

    One 2004 command, sent once and never resent, waited for 10 s, the longer of the two first-party
    windows (Elegoo's LAN client passes no override for this method so its 5 s default applies, and
    the Home Assistant integration waits 10 s for every command it sends). Accepted (an explicit
    error_code 0, which is the printer not refusing), refused (its code kept, and it earns no further
    read) and ambiguous (no answer, an unreadable one, or a dropped session) are kept apart exactly as
    the job and peripheral controls keep them, and a lost answer is never a refusal and never a reason
    to send again. After an accepted or unanswered command one read-only CANVAS look reports what the
    printer says the flag is now, kept apart from acceptance, and a look that contradicts the command
    the printer accepted is said out loud without earning a second command.

    What has not been established is whether the flag performs automatic refill during a print,
    so every reply carries
    verified_on_this_printer false beside the sentence saying why. A refusal from this tool is worth
    recording for that reason: it is evidence about this firmware, not a broken printer."""
    try:
        want = _cc2_canvas.parse_refill(auto_refill)
    except PrinterError as e:
        return _cc2_err(e)
    return await _cc2_auto_refill_tool(_cc2_canvas.refill_params(want), confirm)


# --- Tool annotations (title + read-only/destructive hints) -----------------
# The Claude connectors directory requires every tool to carry a title and the
# applicable readOnlyHint / destructiveHint. Kept as one table so completeness
# is reviewable at a glance; _apply_tool_annotations() raises on any tool
# missing from it, and tests/test_tool_annotations.py enforces the inverse.

_TOOL_ANNOTATIONS: dict[str, tuple[str, bool, bool]] = {
    # name: (title, read_only, destructive)
    "get_status": ("Get OrcaSlicer status", True, False),
    "get_config": ("Get config values", True, False),
    "get_slice_status": ("Get slice status", True, False),
    "get_slice_warnings": ("Get slice warnings", True, False),
    "get_slice_breakdown": ("Get slice time/flow breakdown", True, False),
    "compare_settings": ("Compare settings", True, False),
    "compare_slices": ("Compare slice variants", True, False),
    "list_objects": ("List plate objects", True, False),
    "list_plates": ("List plates", True, False),
    "get_job_status": ("Get background job status", True, False),
    "watch_events": ("Watch OrcaSlicer events", True, False),
    "find_config_keys": ("Find config keys", True, False),
    "diagnose_plate": ("Diagnose plate issues", True, False),
    "check_placement": ("Check object placement", True, False),
    "consult": ("Consult slicing knowledge", True, False),
    "check_profile_physics": ("Sanity-check profile physics", True, False),
    "describe_setting": ("Describe a setting", True, False),
    "search_settings": ("Search settings", True, False),
    "list_presets": ("List presets", True, False),
    "get_preset_config": ("Get preset config", True, False),
    "get_gcode": ("Download sliced gcode", True, False),
    "describe_plate": ("Describe plate placement from G-code", True, False),
    "save_gcode": ("Save G-code and record the slice", False, False),
    "recall_prints": ("Recall past prints of this model", True, False),
    "render_plate": ("Render plate image", True, False),
    "set_config": ("Set config values", False, False),
    "slice": ("Start slicing", False, False),
    "slice_and_wait": ("Slice and wait for result", False, False),
    "apply_and_slice": ("Apply config and slice", False, False),
    "cancel_slice": ("Cancel running slice", False, False),
    "set_object_config": ("Set per-object config", False, False),
    "duplicate_object": ("Duplicate object", False, False),
    "transform_object": ("Transform object", False, False),
    "arrange_plate": ("Arrange plate", False, False),
    "add_plate": ("Add plate", False, False),
    "select_plate": ("Select plate", False, False),
    "get_printer_slots": ("Get CANVAS slots", True, False),
    "get_filament_slot_mapping": ("Get filament slot mapping", True, False),
    "set_filament_slot_mapping": ("Set filament slot mapping", False, False),
    "move_object_to_plate": ("Move object to plate", False, False),
    "auto_orient": ("Auto-orient objects", False, False),
    "remember": ("Save a note", False, False),
    "load_model": ("Load a model file", False, False),
    "select_preset": ("Select preset", False, False),
    "set_layer_height": ("Set layer height", False, False),
    "set_height_range": ("Set height-range config", False, False),
    "rename_preset": ("Rename preset", False, False),
    "save_preset": ("Save preset (overwrites stored settings)", False, True),
    "edit_preset": ("Edit preset (overwrites stored settings)", False, True),
    "delete_object": ("Delete object from plate", False, True),
    "delete_plate": ("Delete plate (its objects are parked off-plate)", False, True),
    "delete_preset": ("Delete preset", False, True),
    "get_printer_status": ("Get printer status", True, False),
    "wait_for_printer": ("Wait for the printer", True, False),
    "list_print_history": ("List print history", False, False),
    "check_printer_match": ("Check the profile against the printer", True, False),
    # Direct CC2 tools. The six reads carry readOnlyHint; the file writes carry
    # destructiveHint=False (neither deletes — no CC2 delete is proven on this firmware: 1047 is
    # described by the protocol and sent by Elegoo's own LAN client, and this branch carries it only
    # as an unregistered, untested candidate, which is why upload still never overwrites) and
    # deliberately NO idempotency hint: an upload is
    # not replay-safe and a start is the definition of not. Of the job controls, stop alone is
    # destructive — it ends the job past the documented state machine's STOPPED end, with no way
    # back — while pause and resume are the reversible pair and say so (the maintainer's Klipper
    # companion marks all three destructive; here the hint is set per what each command undoes).
    # Annotations are advisory metadata, not an access control: what reaches a person is the
    # two-phase confirm enforced in the job-control bodies, and what reaches a printer at all is
    # simply whether an address is configured.
    "cc2_status": ("Read CC2 printer state directly", True, False),
    "cc2_list_files": ("List files on the CC2 printer", True, False),
    # The three new reads are reads: one request each (1001, 1046, 1045), no command, nothing written
    # on the printer or on disk. cc2_file_thumbnail carries no idempotency hint either: re-asking for a
    # picture is harmless in the ordinary sense, but this server never promises a second request lands
    # the same way, and the hint is not the place to imply it has been tested on this firmware.
    "cc2_printer_info": ("Read CC2 printer attributes", True, False),
    "cc2_file_detail": ("Read one CC2 file's detail", True, False),
    "cc2_file_thumbnail": ("Read one CC2 file's thumbnail", True, False),
    # The fourth CC2 read is a read: one parameterless 1036, no command, nothing written on the
    # printer or on disk. Its reply carries its own admission that no 1036 answer has ever been
    # captured from this firmware — a read can be a candidate for live verification, which is exactly
    # why 1037 (task detail) and 1038 (history delete) are absent instead of guessed at.
    "cc2_print_history": ("Read CC2 print history", True, False),
    # The seventh CC2 read is a read of the printer's own camera stream: one bounded HTTP GET, no
    # command, nothing written on the printer or on disk, and no MQTT session involved. It is the one CC2
    # tool whose reply is an image rather than a table of facts, and it is a read because the printer keeps
    # nothing of it. What it can cost somebody else is the stream's single viewer slot, which the reply
    # warns about in words instead of hiding in a hint — annotations are advice, never a control. No
    # idempotency hint: a second picture is harmless to the printer, and this server still never promises
    # that a second request lands the same way.
    "cc2_camera_snapshot": ("Take one picture from the CC2 camera", True, False),
    "cc2_upload_gcode": ("Upload G-code to the CC2 printer", False, False),
    "cc2_start_print": ("Start a print on the CC2 printer", False, False),
    "cc2_pause_print": ("Pause the CC2 print", False, False),
    "cc2_resume_print": ("Resume the CC2 print", False, False),
    "cc2_stop_print": ("Stop the CC2 print (ends the job for good)", False, True),
    # The two peripheral controls change what the printer does but destroy nothing: the light is a
    # lamp and the speed mode is reversible by asking for another one, so neither carries
    # destructiveHint. Both are writes, both reach the printer over the network, and both enforce the
    # two-phase confirm in their own bodies exactly like the job controls do.
    "cc2_set_light": ("Set the CC2 overhead light", False, False),
    "cc2_set_print_speed": ("Set the CC2 print speed mode", False, False),
    # The temperature setpoint is a write that reaches the printer over the network and enforces the
    # two-phase confirm in its own body, and it is not destructive: any setpoint it sends can be
    # replaced by another call (0 turns the heater off), which is what the printer's own end-of-print
    # G-code does. What makes it careful is the ceiling it refuses to exceed and the preview that
    # names its source, not a destructiveHint.
    "cc2_set_temperature": ("Set a CC2 heater temperature", False, False),
    "cc2_set_fan": ("Set a CC2 fan speed", False, False),
    # The CANVAS switch is the same kind of write as the other three and no more destructive than the
    # light: it flips one stored boolean on the printer, it can be flipped back by asking again, and
    # it destroys nothing. What makes it careful is that it has never met this firmware, which is said
    # in its replies rather than hidden in a hint, and it is a write that reaches the printer over the
    # network with the two-phase confirm enforced in its own body exactly like the others.
    "cc2_set_auto_refill": ("Set the CC2 CANVAS auto-refill switch", False, False),
}


# Tools that reach outside OrcaSlicer (the printer on the network), and tools that are safe to repeat
# although they write local state. The direct CC2 tools reach the printer directly too, so all of
# them are open-world, because openWorldHint, like every annotation, is a hint and not a permission:
# a configured printer answers any of them, which is why the job controls enforce their two-phase
# confirm in the tool body instead of relying on a hint or a switch.
_OPEN_WORLD_TOOLS = frozenset({"get_printer_status", "wait_for_printer", "list_print_history",
                               "check_printer_match", "cc2_status", "cc2_list_files",
                               "cc2_printer_info", "cc2_file_detail", "cc2_file_thumbnail",
                               "cc2_print_history", "cc2_camera_snapshot",
                               "cc2_upload_gcode", "cc2_start_print", "cc2_pause_print",
                               "cc2_resume_print", "cc2_stop_print", "cc2_set_light",
                               "cc2_set_print_speed", "cc2_set_temperature", "cc2_set_fan",
                               "cc2_set_auto_refill"})
_IDEMPOTENT_TOOLS = frozenset({"list_print_history"})


def _apply_tool_annotations() -> None:
    for name, tool in mcp._tool_manager._tools.items():
        title, read_only, destructive = _TOOL_ANNOTATIONS[name]
        tool.title = title
        # destructiveHint is only meaningful on tools that modify state, so
        # read-only tools carry no hint rather than a misleading False.
        tool.annotations = ToolAnnotations(
            title=title,
            readOnlyHint=read_only,
            destructiveHint=None if read_only else destructive,
            idempotentHint=True if name in _IDEMPOTENT_TOOLS else None,
            openWorldHint=True if name in _OPEN_WORLD_TOOLS else None,
        )


_apply_tool_annotations()


def _hide_windows_console() -> None:
    """Hide the console window an MCP client pops when launching this stdio server
    on Windows (GUI clients have no console, so uv.exe/the launcher spawns one).

    The MCP transport uses the redirected stdin/stdout *pipes* the client hands us,
    not the console, so hiding the window leaves the protocol untouched. No-op off
    Windows and when there is no console (e.g. launched from a terminal a user owns
    — GetConsoleWindow returns NULL and ShowWindow(NULL, ...) does nothing).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass  # never let a cosmetic tweak stop the server from starting


def main() -> None:
    _hide_windows_console()
    for w in allow_override_warnings():
        print(w, file=sys.stderr)  # stdout carries the MCP protocol; stderr reaches the client log
    import anyio
    anyio.run(_serve_stdio_then_close)


async def _serve_stdio_then_close() -> None:
    """Serve stdio, then hand the printer its MQTT slot back. The CC2 session lives on this loop, so
    this is the only place its close can be awaited: a session left open across the exit is a client
    the printer has to time out on its own, which is the slow part of the slot problem this server is
    otherwise careful about. Never lets a teardown failure hide the reason the server stopped."""
    try:
        await run_stdio(mcp._lowlevel_server)
    finally:
        await _cc2_pool.close_pools([_CC2_POOL])


if __name__ == "__main__":
    main()
