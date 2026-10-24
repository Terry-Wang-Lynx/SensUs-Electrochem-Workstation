"""Tessellate saved mechanical CAD references without modifying their sources."""

import argparse
import hashlib
import json
from pathlib import Path

import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.GeomAbs import GeomAbs_Cylinder


SOURCES = {
    "fusion": "outputs/20261006-130753-Fusion-v3-X150-Z50/bioautomation-v3.step",
    "audit": "outputs/20261011-full-DIN-fastener-audit/bioautomation-full-DIN-audit.step",
    "hardware": "outputs/20261011-full-DIN-fastener-audit/hardware-manifest.json",
    "audit_report": "outputs/20261011-full-DIN-fastener-audit/audit.json",
    "receiver": "outputs/20261012-lower-pin-relief-v3/02-eight-gap1p80-relief6/02-eight-gap1p80-relief6.step",
    "arm": "outputs/20261017-orange-arm-dual-cradle/01-orange-arm-dual-cradle/01-orange-arm-dual-cradle.step",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounds(shape):
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape.wrapped, box, False, False)
    return [round(value, 5) for value in box.Get()]


def tessellate(name, kind, motion, shape, source, solid_index, translation=(0, 0, 0)):
    assert shape.isValid() and shape.Volume() > 0, name
    vertices, faces = shape.tessellate(0.06, 0.18)
    return {"name": name, "kind": kind, "motion": motion,
            "vertices": [[round(value, 5) for value in vertex.toTuple()] for vertex in vertices],
            "faces": faces, "bbox_mm": bounds(shape), "volume_mm3": shape.Volume(),
            "source": source, "solid_index": solid_index, "translation_mm": translation}


def export(source_root, output):
    paths = {name: source_root / filename for name, filename in SOURCES.items()}
    sources = [{"id": name, "path": SOURCES[name], "sha256": digest(path)} for name, path in paths.items()]
    report = json.loads(paths["audit_report"].read_text())
    assert report["source_sha256"][SOURCES["fusion"]] == digest(paths["fusion"])
    original = cq.importers.importStep(str(paths["fusion"])).val().Solids()
    assembly = cq.importers.importStep(str(paths["audit"])).val().Solids()
    hardware = json.loads(paths["hardware"].read_text())
    assert len(original) == 21 and len(assembly) == 132 and len(hardware) == 54
    mapping = [
        ("Base_PRINT", "base", "fixed"), ("X150_Body_ENVELOPE", "metal", "fixed"),
        ("X_Motor_REFERENCE", "chip", "fixed"), ("X_Lead_REFERENCE", "metal", "fixed"),
        ("Z_Backframe_PRINT", "blue", "fixed"), ("Z50_Body_ENVELOPE", "metal", "fixed"),
        ("Z_Motor_REFERENCE", "chip", "fixed"), ("Z_Lead_REFERENCE", "metal", "fixed"),
        ("X_Carriage_REFERENCE", "rail", "x"), ("Tray_PRINT", "blue", "x"),
        ("96well_REFERENCE", "shell", "x"), ("Z_Carriage_REFERENCE", "rail", "z"),
    ]
    parts = []
    for index, (name, kind, motion) in enumerate(mapping):
        assert abs(original[index].Volume() - assembly[index].Volume()) < 0.002
        assert bounds(original[index]) == bounds(assembly[index])
        parts.append(tessellate(name, kind, motion, assembly[index], "audit", index))
    offset = (0, 47, 132)
    for key, name, kind in [("receiver", "Eight_Sockets_Relief6_PRINT", "blue"),
                            ("arm", "Orange_Arm_Dual_Cradle_PRINT", "orange")]:
        shape = cq.importers.importStep(str(paths[key])).val()
        assert len(shape.Solids()) == 1
        parts.append(tessellate(name, kind, "z", shape.translate(offset), key, 0, offset))
    for row_index, row in enumerate("ABCDEFGH"):
        for item_index, (name, kind) in enumerate([("PCB", "pcb"), ("Socket", "chip"),
                                                  ("XH", "shell"), ("Plug", "shell"), ("Electrode", "electrode")]):
            index = 14 + row_index * 5 + item_index
            parts.append(tessellate(f"{row}_{name}_REFERENCE", kind, "z", assembly[index], "audit", index))
    remaining_hardware = dict(enumerate(assembly[54:108], start=54))
    for record in hardware:
        matches = [(index, shape) for index, shape in remaining_hardware.items()
                   if abs(shape.Volume() - record["volume_mm3"]) < 0.002
                   and max(abs(actual - expected) for actual, expected in zip(bounds(shape), record["bbox_mm"])) < 0.0001]
        assert len(matches) == 1, record["name"]
        index, shape = matches[0]
        del remaining_hardware[index]
        parts.append(tessellate(record["name"], "metal", record["motion"], shape, "audit", index))
    assert not remaining_hardware
    wells_by_center = {}
    for face in assembly[10].Faces():
        surface = BRepAdaptor_Surface(face.wrapped)
        if surface.GetType() != GeomAbs_Cylinder:
            continue
        cylinder = surface.Cylinder()
        if abs(abs(cylinder.Axis().Direction().Z()) - 1) > 1e-7 or abs(cylinder.Radius() - 3.1) > 1e-6:
            continue
        position = cylinder.Location()
        center = (round(position.X(), 5), round(position.Y(), 5))
        face_bounds = bounds(face)
        wells_by_center[center] = {"center": [*center, face_bounds[5]],
                                   "floor_z": face_bounds[2], "radius_mm": 3.1}
    columns = sorted({center[0] for center in wells_by_center})
    rows = sorted({center[1] for center in wells_by_center})
    assert len(wells_by_center) == 96 and len(columns) == 12 and len(rows) == 8
    wells = [{"column": column_index + 1, "row": "ABCDEFGH"[row_index], **wells_by_center[(column, row)]}
             for column_index, column in enumerate(columns) for row_index, row in enumerate(rows)]
    result = {"schema_version": "1.0", "units": "mm", "sources": sources, "parts": parts, "wells": wells,
              "reference_pose": report["default_pose"], "excluded_audit_helpers": 24,
              "scope": "Saved Fusion v3 assembly plus dated receiver/arm CAD revisions; not a live Fusion document or hardware calibration",
              "kinematics": {"x": "tray_and_well_plate", "z": "arm_electrodes_and_boards", "backframe": "fixed",
                             "scene_x_sign": -1, "scene_z_sign": -1}}
    result["reference_pose"] = {"x_offset_mm": report["default_pose"]["x"], "lift_mm": report["default_pose"]["lift"]}
    assert len(parts) == 108
    for source in sources:
        assert source["sha256"] == digest(paths[source["id"]]), source["id"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(json.dumps({"parts": len(parts), "wells": len(wells), "triangles": sum(len(part["faces"]) for part in parts),
                      "bytes": output.stat().st_size, "sha256": digest(output), "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "software/host/pa_host/gui/ht-apparatus.json")
    arguments = parser.parse_args()
    export(arguments.source_root.resolve(), arguments.output.resolve())
