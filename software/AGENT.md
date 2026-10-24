# Laminar lab agent

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Laminar is a robot arm that pours measured volumes of liquid from a vial into a tube, checks the result
with a camera, and reports back. It is for research techs in small wet labs who aliquot by hand.

Chat with it:

- `what's on the bench?`: the vials, their liquids and volumes, and the station tube
- `pour 2 mL of blue into tube1`: proposes a plan, checked against the bench before anything moves
- `approve <id>`: runs the plan (approvers only), then reports each step, the measured volume and any abort
- `cancel <id>`, `runs`, `help`

Every plan is built and validated by code. Only approved lab members can start a run, and the arm aborts
to a safe pose if any check fails.
