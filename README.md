# STL Optimizer

Convert common 3D-print mesh files to binary STL, reduce a mesh to a chosen
maximum file size, and decode Prusa BGCode files to plain G-code. Processing is
local: the program does not upload or alter the input file.

## What is supported

| Input | Operation | Notes |
| --- | --- | --- |
| STL | Convert, optimize | STL does not declare units; it is treated as millimetres. |
| 3MF | Convert, optimize | Build items, components, transforms, and the file's declared unit are applied. |
| AMF / zipped AMF | Convert, optimize | Geometry is converted; STL cannot retain materials or colours. |
| OBJ, PLY, OFF | Convert, optimize | These formats normally do not declare units; they are treated as millimetres. |
| GLB, glTF | Convert, optimize | glTF metres are converted to millimetres. |
| BGCode | Decode to G-code | Exact toolpath decode, **not** a reversible conversion to STL. |

Formats such as STEP, IGES, FBX, DXF, and proprietary slicer projects need a
CAD or slicer engine to tessellate their geometry. They are intentionally not
silently converted because that can change scale or geometry. G-code and
BGCode are already sliced printer toolpaths: the original surfaces, materials,
and modelling history are no longer present, so an STL reconstructed from them
would only be an approximation.

## Install

Use a virtual environment so dependencies remain inside the project:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Usage

### Convert a model to STL

```bash
.venv/bin/python optimize_stl.py "model.3mf" --mode convert
```

The default output is next to the input, for example
`model_converted.stl`. Use `--output` to choose another path:

```bash
.venv/bin/python optimize_stl.py "model.obj" --mode convert --output "output/model.stl"
```

### Reduce an STL or mesh size

```bash
.venv/bin/python optimize_stl.py "model.stl" --mode optimize --target-mb 10
```

Optimization writes a new `*_optimized.stl` file and never edits the original.
It is necessarily lossy because it removes triangles. The program reports the
final size, triangle count, watertight state after reopening the STL, and the
relative bounding-box change. Always inspect an optimized result in your slicer
before printing.

### Decode BGCode without losing toolpaths

```bash
.venv/bin/python optimize_stl.py "print.bgcode" --mode decode
```

This writes `print_decoded.gcode`. BGCode uses compressed/encoded G-code
blocks; decoding preserves the printer toolpath but does not recreate the
original STL model.

## Verification

The repository includes representative STL, 3MF, and BGCode files locally for
manual checks, but they are excluded from Git so the repository stays small.
Run:

```bash
.venv/bin/python -m py_compile optimize_stl.py
.venv/bin/python optimize_stl.py "Smurf.3mf" --mode convert
.venv/bin/python optimize_stl.py "Macbook-Air-M2-stand.3mf-fixed.stl" --mode optimize --target-mb 1
.venv/bin/python optimize_stl.py "Dragon whit Edges.bgcode" --mode decode
```

## License

MIT. See [LICENSE](LICENSE).
