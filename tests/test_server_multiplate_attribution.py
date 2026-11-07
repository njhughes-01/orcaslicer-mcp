"""Multi-plate attribution, against the wire shapes fork v2.4.2-mcp.13 actually answers with.

The fault these pin shut has one root and four faces, and all four came out of a re-run of the
2026-11-07 live slicer check (`cc2-verification-2026-11-07.md`), where `save_gcode` named an original
object for a G-code file that independently parsed as a different, temporary part:

1. membership came from each object's own `plate`, which names only where that object's FIRST instance
   sits. `/api/v1/plates` is where the fork publishes what a plate prints, as its own `objects` list of
   {id, instance} copies - the same pair `move_object_to_plate` takes. A duplicated part, or copies
   spread over two plates, is invisible to the object field and unmistakable on the plate list, and a
   fingerprint built from objects cannot tell one copy of a part from three.
2. every plate-read failure was reported the same way as a genuinely missing route, and "this build has
   no plate API" is the one answer that licenses attributing the whole project to one plate's file. A
   timeout on a modern multi-plate fork looked exactly like a pre-mcp.13 fork.
3. the plate was re-read after the objects and before the settings and the estimates, so a plate clicked
   during those went unnoticed and its numbers landed on the row anyway.
4. a reply that contradicts itself - `count` 3 beside a one-entry plate list - was read as whichever of
   the two happened to be consulted first.

The fixtures here therefore carry the per-plate `objects` lists (the earlier fixtures left them empty
and relied on the object field, which is how the fault stayed invisible), and every expectation is
written against what the slicer reported, never against what the project as a whole contains.
"""
import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp import outcomes as oc

B = "http://x:13130"
GCODE = b"G28\nG1 X1\n"
CFG = {"config": {"layer_height": "0.5", "fan_max_speed": "60"}}
STATUS = {"presets": {"printer": "Test Printer", "print": "p", "filaments": ["f"]}}
SLICE = {"state": "done", "percent": 100, "stats": {"estimated_time_seconds": 1234.0,
                                                    "filament_used_g": 5.6}}

CUBE = {"id": 1, "name": "cube.stl", "size": [20.0, 20.0, 20.0]}
BRACKET = {"id": 2, "name": "bracket.stl", "size": [30.0, 10.0, 5.0]}
BRICK = {"id": 3, "name": "brick.stl", "size": [10.0, 10.0, 40.0]}


def _obj(spec, instances=1, plate=None):
    """One entry of `/api/v1/objects` as the fork reports it. `plate` is the field the fix stopped
    trusting: the fork fills it from the object's first instance, so it is only ever set here to prove
    a test is not reading it."""
    out = {"id": spec["id"], "index": spec["id"] - 1, "name": spec["name"], "instances": instances,
           "size_mm": list(spec["size"]),
           "transform": {"offset": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0]}}
    if plate is not None:
        out["plate"] = plate
    return out


def _objects(*entries):
    return {"count": len(entries), "objects": list(entries)}


def _wire_plate(index, current, members, name=None, **extra):
    """One plate entry as the fork answers it: index, name, current flag, lock state, volume, and the
    copies it holds under `objects` as {id, instance}.

    `members` is the (id, instance) pairs, or a raw dict passed through untouched (so a test can leave
    the `instance` key out, or send something malformed on purpose). `members=None` means the entry
    carries no `objects` key at all: the plate stays silent about its copies, which is not the same
    thing as an empty plate."""
    entry = {"index": index, "name": f"Plate {index + 1}" if name is None else name,
             "current": current, "locked": False, "empty": members == [], "slice_result_valid": True,
             "origin": [0.0, 0.0, 0.0], "bbox_min": [0.0, 0.0, 0.0],
             "bbox_max": [256.0, 256.0, 256.0]}
    if members is not None:
        entry["objects"] = [{"id": m[0], "instance": m[1]} if isinstance(m, tuple) else m
                            for m in members]
    entry.update(extra)
    return entry


def _reply(*plates, **kw):
    """The whole reply. `count` and `current` default to what the plate entries themselves say, and any
    of them can be overridden to build a reply that disagrees with itself."""
    out = {"max": 36, "can_add": True, "can_delete": True, "plates": list(plates)}
    out["count"] = len(plates) if "count" not in kw else kw.pop("count")
    if "current" in kw:
        out["current"] = kw.pop("current")
    else:
        flagged = [p["index"] for p in plates if p.get("current") is True]
        if len(flagged) == 1:
            out["current"] = flagged[0]
    out.update(kw)
    return out


def _current_two(current=1):
    """The two-plate project both suites use: cube.stl (id 1) on plate 0, bracket.stl (id 2) on plate 1,
    each one copy, exactly as the plate entries publish it."""
    return _reply(_wire_plate(0, current == 0, [(1, 0)]), _wire_plate(1, current == 1, [(2, 0)]))


def _env(monkeypatch, tmp_path, objects, order=None):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", B)
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))

    def hit(name, response):
        def _seen(request):
            if order is not None:
                order.append(name)
            if isinstance(response, Exception):
                raise response
            return response

        return _seen

    respx.get(f"{B}/api/v1/gcode").mock(side_effect=hit("gcode", httpx.Response(200, content=GCODE)))
    respx.get(f"{B}/api/v1/objects").mock(side_effect=hit("objects", httpx.Response(200, json=objects)))
    respx.get(url__regex=rf"{B}/api/v1/config.*").mock(
        side_effect=hit("config", httpx.Response(200, json=CFG)))
    respx.get(f"{B}/api/v1/slice/status").mock(side_effect=hit("slice_status", httpx.Response(200, json=SLICE)))
    respx.get(f"{B}/api/v1/status").mock(side_effect=hit("status", httpx.Response(200, json=STATUS)))


def _plates(order=None, *replies):
    """The plate route, answering `replies` in order. The last one repeats, so a test that gives one
    reply still gets it on both reads. An item that is an exception is raised instead of returned.
    Returns the route, so a test can count the reads."""
    seq = list(replies) or [httpx.Response(200, json=_current_two())]

    def _seen(request):
        if order is not None:
            order.append("plates")
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item

    return respx.get(f"{B}/api/v1/plates").mock(side_effect=_seen)


def _copies(*specs):
    """The printed-copy list a fingerprint is made of, spelled short: (name, size_mm, instance)."""
    return [{"name": n, "size_mm": list(s), "instance": i} for n, s, i in specs]


def _hash(*specs):
    return oc.printed_geometry_hash(_copies(*specs))


# --- 1. what a plate prints comes from the plate's own {id, instance} list ------------------------

@respx.mock
async def test_a_plate_is_attributed_by_its_own_copy_list_not_by_the_objects_plate_field(monkeypatch,
                                                                                         tmp_path):
    # The object list claims BOTH objects sit on plate 1 - which is what the fork says when each
    # object's first instance is on plate 1 while its other copies are not, and it is the reason this
    # field cannot decide membership. The plate's own list says plate 1 prints bracket.stl alone, and
    # that is what the row says.
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=1), _obj(BRACKET, plate=1)))
    _plates(None, httpx.Response(200, json=_current_two(1)))

    out = await srv.save_gcode("by-membership.gcode")

    assert out["model_name"] == "bracket.stl" and out["plate_attribution"] == "plate"
    assert out["printed_copies"] == 1
    row = oc.get(out["outcome_row_id"])
    assert row["geometry_hash"] == _hash((BRACKET["name"], BRACKET["size"], 0))
    # the object field's answer, and the whole project, are both the wrong answer
    assert row["geometry_hash"] != _hash((CUBE["name"], CUBE["size"], 0))
    assert row["geometry_hash"] != _hash((CUBE["name"], CUBE["size"], 0),
                                         (BRACKET["name"], BRACKET["size"], 0))


@respx.mock
async def test_every_copy_the_plate_lists_is_in_the_fingerprint(monkeypatch, tmp_path):
    # One object, three copies, all three on the plate the fork says is current. A fingerprint that
    # cannot tell three from one makes a duplicate-and-slice look like the same model re-sliced, and
    # hands the old print's history to the new one.
    _env(monkeypatch, tmp_path, _objects(_obj(BRICK, instances=3, plate=0)))
    _plates(None, httpx.Response(200, json=_reply(
        _wire_plate(0, False, []), _wire_plate(1, True, [(3, 0), (3, 1), (3, 2)]), count=2)))

    out = await srv.save_gcode("three-copies.gcode")

    assert out["model_name"] == "brick.stl" and out["printed_copies"] == 3
    row = oc.get(out["outcome_row_id"])
    assert row["geometry_hash"] == _hash((BRICK["name"], BRICK["size"], 0), (BRICK["name"], BRICK["size"], 1),
                                         (BRICK["name"], BRICK["size"], 2))
    assert row["geometry_hash"] != _hash((BRICK["name"], BRICK["size"], 0))
    # ... and the objects-form hash could not tell them apart at all, which is why rows are written
    # with the printed form now
    assert oc.geometry_hash_for([_obj(BRICK, instances=3, plate=0)]) == \
        oc.geometry_hash_for([_obj(BRICK, instances=1, plate=0)])


@respx.mock
async def test_copies_spread_over_two_plates_are_fingerprinted_per_plate(monkeypatch, tmp_path):
    # The case the object field cannot see: three copies of one object, one of them on plate 0 and two
    # on plate 1, and `plate` naming a single plate for all three. Each plate's row carries its own
    # copies and only its own, so printing plate 0 cannot recall plate 1's history.
    objs = _objects(_obj(BRICK, instances=3, plate=1))
    reply0 = _reply(_wire_plate(0, True, [(3, 0)]), _wire_plate(1, False, [(3, 1), (3, 2)]), count=2)
    reply1 = _reply(_wire_plate(0, False, [(3, 0)]), _wire_plate(1, True, [(3, 1), (3, 2)]), count=2)

    _env(monkeypatch, tmp_path, objs)
    _plates(None, httpx.Response(200, json=reply0))
    out0 = await srv.save_gcode("plate0.gcode")
    _env(monkeypatch, tmp_path, objs)
    _plates(None, httpx.Response(200, json=reply1))
    out1 = await srv.save_gcode("plate1.gcode")

    assert (out0["printed_copies"], out1["printed_copies"]) == (1, 2)
    assert out0["model_name"] == out1["model_name"] == "brick.stl"
    hash0 = oc.get(out0["outcome_row_id"])["geometry_hash"]
    hash1 = oc.get(out1["outcome_row_id"])["geometry_hash"]
    assert hash0 == _hash((BRICK["name"], BRICK["size"], 0))
    assert hash1 == _hash((BRICK["name"], BRICK["size"], 1), (BRICK["name"], BRICK["size"], 2))
    assert hash0 != hash1
    # neither row may carry all three: that fingerprint belongs to no plate that was ever sliced
    assert hash1 != _hash((BRICK["name"], BRICK["size"], 0), (BRICK["name"], BRICK["size"], 1),
                          (BRICK["name"], BRICK["size"], 2))


@respx.mock
async def test_the_same_copy_listed_twice_counts_once(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0)))
    _plates(None, httpx.Response(200, json=_reply(
        _wire_plate(0, False, []), _wire_plate(1, True, [(1, 1), (1, 0), (1, 0)]), count=2)))

    out = await srv.save_gcode("deduped.gcode")

    assert out["printed_copies"] == 2
    assert oc.get(out["outcome_row_id"])["geometry_hash"] == _hash((CUBE["name"], CUBE["size"], 0),
                                                                   (CUBE["name"], CUBE["size"], 1))


@respx.mock
async def test_a_plate_naming_an_object_the_object_list_does_not_report_attributes_nothing(monkeypatch,
                                                                                           tmp_path):
    # The plate's list and the object list must be joined on id. If the plate names an id the object
    # list does not carry, part of what this plate prints has no name and no size, and inventing the
    # rest of it would be a fingerprint that half-matches some other part.
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0)))
    _plates(None, httpx.Response(200, json=_reply(
        _wire_plate(0, False, [(1, 0)]), _wire_plate(1, True, [(2, 0), (7, 0)]), count=2)))

    out = await srv.save_gcode("unjoinable.gcode")

    assert out["plate_attribution"] == "unknown" and out["model_name"] is None
    assert out["printed_copies"] is None and out["plate"] == 1
    row = oc.get(out["outcome_row_id"])
    assert row["model_name"] is None and row["geometry_hash"] is None
    # the plate itself never moved, so the estimates it was read with are still its own
    assert row["est_time_s"] == 1234.0 and row["est_filament_g"] == 5.6
    assert "does not report" in out["attribution_warning"]


@respx.mock
async def test_a_membership_that_omits_the_copy_fails_closed_when_an_object_has_copies(monkeypatch,
                                                                                        tmp_path):
    # A member listed as {"id": 3} and nothing else says the object sits here, not which copy: with
    # three copies of it somewhere in the project, how many of them this plate prints is not on the
    # wire, and guessing one of {1, 2, 3} is a fingerprint wrong two thirds of the time.
    _env(monkeypatch, tmp_path, _objects(_obj(BRICK, instances=3)))
    _plates(None, httpx.Response(200, json=_reply(
        _wire_plate(0, False, []), _wire_plate(1, True, [{"id": 3}]), count=2)))

    out = await srv.save_gcode("unnumbered.gcode")

    assert out["plate_attribution"] == "unknown" and out["model_name"] is None
    assert "which copy" in out["attribution_warning"]


@respx.mock
async def test_a_membership_that_omits_the_copy_is_enough_when_nothing_is_duplicated(monkeypatch,
                                                                                     tmp_path):
    # An object with one copy has exactly one copy to place, so a member listed without an instance
    # still says everything there is to say. Nothing is invented here, so nothing is refused either.
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE), _obj(BRACKET)))
    _plates(None, httpx.Response(200, json=_reply(
        _wire_plate(0, False, []), _wire_plate(1, True, [{"id": 1}, {"id": 2}]), count=2)))

    out = await srv.save_gcode("single-copy-members.gcode")

    assert out["plate_attribution"] == "plate" and out["printed_copies"] == 2
    assert out["model_name"] == "cube.stl"
    assert oc.get(out["outcome_row_id"])["geometry_hash"] == _hash((CUBE["name"], CUBE["size"], 0),
                                                                   (BRACKET["name"], BRACKET["size"], 0))


# --- 2. a real legacy 404 is not a transient failure or a mangled reply ---------------------------

# Each of these answers came back from a build that HAS the plate route. None of them may be reported
# as "an older fork, so the project is all we see", because that is the one answer that puts every
# plate's objects on this plate's file.
UNTRUSTWORTHY_READS = [
    pytest.param(httpx.Response(500, json={"error": "internal"}), id="server-error"),
    pytest.param(httpx.ReadTimeout("no answer"), id="timed-out"),
    pytest.param(httpx.ConnectError("down"), id="unreachable"),
    pytest.param(httpx.Response(401, json={"error": "unauthorized"}), id="refused-token"),
    pytest.param(httpx.Response(422, json={"error": "bad_request"}), id="refused-invalid"),
    pytest.param(httpx.Response(404, json={"error": "unknown_plate"}), id="404-for-a-resource"),
    pytest.param(httpx.Response(200, json=[1, 2, 3]), id="json-that-is-not-an-object"),
    pytest.param(httpx.Response(200, json=None), id="json-null"),
    pytest.param(httpx.Response(200, content=b"<html>nope</html>"), id="not-json-at-all"),
]


@respx.mock
@pytest.mark.parametrize("reply", [p for p in UNTRUSTWORTHY_READS])
async def test_a_plate_read_that_stumbled_is_never_read_as_an_older_build(monkeypatch, tmp_path, reply):
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, plate=1)))
    _plates(None, reply)

    out = await srv.save_gcode("stumbled.gcode")

    assert out["plate_attribution"] == "unknown"
    assert out["plate_attribution"] != "project"
    assert out["model_name"] is None and out["plate"] is None and out["printed_copies"] is None
    row = oc.get(out["outcome_row_id"])
    assert row["model_name"] is None and row["geometry_hash"] is None
    # and a read that could not be had says nothing about which plate the estimates came from either
    assert row["est_time_s"] is None and row["est_filament_g"] is None
    assert (tmp_path / "gcode" / "stumbled.gcode").read_bytes() == GCODE
    assert out["outcome_recorded"] is True and "attribution_warning" in out
    assert row["settings_summary"] == {"layer_height": "0.5", "fan_max_speed": "60"}
    assert row["printer_id"] == "Test Printer"


@respx.mock
@pytest.mark.parametrize("reply", UNTRUSTWORTHY_READS)
async def test_a_stumbled_plate_read_is_not_probed_again(monkeypatch, tmp_path, reply):
    # A second read cannot fix a plate list that would not answer, and it is a round trip through a
    # slicer that serves one request at a time: once the window is known to be untrustworthy, stop.
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, plate=1)))
    route = _plates(None, reply)

    await srv.save_gcode("one-probe.gcode")

    assert route.call_count == 1


LEGACY_404_BODIES = [
    pytest.param(httpx.Response(404, json={"error": "not_found"}), id="fork-dispatch-fallback"),
    pytest.param(httpx.Response(404, json={}), id="empty-object-body"),
    pytest.param(httpx.Response(404, json={"detail": "Not Found"}), id="framework-style-404"),
    pytest.param(httpx.Response(404, content=b""), id="no-body-at-all"),
    pytest.param(httpx.Response(404, content=b"<html>404 not found</html>"), id="html-page"),
]


@respx.mock
@pytest.mark.parametrize("reply", LEGACY_404_BODIES)
async def test_a_genuinely_missing_plate_route_is_still_the_older_build_it_is(monkeypatch, tmp_path,
                                                                             reply):
    # The other half of finding 2: the fix must not cost an old fork what it always recorded. A clean
    # 404 of the endpoint is the only evidence a build has no plate API, so these keep the project-wide
    # row, the estimates, and the single probe, exactly as they had them.
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE)))
    route = _plates(None, reply)

    out = await srv.save_gcode("older-build.gcode")

    assert out["plate_attribution"] == "project" and out["model_name"] == "cube.stl"
    assert out["plate"] is None and out["printed_copies"] == 1
    assert "attribution_warning" not in out
    row = oc.get(out["outcome_row_id"])
    assert row["geometry_hash"] == _hash((CUBE["name"], CUBE["size"], 0))
    assert row["est_time_s"] == 1234.0 and row["est_filament_g"] == 5.6
    assert route.call_count == 1


# --- 3. the plate read brackets every read whose answer lands in the row ---------------------------

@respx.mock
async def test_the_plate_is_read_after_the_last_read_whose_answer_lands_in_the_row(monkeypatch, tmp_path):
    # The second read exists to prove the plate did not move, so it has to come after the LAST read
    # whose answer the row keeps. It used to come straight after the objects, before the settings and
    # the estimates, and a plate clicked in between those two went unseen.
    order: list[str] = []
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, plate=1)), order=order)
    _plates(order, httpx.Response(200, json=_current_two(0)), httpx.Response(200, json=_current_two(0)))

    out = await srv.save_gcode("bracketed.gcode")

    assert order == ["plates", "gcode", "objects", "config", "slice_status", "status", "plates"]
    assert out["plate_attribution"] == "plate" and out["model_name"] == "cube.stl"
    assert oc.get(out["outcome_row_id"])["est_time_s"] == 1234.0


@respx.mock
async def test_a_plate_switched_during_the_settings_and_estimate_reads_records_no_mismatched_stats(
        monkeypatch, tmp_path):
    # Same window, plate moved late: the G-code, the objects and the settings still say plate 1, and so
    # do the estimates - but the re-read that closes the window, taken after them, says plate 0. That
    # is a row that cannot say which plate any of its numbers came from, so it says none of them.
    order: list[str] = []
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, plate=1)), order=order)
    _plates(order, httpx.Response(200, json=_current_two(1)), httpx.Response(200, json=_current_two(0)))

    out = await srv.save_gcode("late-switch.gcode")

    assert order[-1] == "plates", "the closing read must be the last one, after the estimates"
    assert out["plate_attribution"] == "unknown" and out["model_name"] is None
    row = oc.get(out["outcome_row_id"])
    assert row["model_name"] is None and row["geometry_hash"] is None
    assert row["est_time_s"] is None and row["est_filament_g"] is None
    assert "changed while" in out["attribution_warning"]
    assert "estimates are left out" in out["attribution_warning"]
    # the file a caller asked for is still the file on disk, whole
    assert (tmp_path / "gcode" / "late-switch.gcode").read_bytes() == GCODE


@respx.mock
async def test_the_same_plate_with_different_copies_records_nothing_false(monkeypatch, tmp_path):
    # The closing read used to compare the plate INDEX, and the index alone cannot close the window: a
    # copy duplicated onto the plate that stayed current changes what that plate's G-code prints without
    # changing which plate is current. The bytes, the settings and the estimates came from the one-copy
    # plate while the fingerprint would have been made from the two-copy list read afterwards, so neither
    # half of that row describes the print, and the row keeps neither.
    order: list[str] = []
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, instances=2, plate=1)),
         order=order)
    _plates(order,
            httpx.Response(200, json=_reply(_wire_plate(0, False, [(1, 0)]),
                                            _wire_plate(1, True, [(2, 0), (2, 1)]))),
            httpx.Response(200, json=_reply(_wire_plate(0, False, [(1, 0)]),
                                            _wire_plate(1, True, [(2, 0), (2, 1), (2, 2)]))))

    out = await srv.save_gcode("copy-added.gcode")

    assert order[-1] == "plates", "the membership check is only as good as the read that closes the window"
    assert out["plate_attribution"] == "unknown" and out["plate"] == 1
    assert out["model_name"] is None and out["printed_copies"] is None
    row = oc.get(out["outcome_row_id"])
    assert row["model_name"] is None and row["geometry_hash"] is None
    assert row["est_time_s"] is None and row["est_filament_g"] is None
    assert "copies the plate holds changed" in out["attribution_warning"]
    assert "estimates are left out" in out["attribution_warning"]
    assert (tmp_path / "gcode" / "copy-added.gcode").read_bytes() == GCODE


@respx.mock
async def test_a_plate_that_names_its_copies_only_at_the_end_attributes_nothing(monkeypatch, tmp_path):
    # The other half of the same window: the read that began it kept the plate's copies to itself, so
    # whatever the closing read names cannot be confirmed as what was sliced. The fingerprint would be of
    # a membership seen exactly once, and the estimates came from a scene nobody recorded.
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, plate=1)))
    _plates(None,
            httpx.Response(200, json=_reply(_wire_plate(0, False, None), _wire_plate(1, True, None),
                                            count=2, current=1)),
            httpx.Response(200, json=_reply(_wire_plate(0, False, [(1, 0)]), _wire_plate(1, True, [(2, 0)]),
                                            count=2, current=1)))

    out = await srv.save_gcode("silent-then-named.gcode")

    assert out["plate_attribution"] == "unknown" and out["model_name"] is None
    row = oc.get(out["outcome_row_id"])
    assert row["model_name"] is None and row["geometry_hash"] is None and row["est_time_s"] is None
    assert "did not say which copies it held" in out["attribution_warning"]


@respx.mock
async def test_copies_changing_on_a_plate_that_is_not_the_current_one_changes_nothing(monkeypatch,
                                                                                       tmp_path):
    # What has to be proved unchanged is the selected plate's membership. Copies appearing on a plate that
    # is not current say nothing about what this file prints, and refusing a good record for them would be
    # a new way of losing history, so this still records the current plate's two copies and their numbers.
    order: list[str] = []
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, instances=2, plate=0), _obj(BRACKET, instances=2, plate=1)),
         order=order)
    _plates(order,
            httpx.Response(200, json=_reply(_wire_plate(0, False, [(1, 0)]),
                                            _wire_plate(1, True, [(2, 0), (2, 1)]))),
            httpx.Response(200, json=_reply(_wire_plate(0, False, [(1, 0), (1, 1)]),
                                            _wire_plate(1, True, [(2, 0), (2, 1)]))))

    out = await srv.save_gcode("other-plate.gcode")

    assert out["plate_attribution"] == "plate" and out["model_name"] == "bracket.stl"
    assert out["printed_copies"] == 2 and "attribution_warning" not in out
    row = oc.get(out["outcome_row_id"])
    assert row["est_time_s"] == 1234.0 and row["geometry_hash"] == _hash((BRACKET["name"], BRACKET["size"], 0),
                                                                          (BRACKET["name"], BRACKET["size"], 1))


@respx.mock
async def test_a_one_plate_project_pays_for_the_closing_read_too(monkeypatch, tmp_path):
    # The closing read used to be skipped here: a project with one plate has no other plate to click to.
    # The plate index was never the only thing that could change under the window - a copy duplicated onto
    # the bed, deleted from it, or moved off it leaves the current index exactly where it was while
    # changing what that plate's G-code prints - and on a one-plate project two reads are the only way to
    # see it, so the round trip is what the membership check costs and it is paid for.
    order: list[str] = []
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE)), order=order)
    route = _plates(order, httpx.Response(200, json=_reply(_wire_plate(0, True, [(1, 0)]), count=1)))

    out = await srv.save_gcode("cheap.gcode")

    assert route.call_count == 2
    assert order == ["plates", "gcode", "objects", "config", "slice_status", "status", "plates"]
    assert out["plate_attribution"] == "plate" and out["model_name"] == "cube.stl"
    assert "attribution_warning" not in out


@respx.mock
async def test_a_copy_added_to_the_only_plate_during_the_job_records_nothing_false(monkeypatch, tmp_path):
    # The case the skipped closing read rested on, with the copies as the thing that moved instead of the
    # plate: one plate throughout, so the index says nothing went wrong, while the plate that was sliced
    # held one copy and now holds two. The bytes, the settings and the estimates all came from the one-copy
    # scene, so a row fingerprinting two copies of that part describes a print that never happened, and it
    # keeps neither the fingerprint nor the numbers.
    order: list[str] = []
    _env(monkeypatch, tmp_path, _objects(_obj(BRICK, instances=2)), order=order)
    _plates(order,
            httpx.Response(200, json=_reply(_wire_plate(0, True, [(3, 0)]), count=1)),
            httpx.Response(200, json=_reply(_wire_plate(0, True, [(3, 0), (3, 1)]), count=1)))

    out = await srv.save_gcode("one-plate-copy.gcode")

    assert order[-1] == "plates", "a one-plate window closes with a read like any other"
    assert out["plate_attribution"] == "unknown" and out["plate"] == 0
    assert out["model_name"] is None and out["printed_copies"] is None
    row = oc.get(out["outcome_row_id"])
    assert row["model_name"] is None and row["geometry_hash"] is None
    assert row["est_time_s"] is None and row["est_filament_g"] is None
    assert "copies the plate holds changed" in out["attribution_warning"]
    assert (tmp_path / "gcode" / "one-plate-copy.gcode").read_bytes() == GCODE


# The closing read named nothing where the opening read had named the copies. `one-plate` is the case
# that used to walk straight through the back door: with no membership on the closing entry the code
# reached for "one plate, so the whole object list must be this plate's list" and fingerprinted the
# project with the membership unconfirmed either way.
MEMBERSHIP_THEN_SILENCE = [
    pytest.param(lambda: _reply(_wire_plate(0, True, [(1, 0)]), count=1),
                 lambda: _reply(_wire_plate(0, True, None), count=1), id="one-plate"),
    pytest.param(lambda: _reply(_wire_plate(0, False, [(1, 0)]), _wire_plate(1, True, [(2, 0)]), count=2),
                 lambda: _reply(_wire_plate(0, False, None), _wire_plate(1, True, None), count=2),
                 id="two-plates"),
]


@respx.mock
@pytest.mark.parametrize("opening,closing", [p for p in MEMBERSHIP_THEN_SILENCE])
async def test_a_plate_that_named_its_copies_then_went_silent_attributes_nothing(monkeypatch, tmp_path,
                                                                                opening, closing):
    # Silence from a plate that was always silent is a fact about the reply, and the caller reports it as
    # one. Silence arriving where there was an answer is a change in what the reply says about the plate,
    # and the closing read is the last chance to confirm the copies that were sliced - so the row keeps no
    # identity it can no longer prove, and no estimates read into the same window.
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE), _obj(BRACKET, plate=1)))
    _plates(None, httpx.Response(200, json=opening()), httpx.Response(200, json=closing()))

    out = await srv.save_gcode("membership-vanished.gcode")

    assert out["plate_attribution"] == "unknown" and out["plate"] is not None
    assert out["model_name"] is None and out["printed_copies"] is None
    row = oc.get(out["outcome_row_id"])
    assert row["model_name"] is None and row["geometry_hash"] is None
    assert row["est_time_s"] is None and row["est_filament_g"] is None, \
        "a membership that vanished is a window that cannot vouch for its estimates"
    assert "stopped saying which copies it holds" in out["attribution_warning"]
    assert "estimates are left out" in out["attribution_warning"]
    assert (tmp_path / "gcode" / "membership-vanished.gcode").read_bytes() == GCODE


# --- 4. a reply that contradicts itself gets nothing, not a guess --------------------------------

SELF_CONTRADICTORY = [
    pytest.param(lambda: _reply(_wire_plate(0, True, [(1, 0)]), count=3),
                 id="count-above-the-list-it-ships"),
    pytest.param(lambda: _reply(_wire_plate(0, True, [(1, 0)]), _wire_plate(1, False, []), count=3),
                 id="count-3-with-two-plates-listed"),
    pytest.param(lambda: _reply(_wire_plate(0, True, [(1, 0)]), _wire_plate(1, False, []), count=1,
                                current=1),
                 id="current-outside-its-own-count"),
    pytest.param(lambda: _reply(_wire_plate(0, False, []), _wire_plate(1, True, []), count=2, current=0),
                 id="current-disagrees-with-the-flagged-plate"),
    pytest.param(lambda: _reply(_wire_plate(0, True, []), _wire_plate(1, True, []), count=2),
                 id="two-plates-flagged-current"),
    pytest.param(lambda: _reply(_wire_plate(0, True, [(1, 0)]), _wire_plate(0, True, [(2, 0)]), count=2,
                                current=0),
                 id="one-plate-listed-twice"),
    pytest.param(lambda: _reply(_wire_plate(0, True, [(1, 0)]), _wire_plate(1, False, [(1, 0)]), count=2),
                 id="one-copy-on-two-plates-at-once"),
    pytest.param(lambda: _reply(count=0, plates="not-a-list"), id="plates-is-not-a-list"),
    pytest.param(lambda: _reply({**_wire_plate(0, True, []), "objects": "not-a-list"}, count=1),
                 id="a-plates-own-objects-is-not-a-list"),
    pytest.param(lambda: _reply(_wire_plate(0, True, [{"nope": 1}]), count=1),
                 id="a-member-with-no-usable-id"),
    pytest.param(lambda: _reply(_wire_plate(0, True, [{"id": 1, "instance": "first"}]), count=1),
                 id="a-member-with-no-usable-instance"),
]


@respx.mock
@pytest.mark.parametrize("build", [p for p in SELF_CONTRADICTORY])
async def test_a_plate_reply_that_contradicts_itself_attributes_nothing(monkeypatch, tmp_path, build):
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, plate=1)))
    _plates(None, httpx.Response(200, json=build()))

    out = await srv.save_gcode("self-contradictory.gcode")

    assert (tmp_path / "gcode" / "self-contradictory.gcode").read_bytes() == GCODE
    assert out["plate_attribution"] == "unknown" and out["model_name"] is None
    assert out["plate"] is None and out["printed_copies"] is None
    row = oc.get(out["outcome_row_id"])
    assert row["model_name"] is None and row["geometry_hash"] is None
    assert row["est_time_s"] is None and row["est_filament_g"] is None
    assert "contradicts itself" in out["attribution_warning"]


@respx.mock
async def test_a_count_of_three_beside_a_one_plate_list_is_read_as_neither(monkeypatch, tmp_path):
    # The finding's own words: `count` says three, the list published with it says one. Whichever one
    # the code happened to consult decides whether this is a three-plate project needing a second read
    # or a one-plate project whose whole object list may be fingerprinted - so it decides neither.
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, plate=1)))
    _plates(None, httpx.Response(200, json=_reply(_wire_plate(0, True, [(1, 0)]), count=3)))

    out = await srv.save_gcode("count-3.gcode")

    assert out["plate"] is None and out["model_name"] is None
    assert out["plate_attribution"] == "unknown"
    assert "count" in out["attribution_warning"]
    assert oc.get(out["outcome_row_id"])["geometry_hash"] is None


# --- recall is held to the same bar ---------------------------------------------------------------

@respx.mock
async def test_recall_counts_the_copies_the_plate_prints(monkeypatch, tmp_path):
    # Three copies on the plate now: a print of one copy of the same part is a different print of a
    # different thing, and handing over its history would be the same fault in the other direction.
    _env(monkeypatch, tmp_path, _objects(_obj(BRICK, instances=3, plate=0)))
    _plates(None, httpx.Response(200, json=_reply(
        _wire_plate(0, False, []), _wire_plate(1, True, [(3, 0), (3, 1), (3, 2)]), count=2)))
    one = oc.printed_geometry_hash(_copies((BRICK["name"], BRICK["size"], 0)))
    three = oc.printed_geometry_hash(_copies((BRICK["name"], BRICK["size"], 0),
                                             (BRICK["name"], BRICK["size"], 1),
                                             (BRICK["name"], BRICK["size"], 2)))
    oc.record_slice("one-copy.gcode", "brick.stl", one, {}, sliced_at=1.0)
    oc.record_slice("three-copies.gcode", "brick.stl", three, {"layer_height": "0.2"}, sliced_at=2.0)

    out = await srv.recall_prints()

    assert out["matched_by"] == "geometry"
    assert [p["gcode_filename"] for p in out["prints"]] == ["three-copies.gcode"]
    assert out["plate"] == 1 and out["plate_attribution"] == "plate"


@respx.mock
async def test_recall_still_finds_a_row_written_before_copies_were_counted(monkeypatch, tmp_path):
    # Rows written before the copies fix carry the objects-form fingerprint of the same plate subset.
    # Refusing to match them would silently cost people their history, so recall looks with both, and
    # only ever with the current plate's subset.
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, plate=1)))
    _plates(None, httpx.Response(200, json=_current_two(1)))
    legacy = oc.geometry_hash_for([_obj(BRACKET, plate=1)])
    oc.record_slice("old-bracket.gcode", "bracket.stl", legacy, {"layer_height": "0.2"}, sliced_at=1.0)
    oc.record_slice("old-cube.gcode", "cube.stl", oc.geometry_hash_for([_obj(CUBE, plate=0)]), {},
                    sliced_at=2.0)

    out = await srv.recall_prints()

    assert out["matched_by"] == "geometry"
    assert [p["gcode_filename"] for p in out["prints"]] == ["old-bracket.gcode"]
    # A match on the pre-copies form is a match on the part, never on what was printed, so it is never
    # handed over without saying that.
    assert "cannot tell one copy" in out["legacy_fingerprint_note"]


# --- What recall may NOT claim from a name --------------------------------------------------------
# A fingerprint that misses says nothing about the part's history, and the answer it falls back to used to
# be a search by the current plate's own part name, reported as "N past prints of <that part>". Names
# cannot count copies, so a plate printing three copies of a part was answering with the settings of a
# print that had one: the same wrong number of copies, with the confidence of a geometry match.

def _three_brick_plates():
    """The two-plate project with brick.stl (id 3) laid down three times on the current plate 1."""
    return httpx.Response(200, json=_reply(_wire_plate(0, False, []),
                                           _wire_plate(1, True, [(3, 0), (3, 1), (3, 2)]), count=2))


@respx.mock
async def test_recall_will_not_call_one_copy_of_a_part_the_history_of_three(monkeypatch, tmp_path):
    # The store's only row is a print of ONE copy of brick.stl written in today's form, whose fingerprint
    # really is a different print, and the plate now prints three. Neither fingerprint matches, so nothing
    # here is this plate's history, and the reply says that instead of naming the part as if it knew.
    _env(monkeypatch, tmp_path, _objects(_obj(BRICK, instances=3, plate=1)))
    _plates(None, _three_brick_plates())
    oc.record_slice("one-copy.gcode", "brick.stl", _hash((BRICK["name"], BRICK["size"], 0)),
                    {"layer_height": "0.2"}, sliced_at=1.0)
    oc.record_outcome({"job_id": "21", "filename": "one-copy.gcode", "status": "completed", "end_time": 2.0})

    out = await srv.recall_prints()

    assert out["matched_by"] == "recent" and out["plate"] == 1
    assert out["plate_attribution"] == "plate"
    assert out["summary"] == "1 recent print: 1 success"
    assert "of brick.stl" not in out["summary"], "the rows are not named as this part's history"
    assert "3 copies of brick.stl" in out["copy_count_note"]
    assert "not the history of a print of several" in out["copy_count_note"]
    assert "legacy_fingerprint_note" not in out


@respx.mock
async def test_a_row_written_before_copies_were_counted_cannot_tell_its_copy_count_and_says_so(
        monkeypatch, tmp_path):
    # The honest half of the same finding: rows written before copies were counted carry a fingerprint that
    # names each part once, so a print of one copy of brick.stl has THE fingerprint of a plate printing
    # three of them. Refusing them would silently cost people their history, and matching them quietly
    # would invent a copy count, so recall matches them and states what it cannot know.
    _env(monkeypatch, tmp_path, _objects(_obj(BRICK, instances=3, plate=1)))
    _plates(None, _three_brick_plates())
    assert oc.geometry_hash_for([_obj(BRICK, instances=1)]) == oc.geometry_hash_for([_obj(BRICK,
                                                                                          instances=3)])
    oc.record_slice("old-brick.gcode", "brick.stl", oc.geometry_hash_for([_obj(BRICK, instances=1)]),
                    {"layer_height": "0.2"}, sliced_at=1.0)
    oc.record_outcome({"job_id": "22", "filename": "old-brick.gcode", "status": "completed", "end_time": 2.0})

    out = await srv.recall_prints()

    assert out["matched_by"] == "geometry"
    assert [p["gcode_filename"] for p in out["prints"]] == ["old-brick.gcode"]
    assert "cannot tell one copy" in out["legacy_fingerprint_note"]
    assert "3 copies of brick.stl" in out["legacy_fingerprint_note"]
    assert "copy_count_note" not in out


@respx.mock
async def test_a_name_the_caller_gave_still_searches_and_says_the_copies_are_unverified(monkeypatch,
                                                                                        tmp_path):
    # Searching by name is what the caller asked for when it named the model, so it still runs - but the
    # rows it finds cannot be checked for a copy count, and the reply carries that beside them rather than
    # letting "2 past prints of brick.stl" stand as this plate's record.
    _env(monkeypatch, tmp_path, _objects(_obj(BRICK, instances=3, plate=1)))
    _plates(None, _three_brick_plates())
    oc.record_slice("one-copy.gcode", "brick.stl", _hash((BRICK["name"], BRICK["size"], 0)),
                    {"layer_height": "0.2"}, sliced_at=1.0)
    oc.record_outcome({"job_id": "23", "filename": "one-copy.gcode", "status": "completed", "end_time": 2.0})

    out = await srv.recall_prints(model_name="brick.stl")

    assert out["matched_by"] == "name"
    assert [p["gcode_filename"] for p in out["prints"]] == ["one-copy.gcode"]
    assert out["summary"] == "1 past print of brick.stl: 1 success"
    assert "3 copies of brick.stl" in out["copy_count_note"]


@respx.mock
@pytest.mark.parametrize("reply", UNTRUSTWORTHY_READS)
async def test_recall_never_matches_the_project_when_the_plate_read_stumbled(monkeypatch, tmp_path,
                                                                              reply):
    # What the old code did on a modern multi-plate fork whose plate read failed: fall back to "project"
    # and match on every object in it, reporting a whole-project fingerprint as this plate's history.
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, plate=1)))
    _plates(None, reply)
    everything = oc.geometry_hash_for([_obj(CUBE, plate=0), _obj(BRACKET, plate=1)])
    oc.record_slice("everything.gcode", "cube.stl", everything, {}, sliced_at=1.0)

    out = await srv.recall_prints()

    assert out["matched_by"] == "recent"
    assert out["plate_attribution"] == "unknown" and out["plate"] is None
    assert "No fingerprint match was attempted" in out["attribution_note"]


@respx.mock
async def test_recall_refuses_the_fingerprint_when_the_plate_moves_while_it_looks(monkeypatch, tmp_path):
    order: list[str] = []
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, plate=1)), order=order)
    _plates(order, httpx.Response(200, json=_current_two(1)), httpx.Response(200, json=_current_two(0)))
    oc.record_slice("bracket.gcode", "bracket.stl",
                    _hash((BRACKET["name"], BRACKET["size"], 0)), {}, sliced_at=1.0)

    out = await srv.recall_prints()

    assert order == ["plates", "objects", "plates"]
    assert out["matched_by"] == "recent"
    assert "changed while" in out["attribution_note"]


@respx.mock
async def test_recall_refuses_the_fingerprint_when_the_copies_change_while_it_looks(monkeypatch, tmp_path):
    # Recall brackets its own look the same way, so the same window applies to it: the plate that stayed
    # current gained a copy between the two reads, and the fingerprint of one copy is not the fingerprint
    # of the plate, so it looks at nothing and says which of its reads could not be paired.
    order: list[str] = []
    _env(monkeypatch, tmp_path, _objects(_obj(CUBE, plate=0), _obj(BRACKET, instances=2, plate=1)), order=order)
    _plates(order,
            httpx.Response(200, json=_reply(_wire_plate(0, False, [(1, 0)]),
                                            _wire_plate(1, True, [(2, 0)]))),
            httpx.Response(200, json=_reply(_wire_plate(0, False, [(1, 0)]),
                                            _wire_plate(1, True, [(2, 0), (2, 1)]))))
    oc.record_slice("bracket.gcode", "bracket.stl", _hash((BRACKET["name"], BRACKET["size"], 0)), {},
                    sliced_at=1.0)

    out = await srv.recall_prints()

    assert order == ["plates", "objects", "plates"]
    assert out["matched_by"] == "recent"
    assert out["plate"] == 1 and out["plate_attribution"] == "unknown"
    assert "copies the plate holds changed while" in out["attribution_note"]


@respx.mock
async def test_recall_pays_for_the_closing_read_on_a_one_plate_project(monkeypatch, tmp_path):
    # The closing read used to be skipped when there was only one plate to read, because there was no
    # other plate to switch to. Recall is about to search for a fingerprint of COPIES, and on a one-plate
    # project the plate index is exactly the thing that cannot say whether the copies are still the ones
    # it fingerprinted, so the bracket is paid for here too.
    order: list[str] = []
    _env(monkeypatch, tmp_path, _objects(_obj(BRICK)), order=order)
    route = _plates(order, httpx.Response(200, json=_reply(_wire_plate(0, True, [(3, 0)]), count=1)))
    oc.record_slice("brick.gcode", "brick.stl", _hash((BRICK["name"], BRICK["size"], 0)), {}, sliced_at=1.0)

    out = await srv.recall_prints()

    assert route.call_count == 2 and order == ["plates", "objects", "plates"]
    assert out["matched_by"] == "geometry" and out["plate_attribution"] == "plate"
    assert [p["gcode_filename"] for p in out["prints"]] == ["brick.gcode"]


@respx.mock
async def test_recall_refuses_the_fingerprint_when_a_copy_changes_on_the_only_plate(monkeypatch, tmp_path):
    # The window the skipped closing read left open, on the reader's side: one plate the whole time, and a
    # copy duplicated onto it between the two reads. The fingerprint of one copy is not the fingerprint of
    # the plate, so recall looks with neither and says which of its reads could not be paired.
    order: list[str] = []
    _env(monkeypatch, tmp_path, _objects(_obj(BRICK, instances=2)), order=order)
    _plates(order,
            httpx.Response(200, json=_reply(_wire_plate(0, True, [(3, 0)]), count=1)),
            httpx.Response(200, json=_reply(_wire_plate(0, True, [(3, 0), (3, 1)]), count=1)))
    oc.record_slice("brick.gcode", "brick.stl", _hash((BRICK["name"], BRICK["size"], 0)), {}, sliced_at=1.0)

    out = await srv.recall_prints()

    assert order == ["plates", "objects", "plates"]
    assert out["matched_by"] == "recent" and out["plate"] == 0
    assert out["plate_attribution"] == "unknown"
    assert "copies the plate holds changed while" in out["attribution_note"]


@respx.mock
async def test_recall_refuses_the_fingerprint_when_the_membership_vanishes_while_it_looks(monkeypatch,
                                                                                         tmp_path):
    # Recall brackets its look the same way, and the same asymmetry closes it: the read that began the
    # look named the plate's copies, the one that ended it stopped saying what the plate holds, so the
    # fingerprint it would search for is one it cannot confirm, and it searches nothing.
    _env(monkeypatch, tmp_path, _objects(_obj(BRICK)))
    _plates(None,
            httpx.Response(200, json=_reply(_wire_plate(0, True, [(3, 0)]), count=1)),
            httpx.Response(200, json=_reply(_wire_plate(0, True, None), count=1)))
    oc.record_slice("brick.gcode", "brick.stl", _hash((BRICK["name"], BRICK["size"], 0)), {}, sliced_at=1.0)

    out = await srv.recall_prints()

    assert out["matched_by"] == "recent" and out["plate"] == 0
    assert out["plate_attribution"] == "unknown"
    assert "stopped saying which copies it holds" in out["attribution_note"]


@respx.mock
async def test_a_legacy_fork_still_gets_its_history_matched(monkeypatch, tmp_path):
    # The one build with no plate route has no plate to be wrong about, so recall keeps matching on the
    # project, and the copies a single-plate project holds are still its own to count.
    _env(monkeypatch, tmp_path, _objects(_obj(BRICK, instances=2)))
    _plates(None, httpx.Response(404, json={"error": "not_found"}))
    oc.record_slice("two-bricks.gcode", "brick.stl",
                    _hash((BRICK["name"], BRICK["size"], 0), (BRICK["name"], BRICK["size"], 1)),
                    {"layer_height": "0.2"}, sliced_at=1.0)

    out = await srv.recall_prints()

    assert out["matched_by"] == "geometry" and out["plate_attribution"] == "project"
    assert out["plate"] is None and "attribution_note" not in out


# --- the fingerprint itself ----------------------------------------------------------------------

def test_the_printed_fingerprint_counts_copies_and_the_objects_form_cannot():
    one = _copies((BRICK["name"], BRICK["size"], 0))
    three = _copies((BRICK["name"], BRICK["size"], 0), (BRICK["name"], BRICK["size"], 1),
                    (BRICK["name"], BRICK["size"], 2))
    assert oc.printed_geometry_hash(one) != oc.printed_geometry_hash(three)
    # order on the wire must not matter, or the same plate re-saved after a re-sort would look like new
    assert oc.printed_geometry_hash(list(reversed(three))) == oc.printed_geometry_hash(three)
    # two objects that share a name and a size are still two printed things
    twins = _copies(("peg.stl", [5, 5, 5], 0), ("peg.stl", [5, 5, 5], 0))
    assert oc.printed_geometry_hash(twins) != oc.printed_geometry_hash(_copies(("peg.stl", [5, 5, 5], 0)))
    # an instance index that is not a number is read as the first copy, never as a fourth part
    assert oc.printed_geometry_hash([{"name": "x", "size_mm": [1, 1, 1], "instance": True}]) == \
        oc.printed_geometry_hash([{"name": "x", "size_mm": [1, 1, 1], "instance": 0}])
    assert oc.printed_geometry_hash([]) == oc.printed_geometry_hash(None)
    # the objects form stays exactly what it was, so rows already written stay findable
    assert oc.geometry_hash_for([_obj(BRICK, instances=1)]) == oc.geometry_hash_for([_obj(BRICK, instances=3)])


def test_recall_matches_any_of_the_candidate_fingerprints_and_needs_no_schema_change(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_OUTCOMES_DIR", str(tmp_path))
    oc.record_slice("a.gcode", "part", "hash-a", {}, sliced_at=1.0)
    oc.record_slice("b.gcode", "part", "hash-b", {}, sliced_at=2.0)
    assert [r["gcode_filename"] for r in oc.recall(geometry_hashes=["hash-b", "hash-a"], limit=5)] == \
        ["b.gcode", "a.gcode"]
    assert [r["gcode_filename"] for r in oc.recall(geometry_hashes=["hash-a"], limit=5)] == ["a.gcode"]
    assert [r["gcode_filename"] for r in oc.recall(geometry_hashes=[], limit=5)] == ["b.gcode", "a.gcode"]
    assert oc.recall(geometry_hashes=["nope"], limit=5) == []
