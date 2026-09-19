# perception (WS3)

Separate process (`python -m asimoov.perception --hub ws://host:7331 --config
robot.yaml`): camera sources, SCRFD + MobileFaceNet (ONNX) face detection and
identification, IoU tracker, gallery matching, enrolment, VAD module,
direction stub.

Frames never leave this process and are never written to disk.

Documentation: `docs/perception.md`. Owned by WS3.
