"""Small, self-contained regression tests for the conversion paths."""

from pathlib import Path
import struct
import tempfile
import unittest
import zlib

import lib3mf
import numpy as np
import trimesh

import optimize_stl


class ConverterTests(unittest.TestCase):
    def test_convert_3mf_to_stl_applies_unit_and_build_transform(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "tetra.3mf"
            destination = root / "tetra.stl"
            wrapper = lib3mf.Wrapper()
            model = wrapper.CreateModel()
            mesh = model.AddMeshObject()
            vertices = []
            for coordinates in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)):
                vertex = lib3mf.Position()
                vertex.Coordinates[:] = coordinates
                vertices.append(vertex)
            triangles = []
            for indices in ((0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3)):
                triangle = lib3mf.Triangle()
                triangle.Indices[:] = indices
                triangles.append(triangle)
            mesh.SetGeometry(vertices, triangles)
            transform = lib3mf.Transform()
            transform.Fields[0][:] = (1, 0, 0)
            transform.Fields[1][:] = (0, 1, 0)
            transform.Fields[2][:] = (0, 0, 1)
            transform.Fields[3][:] = (2, 0, 0)
            model.AddBuildItem(mesh, transform)
            model.QueryWriter("3mf").WriteToFile(str(source))

            optimize_stl.convert(source, destination)

            result = trimesh.load_mesh(destination, process=True)
            np.testing.assert_allclose(result.bounds, [[2, 0, 0], [3, 1, 1]])

    def test_convert_amf_to_stl_converts_inches_to_mm(self):
        amf = """<?xml version='1.0'?>
<amf unit='inch'><object id='0'><mesh><vertices>
<vertex><coordinates><x>0</x><y>0</y><z>0</z></coordinates></vertex>
<vertex><coordinates><x>1</x><y>0</y><z>0</z></coordinates></vertex>
<vertex><coordinates><x>0</x><y>1</y><z>0</z></coordinates></vertex>
<vertex><coordinates><x>0</x><y>0</y><z>1</z></coordinates></vertex>
</vertices><volume>
<triangle><v1>0</v1><v2>2</v2><v3>1</v3></triangle>
<triangle><v1>0</v1><v2>1</v2><v3>3</v3></triangle>
<triangle><v1>1</v1><v2>2</v2><v3>3</v3></triangle>
<triangle><v1>2</v1><v2>0</v2><v3>3</v3></triangle>
</volume></mesh></object></amf>"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "tetra.amf"
            destination = root / "tetra.stl"
            source.write_text(amf, encoding="utf-8")
            optimize_stl.convert(source, destination)
            result = trimesh.load_mesh(destination, process=True)
            self.assertEqual(len(result.faces), 4)
            np.testing.assert_allclose(result.extents, [25.4, 25.4, 25.4])

    def test_convert_obj_to_stl_preserves_mesh_extent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "cube.obj"
            destination = root / "cube.stl"
            trimesh.creation.box(extents=(10, 20, 30)).export(source)
            optimize_stl.convert(source, destination)
            result = trimesh.load_mesh(destination, process=True)
            self.assertEqual(len(result.faces), 12)
            np.testing.assert_allclose(result.extents, [10, 20, 30])

    def test_decode_uncompressed_meatpack_bgcode_without_checksum(self):
        # Enable MeatPack followed by packed bytes for G1.
        gcode = b"\xff\xff\xfb\x1d"
        block = struct.pack("<HHI", 1, 0, len(gcode)) + struct.pack("<H", 1) + gcode
        data = b"GCDE" + struct.pack("<IH", 1, 0) + block
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "toolpath.bgcode"
            destination = root / "toolpath.gcode"
            source.write_bytes(data)
            optimize_stl.decode_bgcode(source, destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), "G1")

    def test_decode_bgcode_rejects_invalid_crc32(self):
        gcode = b"G1 X1\n"
        payload = struct.pack("<HHI", 1, 0, len(gcode)) + struct.pack("<H", 0) + gcode
        block = payload + struct.pack("<I", zlib.crc32(payload) ^ 1)
        data = b"GCDE" + struct.pack("<IH", 1, 1) + block
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "corrupted.bgcode"
            destination = root / "toolpath.gcode"
            source.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "checksum"):
                optimize_stl.decode_bgcode(source, destination)

    def test_decode_bgcode_accepts_valid_crc32(self):
        gcode = b"G1 X1\n"
        payload = struct.pack("<HHI", 1, 0, len(gcode)) + struct.pack("<H", 0) + gcode
        block = payload + struct.pack("<I", zlib.crc32(payload))
        data = b"GCDE" + struct.pack("<IH", 1, 1) + block
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "toolpath.bgcode"
            destination = root / "toolpath.gcode"
            source.write_bytes(data)
            optimize_stl.decode_bgcode(source, destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), "G1 X1\n")


if __name__ == "__main__":
    unittest.main()
