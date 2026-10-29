"""Convert 3MF files to STL or reduce the size of STL/3MF meshes."""

import argparse
from pathlib import Path
import sys
import tempfile

import numpy as np

try:
    import trimesh
except ImportError:
    trimesh = None

try:
    import lib3mf
except ImportError:
    lib3mf = None


# A binary STL consists of an 80-byte header, a 4-byte face count, and
# 50 bytes for each triangular face.
MIN_BINARY_STL_BYTES = 84 + (4 * 50)
UNIT_TO_MM = {
    "MicroMeter": 0.001,
    "MilliMeter": 1.0,
    "CentiMeter": 10.0,
    "Inch": 25.4,
    "Foot": 304.8,
    "Meter": 1000.0,
}


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Convert 3MF files to STL or reduce mesh size."
    )
    parser.add_argument("input", help="Input STL or 3MF file")
    parser.add_argument(
        "--mode",
        choices=("convert", "optimize"),
        help="convert creates an STL; optimize reduces STL/3MF to the target size",
    )
    parser.add_argument(
        "--target-mb",
        type=float,
        default=48.0,
        help="maximum output size for optimize mode (default: 48)",
    )
    return parser.parse_args()


def require_dependencies(source: Path):
    if trimesh is None:
        sys.exit(
            "Missing dependency: trimesh. Create and activate a virtual "
            "environment, then run: pip install -r requirements.txt"
        )
    if source.suffix.lower() == ".3mf" and lib3mf is None:
        sys.exit(
            "Missing dependency for 3MF files: lib3mf. Create and activate "
            "a virtual environment, then run: pip install -r requirements.txt"
        )


def load_3mf_mesh(source: Path):
    """Load the 3MF build, including component and build transforms."""
    wrapper = lib3mf.Wrapper()
    model = wrapper.CreateModel()
    reader = model.QueryReader("3mf")

    try:
        reader.ReadFromFile(str(source))
    except Exception as exc:
        sys.exit(f"Could not read 3MF file: {exc}")

    def transform_matrix(transform):
        # lib3mf exposes the 3MF 3x4 matrix in column-major fields.
        matrix = np.eye(4)
        matrix[:3, :4] = np.asarray(transform.Fields, dtype=float).T
        return matrix

    def mesh_from_object(model_object, ancestors=()):
        resource_id = model_object.GetModelResourceID()
        if resource_id in ancestors:
            sys.exit("The 3MF file contains a circular component reference.")

        if isinstance(model_object, lib3mf.MeshObject):
            return trimesh.Trimesh(
                vertices=[list(vertex.Coordinates) for vertex in model_object.GetVertices()],
                faces=[
                    list(triangle.Indices)
                    for triangle in model_object.GetTriangleIndices()
                ],
                process=False,
            )

        if not isinstance(model_object, lib3mf.ComponentsObject):
            sys.exit("The 3MF file contains an unsupported object type.")

        meshes = []
        for index in range(model_object.GetComponentCount()):
            component = model_object.GetComponent(index)
            mesh = mesh_from_object(
                component.GetObjectResource(), ancestors + (resource_id,)
            )
            if component.HasTransform():
                mesh.apply_transform(transform_matrix(component.GetTransform()))
            meshes.append(mesh)
        return trimesh.util.concatenate(meshes) if meshes else None

    meshes = []
    build_items = model.GetBuildItems()
    while build_items.MoveNext():
        build_item = build_items.GetCurrent()
        mesh = mesh_from_object(build_item.GetObjectResource())
        if mesh is None:
            continue
        if build_item.HasObjectTransform():
            mesh.apply_transform(transform_matrix(build_item.GetObjectTransform()))
        meshes.append(mesh)

    # Some valid 3MF files omit build items. In that case, export their mesh
    # resources as-is rather than rejecting an otherwise usable model.
    if not meshes:
        mesh_objects = model.GetMeshObjects()
        while mesh_objects.MoveNext():
            mesh_object = mesh_objects.GetCurrentMeshObject()
            mesh = mesh_from_object(mesh_object)
            if len(mesh.faces):
                meshes.append(mesh)

    if not meshes:
        sys.exit("The 3MF file does not contain a triangle mesh to convert.")

    mesh = trimesh.util.concatenate(meshes)
    unit_scale = UNIT_TO_MM.get(model.GetUnit().name)
    if unit_scale is None:
        sys.exit("The 3MF file uses an unsupported measurement unit.")
    mesh.apply_scale(unit_scale)
    return mesh


def load_mesh(source: Path):
    if source.suffix.lower() == ".3mf":
        return load_3mf_mesh(source)

    mesh = trimesh.load_mesh(source, process=False)
    if not isinstance(mesh, trimesh.Trimesh):
        sys.exit("Could not load a valid triangle mesh.")
    return mesh


def export_stl(mesh, output: Path):
    mesh.export(str(output), file_type="stl")
    print(f"Saved: {output}")
    print(f"Triangles: {len(mesh.faces):,}")
    print(f"Final size: {output.stat().st_size / 1e6:.2f} MB")


def convert(source: Path):
    mesh = load_mesh(source)
    if len(mesh.faces) < 4:
        sys.exit("Could not load a valid triangle mesh.")
    output = source.with_name(source.stem + "_converted.stl")
    export_stl(mesh, output)


def optimize(source: Path, target_mb: float):
    if not 0 < target_mb < 50:
        sys.exit("Target must be greater than 0 and less than 50 MB.")

    target_bytes = int(target_mb * 1_000_000)
    if target_bytes < MIN_BINARY_STL_BYTES:
        sys.exit(
            "Target is too small for a valid binary STL with at least four "
            f"triangles ({MIN_BINARY_STL_BYTES / 1e6:.6f} MB)."
        )

    mesh = load_mesh(source)
    if len(mesh.faces) < 4:
        sys.exit("Could not load a valid triangle mesh.")

    # STL frequently repeats a vertex for every face. Decimation needs shared
    # vertices to recognise topology and reduce the mesh effectively.
    mesh.merge_vertices()
    output = source.with_name(source.stem + "_optimized.stl")

    print(f"Original size: {source.stat().st_size / 1e6:.2f} MB")
    if source.suffix.lower() == ".stl" and source.stat().st_size <= target_bytes:
        print("File already meets the target.")
        return

    print(f"Original triangles: {len(mesh.faces):,}")
    face_target = max(4, (target_bytes - 84) // 50)
    if face_target >= len(mesh.faces):
        print("Mesh already meets the target; exporting without simplification.")
        export_stl(mesh, output)
        return

    for attempt in range(1, 10):
        face_count = max(4, int(face_target * (0.85 ** (attempt - 1))))
        print(f"Attempt {attempt}: target {face_count:,} triangles")

        try:
            reduced = mesh.simplify_quadric_decimation(face_count=face_count)
        except Exception as exc:
            sys.exit(
                "Simplification failed. Check fast-simplification installation. "
                f"Details: {exc}"
            )

        if not isinstance(reduced, trimesh.Trimesh) or len(reduced.faces) < 4:
            print("Invalid result; trying a different reduction.")
            continue

        with tempfile.NamedTemporaryFile(
            dir=output.parent,
            prefix=f".{output.stem}-",
            suffix=".stl",
            delete=False,
        ) as temporary_file:
            candidate = Path(temporary_file.name)

        try:
            reduced.export(str(candidate), file_type="stl")
            actual_size = candidate.stat().st_size
            print(f"Result: {len(reduced.faces):,} triangles, {actual_size / 1e6:.2f} MB")

            if actual_size <= target_bytes:
                scale = max(float(mesh.extents.max()), 1e-12)
                extent_error = max(abs(reduced.extents - mesh.extents)) / scale
                candidate.replace(output)
                print(f"\nSaved: {output}")
                print(f"Final size: {output.stat().st_size / 1e6:.2f} MB")
                print(f"Watertight: {reduced.is_watertight}")
                print(f"Relative bounding-box change: {extent_error:.4%}")
                if extent_error > 0.005:
                    print("WARNING: Dimensions changed by more than 0.5%.")
                print("IMPORTANT: Inspect the model in a slicer before printing.")
                return
        finally:
            candidate.unlink(missing_ok=True)

    sys.exit("Could not reach the requested size after 9 attempts.")


def main():
    args = parse_arguments()
    source = Path(args.input).expanduser().resolve()

    if source.suffix.lower() in {".gcode", ".bgcode"}:
        sys.exit(
            "BGCode/G-code is printer toolpath data, not the original 3D "
            "model. Reconstructing an STL would only be an approximation."
        )
    if not source.is_file():
        sys.exit(f"File not found: {source}")
    if source.suffix.lower() not in {".stl", ".3mf"}:
        sys.exit("Input must be an STL or 3MF file.")

    require_dependencies(source)
    mode = args.mode or ("convert" if source.suffix.lower() == ".3mf" else "optimize")
    if mode == "convert":
        convert(source)
    else:
        optimize(source, args.target_mb)


if __name__ == "__main__":
    main()
