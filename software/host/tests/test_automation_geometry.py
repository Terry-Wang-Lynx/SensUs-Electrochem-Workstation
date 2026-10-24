import json
from pathlib import Path

import pytest


ASSET = Path(__file__).parents[1] / "pa_host/gui/ht-apparatus.json"


def geometry():
    assert ASSET.exists(), "The scene must use the exported Fusion assembly, not invented stage boxes"
    return json.loads(ASSET.read_text())


def test_fusion_assembly_keeps_fixed_backframe_and_tray_motion_membership():
    data = geometry()
    parts = {part["name"]: part for part in data["parts"]}
    assert len(parts) == 108
    assert parts["Z_Backframe_PRINT"]["motion"] == "fixed"
    assert parts["Z50_Body_ENVELOPE"]["motion"] == "fixed"
    assert parts["Tray_PRINT"]["motion"] == "x"
    assert parts["96well_REFERENCE"]["motion"] == "x"
    assert parts["Orange_Arm_Dual_Cradle_PRINT"]["motion"] == "z"
    assert parts["Eight_Sockets_Relief6_PRINT"]["motion"] == "z"
    assert parts["Base_PRINT"]["bbox_mm"] == pytest.approx([-140, -95, -6, 140, 135, 8])
    assert data["reference_pose"] == {"x_offset_mm": 49.5, "lift_mm": 35}
    assert all(len(source["sha256"]) == 64 for source in data["sources"])
    assert data["excluded_audit_helpers"] == 24


def test_well_centers_come_from_saved_brep_and_align_with_eight_electrodes():
    data = geometry()
    assert len(data["wells"]) == 96
    electrodes = {part["name"][0]: part for part in data["parts"] if part["name"].endswith("_Electrode_REFERENCE")}
    assert set(electrodes) == set("ABCDEFGH")
    for well in data["wells"]:
        bounds = electrodes[well["row"]]["bbox_mm"]
        center = [(bounds[axis] + bounds[axis + 3]) / 2 for axis in range(2)]
        assert [well["center"][0] - (well["column"] - 1) * 9, well["center"][1]] == pytest.approx(center)
        assert well["center"][2] == pytest.approx(67)
        assert well["floor_z"] == pytest.approx(57)


def test_meshes_preserve_cad_extents_and_have_only_valid_triangle_indices():
    for part in geometry()["parts"]:
        vertices, faces = part["vertices"], part["faces"]
        assert vertices and faces
        assert all(len(face) == 3 and min(face) >= 0 and max(face) < len(vertices) for face in faces)
        bounds = [min(vertex[axis] for vertex in vertices) for axis in range(3)]
        bounds += [max(vertex[axis] for vertex in vertices) for axis in range(3)]
        assert bounds == pytest.approx(part["bbox_mm"], abs=0.02), part["name"]
