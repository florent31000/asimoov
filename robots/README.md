# Robots

Each subfolder is a runnable robot: `robot.yaml`, `persona.yaml`, and a `behaviors/` directory
(see plan.md section 4.3 and `docs/contracts.md`).

`robot.yaml` has no JSON Schema in contracts v1: it is validated by the core config loader
(WS1), not by `src/asimoov/contracts/schemas/`.
