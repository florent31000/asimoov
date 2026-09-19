# perception (WS3)

Separate process (`python -m asimoov.perception`): camera sources, SCRFD +
MobileFaceNet (ONNX) face detection/embedding, IoU tracker, gallery matching,
enrolment, VAD module, direction stub.

Owned by WS3. Implements `asimoov.contracts.perception.PerceptionModule` and
publishes `percept.person_seen` / `percept.person_lost` per `percept.v1`
(frozen). See `plan.md` section 4.12, row WS3, and section 4.7.
