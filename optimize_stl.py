"""Safely convert common 3D-print mesh formats to STL and optimize meshes."""

import argparse
import struct
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
import zipfile
import zlib

import numpy as np

try:
    import heatshrink2
except ImportError:
    heatshrink2 = None

try:
    import lib3mf
except ImportError:
    lib3mf = None

try:
    import trimesh
except ImportError:
    trimesh = None


MESH_EXTENSIONS = {".stl", ".obj", ".ply", ".off", ".glb", ".gltf", ".amf"}
THREE_MF_EXTENSION = ".3mf"
TOOLPATH_EXTENSIONS = {".gcode", ".bgcode"}
MIN_BINARY_STL_BYTES = 84 + (4 * 50)
UNIT_TO_MM = {
    "micrometer": 0.001,
    "micron": 0.001,
    "millimeter": 1.0,
    "millimetre": 1.0,
    "centimeter": 10.0,
    "centimetre": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
    "metre": 1000.0,
}
THREE_MF_UNIT_TO_MM = {
    "MicroMeter": 0.001,
    "MilliMeter": 1.0,
    "CentiMeter": 10.0,
    "Inch": 25.4,
    "Foot": 304.8,
    "Meter": 1000.0,
}


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Convert common 3D-print meshes to STL, optimize meshes, or decode BGCode."
    )
    parser.add_argument("input", help="input file")
    parser.add_argument(
        "--mode",
        choices=("convert", "optimize", "decode"),
        help="convert to STL, optimize an STL-sized mesh, or decode BGCode to G-code",
    )
    parser.add_argument("--target-mb", type=float, default=48.0)
    parser.add_argument("--output", type=Path, help="output path; never overwrites input")
    return parser.parse_args()


def fail(message):
    raise ValueError(message)


def require_dependencies(source):
    if trimesh is None:
        fail("Missing dependency: trimesh. Run: pip install -r requirements.txt")
    if source.suffix.lower() == THREE_MF_EXTENSION and lib3mf is None:
        fail("Missing dependency for 3MF files: lib3mf. Run: pip install -r requirements.txt")
    if source.suffix.lower() == ".bgcode" and heatshrink2 is None:
        fail("Missing dependency for BGCode files: heatshrink2. Run: pip install -r requirements.txt")


def transform_matrix(transform):
    """Convert lib3mf's column-major 3x4 transform to a NumPy 4x4 matrix."""
    matrix = np.eye(4)
    matrix[:3, :4] = np.asarray(transform.Fields, dtype=float).T
    return matrix


def load_3mf_mesh(source):
    """Load 3MF build items, components, transforms, and the declared unit."""
    wrapper = lib3mf.Wrapper()
    model = wrapper.CreateModel()
    try:
        model.QueryReader("3mf").ReadFromFile(str(source))
    except Exception as exc:
        fail(f"Could not read 3MF file: {exc}")

    def mesh_from_object(model_object, ancestors=()):
        resource_id = model_object.GetModelResourceID()
        if resource_id in ancestors:
            fail("The 3MF file contains a circular component reference.")
        if isinstance(model_object, lib3mf.MeshObject):
            return trimesh.Trimesh(
                vertices=[list(item.Coordinates) for item in model_object.GetVertices()],
                faces=[list(item.Indices) for item in model_object.GetTriangleIndices()],
                process=False,
            )
        if not isinstance(model_object, lib3mf.ComponentsObject):
            fail("The 3MF file contains an unsupported object type.")
        meshes = []
        for index in range(model_object.GetComponentCount()):
            component = model_object.GetComponent(index)
            mesh = mesh_from_object(component.GetObjectResource(), ancestors + (resource_id,))
            if component.HasTransform():
                mesh.apply_transform(transform_matrix(component.GetTransform()))
            meshes.append(mesh)
        return trimesh.util.concatenate(meshes) if meshes else None

    meshes = []
    build_items = model.GetBuildItems()
    while build_items.MoveNext():
        item = build_items.GetCurrent()
        mesh = mesh_from_object(item.GetObjectResource())
        if mesh is not None:
            if item.HasObjectTransform():
                mesh.apply_transform(transform_matrix(item.GetObjectTransform()))
            meshes.append(mesh)
    if not meshes:
        resources = model.GetMeshObjects()
        while resources.MoveNext():
            meshes.append(mesh_from_object(resources.GetCurrentMeshObject()))
    if not meshes:
        fail("The 3MF file does not contain a triangle mesh.")
    scale = THREE_MF_UNIT_TO_MM.get(model.GetUnit().name)
    if scale is None:
        fail("The 3MF file uses an unsupported unit.")
    mesh = trimesh.util.concatenate(meshes)
    mesh.apply_scale(scale)
    return mesh


def amf_data(source):
    """Return AMF XML from a plain AMF or a zipped AMF package."""
    raw = source.read_bytes()
    if not zipfile.is_zipfile(source):
        return raw
    with zipfile.ZipFile(source) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith((".amf", ".xml"))]
        if len(names) != 1:
            fail("The AMF archive must contain exactly one .amf or .xml model file.")
        entry = archive.getinfo(names[0])
        if entry.file_size > 1_000_000_000:
            fail("The AMF model is too large to load safely.")
        return archive.read(entry)


def text_of(node, name):
    item = node.find(f"{{*}}{name}")
    if item is None or item.text is None:
        fail(f"AMF data is missing <{name}>.")
    return float(item.text)


def load_amf_mesh(source):
    """Load standard AMF geometry; material and color data are not in STL."""
    try:
        root = ElementTree.fromstring(amf_data(source))
    except (ElementTree.ParseError, zipfile.BadZipFile) as exc:
        fail(f"Could not read AMF file: {exc}")
    if root.tag.rsplit("}", 1)[-1] != "amf":
        fail("The file is not a valid AMF model.")
    scale = UNIT_TO_MM.get(root.attrib.get("unit", "millimeter").lower())
    if scale is None:
        fail("The AMF file uses an unsupported unit.")
    meshes = []
    for mesh_node in root.findall(".//{*}object/{*}mesh"):
        vertices = [
            [text_of(vertex, "x"), text_of(vertex, "y"), text_of(vertex, "z")]
            for vertex in mesh_node.findall("{*}vertices/{*}vertex/{*}coordinates")
        ]
        faces = [
            [int(text_of(triangle, "v1")), int(text_of(triangle, "v2")), int(text_of(triangle, "v3"))]
            for triangle in mesh_node.findall("{*}volume/{*}triangle")
        ]
        if vertices and faces:
            meshes.append(trimesh.Trimesh(vertices=vertices, faces=faces, process=False))
    if not meshes:
        fail("The AMF file does not contain a triangle mesh.")
    mesh = trimesh.util.concatenate(meshes)
    mesh.apply_scale(scale)
    return mesh


def load_trimesh_mesh(source):
    try:
        loaded = trimesh.load(source, force="scene", process=False)
        mesh = loaded.to_mesh() if isinstance(loaded, trimesh.Scene) else loaded
    except Exception as exc:
        fail(f"Could not load mesh: {exc}")
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 4:
        fail("Could not load a valid triangle mesh.")
    # glTF specifies metres; STL has no units and OBJ/PLY/OFF commonly use mm.
    if source.suffix.lower() in {".gltf", ".glb"}:
        mesh.apply_scale(1000.0)
    return mesh


def load_mesh(source):
    suffix = source.suffix.lower()
    if suffix == THREE_MF_EXTENSION:
        return load_3mf_mesh(source)
    if suffix == ".amf":
        return load_amf_mesh(source)
    if suffix in MESH_EXTENSIONS:
        return load_trimesh_mesh(source)
    fail(f"Unsupported mesh format: {suffix or 'no extension'}.")


def output_path(source, suffix, output):
    destination = output.expanduser().resolve() if output else source.with_name(source.stem + suffix)
    if destination == source:
        fail("Output path must be different from the input path.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def export_stl(mesh, destination):
    mesh.export(str(destination), file_type="stl")
    exported = trimesh.load_mesh(destination, process=True)
    print(f"Saved: {destination}")
    print(f"Triangles: {len(mesh.faces):,}")
    print(f"Final size: {destination.stat().st_size / 1e6:.2f} MB")
    print(f"Watertight after STL round-trip: {exported.is_watertight}")


def convert(source, output):
    mesh = load_mesh(source)
    export_stl(mesh, output_path(source, "_converted.stl", output))


def optimize(source, target_mb, output):
    if not 0 < target_mb < 50:
        fail("Target must be greater than 0 and less than 50 MB.")
    target_bytes = int(target_mb * 1_000_000)
    if target_bytes < MIN_BINARY_STL_BYTES:
        fail("Target is too small for a valid binary STL with at least four triangles.")
    mesh = load_mesh(source)
    mesh.merge_vertices()
    destination = output_path(source, "_optimized.stl", output)
    print(f"Original size: {source.stat().st_size / 1e6:.2f} MB")
    print(f"Original triangles: {len(mesh.faces):,}")
    target_faces = max(4, (target_bytes - 84) // 50)
    if target_faces >= len(mesh.faces):
        print("Mesh already meets the target; exporting without simplification.")
        export_stl(mesh, destination)
        return
    for attempt in range(1, 10):
        face_count = max(4, int(target_faces * (0.85 ** (attempt - 1))))
        print(f"Attempt {attempt}: target {face_count:,} triangles")
        try:
            reduced = mesh.simplify_quadric_decimation(face_count=face_count)
        except Exception as exc:
            fail(f"Simplification failed. Check fast-simplification installation. Details: {exc}")
        if not isinstance(reduced, trimesh.Trimesh) or len(reduced.faces) < 4:
            continue
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".stl", delete=False) as handle:
            candidate = Path(handle.name)
        try:
            reduced.export(str(candidate), file_type="stl")
            if candidate.stat().st_size <= target_bytes:
                candidate.replace(destination)
                error = max(abs(reduced.extents - mesh.extents)) / max(float(mesh.extents.max()), 1e-12)
                exported = trimesh.load_mesh(destination, process=True)
                print(f"Saved: {destination}")
                print(f"Final size: {destination.stat().st_size / 1e6:.2f} MB")
                print(
                    f"Triangles: {len(reduced.faces):,}; "
                    f"watertight after STL round-trip: {exported.is_watertight}"
                )
                print(f"Relative bounding-box change: {error:.4%}")
                print("Inspect the new file in a slicer before printing; simplification is lossy.")
                return
        finally:
            candidate.unlink(missing_ok=True)
    fail("Could not reach the requested size after 9 attempts.")


def unbinarize_meatpack(data):
    """Decode MeatPack/MeatPack-comments bytes used by BGCode."""
    table = "0123456789. \nGX"
    result, enabled, no_spaces = [], False, False
    signal_count = full_chars = 0
    command_active, buffered = False, None

    def character(value):
        return "E" if value == 11 and no_spaces else table[value]

    def receive(value):
        nonlocal full_chars, buffered
        if not enabled:
            result.append(chr(value))
            return
        if full_chars:
            result.append(chr(value))
            if buffered is not None:
                result.append(buffered)
                buffered = None
            full_chars -= 1
            return
        low, high = value & 0x0F, value >> 4
        low_full, high_full = low == 15, high == 15
        first = None if low_full else character(low)
        second = None if high_full else character(high)
        if low_full:
            full_chars += 1 + high_full
            if not high_full:
                buffered = second
        else:
            result.append(first)
            if first != "\n":
                if high_full:
                    full_chars += 1
                else:
                    result.append(second)

    for value in data:
        if value == 255:
            if signal_count:
                command_active, signal_count = True, 0
            else:
                signal_count = 1
            continue
        if command_active:
            if value == 251:
                enabled = True
            elif value in {250, 249}:
                enabled = False
            elif value == 247:
                no_spaces = True
            elif value == 246:
                no_spaces = False
            command_active = False
            continue
        if signal_count:
            receive(255)
            signal_count = 0
        receive(value)
    return "".join(result)


def decode_compressed_block(data, compression, expected_size):
    if compression == 0:
        return data
    if compression == 1:
        return zlib.decompress(data)
    if compression in {2, 3}:
        return heatshrink2.decompress(
            data, window_sz2=11 if compression == 2 else 12, lookahead_sz2=4
        )
    fail(f"Unsupported BGCode compression type: {compression}.")


def decode_bgcode(source, output):
    raw = source.read_bytes()
    if len(raw) < 10 or raw[:4] != b"GCDE":
        fail("The file is not a supported BGCode file.")
    version, _checksum = struct.unpack_from("<IH", raw, 4)
    if version != 1:
        fail(f"Unsupported BGCode version: {version}.")
    offset, chunks = 10, []
    while offset < len(raw):
        if offset + 8 > len(raw):
            fail("BGCode block header is truncated.")
        block_type, compression, uncompressed_size = struct.unpack_from("<HHI", raw, offset)
        offset += 8
        stored_size = uncompressed_size
        if compression:
            if offset + 4 > len(raw):
                fail("BGCode compressed block header is truncated.")
            stored_size = struct.unpack_from("<I", raw, offset)[0]
            offset += 4
        parameter_size = 6 if block_type == 5 else 2
        end = offset + parameter_size + stored_size + 4
        if end > len(raw):
            fail("BGCode block is truncated.")
        encoding = struct.unpack_from("<H", raw, offset)[0]
        body_start = offset + parameter_size
        body = raw[body_start : body_start + stored_size]
        offset = end
        if block_type != 1:
            continue
        decoded = decode_compressed_block(body, compression, uncompressed_size)
        if len(decoded) != uncompressed_size:
            fail("BGCode decompression produced an unexpected size.")
        if encoding == 0:
            chunks.append(decoded.decode("utf-8", errors="replace"))
        elif encoding in {1, 2}:
            chunks.append(unbinarize_meatpack(decoded))
        else:
            fail(f"Unsupported BGCode G-code encoding: {encoding}.")
    if not chunks:
        fail("The BGCode file contains no G-code blocks.")
    destination = output_path(source, "_decoded.gcode", output)
    destination.write_text("".join(chunks), encoding="utf-8", newline="\n")
    print(f"Saved lossless decoded toolpath: {destination}")
    print(f"Final size: {destination.stat().st_size / 1e6:.2f} MB")


def main():
    args = parse_arguments()
    source = Path(args.input).expanduser().resolve()
    try:
        if not source.is_file():
            fail(f"File not found: {source}")
        suffix = source.suffix.lower()
        require_dependencies(source)
        mode = args.mode or ("decode" if suffix == ".bgcode" else "convert")
        if mode == "decode":
            if suffix != ".bgcode":
                fail("Decode mode currently supports BGCode files only.")
            decode_bgcode(source, args.output)
        elif suffix in TOOLPATH_EXTENSIONS:
            fail("G-code and BGCode are toolpaths, not the original mesh. Use --mode decode for BGCode.")
        elif mode == "convert":
            convert(source, args.output)
        else:
            optimize(source, args.target_mb, args.output)
    except ValueError as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
