import httpx, respx
import orcaslicer_mcp.server as srv
from orcaslicer_mcp import outcomes as oc

B = "http://x:13130"
OBJS = {"count": 1, "objects": [{"id": 1, "index": 0, "name": "cube", "size_mm": [20, 20, 20], "instances": 1,
                                 "transform": {"offset": [0, 0, 10], "rotation": [0, 0, 0], "scale": [1, 1, 1]}}]}


def _env(m, tmp_path):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", B)
    m.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    # The plate API is what tells recall which objects belong to the plate in front of the user. A
    # fork before v2.4.2-mcp.13 has none, so tests that do not replace this route match on the whole
    # project's objects, as they always did.
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(404, json={"error": "not_found"}))


async def test_no_store_is_silent_no_op(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path / "absent")
    out = await srv.recall_prints()
    assert out == {"available": False, "matched_by": None, "prints": [], "summary": "no outcome store"}


@respx.mock
async def test_matches_current_plate_by_geometry_then_summarises(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=OBJS))
    h = oc.geometry_hash_for(OBJS["objects"])
    oc.record_slice("c1.gcode", "cube", h, {"layer_height": "0.5", "fan_max_speed": "60"}, sliced_at=1.0)
    oc.record_outcome({"job_id": "1", "filename": "c1.gcode", "status": "completed", "end_time": 2.0,
                       "total_duration": 600, "metadata": {"filament_weight_total": 6.3}})
    oc.set_verdict("warped", "c1.gcode")
    oc.record_slice("c2.gcode", "cube", h, {"layer_height": "0.3"}, sliced_at=3.0)
    oc.record_outcome({"job_id": "2", "filename": "c2.gcode", "status": "completed", "end_time": 4.0})
    oc.record_slice("other.gcode", "peg", "zzz", {}, sliced_at=5.0)
    out = await srv.recall_prints()
    assert out["available"] is True and out["matched_by"] == "geometry"
    names = [p["gcode_filename"] for p in out["prints"]]
    assert names == ["c2.gcode", "c1.gcode"]  # newest first, 'peg' excluded
    assert out["prints"][1]["human_verdict"] == "warped"
    assert out["prints"][1]["settings_summary"]["fan_max_speed"] == "60"
    assert out["summary"] == "2 past prints of cube: 2 success (1 marked warped)"


@respx.mock
async def test_falls_back_to_name_when_slicer_offline(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/objects").mock(side_effect=httpx.ConnectError("down"))
    oc.record_slice("c1.gcode", "cube", "h", {}, sliced_at=1.0)
    out = await srv.recall_prints(model_name="cube")
    assert out["matched_by"] == "name" and len(out["prints"]) == 1


@respx.mock
async def test_recent_when_nothing_to_match_on(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json={"count": 0, "objects": []}))
    oc.record_outcome({"job_id": "9", "filename": "x.gcode", "status": "cancelled", "end_time": 1.0})
    out = await srv.recall_prints(limit=3)
    assert out["matched_by"] == "recent" and out["prints"][0]["result"] == "cancelled"
    assert out["summary"] == "1 recent print: 1 cancelled"


@respx.mock
async def test_rows_carry_failure_reason_and_estimates(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=OBJS))
    h = oc.geometry_hash_for(OBJS["objects"])
    oc.record_slice("c3.gcode", "cube", h, {"layer_height": "0.5"}, sliced_at=1.0, est_time_s=600, est_filament_g=6.0)
    oc.record_outcome({"job_id": "9", "filename": "c3.gcode", "status": "error", "end_time": 2.0,
                       "total_duration": 120.0}, failure_reason="Must home axis first")
    out = await srv.recall_prints()
    p = out["prints"][0]
    assert p["failure_reason"] == "Must home axis first" and p["est_time_s"] == 600 and p["duration_s"] == 120.0


# --- Which plate the fingerprint covers (fork v2.4.2-mcp.13 plate API) -------------------------
# save_gcode fingerprints the objects of the plate that was sliced, so recall has to look with the
# same subset: matching the whole project instead would either miss the plate's own prints or, worse,
# hand over another plate's print as if it were a print of what is on the bed now.

CUBE0 = {"id": 1, "name": "cube.stl", "plate": 0, "size_mm": [20, 20, 20]}
BRACKET1 = {"id": 2, "name": "bracket.stl", "plate": 1, "size_mm": [30, 10, 5]}
BOTH = {"count": 2, "objects": [dict(CUBE0), dict(BRACKET1)]}


def _two_plates(current: int, members=None):
    """The two-plate project as the fork reports it: each plate entry naming the copies it holds as
    {id, instance}. `members` defaults to cube.stl (id 1) on plate 0 and bracket.stl (id 2) on plate 1."""
    if members is None:
        members = {0: [(1, 0)], 1: [(2, 0)]}
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=BOTH))
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json={
        "count": 2, "current": current,
        "plates": [{"index": i, "name": f"Plate {i + 1}", "current": current == i,
                    "objects": [{"id": oid, "instance": inst} for oid, inst in members[i]]}
                   for i in range(2)]}))


def _bracket_print(hash_: str | None = None):
    """A finished print of plate 1's bracket.stl, recorded the way save_gcode records it now: the
    printed copy, not the bare object list."""
    oc.record_slice("b1.gcode", "bracket.stl",
                    oc.printed_geometry_hash([{"name": "bracket.stl", "size_mm": [30, 10, 5],
                                                "instance": 0}]) if hash_ is None else hash_,
                    {"layer_height": "0.2"}, sliced_at=1.0)
    oc.record_outcome({"job_id": "11", "filename": "b1.gcode", "status": "completed", "end_time": 2.0})


@respx.mock
async def test_recall_matches_the_plate_in_front_of_the_user(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _two_plates(current=1)
    _bracket_print()

    out = await srv.recall_prints()

    assert out["matched_by"] == "geometry"
    assert out["plate"] == 1 and out["plate_attribution"] == "plate"
    assert "attribution_note" not in out
    assert [p["gcode_filename"] for p in out["prints"]] == ["b1.gcode"]
    assert out["summary"] == "1 past print of bracket.stl: 1 success"


@respx.mock
async def test_recall_does_not_pass_another_plates_print_off_as_this_plates(monkeypatch, tmp_path):
    # Same store, plate 0 current: bracket.stl is in the project but not on this plate, so its row
    # may only arrive as a recent print of something else.
    _env(monkeypatch, tmp_path)
    _two_plates(current=0)
    _bracket_print()

    out = await srv.recall_prints()

    assert out["matched_by"] == "recent"
    assert out["plate"] == 0 and out["plate_attribution"] == "plate"
    assert out["summary"] == "1 recent print: 1 success"


@respx.mock
async def test_recall_does_not_answer_a_fingerprint_miss_with_the_plate_object_name(monkeypatch, tmp_path):
    # Neither fingerprint matches any row. This used to fall back to searching by this plate's own object
    # name and come back as matched_by="name" - "1 past print of bracket.stl" - which reads to a user as
    # this plate's history while a name cannot say how many copies that print had, which is exactly the
    # thing the fingerprint exists to tell apart. A name search is the caller's call, so it runs on
    # `model_name` and not on the tool's own guess; a miss answers with recent prints and the sentence
    # saying why they are not this plate's record.
    _env(monkeypatch, tmp_path)
    _two_plates(current=1)
    oc.record_slice("cube-old.gcode", "cube.stl", "zzz", {}, sliced_at=1.0)
    oc.record_slice("bracket-old.gcode", "bracket.stl", "yyy", {}, sliced_at=2.0)

    out = await srv.recall_prints()

    assert out["matched_by"] == "recent" and out["plate"] == 1
    assert out["summary"] == "2 recent prints: 2 unprinted"
    assert "1 copy of bracket.stl" in out["copy_count_note"]

    named = await srv.recall_prints(model_name="bracket.stl")
    assert named["matched_by"] == "name"
    assert [p["gcode_filename"] for p in named["prints"]] == ["bracket-old.gcode"]


@respx.mock
async def test_recall_gives_up_on_the_fingerprint_when_plate_membership_is_unknown(monkeypatch,
                                                                                   tmp_path):
    # Two plates, and the reply keeps each plate's copies to itself: whose plate is in front of the user
    # is knowable but what it prints is not, so a fingerprint match here would be a guess, and recall
    # refuses it. OBJS (one object, no plate keys) is what such a build reports, and the row below
    # carries exactly the project-wide fingerprint the old code matched on.
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=OBJS))
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json={
        "count": 2, "current": 1, "plates": [{"index": 0}, {"index": 1, "current": True}]}))
    oc.record_slice("c1.gcode", "cube", oc.geometry_hash_for(OBJS["objects"]), {}, sliced_at=1.0)

    out = await srv.recall_prints()

    assert out["matched_by"] == "recent"
