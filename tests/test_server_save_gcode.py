import time

import httpx, pytest, respx
import orcaslicer_mcp.server as srv
from orcaslicer_mcp import outcomes as oc

B = "http://x:13130"
OBJS = {"count": 1, "objects": [{"id": 1, "index": 0, "name": "cube", "size_mm": [20, 20, 20], "instances": 1,
                                 "transform": {"offset": [0, 0, 10], "rotation": [0, 0, 0], "scale": [1, 1, 1]}}]}
CFG = {"config": {"layer_height": "0.5", "fan_max_speed": "60", "unrelated": "x"}}
STATUS = {"presets": {"printer": "Test Printer", "print": "p", "filaments": ["f"]}}
SLICE = {"state": "done", "percent": 100, "stats": {"estimated_time_seconds": 1234.0, "filament_used_g": 5.6}}


def _routes(slice_resp=None):
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=b"G28\nG1 X1\n"))
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=OBJS))
    # No plate API: a fork before v2.4.2-mcp.13. Every test below that does not replace this route
    # saves and records exactly as it always did, attributed to the project (see test_save_gcode_older_build).
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    respx.get(url__regex=rf"{B}/api/v1/config.*").mock(return_value=httpx.Response(200, json=CFG))
    respx.get(f"{B}/api/v1/slice/status").mock(return_value=slice_resp or httpx.Response(200, json=SLICE))
    respx.get(f"{B}/api/v1/status").mock(return_value=httpx.Response(200, json=STATUS))


def _env(m, tmp_path, slice_resp=None):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", B)
    m.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    _routes(slice_resp)


@respx.mock
async def test_save_gcode_writes_file_and_records_slice(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    out = await srv.save_gcode("cube_test.gcode")
    assert out["filename"] == "cube_test.gcode" and out["bytes"] == 10
    assert (tmp_path / "gcode" / "cube_test.gcode").read_bytes() == b"G28\nG1 X1\n"
    assert out["outcome_recorded"] is True and out["model_name"] == "cube"
    row = oc.recall(model_name="cube")[0]
    assert row["gcode_filename"] == "cube_test.gcode"
    assert row["settings_summary"] == {"layer_height": "0.5", "fan_max_speed": "60"}
    assert row["geometry_hash"] == oc.printed_geometry_hash(_copies(("cube", [20, 20, 20], 0)))


@respx.mock
async def test_save_gcode_creates_the_store_and_records_estimates(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    assert oc.is_available() is False
    out = await srv.save_gcode("a.gcode")
    assert (tmp_path / "gcode" / "a.gcode").exists()
    assert out["outcome_recorded"] is True and oc.is_available() is True
    row = oc.get(out["outcome_row_id"])
    assert row["est_time_s"] == 1234.0 and row["est_filament_g"] == 5.6
    assert row["printer_id"] == "Test Printer"


@respx.mock
async def test_save_gcode_uses_orca_printer_id(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCA_PRINTER_ID", "bench")
    out = await srv.save_gcode("b.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "bench"


@respx.mock
async def test_save_gcode_names_the_printer_by_its_host_when_orca_printer_url_is_set(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://test-user:pw-secret@192.0.2.10:7125")
    out = await srv.save_gcode("host.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "192.0.2.10"


@respx.mock
async def test_orca_printer_id_beats_orca_printer_url(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://192.0.2.10")
    monkeypatch.setenv("ORCA_PRINTER_ID", "bench")
    out = await srv.save_gcode("both.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "bench"


@respx.mock
async def test_a_whitespace_orca_printer_id_counts_as_unset(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCA_PRINTER_ID", "   ")
    out = await srv.save_gcode("blank.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "Test Printer"
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://192.0.2.10")
    out = await srv.save_gcode("blank2.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "192.0.2.10"


@respx.mock
async def test_save_gcode_with_no_printer_name_at_all_records_unknown(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/status").mock(return_value=httpx.Response(200, json={"presets": {}}))
    out = await srv.save_gcode("nameless.gcode")
    assert oc.get(out["outcome_row_id"])["printer_id"] == "unknown"


@pytest.mark.parametrize("status_reply", [
    httpx.Response(500, json={"error": "boom"}),                  # OrcaSlicer's status call fails
    httpx.Response(200, json={"presets": {"printer": "   "}}),    # a blank profile name
    httpx.Response(200, json={"presets": {"printer": ""}}),
], ids=["status-fails", "blank-name", "empty-name"])
@respx.mock
async def test_save_gcode_records_unknown_when_the_printer_name_cannot_be_had(monkeypatch, tmp_path, status_reply):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/status").mock(return_value=status_reply)
    out = await srv.save_gcode("nameless2.gcode")
    assert out["outcome_recorded"] is True
    assert oc.get(out["outcome_row_id"])["printer_id"] == "unknown"


@respx.mock
async def test_save_gcode_still_records_without_an_estimate(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, slice_resp=httpx.Response(500, json={"error": "boom"}))
    out = await srv.save_gcode("c.gcode")
    row = oc.get(out["outcome_row_id"])
    assert out["outcome_recorded"] is True and row["est_time_s"] is None and row["printer_id"] == "Test Printer"


@respx.mock
async def test_save_gcode_default_name_and_sanitising(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    out = await srv.save_gcode()
    assert out["filename"].startswith("cube_") and out["filename"].endswith(".gcode")
    out2 = await srv.save_gcode("../evil name?.gcode")
    assert "/" not in out2["filename"] and ".." not in out2["filename"] and " " not in out2["filename"]


@respx.mock
async def test_save_gcode_not_sliced(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(409, json={"error": "not_sliced"}))
    assert (await srv.save_gcode("x.gcode"))["error"] == "not_sliced"


@respx.mock
async def test_save_gcode_unicode_only_name_falls_back_to_print_prefix(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    out = await srv.save_gcode("日本語.gcode")
    assert out["filename"].startswith("print_")


@respx.mock
async def test_save_gcode_degrades_when_store_write_fails(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)

    def _boom(*args, **kwargs):
        raise __import__("sqlite3").OperationalError("database is locked")

    monkeypatch.setattr(srv._outcomes, "record_slice", _boom)
    out = await srv.save_gcode("locked.gcode")
    assert (tmp_path / "gcode" / "locked.gcode").exists()
    assert out["outcome_recorded"] is False
    assert out["outcome_row_id"] is None
    assert "locked" in out["outcome_error"]


@pytest.mark.parametrize("exc,shown", [
    (RuntimeError("store exploded"), "store exploded"),
    (ValueError("bad value in the slice"), "bad value in the slice"),
    (TypeError("not serialisable"), "not serialisable"),
    (KeyError("size_mm"), "size_mm"),
    (ValueError(), "ValueError"),                                  # no message: the type still says what happened
], ids=["runtime", "value", "type", "key", "no-message"])
@respx.mock
async def test_save_gcode_keeps_the_file_whatever_goes_wrong_while_recording(monkeypatch, tmp_path, exc, shown):
    _env(monkeypatch, tmp_path)

    def _boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(srv._outcomes, "record_slice", _boom)
    out = await srv.save_gcode("kept.gcode")
    assert (tmp_path / "gcode" / "kept.gcode").read_bytes() == b"G28\nG1 X1\n"
    assert out["outcome_recorded"] is False and out["outcome_row_id"] is None
    assert shown in out["outcome_error"] and out["path"] == str(tmp_path / "gcode" / "kept.gcode")


@respx.mock
async def test_save_gcode_keeps_the_file_when_an_object_has_an_unreadable_size(monkeypatch, tmp_path):
    # no patching: the geometry hash is computed inside the recording step and float(None) raises TypeError
    _env(monkeypatch, tmp_path)
    odd = {"count": 1, "objects": [{**OBJS["objects"][0], "size_mm": [None, 20, 20]}]}
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=odd))
    out = await srv.save_gcode("odd.gcode")
    assert (tmp_path / "gcode" / "odd.gcode").exists()
    assert out["outcome_recorded"] is False and out["outcome_error"]


@respx.mock
async def test_save_gcode_falls_back_to_home_dotfolder_when_no_shared_store(monkeypatch, tmp_path):
    # The module path constants are fixed at import time, so patch them (not HOME alone): this test
    # can then never land on a real store whatever the import order.
    monkeypatch.delenv("PRINT_OUTCOMES_DIR", raising=False)
    fake_shared = tmp_path / "would_be_shared" / "print-outcomes"
    monkeypatch.setattr(oc, "LEGACY_DIR", fake_shared)
    monkeypatch.setattr(oc, "DEFAULT_DIR", tmp_path / ".orcaslicer-mcp" / "outcomes")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ORCA_API_TOKEN", "tok"); monkeypatch.setenv("ORCA_API_URL", B)
    _routes()

    out = await srv.save_gcode("a.gcode")

    assert out["outcome_recorded"] is True
    assert (tmp_path / ".orcaslicer-mcp" / "gcode" / "a.gcode").exists()
    assert (tmp_path / ".orcaslicer-mcp" / "outcomes" / "outcomes.db").exists()
    assert not fake_shared.exists()

    # The first save created the store folder. The G-code folder must not flip to it: the rule is
    # "the shared folder exists", not "a store folder exists", so a second save lands beside the first.
    second = await srv.save_gcode("b.gcode")

    assert second["outcome_recorded"] is True
    assert (tmp_path / ".orcaslicer-mcp" / "gcode" / "b.gcode").exists()
    assert not (tmp_path / ".orcaslicer-mcp" / "outcomes" / "gcode").exists()
    assert not fake_shared.exists()


@respx.mock
async def test_whitespace_only_outcomes_dir_counts_as_unset(monkeypatch, tmp_path):
    # A whitespace-only PRINT_OUTCOMES_DIR is unset (outcomes.store_dir() strips it too). chdir so a
    # regression that used the raw value as a relative path lands inside tmp_path and is caught.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", "   ")
    fake_shared = tmp_path / "would_be_shared" / "print-outcomes"
    monkeypatch.setattr(oc, "LEGACY_DIR", fake_shared)
    monkeypatch.setattr(oc, "DEFAULT_DIR", tmp_path / ".orcaslicer-mcp" / "outcomes")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ORCA_API_TOKEN", "tok"); monkeypatch.setenv("ORCA_API_URL", B)
    _routes()

    out = await srv.save_gcode("w.gcode")

    assert out["outcome_recorded"] is True
    assert (tmp_path / ".orcaslicer-mcp" / "gcode" / "w.gcode").exists()
    assert (tmp_path / ".orcaslicer-mcp" / "outcomes" / "outcomes.db").exists()
    assert not (tmp_path / "   ").exists()


@respx.mock
async def test_save_gcode_uses_shared_store_when_it_already_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("PRINT_OUTCOMES_DIR", raising=False)
    fake_shared = tmp_path / "would_be_shared" / "print-outcomes"
    fake_shared.mkdir(parents=True)
    monkeypatch.setattr(oc, "LEGACY_DIR", fake_shared)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ORCA_API_TOKEN", "tok"); monkeypatch.setenv("ORCA_API_URL", B)
    _routes()

    out = await srv.save_gcode("a.gcode")

    assert (fake_shared / "gcode" / "a.gcode").exists()
    assert (fake_shared / "outcomes.db").exists() and out["outcome_recorded"] is True
    assert not (tmp_path / ".orcaslicer-mcp").exists()


@respx.mock
async def test_save_gcode_never_overwrites_existing_file(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    out1 = await srv.save_gcode("dup.gcode")
    assert out1["filename"] == "dup.gcode"

    respx.get(f"{B}/api/v1/gcode").mock(return_value=httpx.Response(200, content=b"G28\nG1 X2\n"))
    out2 = await srv.save_gcode("dup.gcode")
    assert out2["filename"] == "dup-2.gcode"

    gdir = tmp_path / "gcode"
    assert (gdir / "dup.gcode").read_bytes() == b"G28\nG1 X1\n"
    assert (gdir / "dup-2.gcode").read_bytes() == b"G28\nG1 X2\n"


# --- Which plate the saved slice belongs to (fork v2.4.2-mcp.13 plate API) --------------------
# get_gcode always answers for the CURRENT plate while /api/v1/objects lists the whole PROJECT.
# Taking objs[0] and hashing every object attributed another plate's part, and the settings that
# produced it, to a file holding one plate's toolpaths: the fault these tests pin shut.

def _plate_reply(current: int, count: int = 2, names=("Plate 1", "Plate 2"), members=None):
    """A `/api/v1/plates` reply in the shape the fork actually answers: each plate entry carries its
    own `objects` list, naming every copy it holds as {id, instance} - the same pair
    move_object_to_plate takes as arguments. The old fixtures here left that list empty on every plate
    while relying on each object's own `plate`, which is only the plate of that object's first
    instance: that is how a multi-plate bug stayed invisible. `members` overrides the default layout
    (plate i holds object i+1, one copy), which is what CUBE / BRACKET below describe.
    Pass `members={}` to a plate's entry to leave that plate silent about its copies."""
    if members is None:
        members = {i: [(i + 1, 0)] for i in range(count)}
    return {"count": count, "current": current, "max": 36, "can_add": True, "can_delete": True,
            "plates": [{"index": i, "name": names[i], "current": i == current, "empty": not members[i],
                        "origin": [0, 0, 0], "bbox_min": [0, 0, 0], "bbox_max": [256, 256, 256],
                        "objects": [{"id": oid, "instance": inst} for oid, inst in members[i]]}
                       for i in range(count)]}


def _silent_plates(current: int, count: int = 2):
    """A plate reply that names the plates but keeps their copies to itself: no `objects` key on the
    entries at all. Used for the cases where membership genuinely is not on the wire."""
    return {"count": count, "current": current,
            "plates": [{"index": i, "name": f"Plate {i + 1}", "current": i == current}
                       for i in range(count)]}


def _copies(*specs):
    """The printed-copy list a fingerprint is made of, spelled short: (name, size_mm, instance)."""
    return [{"name": n, "size_mm": s, "instance": i} for n, s, i in specs]


CUBE = {"id": 1, "index": 0, "name": "cube.stl", "plate": 0, "instances": 1, "size_mm": [20, 20, 20]}
BRACKET = {"id": 2, "index": 1, "name": "bracket.stl", "plate": 1, "instances": 1,
           "size_mm": [30, 10, 5]}
TWO_PLATES = {"count": 2, "objects": [dict(CUBE), dict(BRACKET)]}


def _two_plates(current: int = 1):
    """cube.stl on plate 0, bracket.stl on plate 1, and `current` is the plate that gets sliced."""
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=TWO_PLATES))
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json=_plate_reply(current)))


@respx.mock
async def test_save_gcode_records_the_current_plate_not_the_whole_project(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _two_plates(current=1)
    out = await srv.save_gcode("bracket.gcode")

    row = oc.get(out["outcome_row_id"])
    assert out["outcome_recorded"] is True and out["model_name"] == "bracket.stl"
    assert out["plate"] == 1 and out["plate_attribution"] == "plate"
    assert "attribution_warning" not in out
    assert row["model_name"] == "bracket.stl"
    assert row["geometry_hash"] == oc.printed_geometry_hash(_copies(("bracket.stl", [30, 10, 5], 0)))
    # the whole-project fingerprint is the bug: it says "cube.stl and bracket.stl", one plate's
    # slice must never carry it
    assert row["geometry_hash"] != oc.printed_geometry_hash(
        _copies(("cube.stl", [20, 20, 20], 0), ("bracket.stl", [30, 10, 5], 0)))


@respx.mock
async def test_save_gcode_default_filename_comes_from_the_current_plate(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _two_plates(current=1)
    out = await srv.save_gcode()
    assert out["filename"].startswith("bracket.stl_")


@respx.mock
async def test_save_gcode_the_plate_that_is_current_decides_what_the_row_says(monkeypatch, tmp_path):
    # One project, one object list, two saves with a different plate current: the rows cannot share
    # a fingerprint or a name, or a print of one plate recalls the other plate's settings.
    _env(monkeypatch, tmp_path)
    _two_plates(current=0)
    row0 = oc.get((await srv.save_gcode("plate0.gcode"))["outcome_row_id"])
    _two_plates(current=1)
    row1 = oc.get((await srv.save_gcode("plate1.gcode"))["outcome_row_id"])

    assert (row0["model_name"], row1["model_name"]) == ("cube.stl", "bracket.stl")
    assert row0["geometry_hash"] == oc.printed_geometry_hash(_copies(("cube.stl", [20, 20, 20], 0)))
    assert row1["geometry_hash"] == oc.printed_geometry_hash(_copies(("bracket.stl", [30, 10, 5], 0)))
    assert row0["geometry_hash"] != row1["geometry_hash"]


@respx.mock
async def test_save_gcode_leaves_out_an_object_parked_off_every_plate(monkeypatch, tmp_path):
    # plate -1 in the object list is the fork's answer for an object an earlier delete_plate left
    # beside the bed. On the wire it simply is not on any plate's list, and a plate's own list is what
    # decides what that plate prints, so it belongs to no slice.
    _env(monkeypatch, tmp_path)
    parked = {"id": 3, "index": 2, "name": "parked.stl", "plate": -1, "instances": 1,
              "size_mm": [10, 10, 10]}
    objs = {"count": 3, "objects": [dict(BRACKET), dict(CUBE, id=4, plate=1), dict(parked)]}
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=objs))
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json=_plate_reply(
        1, members={0: [(1, 0)], 1: [(2, 0), (4, 0)]})))

    out = await srv.save_gcode("two-on-one.gcode")

    assert out["model_name"] == "bracket.stl" and out["plate"] == 1
    assert out["printed_copies"] == 2
    row = oc.get(out["outcome_row_id"])
    assert row["geometry_hash"] == oc.printed_geometry_hash(
        _copies(("bracket.stl", [30, 10, 5], 0), ("cube.stl", [20, 20, 20], 0)))
    # borrowing the parked object's identity, or leaving it out of the count of what the plate prints,
    # are both the same fault in the other direction
    assert row["geometry_hash"] != oc.printed_geometry_hash(
        _copies(("bracket.stl", [30, 10, 5], 0), ("cube.stl", [20, 20, 20], 0),
                ("parked.stl", [10, 10, 10], 0)))


@respx.mock
async def test_save_gcode_a_plate_that_lists_its_copies_is_attributed_by_that_list(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json=_plate_reply(0, count=1)))

    out = await srv.save_gcode("listed.gcode")

    assert out["plate"] == 0 and out["plate_attribution"] == "plate" and out["model_name"] == "cube"
    assert out["printed_copies"] == 1
    assert oc.get(out["outcome_row_id"])["geometry_hash"] == oc.printed_geometry_hash(
        _copies(("cube", [20, 20, 20], 0)))
    assert "attribution_warning" not in out


@respx.mock
async def test_save_gcode_a_single_plate_project_that_lists_nothing_still_takes_every_copy(monkeypatch,
                                                                                           tmp_path):
    # A fork that reports one plate and keeps its copies to itself: with one plate every copy of every
    # object is on it, so the whole list is that plate's list, as it was before the plate API.
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json=_silent_plates(0, count=1)))

    out = await srv.save_gcode("one-plate.gcode")

    assert out["plate"] == 0 and out["plate_attribution"] == "single-plate" and out["model_name"] == "cube"
    assert out["printed_copies"] == 1
    assert oc.get(out["outcome_row_id"])["geometry_hash"] == oc.printed_geometry_hash(
        _copies(("cube", [20, 20, 20], 0)))
    assert "attribution_warning" not in out


@respx.mock
async def test_save_gcode_older_build_without_the_plate_api_keeps_working(monkeypatch, tmp_path):
    # _routes() answers /api/v1/plates with 404: a fork before v2.4.2-mcp.13 shows nothing about
    # plates, so the project is all there is to go on. Saving must not break or lose the row over it.
    _env(monkeypatch, tmp_path)
    route = respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.save_gcode("old-build.gcode")

    assert (tmp_path / "gcode" / "old-build.gcode").exists()
    assert out["outcome_recorded"] is True and out["model_name"] == "cube"
    assert out["plate"] is None and out["plate_attribution"] == "project"
    assert out["printed_copies"] == 1
    assert "attribution_warning" not in out
    assert oc.get(out["outcome_row_id"])["geometry_hash"] == oc.printed_geometry_hash(
        _copies(("cube", [20, 20, 20], 0))
    )
    # the legacy build still pays for exactly one plate probe: there is no plate to switch to
    assert route.call_count == 1


@respx.mock
async def test_save_gcode_a_404_answered_with_a_page_is_still_a_missing_route(monkeypatch, tmp_path):
    # An old build answering an unknown path with a page and no JSON body at all: the route's absence
    # is the one thing a 404 can be evidence of, and it is read as such whatever the body looks like.
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(404, content=b"<html>404</html>"))

    out = await srv.save_gcode("old-page.gcode")

    assert out["plate_attribution"] == "project" and out["model_name"] == "cube"
    assert "attribution_warning" not in out


@respx.mock
async def test_save_gcode_a_plates_reply_that_is_not_json_at_all_still_saves(monkeypatch, tmp_path):
    # 200 with an HTML body: something answered, and it was not the plate API. That is NOT a legacy
    # build - only a 404 of the route itself can say that - so the save goes on and the row claims no
    # project-wide identity, because a modern multi-plate fork whose reply was mangled would otherwise
    # get every plate's objects fingerprinted onto this one plate's file.
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, content=b"<html>nope</html>"))

    out = await srv.save_gcode("html-reply.gcode")

    assert out["outcome_recorded"] is True and out["plate_attribution"] == "unknown"
    assert out["model_name"] is None and oc.get(out["outcome_row_id"])["geometry_hash"] is None
    assert "not JSON" in out["attribution_warning"]
    assert (tmp_path / "gcode" / "html-reply.gcode").read_bytes() == b"G28\nG1 X1\n"


@respx.mock
async def test_save_gcode_several_plates_without_membership_records_nothing_false(monkeypatch, tmp_path):
    # Two plates and objects that do not say which one they are on: some of them may be on the plate
    # just sliced and some not. The row keeps what is true of it and drops what would be a guess.
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json=_silent_plates(1)))

    out = await srv.save_gcode("nameless-objects.gcode")

    row = oc.get(out["outcome_row_id"])
    assert (tmp_path / "gcode" / "nameless-objects.gcode").read_bytes() == b"G28\nG1 X1\n"
    assert out["outcome_recorded"] is True and out["plate_attribution"] == "unknown"
    assert out["model_name"] is None and row["model_name"] is None and row["geometry_hash"] is None
    assert row["settings_summary"] == {"layer_height": "0.5", "fan_max_speed": "60"}
    # the plate named itself the same both times, so the estimates read in between may stay: only a
    # window whose plate moved (or could not be pinned) loses them
    assert row["est_time_s"] == 1234.0 and row["est_filament_g"] == 5.6
    assert row["printer_id"] == "Test Printer"
    assert "several plates" in out["attribution_warning"]
    # a default filename still says where the file came from, because the slicer did name the plate
    # (the plate's space is sanitised to "_" like every other character in a filename)
    named = await srv.save_gcode()
    assert named["filename"].startswith("Plate_2_")


@respx.mock
async def test_save_gcode_a_plate_switched_mid_read_records_nothing_false(monkeypatch, tmp_path):
    # The user clicks another plate between the G-code being taken and the objects being read: which
    # plate the bytes came from is no longer knowable, so nothing gets attributed to either.
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json=TWO_PLATES))
    route = respx.get(f"{B}/api/v1/plates").mock(side_effect=[
        httpx.Response(200, json=_plate_reply(0)), httpx.Response(200, json=_plate_reply(1))])

    out = await srv.save_gcode("switched.gcode")

    assert route.call_count == 2, "the plate must be re-read once a second plate exists to switch to"
    row = oc.get(out["outcome_row_id"])
    assert out["plate_attribution"] == "unknown" and out["model_name"] is None
    assert row["model_name"] is None and row["geometry_hash"] is None
    # and the estimates go the same way as the identity: they are the current plate's numbers, read
    # inside a window whose plate is in doubt, so they belong to no row
    assert row["est_time_s"] is None and row["est_filament_g"] is None
    assert "changed while" in out["attribution_warning"]
    assert "estimates are left out" in out["attribution_warning"]
    assert (tmp_path / "gcode" / "switched.gcode").exists()


@respx.mock
async def test_save_gcode_a_plate_reply_that_names_no_current_plate_records_nothing_false(monkeypatch,
                                                                                            tmp_path):
    # 200 with no `current`, no `plates` list and no `count` (a malformed or partial reply): we know
    # nothing, so we attribute nothing, and the save still lands.
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json={"detail": "nonsense"}))

    out = await srv.save_gcode("malformed.gcode")

    assert out["outcome_recorded"] is True and out["plate_attribution"] == "unknown"
    assert out["model_name"] is None and oc.get(out["outcome_row_id"])["geometry_hash"] is None
    assert "does not say which plate is current" in out["attribution_warning"]


@respx.mock
async def test_save_gcode_a_one_plate_project_reads_the_plate_list_twice(monkeypatch, tmp_path):
    # The closing read used to be skipped on a one-plate project, on the theory that there is no other
    # plate to click. The index is not the only thing that can stay put while the plate changes: a copy
    # duplicated onto the bed, deleted from it, or moved off it changes what that plate's G-code prints
    # with the index exactly where it was, and across two reads is the only place that can be seen. The
    # second round trip is what buys the membership check, so an ordinary save pays for it.
    _env(monkeypatch, tmp_path)
    route = respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json=_plate_reply(0, count=1)))

    out = await srv.save_gcode("twice.gcode")

    assert route.call_count == 2, "the closing read is what proves the copies did not change"
    assert out["plate"] == 0 and out["plate_attribution"] == "plate" and out["model_name"] == "cube"
    assert "attribution_warning" not in out


@respx.mock
async def test_save_gcode_an_empty_current_plate_is_named_by_the_plate(monkeypatch, tmp_path):
    # A plate with no objects on it is a fact about the plate, not an unknown: the row says which
    # plate it was rather than borrowing another plate's object.
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json={"count": 1,
                                                                                 "objects": [dict(CUBE)]}))
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json=_plate_reply(
        1, members={0: [(1, 0)], 1: []})))

    out = await srv.save_gcode("empty-plate.gcode")

    assert out["model_name"] == "Plate 2" and out["plate"] == 1 and out["plate_attribution"] == "plate"
    assert oc.get(out["outcome_row_id"])["model_name"] == "Plate 2"
    assert "attribution_warning" not in out


@respx.mock
async def test_save_gcode_names_the_row_by_the_plate_when_the_plate_has_no_name(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/objects").mock(return_value=httpx.Response(200, json={"count": 0, "objects": []}))
    reply = _plate_reply(1, members={0: [], 1: []})
    for plate in reply["plates"]:
        plate["name"] = ""
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json=reply))

    out = await srv.save_gcode()

    assert out["model_name"] == "plate" and out["plate"] == 1
    assert out["filename"].startswith("plate_")


@respx.mock
async def test_save_gcode_a_row_without_attribution_still_joins_to_its_print(monkeypatch, tmp_path):
    # The warning promises the finished print still finds its row by filename, so that has to be true
    # of a row carrying neither a model name nor a fingerprint.
    _env(monkeypatch, tmp_path)
    respx.get(f"{B}/api/v1/plates").mock(return_value=httpx.Response(200, json=_silent_plates(1)))
    out = await srv.save_gcode("joinable.gcode")
    row_id = out["outcome_row_id"]

    joined = oc.record_outcome({"job_id": "77", "filename": "joinable.gcode", "status": "completed",
                                "start_time": time.time() + 1, "end_time": time.time() + 61,
                                "total_duration": 600})

    assert joined == row_id
    row = oc.get(row_id)
    assert row["result"] == "success" and row["duration_s"] == 600
    assert row["model_name"] is None and row["geometry_hash"] is None
    assert out["attribution_warning"].endswith("select_plate and save again.")
