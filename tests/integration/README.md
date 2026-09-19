# tests/integration

Whole robots, assembled the way the CLI assembles them, driven by the replay
fixtures in `../fixtures/replays/`. Only the last inch is faked: the Go2's
WebRTC link and the InMoov's firmware. Nothing here opens a camera, a
microphone, a browser or a connection to a real robot.
