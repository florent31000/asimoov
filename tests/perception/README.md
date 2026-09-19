# tests/perception (WS3)

Synthetic fixtures only (drawn shapes, stubbed detectors and embedders), no
committed real faces and no model download required. Tests that load the real
ONNX graphs are skipped when `~/.asimoov/models/` is empty; run
`python tools/download_models.py` to enable them.
