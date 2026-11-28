# SkyWeave on the Cubie A7S: install and bench test

This directory installs the SkyWeave drone stack on the companion computer
(Radxa Cubie A7S, aarch64 Linux). It implements contract decision B-D3
(`v2/docs/DRONE_CONTRACTS_D0.md` §11.4).

**Bench use only (B-D1).** Remove the props. Do not connect power to the
motors. The companion cannot send setpoints, arm or take off in this
configuration.

Words in this document:

- **board**: the Cubie A7S.
- **laptop**: your computer. It has this repository, `git` and `uv`.
- **FC**: the flight controller.
- **`<...>`**: a value that you supply. This document has no real addresses,
  user names or passwords. Do not add them.

## 1. What the install puts on the board

| Path | Content | Owner, mode |
| --- | --- | --- |
| `/opt/skyweave/python/` | CPython 3.10 (uv-managed build) | root |
| `/opt/skyweave/venv/` | The locked Python packages (`v2/uv.lock`) | root |
| `/opt/skyweave/app/src/` | The code (`skyweave2`). `app/REVISION` has the commit | root |
| `/opt/skyweave/bin/skyweave-run` | The launcher: tests the configuration, starts a process | root |
| `/opt/skyweave/install.sh`, `/opt/skyweave/README.md` | Copies of the install script (for uninstall) and of this document | root |
| `/etc/skyweave/skyweave.env` | **The one configuration file. It holds the UI token** | root, 0600 |
| `/etc/skyweave/skyweave.env.template` | The newest template, for comparison | root, 0644 |
| `/var/lib/skyweave/recordings/` | Recordings (`rec-*.jsonl`) | skyweave |
| `/var/lib/skyweave/models/` | Detector model files | root |
| `/etc/systemd/system/skyweave-companion.service` | Companion service | root |
| `/etc/systemd/system/skyweave-percepd.service` | percepd service | root |
| `/etc/udev/rules.d/60-skyweave.rules` | Camera name `/dev/skyweave-cam` | root |

The two services run as the user `skyweave`. This user is in the groups
`dialout` (serial port) and `video` (camera). The two services do not depend
on each other. If one stops, the other continues.

## 2. What you need

1. The board with its OS installed. The 2026-09 bring-up used the Radxa
   Debian 13 (trixie) image. The install was tested in a Debian 13 arm64
   container only (section 12).
2. A login on the board with `sudo`.
3. A network path from the laptop to the board for `scp` (Ethernet or Wi-Fi).
4. A Wi-Fi access point on the board. **This install does not make the
   access point.** That is separate work. Read the note below.
5. The UART of the board connected to FC SERIAL1 (UART7): MAVLink2,
   921600 baud (E1 brief §2). Connect TX to RX, RX to TX, ground to ground.
   Before you connect, make sure that the logic voltage of the board pins and
   of the FC pads agree (see the Radxa and Matek documents).
6. The AR0234 USB3 camera. Connect it before you apply power. In the
   bring-up, a camera connected to a board in operation caused a restart of
   the board.
7. A detector model file (`.onnx`) for percepd (B-D2).

Note about the access point and the kernel. The bring-up notes say:

- The NPU operates only with the Rabs9 community kernel (E1 brief §2).
- That kernel had no driver for the Wi-Fi chip of the A7S.
- That kernel needed V4L2 and UVC modules built on the board for the camera.

percepd v1 uses the CPU (B-D2). Thus the bench test does not need the NPU
kernel. You make the decision which kernel to start. If there is no access
point, use the wired address of the board as the UI address (section 5.3).

The board has no real-time clock. After each power cycle the clock is wrong
until the network time service sets it. This has two effects:

- `apt` and downloads fail while the clock is wrong. Use the bundle
  (section 3). It needs no downloads.
- The times in the log and in recording names can be wrong (section 8).

## 3. What needs internet

| Step | Where | Internet |
| --- | --- | --- |
| Make the bundle (`make-bundle.sh`) | laptop | Yes: PyPI and the uv Python download |
| Install from the bundle | board | No, for Python and the packages |
| OS packages (`iproute2`, `udev`, `v4l-utils`, `rsync`, `ca-certificates`) | board | Only if one is missing. If `apt` fails, the install continues and tells you |
| Install from a repository checkout (section 4.4) | board | Yes: PyPI and the uv Python download. `uv` must be installed. The clock must be correct |

Use the bundle. It is the simplest path. In the bring-up, the board could
not connect to GitHub, and the clock was wrong after each power cycle.

## 4. Install

### 4.1 Make the bundle (on the laptop)

1. Commit your changes. The bundle has the commit `HEAD` only.
2. Do this command from the repository root:

   ```bash
   v2/deploy/cubie/make-bundle.sh --out <bundle-directory>
   ```

3. The script prints the names of two files: `skyweave-cubie-bundle-<commit>.tar.gz`
   (about 100 MB) and its `.sha256` file.

The bundle contains the code, CPython 3.10 for the board, the wheels that
`v2/uv.lock` pins (with hash checks), and this directory. If percepd needs
more packages, give its extra: `--extras drone,<extra-name>`.

### 4.2 Copy to the board

```bash
scp <bundle-directory>/skyweave-cubie-bundle-<commit>.tar.gz \
    <bundle-directory>/skyweave-cubie-bundle-<commit>.tar.gz.sha256 \
    <user>@<board-address>:
```

`rsync` is an alternative: `rsync -av --partial <files> <user>@<board-address>:`

### 4.3 Install (on the board)

```bash
sha256sum -c skyweave-cubie-bundle-<commit>.tar.gz.sha256
tar -xzf skyweave-cubie-bundle-<commit>.tar.gz
sudo skyweave-cubie-bundle-<commit>/deploy/install.sh --dry-run
sudo skyweave-cubie-bundle-<commit>/deploy/install.sh
```

1. The first command must print `OK`. If not, copy the file again.
2. `--dry-run` prints each step and changes nothing.
3. The install takes about one minute. It is safe to run it again.
4. The last lines tell you the next steps (section 5).

The install enables the two services. It does not start them on the first
install, because the configuration is not complete.

### 4.4 Alternative: install from a repository checkout (needs internet)

Use this only if the board has `uv`, internet and a correct clock.

```bash
rsync -av --exclude .venv --exclude __pycache__ <repository>/v2 <user>@<board-address>:skyweave/
sudo skyweave/v2/deploy/cubie/install.sh
```

`uv` downloads CPython 3.10 and the locked packages. For more extras:
`--extras drone,<extra-name>`.

## 5. Configure

Edit the one configuration file:

```bash
sudoedit /etc/skyweave/skyweave.env
```

The file has a comment for each value. A value that contains `CHANGE_ME` is a
placeholder. A service does not start while its placeholders are there. Set
these four values:

| Variable | Value |
| --- | --- |
| `SKYWEAVE_UI_TOKEN` | The UI token (section 5.1) |
| `SKYWEAVE_FC_DEVICE` | The UART device (section 5.2) |
| `SKYWEAVE_UI_HOST` | The access-point address of the board (section 5.3) |
| `SKYWEAVE_MODEL` | The model file (section 5.5) |

Then test the file:

```bash
sudo /opt/skyweave/bin/skyweave-run check
```

Pass: the last lines are `companion: OK` and `percepd: OK`. The command
starts nothing. It never prints the token.

### 5.1 Set the token

1. Make a token of 8 to 256 characters. Use only letters, digits, `.`, `_`
   and `-`. Example of a command that makes one: `openssl rand -hex 16`.
2. Write it after `SKYWEAVE_UI_TOKEN=` in `/etc/skyweave/skyweave.env`.
3. Type the same token in the UI page when you use the UI.

Rules for the token:

- Keep it only in `/etc/skyweave/skyweave.env` (owner root, mode 0600).
- Do not put it in the repository, in a log or in a message.
- The token goes over the Wi-Fi link as clear text (finding E1-F14). Use
  the access point for controlled tests only.
- To change the token, edit the file and restart the companion.

### 5.2 Find the UART

The device name of the UART on the Cubie A7S is not known. Find it:

1. List the serial devices: `ls -l /dev/ttyS* /dev/ttyAS* 2>/dev/null`.
   Allwinner vendor kernels usually use the names `ttyAS<n>`.
2. Look at the kernel messages: `sudo dmesg | grep -i -E 'uart|tty'`.
3. Find the console port: `cat /proc/cmdline` shows `console=<name>`. Do not
   use that port for the FC.
4. If the UART pins of the header are not enabled, enable their device-tree
   overlay (see the Radxa documents for the board). Then restart the board.
5. Make sure that the group of the device is `dialout`: `ls -l /dev/<name>`.
   If the group is different, add the user to it:
   `sudo usermod -a -G <group> skyweave`.
6. Test the port. Apply power to the FC (props off). Stop the companion.
   Then read the port:

   ```bash
   sudo systemctl stop skyweave-companion
   sudo -u skyweave stty -F /dev/<name> 921600 raw -echo
   sudo -u skyweave timeout 3 cat /dev/<name> | od -An -tx1 | head -n 5
   ```

   Pass: bytes show, and the byte `fd` (the MAVLink2 start byte) is in them.
   No bytes: the wiring, the port or the FC serial parameters are wrong.
7. Write the name to `SKYWEAVE_FC_DEVICE` (example: `/dev/ttyAS1`).

For a USB serial adapter, see the comment in
`/etc/udev/rules.d/60-skyweave.rules`.

### 5.3 UI address

The UI listens on one address only: `SKYWEAVE_UI_HOST`.

1. Find the address of the access-point interface: `ip -4 addr`.
2. Write it to `SKYWEAVE_UI_HOST`.

If the address is not on the board when the companion starts, the launcher
prints `NOT READY` and stops. systemd starts it again after a delay (1 s to
15 s). The companion comes up when the access point is up.

Fallback: `SKYWEAVE_UI_HOST=0.0.0.0`. The UI then listens on all interfaces,
Ethernet included. Use it when the access-point address is not stable. Each
device on each network of the board can then get the status page.

No access point: use the wired address of the board, or `0.0.0.0`.

### 5.4 Camera

The udev rule gives the camera the stable name `/dev/skyweave-cam`.

1. Do `lsusb`. Find the camera. The rule expects the USB id `1bcf:2d50`
   (from the bring-up).
2. If the id is different, change it in `/etc/udev/rules.d/60-skyweave.rules`.
   Then do `sudo udevadm control --reload-rules && sudo udevadm trigger`.
3. Do `ls -l /dev/skyweave-cam`. Pass: the link exists.
4. Do `v4l2-ctl -d /dev/skyweave-cam --list-formats-ext`.
   Pass: the list has `YUYV` with `1920x1200`.

Alternative with no rule: use the path from `ls /dev/v4l/by-id/` that ends
in `-video-index0` as `SKYWEAVE_CAMERA`.

If install.sh is run again from a new bundle, it installs the rule file
again. A changed USB id is then lost. Change it again, or change it in the
repository.

### 5.5 Model and percepd command line

1. Copy the model file to the board, then to the models directory:

   ```bash
   scp <model>.onnx <user>@<board-address>:
   sudo install -m 0644 <model>.onnx /var/lib/skyweave/models/
   ```

2. Write its path to `SKYWEAVE_MODEL`.
3. The line `SKYWEAVE_PERCEPD_CMD` is the full percepd command line. The
   flag names of percepd are not final. If they change, change this line.

`SKYWEAVE_COMPANION_CMD` is the full companion command line. Do not add
`--enable-setpoints`. The launcher refuses it (B-D1).

## 6. Start, stop, status, log

| Task | Command |
| --- | --- |
| Start | `sudo systemctl start skyweave-companion skyweave-percepd` |
| Stop | `sudo systemctl stop skyweave-companion skyweave-percepd` |
| Restart (after a configuration change) | `sudo systemctl restart skyweave-companion skyweave-percepd` |
| Status | `systemctl status skyweave-companion skyweave-percepd` |
| Log, live | `journalctl -u skyweave-companion -u skyweave-percepd -f` |
| Log, this start of the board | `journalctl -b -u skyweave-companion -u skyweave-percepd` |
| Test the configuration | `sudo /opt/skyweave/bin/skyweave-run check` |
| No start when the board starts | `sudo systemctl disable skyweave-companion skyweave-percepd` |

The two services start when the board starts.

How to read a failure:

| Log line or status | Meaning | What to do |
| --- | --- | --- |
| `CONFIG ERROR: ...`, status `failed`, `status=78/CONFIG` | The configuration is wrong. systemd does not try again | Correct `/etc/skyweave/skyweave.env`. Then `sudo systemctl restart <service>` |
| `NOT READY: ...`, status `activating (auto-restart)` | Something is not there yet (UI address, model file, disk space) | Read the line. systemd tries again every 1 s to 15 s |
| `WARNING: FC device ... does not exist` | The UART name is wrong or the port is not enabled | Section 5.2. The companion operates and tries the FC link again |
| `WARNING: camera ... does not exist` | The camera is not connected or the rule did not match | Section 5.4. systemd starts percepd again until the camera is there |
| `companion not started: ...` | The companion refused its command line or could not open a port | Read the line. systemd tries again |

## 7. Bench checklist

Do the items in sequence. Each item has a **Pass** line.

1. **Safety.** Remove all props. Disconnect the motor power (ESC battery
   lead). Apply power to the FC and the board only.
   Pass: no prop is installed, and the motors have no power.
2. **Configuration.** Do `sudo /opt/skyweave/bin/skyweave-run check`.
   Pass: `companion: OK` and `percepd: OK`.
3. **Services up.** Do `sudo systemctl start skyweave-companion skyweave-percepd`.
   Wait 30 seconds. Do `systemctl is-active skyweave-companion skyweave-percepd`.
   Pass: two lines `active`. Then do
   `systemctl show -p NRestarts skyweave-companion skyweave-percepd` two
   times, one minute apart. Pass: the numbers do not increase.
4. **UI reachable from the laptop.** Connect the laptop to the access point.
   Open `http://<SKYWEAVE_UI_HOST>:8080/` in a browser.
   Pass: the page shows, with the banner `UNPRIMED`.
5. **Ground link.** Type the token in the `Token` field of the page.
   Pass: the row `Ground link` shows `up`.
6. **Setpoint gate.** Look at the row `Setpoint gate`.
   Pass: `locked`. If it shows `enabled`, stop. Remove power. Report it.
7. **FC link up.** Look at the row `FC link`.
   Pass: `up`. Also, `FC mode` shows a mode name and `Armed` shows `disarmed`.
8. **Attitude age small.** Look at the row `Attitude age` for one minute.
   Pass: the value stays below 100 ms (`attitude_bound_ms`, contract §9).
   Tilt the vehicle by hand; the value must stay small.
9. **Switch positions read.** Set the radio on. Pass: the row `RC` shows
   `seen`. Move the flight-mode switch to each position.
   Pass: the row `FC mode` shows the mode of each position (E1-D16 and brief
   3.11 give the planned modes).
10. **Radio approve read.** Move the approve switch (RC channel 8) from low
    to high one time. Then do, on the board:

    ```bash
    grep -c '"cmd_id":"rc:approve:' "$(ls /var/lib/skyweave/recordings/rec-*.jsonl | tail -n 1)"
    ```

    Pass: the number increases by 1 for each low-to-high movement. The ack
    result is `rejected_state`, because the mission is not primed. That is
    correct on the bench.
11. **percepd sends packets.** Do this command two times, 10 seconds apart:

    ```bash
    grep -c '"stream":"detection"' "$(ls /var/lib/skyweave/recordings/rec-*.jsonl | tail -n 1)"
    ```

    Pass: the number increases (one packet for each processed frame).
    Put an object of a detected class in front of the camera.
    Pass: `grep -c '"stream":"track"'` on the same file increases too.
12. **Recording replays.** Stop the companion, then replay its recording:

    ```bash
    sudo systemctl stop skyweave-companion
    /opt/skyweave/bin/skyweave-run replay
    sudo systemctl start skyweave-companion
    ```

    Pass: the last line is `REPLAY OK: the recording replays ([R3])`. The
    line `records:` must show `mavlink`, `detection` and `tick` records.
13. **Restart.** Do a power cycle of the board. Wait one minute. Do items 3,
    4 and 7 again. Pass: the services are `active` with no command from you,
    and there is a new recording file.
14. **Copy the recordings to the laptop** (section 8). Keep them with your
    bench notes.

Nothing here is Measured until you record the results (label discipline,
`CLAUDE.md`).

## 8. Recordings

Location: `/var/lib/skyweave/recordings/`. Each companion start makes a new
file. The companion never overwrites a file.

Name: `rec-<sequence>-<boot id>-<UTC time>.jsonl`, for example
`rec-000012-1a2b3c4d-20261127T101502Z.jsonl`.

- The sequence number gives the order. The newest file has the largest
  number.
- The boot id is the same for all files of one start of the board.
- The UTC time is the board clock. It can be wrong (no real-time clock). Do
  not use it for the order.
- The times in a recording are board milliseconds ([C1]), not clock time.

Copy the recordings to the laptop (do this on the laptop):

```bash
rsync -av --partial <user>@<board-address>:/var/lib/skyweave/recordings/ <local-directory>/
```

or `scp '<user>@<board-address>:/var/lib/skyweave/recordings/rec-*.jsonl' <local-directory>/`.

Replay a copy on the laptop (from `v2/`):

```bash
uv run python -c 'import sys; from skyweave2.drone.core import replay; r = replay(sys.argv[1]); print(r.mismatch() or "REPLAY OK")' <file>
```

Delete a recording on the board after you copied it:
`sudo rm /var/lib/skyweave/recordings/<file>`. Do not delete the newest file
while the companion is in operation.

Disk safety (what was chosen):

- Before each companion start, the launcher deletes the oldest recordings
  until the directory is smaller than `SKYWEAVE_RECORD_MAX_MB` (2048 MB).
  It always keeps the newest recording.
- The companion does not start if the free space is less than
  `SKYWEAVE_RECORD_MIN_FREE_MB` (512 MB). The log shows `NOT READY`.
- Limit: one recording has no size limit while the companion is in
  operation. A recording has no camera frames, only packets. An estimate
  (not measured) is 50 to 300 MB for each hour. Do not let the board
  operate for days without a restart of the companion. A restart starts a
  new file and deletes old ones.
- The system log (journald) has its own size limit.

## 9. Update

1. Make a new bundle from the new commit (section 4.1).
2. Copy it to the board and extract it (sections 4.2 and 4.3).
3. Do `sudo skyweave-cubie-bundle-<commit>/deploy/install.sh`.

The install replaces the code and the service files. It builds the Python
environment again only if the locked packages changed. It keeps
`/etc/skyweave/skyweave.env`, the recordings and the models. It restarts the
services that are in operation. If the new template has new variables, the
install prints their names. Copy them from
`/etc/skyweave/skyweave.env.template`.

Make sure of the result: `cat /opt/skyweave/app/REVISION` shows the commit.
You can delete old bundle directories from the home directory.

## 10. Uninstall

The install keeps a copy of `install.sh` on the board for this task:

```bash
sudo /opt/skyweave/install.sh --uninstall --dry-run
sudo /opt/skyweave/install.sh --uninstall
```

This stops and removes the services, the udev rule and `/opt/skyweave`. It
keeps the configuration (with the token), the recordings, the models and
the user `skyweave`.

To remove those also (this deletes all recordings on the board), use the
`install.sh` of a bundle:

```bash
sudo skyweave-cubie-bundle-<commit>/deploy/install.sh --uninstall --purge
```

The install does not remove the OS packages of section 3.

## 11. Design choices

- **Bundle first.** All downloads occur on the laptop. The board needs no
  PyPI, no GitHub and no correct clock. `pip` installs the wheels with
  `--no-index --require-hashes`; the hashes are those of `v2/uv.lock`.
- **uv stays the source of truth.** `uv export` makes the requirements from
  `uv.lock`. The Python on the board is the uv-managed CPython 3.10 build.
- **The project is not installed as a package.** The code is copied to
  `/opt/skyweave/app/src`, and a `.pth` file puts it on the Python path
  (the same idea as `v2/docker/sitl/`).
- **One configuration file.** All flags of the two processes are in
  `/etc/skyweave/skyweave.env`. The launcher fills the `{fields}`.
- **Clear stops.** A configuration error stops the service (exit code 78,
  no restart). A thing that is not ready causes a new try after a delay.
- **The percepd service does not get the token.**
- **Recording names do not rely on the clock.**

## 12. What was tested, and what was not

Tested without the board (2026-11-27):

- `shellcheck` on `install.sh`, `make-bundle.sh` and `skyweave-run`.
- `make-bundle.sh` on macOS (arm64).
- `install.sh --dry-run`, install, second install, update, `--uninstall`
  and `--purge` in a Debian 13 (trixie) arm64 container, from a bundle with
  no network and from a read-only repository checkout with `uv`.
- `python -m skyweave2.drone.companiond --help` in the installed environment.
- The two units under systemd 257 in a container: `systemd-analyze verify`,
  start when the system starts, configuration error with no restart loop,
  restart with a growing delay, a new recording for each start, the wait for
  the UI address, the UI on the configured address only, no token in the
  log, the recordings or the percepd environment, and `skyweave-run replay`.
  A stand-in for percepd was used, because this revision has no percepd.

NOT tested:

- The real board, its OS image and its kernel.
- The serial port, `--real-fc` and the `serial:` endpoint (the code is in
  work in parallel; the container used a TCP endpoint with no FC).
- The camera, the udev rule with a real device, and the real percepd.
- The access point and the UI from a laptop over Wi-Fi.
- A wrong board clock during a real install.
