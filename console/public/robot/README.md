# reBot B601-RS web model (real geometry, 5.5 MB)

Built from Seeed's official URDF + STL meshes (ReBot_Arm_DigitalTwin_RS), decimated and coloured, one GLB per link.

Files
- robot.json         kinematic chain from the URDF (joint origins, axes, limits) + which GLB belongs to which link
- meshes/*.glb       one per link, colours baked (Seeed lime, aluminium, black)
- viewer.html        standalone three.js viewer; exports loadRobot() which builds the chain and returns setFromTelemetry()
- ReBot_Arm_RS.urdf  same chain in URDF form (mesh paths relative), if you prefer urdf-loader

Conventions (verified against robotics/kinematics.py and Seeed's ROS driver)
- telemetry.joints[i] (degrees from the follower zero pose) -> joint(i+1) value = radians(deg). No sign flip, no offset.
- URDF is z-up; rotate the robot root -90 deg about X for three.js (viewer.html does this).
- Zero pose is the folded shipping pose, that is correct.
- Gripper: map gripper_deg to the prismatic joints (0..0.05 m); viewer.html uses gripper_deg/100*0.05, tune to taste.

Try it: `python -m http.server 8765` in this folder, then
http://localhost:8765/viewer.html?q=20,40,20,-10,5,2&g=20
