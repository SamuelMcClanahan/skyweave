Flying object detection system using voxel projection and central yolo tracking node

**Detection**

By taking the ray cast from the focal point of a variety of cameras set out over an array, we can reconstruct the most likely voxel in which the tracked object exists (hungarian algorithm to get to the closest possible average intersection point in 3d of the cameras/mahalanobis distance), because we know where all the camera nodes are. 

We track an object from an individual camera through black and white frame differencing- between two frames everything is black, but the difference is highlighted white, representing motion. Still thinking of ideas on how to filter out noise (rustling leaves rain, etc. maybe something to do with how they move in a pattern where as an object in the sky is only occupies new points). The cameras are rolling shutter, so we must apply a transform across the frame for fast moving objects like drones. (need to find specific research on this, kind of similar to how tennis balls are triangulated onto a court to see if a ball was out or not).  It is possible we could just use the centroids but i think we have enough overhead to use the entire frame. 

In addition, our central node takes in data from a rotating turret with a global shutter camera (mono) running object detection to get faster real time tracking on an object in real time (100fps). We combine all these inputs through a UKF to model the tracked system, and extrapolate the motion profile of the object, which we can use to infer about what type of object it is. One idea was an an IWR1843 mmWave radar but these are expensive, so probably not for now. To avoid ethernet slip ring complexities, the turret has ~440 degrees of travel before a reset, and is powered off a NEMA17 on anywhere from a 2 to 4:1 ratio. It can also pitch up and down of course. 

**Calibration**
This is tricky because with our budget we cant afford fancy super-precise gps/gnss modules or an rtk-b drone yet so we have a few tricks. We individually calibrate all cameras with a charuco board, then use a 9-dof imu attached to every camera to measure its pitch and roll. For distance, one idea is to print a super massive apriltag and attach it to the central node and calibrate off that because the fovs of the cameras must point to the center to overlap and successfully detect an object. Another idea is the central node keeps one ads-b module attached, and we could devise a way to calibrate our cameras by taking their measurements of an object transmitting ads-b then compare that against the actual logged ads-b position to localize. Otherwise we have to use laser rangefinders and hope to be precise on measuring the angle.

**Communication**
we then broadcast these entire frames over raw UDP(?) where our central node reconstructs in 3d and does the voxel math to localize the object. Timestamp syncing is a challenge, need ideas on how to keep those consistent as well. The board we're doing can't support hardware timestamping I believe (RV1106), athough maybe we could add on an STM32 MCU that does? The node cameras are MIPI-CSI, data transmits back through Gbe, central node runs a USB UVC camera, through a semi- managed switch with an attached travel router to be able to remotely monitor even if a part of the system goes down.

**Pose Estimation and Recognition**
We apply an Unscented Kalman Filter (probably since flying drone are hard to model as a linear system), could be an EKF or regular KF though to fuse our data and estimate over time. Depending on # and precision of calibration the external nodes should be somewhat precise (1-10 inches?) and the yolo camera more precise and faster. Motion profiling alone should tell us a significant amount about the object we are tracking; a 777 moves very differently than a paper airplane as does a drone. Furthermore, the yolo model allows a higher level of precision, trained on super large datasets. In addition, maybe an IMM could eventually be applied through a large amount of generated synthetic datasets on drones (Nvidia IssacSim). I wonder if some type of reinforcement learning is possible on the detection pipeline as an entire system (estimating with synthetic data and comparing since synthetic data allows us to get the actual postion), but at minimum it could be used to tune the kalman gains. Eventually I will try and write a CUDA-based detector for this and run it on a jetson orin nano super, versus just a c++ cpu one for the first draft. 

In addition, a three.js webui for visualzing is probably pretty sick as well. 

**Phase 2: Drone**
Once we've developed a robust detection pipeline, the last step is to attempt to guide a drone to a paper airplane mid-air. The system is somewhat precise, but probably not entirely precise enough to hit a small moving target with precision-precise. To see if an object IS there, and then to get the general area where it is we can use our voxel projection followed up with faster pings from the yolo turret. If we can succesfully track its heading, then ostensibly we could guide a drone into it just using a 1D-Lidar sensor that's sufficiently long range. Maybe if our drone is higher than the other an optical flow sensor could help, but I am dubious about the latency and precision. One other thing I need to search for is cheap CV tricks to detect where somtehing is (color or contour pipelines?) without adding a heavy detection network. Although how heavy is that? The drone must run off a system like a rpi 2 zero or lighter.  There might be some pre-existing solution with laser guidance weapon systems from the 20th century. Probably a 3-4" cinewhoop (protect electronics over frame), unsure if should go with custom waterjetted cf frames or all stock. Small Lipo for power, control with radiomaster pocket and elrs.

System hardware:
External Nodes:
- RV1106 clone board (clone of luckfox pico pro/similar)
	- 256MB DDR2L, ethernet and usb-c exposed
- 5V3.5A PoE Splitter exposed to power and transmit data, powered off PoE (Cat6E)
- BNO055 IMU
- SC3336 Rolling shutter camera over 2-lane mipi-csi
- Sealed in printed container with tpu gaskets, covered in waterproof paint, with 
- Lexan lens cover with tpu gasket, all to be waterproof

Central Node:
- Jetson Orin Nano Super
- OV9281 100FPS USB 2.0 Camera (so have to have mjpeg copmressino probably), coudl have 2x perhaps on opposing side of turret? idk
- Nema17 42mm driving pitch and rotation
- Driven by ESP32S3, with AS5408 magnetic encoders
-  Need ideas on how to waterproof node
- Powered off 12V SLA (motorcycle battery tier) with a 12 to 13.2V stepup

Networking and Power:
- DC PoE Switch (8 active poe, 2 sfp with adapters to communicate with jetson and router)
- powered off 12v sla stepped up to 48V
- Cudy AC12000 Mini Router
- 

Also have acess to a Rubik Pi 3 i could put more stuff on ostensibly, maybe IR-CUT sensors? tbd


**MVP Notes**
Trying to ship an MVP in the next week. what this will be done with is a Rubik Pi 3 and 3 USB UVC OV9281 cameras + a macbook camera if i want + an RV1103 and SC3336 if i want. So i need a somewhat agnostic stack with the core ideas re-usable or learnable from to quickly develop on that to demonstrate the technology. Need the three.js visualzer for both of course





