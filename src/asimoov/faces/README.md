# faces (WS6)

Face server (`server.py`, hosts the WebSocket hub's `/face` page via
`process_request`), the web canvas face (`web/index.html`, `face.js`,
`face.css`), and the Kivy renderer (`kivy/renderer.py`, `kivy/app.py`).

Owned by WS6. Implements `asimoov.contracts.face.FaceRenderer` and the
`process_request(path)` hook consumed by WS1's hub. See `plan.md` section 4.6.
